"""
Setup Verification Script
==========================
Verifies that all required Python dependencies and signal processing
modules are correctly installed before connecting the hardware.

Usage: python verify_setup.py
"""

import sys
import importlib

STATUS_PASS = "[PASS]"
STATUS_FAIL = "[FAIL]"
STATUS_WARN = "[WARN]"

results = []

def check(name, fn):
    try:
        msg = fn()
        results.append((STATUS_PASS, name, msg or "OK"))
    except Exception as e:
        results.append((STATUS_FAIL, name, str(e)))

# ── 1. Python version ──
def check_python():
    v = sys.version_info
    assert v >= (3, 9), f"Requires Python 3.9+, found {v.major}.{v.minor}"
    return f"Python {v.major}.{v.minor}.{v.micro}"
check("Python version", check_python)

# ── 2. Required packages ──
PACKAGES = {
    "serial":      "pyserial",
    "numpy":       "numpy",
    "scipy":       "scipy",
    "matplotlib":  "matplotlib",
    "sklearn":     "scikit-learn",
    "pandas":      "pandas",
    "joblib":      "joblib",
}
for imp, pkg in PACKAGES.items():
    def _c(i=imp, p=pkg):
        mod = importlib.import_module(i)
        ver = getattr(mod, "__version__", "unknown")
        return f"{p} {ver}"
    check(f"Package: {pkg}", _c)

# ── 3. Serial port detection ──
def check_serial_ports():
    import serial.tools.list_ports
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        raise RuntimeError("No serial ports found (ESP32 is not plugged in yet)")
    return f"{len(ports)} port(s): " + ", ".join(p.device for p in ports)
check("Serial ports", check_serial_ports)

# ── 4. NumPy operations ──
def check_numpy():
    import numpy as np
    a = np.random.randn(250)
    assert a.shape == (250,)
    return f"array operations OK, version {np.__version__}"
check("NumPy operations", check_numpy)

# ── 5. SciPy signal processing ──
def check_scipy():
    import numpy as np
    from scipy import signal
    b, a = signal.butter(4, [8/125, 13/125], btype='band')
    x = np.random.randn(500)
    y = signal.filtfilt(b, a, x)
    assert y.shape == x.shape
    freqs, psd = signal.welch(x, fs=250, nperseg=250)
    return "bandpass filter and Welch PSD computation OK"
check("SciPy signal processing", check_scipy)

# ── 6. Matplotlib figure creation ──
def check_matplotlib():
    import matplotlib
    matplotlib.use('Agg')   # non-interactive test mode
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [4, 5, 6])
    plt.close(fig)
    return f"rendering backend OK, version {matplotlib.__version__}"
check("Matplotlib", check_matplotlib)

# ── 7. Scikit-learn ML pipeline ──
def check_sklearn():
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import cross_val_score

    rng = np.random.RandomState(42)
    X = rng.randn(100, 15)
    y = rng.randint(0, 2, 100)
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", RandomForestClassifier(n_estimators=10, random_state=0))
    ])
    scores = cross_val_score(pipe, X, y, cv=3)
    return f"pipeline and CV execution OK, mean accuracy: {scores.mean():.2f}"
check("scikit-learn ML pipeline", check_sklearn)

# ── 8. Mock EEG signal generator ──
def check_mock():
    sys.path.insert(0, "pc_software")
    import mock_eeg_device as m
    gen = m.EEGSignalGenerator()
    samples = [gen.next_sample() for _ in range(250)]
    import numpy as np
    arr = np.array(samples)
    assert arr.min() > 1400, "Signal baseline below expected threshold"
    assert arr.max() < 1900, "Signal peak above expected threshold"
    return f"250 samples generated, voltage range: [{arr.min():.1f}, {arr.max():.1f}] mV"
check("Mock EEG generator", check_mock)

# ── 9. Feature extraction ──
def check_features():
    sys.path.insert(0, "pc_software")
    import mock_eeg_device as m
    import ml_classifier as ml
    import numpy as np
    gen = m.EEGSignalGenerator()
    samples = np.array([gen.next_sample() for _ in range(250)])
    feats = ml.extract_features(samples)
    assert len(feats) == len(ml.FEATURE_NAMES), (
        f"Expected {len(ml.FEATURE_NAMES)} features, received {len(feats)}"
    )
    return f"{len(feats)} features extracted successfully"
check("Feature extraction", check_features)

# ── 10. esptool ──
def check_esptool():
    import subprocess
    r = subprocess.run([sys.executable, "-m", "esptool", "version"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    return r.stdout.strip().split('\n')[0]
check("esptool (firmware flash utility)", check_esptool)

# ── 11. mpremote ──
def check_mpremote():
    import subprocess
    r = subprocess.run([sys.executable, "-m", "mpremote", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    return "mpremote utility found"
check("mpremote (device file sync)", check_mpremote)

# ─────────────────────────────────────────────
#  Print Results Summary
# ─────────────────────────────────────────────

print()
print("=" * 65)
print("  EEG BCI Setup Verification")
print("=" * 65)
print()

passed = sum(1 for s, _, _ in results if s == STATUS_PASS)
failed = sum(1 for s, _, _ in results if s == STATUS_FAIL)

for status, name, msg in results:
    print(f"  {status:<8s} {name:<38s} {msg}")

print()
print("-" * 65)
print(f"  Result: {passed}/{len(results)} checks passed", end="")
if failed == 0:
    print(" -> Environment is fully configured!")
elif failed <= 2:
    print(f" -> {failed} minor check(s) flagged (see log above).")
else:
    print(f" -> {failed} failures detected. Install missing packages before continuing.")
print()

if failed > 0:
    print("  Resolution command:")
    print("  pip install pyserial numpy scipy matplotlib scikit-learn pandas joblib")
    print()
