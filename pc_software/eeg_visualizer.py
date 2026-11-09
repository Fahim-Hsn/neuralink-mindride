"""
EEG PC-Side Receiver & Real-Time Visualizer
==========================================
Receives EEG data from ESP32-S3 via USB Serial,
displays real-time waveform, computes alpha/beta band power,
and saves data to CSV for later ML training.

Requirements:
    pip install pyserial numpy scipy matplotlib
"""

import serial
import serial.tools.list_ports
import json
import time
import threading
import collections
import csv
import os

import numpy as np
from scipy import signal as sp_signal
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.gridspec import GridSpec

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

BAUD_RATE      = 115200
SAMPLE_RATE    = 250          # Hz
BUFFER_SECONDS = 5            # seconds of data shown in rolling window
BUFFER_SIZE    = SAMPLE_RATE * BUFFER_SECONDS  # 1250 samples

# Band definitions (Hz)
DELTA_BAND  = (0.5, 4.0)
THETA_BAND  = (4.0, 8.0)
ALPHA_BAND  = (8.0, 13.0)
BETA_BAND   = (13.0, 30.0)

# Notch filter: remove 50 Hz (Bangladesh power line)
NOTCH_FREQ  = 50.0
NOTCH_Q     = 30.0

# Update rate for plots
PLOT_UPDATE_MS = 100          # 10 fps — smooth but not too heavy

# CSV output
CSV_DIR  = "eeg_recordings"
os.makedirs(CSV_DIR, exist_ok=True)

# ─────────────────────────────────────────────
#  Signal Processing Utilities
# ─────────────────────────────────────────────

def design_bandpass(low, high, fs, order=4):
    """Butterworth bandpass filter coefficients."""
    nyq = fs / 2.0
    b, a = sp_signal.butter(order, [low / nyq, high / nyq], btype='band')
    return b, a


def design_notch(freq, q, fs):
    """IIR notch filter at given frequency."""
    b, a = sp_signal.iirnotch(freq / (fs / 2.0), q)
    return b, a


def band_power(data, fs, low, high):
    """
    Compute band power using Welch's method.
    Returns power in µV² (or mV² depending on input units).
    """
    if len(data) < fs:          # need at least 1 second
        return 0.0
    freqs, psd = sp_signal.welch(
        data, fs=fs, nperseg=min(len(data), fs),
        scaling='density'
    )
    idx = np.logical_and(freqs >= low, freqs <= high)
    return float(np.trapz(psd[idx], freqs[idx]))


def signal_quality(data):
    """
    Simple quality metric:
    - Returns 'GOOD', 'NOISY', or 'FLAT' (lead-off)
    """
    if len(data) < SAMPLE_RATE:
        return "WAIT"
    arr = np.array(data)
    rms = np.sqrt(np.mean(arr ** 2))
    std = np.std(arr)
    if std < 0.5:               # near-zero variance → flat → lead off
        return "FLAT (check electrodes!)"
    if std > 500:               # extreme variance → heavy artifact
        return "VERY NOISY"
    if std > 100:
        return "NOISY"
    return "GOOD [OK]"


# ─────────────────────────────────────────────
#  Serial Port Auto-Detection
# ─────────────────────────────────────────────

def find_esp32_port():
    """Find the first available serial port (likely ESP32)."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        # ESP32-S3 USB shows up as CP210x or CH340 or USB Serial
        if any(kw in (p.description or "").upper()
               for kw in ["CP210", "CH340", "USB", "UART", "SILICON"]):
            return p.device
    # Fallback: return first available port
    if ports:
        return ports[0].device
    return None


# ─────────────────────────────────────────────
#  EEG Receiver (runs in background thread)
# ─────────────────────────────────────────────

class EEGReceiver:
    def __init__(self, port, baud=BAUD_RATE):
        self.port        = port
        self.baud        = baud
        self.raw_buffer  = collections.deque(maxlen=BUFFER_SIZE)
        self.all_samples = []           # for CSV saving
        self.timestamps  = []
        self.leads_ok    = False
        self.connected   = False
        self._stop       = threading.Event()
        self._lock       = threading.Lock()
        self._csv_file   = None
        self._csv_writer = None
        self._start_new_csv()

    def _start_new_csv(self):
        fname = os.path.join(
            CSV_DIR,
            "eeg_{}.csv".format(time.strftime("%Y%m%d_%H%M%S"))
        )
        self._csv_file   = open(fname, 'w', newline='')
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(["timestamp_s", "voltage_mV", "label"])
        print("[EEG] Saving to:", fname)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._csv_file:
            self._csv_file.flush()
            self._csv_file.close()

    def _run(self):
        try:
            ser = serial.Serial(self.port, self.baud, timeout=1)
            self.connected = True
            print(f"[EEG] Connected to {self.port} @ {self.baud} baud")
            t0 = time.time()

            while not self._stop.is_set():
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                try:
                    pkt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ptype = pkt.get("t") or pkt.get("type")

                if ptype == "d":          # data sample
                    mv  = float(pkt["v"])
                    ts  = time.time() - t0
                    with self._lock:
                        self.raw_buffer.append(mv)
                        self.all_samples.append(mv)
                        self.timestamps.append(ts)
                    # Write to CSV (label="" until user marks it)
                    self._csv_writer.writerow([f"{ts:.4f}", f"{mv:.6f}", ""])

                elif ptype == "leads":
                    self.leads_ok = pkt.get("data", {}).get("connected", False)

                elif ptype in ("info", "error"):
                    print("[ESP32]", pkt)

        except serial.SerialException as e:
            print(f"[EEG] Serial error: {e}")
            self.connected = False

    def get_buffer(self):
        """Thread-safe copy of current rolling buffer."""
        with self._lock:
            return list(self.raw_buffer)

    def label_last_n_seconds(self, n, label):
        """Mark the last n seconds of CSV data with a label."""
        n_samples = int(n * SAMPLE_RATE)
        # We'll write a marker row to CSV
        self._csv_writer.writerow(
            [f"{time.time():.4f}", "LABEL_MARKER", label]
        )
        self._csv_file.flush()
        print(f"[Label] Marked last {n}s as '{label}'")


# ─────────────────────────────────────────────
#  Real-Time Visualizer
# ─────────────────────────────────────────────

class EEGVisualizer:
    def __init__(self, receiver: EEGReceiver):
        self.rx   = receiver
        self.time_axis = np.linspace(-BUFFER_SECONDS, 0, BUFFER_SIZE)

        # Filters
        self._b_notch, self._a_notch = design_notch(NOTCH_FREQ, NOTCH_Q, SAMPLE_RATE)
        self._b_alpha, self._a_alpha = design_bandpass(*ALPHA_BAND, SAMPLE_RATE)
        self._b_beta,  self._a_beta  = design_bandpass(*BETA_BAND,  SAMPLE_RATE)

        self._setup_figure()

    def _setup_figure(self):
        self.fig = plt.figure(figsize=(14, 9), facecolor='#0d1117')
        self.fig.suptitle(
            "Real-Time EEG Monitor - Neuralink MindRide",
            color='white', fontsize=14, fontweight='bold'
        )

        gs = GridSpec(3, 2, figure=self.fig,
                      hspace=0.45, wspace=0.35,
                      left=0.07, right=0.97, top=0.92, bottom=0.07)

        ax_style = dict(facecolor='#161b22', frameon=True)

        # Row 0: Raw EEG (full width)
        self.ax_raw   = self.fig.add_subplot(gs[0, :], **ax_style)
        # Row 1: Alpha-filtered | Beta-filtered
        self.ax_alpha = self.fig.add_subplot(gs[1, 0], **ax_style)
        self.ax_beta  = self.fig.add_subplot(gs[1, 1], **ax_style)
        # Row 2: Band power bar chart | PSD
        self.ax_power = self.fig.add_subplot(gs[2, 0], **ax_style)
        self.ax_psd   = self.fig.add_subplot(gs[2, 1], **ax_style)

        for ax in [self.ax_raw, self.ax_alpha, self.ax_beta,
                   self.ax_power, self.ax_psd]:
            ax.tick_params(colors='#8b949e')
            ax.xaxis.label.set_color('#8b949e')
            ax.yaxis.label.set_color('#8b949e')
            for spine in ax.spines.values():
                spine.set_edgecolor('#30363d')

        # Raw waveform line
        self.line_raw,   = self.ax_raw.plot(
            self.time_axis, np.zeros(BUFFER_SIZE), color='#58a6ff', lw=0.8)
        self.ax_raw.set_title("Raw EEG (after 50 Hz notch)", color='white', fontsize=10)
        self.ax_raw.set_xlabel("Time (s)", color='#8b949e')
        self.ax_raw.set_ylabel("mV", color='#8b949e')

        # Alpha
        self.line_alpha, = self.ax_alpha.plot(
            self.time_axis, np.zeros(BUFFER_SIZE), color='#3fb950', lw=0.9)
        self.ax_alpha.set_title("Alpha Band (8–13 Hz)", color='white', fontsize=10)
        self.ax_alpha.set_xlabel("Time (s)")
        self.ax_alpha.set_ylabel("mV")

        # Beta
        self.line_beta,  = self.ax_beta.plot(
            self.time_axis, np.zeros(BUFFER_SIZE), color='#f78166', lw=0.9)
        self.ax_beta.set_title("Beta Band (13–30 Hz)", color='white', fontsize=10)
        self.ax_beta.set_xlabel("Time (s)")
        self.ax_beta.set_ylabel("mV")

        # Band power bars
        self.band_names   = ['Delta\n(0.5-4)', 'Theta\n(4-8)',
                              'Alpha\n(8-13)', 'Beta\n(13-30)']
        self.band_colors  = ['#79c0ff', '#d2a8ff', '#3fb950', '#f78166']
        self.bar_rects    = self.ax_power.bar(
            self.band_names, [0, 0, 0, 0],
            color=self.band_colors, edgecolor='#30363d'
        )
        self.ax_power.set_title("Band Power (µV²/Hz)", color='white', fontsize=10)
        self.ax_power.set_ylabel("Power")
        self.ax_power.set_yscale('log')

        # PSD line
        self.line_psd,   = self.ax_psd.plot([], [], color='#e3b341', lw=1.0)
        self.ax_psd.set_title("Power Spectral Density", color='white', fontsize=10)
        self.ax_psd.set_xlabel("Frequency (Hz)")
        self.ax_psd.set_ylabel("PSD (mV²/Hz)")
        self.ax_psd.set_xlim(0, 50)

        # Status text
        self.status_text = self.fig.text(
            0.5, 0.005,
            "Connecting...",
            ha='center', color='yellow', fontsize=10
        )

        # Key bindings info
        self.fig.text(
            0.99, 0.01,
            "Keys: [O]=eyes open  [C]=eyes closed  [Q]=quit",
            ha='right', color='#8b949e', fontsize=8
        )

        self.fig.canvas.mpl_connect('key_press_event', self._on_key)

    def _on_key(self, event):
        if event.key == 'o':
            self.rx.label_last_n_seconds(2, "eyes_open")
            self.status_text.set_text("Labeled: EYES OPEN (last 2s)")
        elif event.key == 'c':
            self.rx.label_last_n_seconds(2, "eyes_closed")
            self.status_text.set_text("Labeled: EYES CLOSED (last 2s)")
        elif event.key == 'q':
            plt.close('all')
            self.rx.stop()

    def update(self, frame):
        buf = self.rx.get_buffer()
        n   = len(buf)

        if n < SAMPLE_RATE:
            self.status_text.set_text("Buffering... ({}/{} samples)".format(
                n, SAMPLE_RATE))
            return

        arr = np.array(buf)
        t   = self.time_axis[-n:]

        # Apply 50 Hz notch filter
        arr_notch = sp_signal.filtfilt(self._b_notch, self._a_notch, arr)

        # Bandpass filters
        arr_alpha = sp_signal.filtfilt(self._b_alpha, self._a_alpha, arr_notch)
        arr_beta  = sp_signal.filtfilt(self._b_beta,  self._a_beta,  arr_notch)

        # Update waveforms
        pad = BUFFER_SIZE - n
        self.line_raw.set_data(
            self.time_axis,
            np.concatenate([np.zeros(pad), arr_notch])
        )
        self.ax_raw.relim(); self.ax_raw.autoscale_view()

        self.line_alpha.set_data(
            self.time_axis,
            np.concatenate([np.zeros(pad), arr_alpha])
        )
        self.ax_alpha.relim(); self.ax_alpha.autoscale_view()

        self.line_beta.set_data(
            self.time_axis,
            np.concatenate([np.zeros(pad), arr_beta])
        )
        self.ax_beta.relim(); self.ax_beta.autoscale_view()

        # Band powers
        powers = [
            band_power(arr_notch, SAMPLE_RATE, *DELTA_BAND),
            band_power(arr_notch, SAMPLE_RATE, *THETA_BAND),
            band_power(arr_notch, SAMPLE_RATE, *ALPHA_BAND),
            band_power(arr_notch, SAMPLE_RATE, *BETA_BAND),
        ]
        for rect, h in zip(self.bar_rects, powers):
            rect.set_height(max(h, 1e-10))
        self.ax_power.relim(); self.ax_power.autoscale_view()

        # PSD
        freqs, psd = sp_signal.welch(
            arr_notch, fs=SAMPLE_RATE,
            nperseg=min(n, SAMPLE_RATE),
            scaling='density'
        )
        mask = freqs <= 50
        self.line_psd.set_data(freqs[mask], psd[mask])
        self.ax_psd.relim(); self.ax_psd.autoscale_view()

        # Status bar
        quality = signal_quality(buf)
        alpha_p = powers[2]
        beta_p  = powers[3]
        ratio   = (alpha_p / beta_p) if beta_p > 1e-10 else 0
        leads   = "[OK] Electrodes Connected" if self.rx.leads_ok else "[WARN] Check Electrodes"
        self.status_text.set_text(
            f"{leads}  |  Quality: {quality}  |  "
            f"Alpha: {alpha_p:.3f}  Beta: {beta_p:.3f}  "
            f"Alpha/Beta ratio: {ratio:.2f}"
        )
        self.status_text.set_color('lime' if 'GOOD' in quality else 'orange')

    def run(self):
        self.ani = animation.FuncAnimation(
            self.fig, self.update,
            interval=PLOT_UPDATE_MS,
            cache_frame_data=False
        )
        plt.show()


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  EEG Real-Time Monitor — Neuralink DIY Project")
    print("=" * 55)

    # Auto-detect port
    port = find_esp32_port()
    if port is None:
        print("[ERROR] No serial port found. Connect ESP32 and try again.")
        print("Available ports:")
        for p in serial.tools.list_ports.comports():
            print(f"  {p.device}: {p.description}")
        return

    print(f"[INFO] Using port: {port}")
    print("[INFO] Press [O] = mark eyes open, [C] = mark eyes closed, [Q] = quit")
    print("[INFO] Data is auto-saved to eeg_recordings/")
    print()

    rx   = EEGReceiver(port)
    rx.start()

    viz  = EEGVisualizer(rx)
    viz.run()

    rx.stop()
    print("[INFO] Session ended. Check eeg_recordings/ for your data.")


if __name__ == "__main__":
    main()
