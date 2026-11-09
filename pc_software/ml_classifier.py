"""
EEG ML Training & Real-Time Classifier
======================================
Step 1: Train a classifier on labeled CSV data (eyes_open vs eyes_closed)
Step 2: Run real-time inference on live EEG stream

Workflow:
  1. Collect data with eeg_visualizer.py (press O/C to label)
  2. Run: python ml_classifier.py --train
  3. Run: python ml_classifier.py --live

Requirements:
    pip install scikit-learn numpy scipy pandas matplotlib joblib
"""

import os
import glob
import argparse
import json
import time
import threading
import collections

import numpy as np
import pandas as pd
from scipy import signal as sp_signal
import matplotlib.pyplot as plt
import matplotlib.animation as animation

from sklearn.ensemble import RandomForestClassifier
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.pipeline import Pipeline
import joblib

# Optional: serial for live mode
try:
    import serial
    import serial.tools.list_ports
    SERIAL_OK = True
except ImportError:
    SERIAL_OK = False

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

SAMPLE_RATE    = 250           # Hz
EPOCH_SEC      = 1.0           # Feature window: 1 second
EPOCH_SAMPLES  = int(EPOCH_SEC * SAMPLE_RATE)   # 250 samples per epoch
OVERLAP        = 0.5           # 50% overlap between epochs

# Frequency bands
BANDS = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta":  (13.0, 30.0),
    "gamma": (30.0, 45.0),
}

MODEL_PATH = "eeg_model.pkl"
CSV_DIR    = "eeg_recordings"

# ─────────────────────────────────────────────
#  Signal Processing Helpers
# ─────────────────────────────────────────────

def design_notch(freq=50, q=30, fs=SAMPLE_RATE):
    b, a = sp_signal.iirnotch(freq / (fs / 2), q)
    return b, a


def design_bandpass(low, high, fs=SAMPLE_RATE, order=4):
    nyq = fs / 2
    b, a = sp_signal.butter(order, [low / nyq, high / nyq], btype='band')
    return b, a


B_NOTCH, A_NOTCH = design_notch()


def preprocess(arr):
    """Apply 50 Hz notch filter to a 1D numpy array."""
    if len(arr) < 10:
        return arr
    return sp_signal.filtfilt(B_NOTCH, A_NOTCH, arr)


def band_power(arr, low, high, fs=SAMPLE_RATE):
    """Welch PSD band power."""
    if len(arr) < fs:
        return 0.0
    freqs, psd = sp_signal.welch(arr, fs=fs,
                                  nperseg=min(len(arr), fs),
                                  scaling='density')
    idx = (freqs >= low) & (freqs <= high)
    return float(np.trapz(psd[idx], freqs[idx]))


# ─────────────────────────────────────────────
#  Feature Extraction
# ─────────────────────────────────────────────

def extract_features(epoch):
    """
    Extract features from a 1-second EEG epoch.
    Returns a 1D feature vector.

    Features (total ~15):
      - Band power: delta, theta, alpha, beta, gamma         (5)
      - Band power ratios: alpha/beta, alpha/theta, theta/alpha (3)
      - Statistical: mean, std, RMS, kurtosis, skewness      (5)
      - Peak frequency in alpha band                         (1)
      - Alpha asymmetry proxy                                (1)
    """
    epoch = preprocess(epoch)
    feats = []

    # ── Band powers ──
    powers = {}
    for name, (lo, hi) in BANDS.items():
        p = band_power(epoch, lo, hi)
        powers[name] = p
        feats.append(p)

    # ── Band ratios ──
    eps = 1e-12
    feats.append(powers["alpha"] / (powers["beta"]  + eps))  # relaxation index
    feats.append(powers["alpha"] / (powers["theta"] + eps))
    feats.append(powers["theta"] / (powers["alpha"] + eps))

    # ── Statistical features ──
    from scipy.stats import kurtosis, skew
    feats.append(float(np.mean(epoch)))
    feats.append(float(np.std(epoch)))
    feats.append(float(np.sqrt(np.mean(epoch ** 2))))  # RMS
    feats.append(float(kurtosis(epoch)))
    feats.append(float(skew(epoch)))

    # ── Peak frequency in alpha band ──
    freqs, psd = sp_signal.welch(epoch, fs=SAMPLE_RATE,
                                  nperseg=min(len(epoch), SAMPLE_RATE))
    alpha_mask = (freqs >= 8) & (freqs <= 13)
    if alpha_mask.any():
        peak_alpha_freq = float(freqs[alpha_mask][np.argmax(psd[alpha_mask])])
    else:
        peak_alpha_freq = 0.0
    feats.append(peak_alpha_freq)

    # ── Total power ──
    total = sum(powers.values()) + eps
    feats.append(powers["alpha"] / total)   # alpha fraction of total power

    return np.array(feats, dtype=np.float32)


FEATURE_NAMES = (
    list(BANDS.keys()) +
    ["alpha_beta_ratio", "alpha_theta_ratio", "theta_alpha_ratio",
     "mean", "std", "rms", "kurtosis", "skewness",
     "peak_alpha_freq", "alpha_total_fraction"]
)


def epochify(samples, labels_at, fs=SAMPLE_RATE,
             epoch_sec=EPOCH_SEC, overlap=OVERLAP):
    """
    Slice the full recording into labeled epochs.

    samples:    list of float (mV)
    labels_at:  list of (sample_index, label) tuples
    Returns:    (X, y) numpy arrays
    """
    step   = int(epoch_sec * fs * (1 - overlap))
    epoch_n = int(epoch_sec * fs)
    arr    = np.array(samples)

    X, y = [], []
    for start in range(0, len(arr) - epoch_n, step):
        end   = start + epoch_n
        epoch = arr[start:end]
        # Find the dominant label in this epoch
        label = _majority_label(start, end, labels_at)
        if label is None:
            continue
        feats = extract_features(epoch)
        X.append(feats)
        y.append(label)

    return np.array(X), np.array(y)


def _majority_label(start, end, labels_at):
    """Return the most common label in [start, end) sample range."""
    votes = {}
    for (idx, lab) in labels_at:
        if start <= idx < end:
            votes[lab] = votes.get(lab, 0) + 1
    if not votes:
        return None
    return max(votes, key=votes.get)


# ─────────────────────────────────────────────
#  CSV Loader
# ─────────────────────────────────────────────

def load_csv_recordings(csv_dir=CSV_DIR):
    """
    Load all CSV files from eeg_recordings/.
    Returns (all_samples, labels_at) where labels_at is
    list of (sample_index, label).
    """
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    if not files:
        raise FileNotFoundError(f"No CSV files found in {csv_dir}/")

    all_samples  = []
    labels_at    = []
    sample_offset = 0

    for fpath in files:
        print(f"  Loading: {os.path.basename(fpath)}")
        df = pd.read_csv(fpath)

        samples_in_file = []
        for _, row in df.iterrows():
            label = str(row.get("label", "")).strip()
            try:
                mv = float(row["voltage_mV"])
                samples_in_file.append(mv)
                if label in ("eyes_open", "eyes_closed"):
                    labels_at.append(
                        (sample_offset + len(samples_in_file) - 1, label)
                    )
            except (ValueError, KeyError):
                # LABEL_MARKER rows — apply label backward
                if label in ("eyes_open", "eyes_closed"):
                    n_back = int(2 * SAMPLE_RATE)  # last 2 seconds
                    for i in range(max(0, len(samples_in_file) - n_back),
                                   len(samples_in_file)):
                        labels_at.append((sample_offset + i, label))

        all_samples.extend(samples_in_file)
        sample_offset += len(samples_in_file)

    print(f"  Total samples: {len(all_samples)}")
    print(f"  Label events:  {len(labels_at)}")
    return all_samples, labels_at


# ─────────────────────────────────────────────
#  Training Pipeline
# ─────────────────────────────────────────────

def train(csv_dir=CSV_DIR, model_path=MODEL_PATH):
    print("\n" + "=" * 50)
    print("  EEG Classifier Training")
    print("=" * 50)

    # 1. Load data
    print("\n[1/5] Loading recordings...")
    samples, labels_at = load_csv_recordings(csv_dir)

    # 2. Epoch + feature extraction
    print("[2/5] Extracting features from epochs...")
    X, y = epochify(samples, labels_at)
    if len(X) == 0:
        print("[ERROR] No labeled epochs found!")
        print("  → Open eeg_visualizer.py, collect data,")
        print("    press [O] for eyes open, [C] for eyes closed.")
        return

    classes, counts = np.unique(y, return_counts=True)
    print(f"  Epochs: {len(X)} total")
    for c, n in zip(classes, counts):
        print(f"    {c}: {n} epochs")

    # 3. Build & evaluate model
    print("[3/5] Training Random Forest classifier...")
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("clf",    RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            min_samples_leaf=3,
            random_state=42,
            n_jobs=-1
        ))
    ])

    cv    = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(pipe, X, y, cv=cv, scoring='accuracy')
    print(f"  5-fold CV Accuracy: {scores.mean():.3f} ± {scores.std():.3f}")

    # 4. Final fit on all data
    print("[4/5] Fitting on full dataset...")
    pipe.fit(X, y)

    # Feature importance (Random Forest)
    importances = pipe.named_steps["clf"].feature_importances_
    top_idx     = np.argsort(importances)[::-1][:5]
    print("\n  Top 5 most important features:")
    for i in top_idx:
        print(f"    {FEATURE_NAMES[i]:30s}  importance={importances[i]:.4f}")

    # 5. Save
    print(f"\n[5/5] Saving model to {model_path} ...")
    joblib.dump(pipe, model_path)
    print(f"  ✅ Model saved!")

    # Plot confusion matrix on full data
    y_pred = pipe.predict(X)
    cm     = confusion_matrix(y, y_pred, labels=list(classes))
    _plot_confusion(cm, classes)
    print("\n" + classification_report(y, y_pred))


def _plot_confusion(cm, classes):
    fig, ax = plt.subplots(figsize=(5, 4))
    im = ax.imshow(cm, interpolation='nearest', cmap='Blues')
    ax.set_title("Confusion Matrix (training data)")
    ax.set_xlabel("Predicted"); ax.set_ylabel("True")
    ax.set_xticks(range(len(classes))); ax.set_xticklabels(classes)
    ax.set_yticks(range(len(classes))); ax.set_yticklabels(classes)
    for i in range(len(classes)):
        for j in range(len(classes)):
            ax.text(j, i, str(cm[i, j]), ha='center', va='center',
                    color='white' if cm[i, j] > cm.max() / 2 else 'black')
    plt.tight_layout()
    plt.savefig("confusion_matrix.png", dpi=150)
    plt.show()
    print("  Confusion matrix saved to confusion_matrix.png")


# ─────────────────────────────────────────────
#  Real-Time Inference
# ─────────────────────────────────────────────

class LiveClassifier:
    def __init__(self, model_path=MODEL_PATH, port=None, baud=115200):
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found: {model_path}\n"
                f"Run: python ml_classifier.py --train"
            )
        self.pipe   = joblib.load(model_path)
        self.port   = port
        self.baud   = baud
        self.buffer = collections.deque(maxlen=EPOCH_SAMPLES * 3)
        self.pred_history = collections.deque(maxlen=5)   # smooth over 5 preds
        self.last_pred = "unknown"
        self.confidence = 0.0
        self._stop  = threading.Event()

    def _find_port(self):
        ports = serial.tools.list_ports.comports()
        for p in ports:
            if any(kw in (p.description or "").upper()
                   for kw in ["CP210", "CH340", "USB", "SILICON"]):
                return p.device
        return ports[0].device if ports else None

    def _serial_thread(self):
        port = self.port or self._find_port()
        ser  = serial.Serial(port, self.baud, timeout=1)
        print(f"[Live] Connected to {port}")
        while not self._stop.is_set():
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                continue
            try:
                pkt = json.loads(line)
                if pkt.get("t") == "d":
                    self.buffer.append(float(pkt["v"]))
            except Exception:
                pass

    def _inference_loop(self, callback):
        """Every 500ms, extract features from latest epoch and classify."""
        while not self._stop.is_set():
            time.sleep(0.5)
            if len(self.buffer) < EPOCH_SAMPLES:
                continue
            epoch = np.array(list(self.buffer)[-EPOCH_SAMPLES:])
            feats = extract_features(epoch).reshape(1, -1)
            pred  = self.pipe.predict(feats)[0]
            proba = self.pipe.predict_proba(feats)[0]
            conf  = float(proba.max())

            self.pred_history.append(pred)
            # Majority vote for smooth output
            from collections import Counter
            self.last_pred  = Counter(self.pred_history).most_common(1)[0][0]
            self.confidence = conf
            if callback:
                callback(self.last_pred, self.confidence)

    def run(self, callback=None):
        t1 = threading.Thread(target=self._serial_thread, daemon=True)
        t2 = threading.Thread(target=self._inference_loop,
                               args=(callback,), daemon=True)
        t1.start()
        t2.start()

    def stop(self):
        self._stop.set()


def live_demo(model_path=MODEL_PATH, port=None):
    """Simple live demo: print predictions to terminal."""
    print("\n[Live Classifier] Starting...")
    print("  Press Ctrl+C to stop.\n")

    def on_prediction(label, conf):
        bar   = "█" * int(conf * 20)
        emoji = "😌" if label == "eyes_closed" else "👀"
        print(f"\r  {emoji}  {label:15s}  confidence: [{bar:<20s}] {conf:.0%}   ",
              end="", flush=True)

    clf = LiveClassifier(model_path=model_path, port=port)
    clf.run(callback=on_prediction)
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        clf.stop()
        print("\n[Live] Stopped.")


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EEG ML Classifier — train and live inference"
    )
    parser.add_argument(
        "--train", action="store_true",
        help="Train classifier from CSV recordings in eeg_recordings/"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Run real-time inference from ESP32 serial stream"
    )
    parser.add_argument(
        "--port", type=str, default=None,
        help="Serial port (e.g. COM3). Auto-detected if omitted."
    )
    args = parser.parse_args()

    if args.train:
        train()
    elif args.live:
        if not SERIAL_OK:
            print("[ERROR] pyserial not installed. Run: pip install pyserial")
        else:
            live_demo(port=args.port)
    else:
        print("Usage:")
        print("  python ml_classifier.py --train       # train from CSV data")
        print("  python ml_classifier.py --live        # real-time inference")
        print("  python ml_classifier.py --live --port COM3")

