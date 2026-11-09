"""
EEG Signal Simulator — Synthetic Data Generation
=================================================
Generates realistic synthetic EEG signals for offline testing
of eeg_visualizer.py and ml_classifier.py without requiring physical hardware.

Usage options:
  1. Generate synthetic labeled CSV dataset:
     python mock_eeg_device.py --csv --duration 180

  2. Stream via virtual serial loopback port (e.g. COM10 paired to COM11):
     python mock_eeg_device.py --port COM10
"""

import sys
import time
import json
import math
import random
import argparse
import csv
import os

# ─────────────────────────────────────────────
#  Signal Generator Parameters
# ─────────────────────────────────────────────

SAMPLE_RATE = 250   # Sampling rate in Hz
DT = 1.0 / SAMPLE_RATE


class EEGSignalGenerator:
    """
    Synthetic EEG signal generator.
    Simulates:
      - Delta rhythm (1 Hz)
      - Theta rhythm (6 Hz)
      - Alpha rhythm (10 Hz) — dynamically modulated by eyes open/closed state
      - Beta rhythm  (20 Hz)
      - 50 Hz powerline interference
      - Gaussian white noise
    """

    def __init__(self):
        self.t = 0.0
        self.eyes_open = True
        self._alpha_amp = 5.0
        self._target_alpha = 5.0

    def set_state(self, eyes_open: bool):
        self.eyes_open = eyes_open
        # Alpha wave amplitude increases significantly when eyes are closed
        self._target_alpha = 5.0 if eyes_open else 50.0

    def next_sample(self) -> float:
        """Generate a single sample at the current simulation timestamp."""
        # Smooth exponential moving average for state transition
        self._alpha_amp += (self._target_alpha - self._alpha_amp) * 0.01

        delta = 20.0 * math.sin(2 * math.pi * 1.0 * self.t)
        theta = 8.0 * math.sin(2 * math.pi * 6.0 * self.t)
        alpha = self._alpha_amp * math.sin(2 * math.pi * 10.0 * self.t)
        beta  = 3.0 * math.sin(2 * math.pi * 20.0 * self.t)

        # 50 Hz powerline hum
        line_noise = 2.0 * math.sin(2 * math.pi * 50.0 * self.t)

        # Baseline noise
        noise = random.gauss(0, 2.0)

        # Total signal in millivolts, biased to AD8232 mid-supply rail (~1650 mV)
        mv = delta + theta + alpha + beta + line_noise + noise + 1650.0

        self.t += DT
        return mv


# ─────────────────────────────────────────────
#  Serial Port Emulation
# ─────────────────────────────────────────────

def run_serial(port: str):
    """
    Stream synthetic EEG packets over a virtual COM port.
    Use software like com0com (Windows) or socat (Linux) to create a null-modem pair.
    """
    try:
        import serial
    except ImportError:
        print("[ERROR] pyserial is not installed: pip install pyserial")
        sys.exit(1)

    gen = EEGSignalGenerator()
    ser = serial.Serial(port, 115200, timeout=1)
    idx = 0
    state = True

    print(f"[Mock] Streaming synthetic EEG to {port} at 250 Hz")
    print("[Mock] State toggles automatically every 10 seconds")
    print("[Mock] Press Ctrl+C to stop\n")

    # Handshake packets
    ser.write(json.dumps({
        "type": "info",
        "data": {"device": "MOCK_EEG", "sample_rate": 250, "units": "mV"}
    }).encode() + b'\n')
    ser.write(json.dumps({
        "type": "leads", "data": {"connected": True}
    }).encode() + b'\n')

    t_state_change = time.time()

    while True:
        t_start = time.perf_counter()

        # Alternate eyes open vs eyes closed every 10 seconds
        if time.time() - t_state_change > 10:
            state = not state
            gen.set_state(state)
            state_str = "EYES OPEN" if state else "EYES CLOSED"
            print(f"\r[Mock] State transitioned to: {state_str}            ")
            t_state_change = time.time()

        mv = gen.next_sample()
        pkt = f'{{"t":"d","i":{idx},"v":{mv:.4f}}}\n'
        ser.write(pkt.encode())
        idx += 1

        elapsed = time.perf_counter() - t_start
        sleep_t = DT - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)


# ─────────────────────────────────────────────
#  CSV File Generation Mode
# ─────────────────────────────────────────────

def run_csv(duration_sec=120, output_dir="eeg_recordings"):
    """
    Generate synthetic labeled dataset for training the ML classifier directly.
    """
    os.makedirs(output_dir, exist_ok=True)
    fname = os.path.join(
        output_dir,
        "mock_eeg_{}.csv".format(time.strftime("%Y%m%d_%H%M%S"))
    )

    gen = EEGSignalGenerator()
    total_samples = int(duration_sec * SAMPLE_RATE)
    block_samples = 30 * SAMPLE_RATE  # 30-second intervals per condition

    print(f"[Mock CSV] Generating {duration_sec}s of synthetic EEG data...")
    print(f"[Mock CSV] Output path: {fname}")

    with open(fname, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_s", "voltage_mV", "label"])

        state = True

        for i in range(total_samples):
            if i % block_samples == 0 and i > 0:
                state = not state
                gen.set_state(state)
                label_name = "eyes_open" if state else "eyes_closed"
                writer.writerow([f"{i/SAMPLE_RATE:.4f}", "LABEL_MARKER", label_name])
                print(f"  t = {i//SAMPLE_RATE}s -> Transition to {label_name}")

            mv = gen.next_sample()
            ts = i / SAMPLE_RATE
            label = "eyes_open" if state else "eyes_closed"
            writer.writerow([f"{ts:.4f}", f"{mv:.6f}", label])

            if i % (10 * SAMPLE_RATE) == 0:
                pct = int(100 * i / total_samples)
                print(f"  Progress: {pct}%", end='\r')

    print(f"\n[Mock CSV] Finished! Saved {total_samples} samples to {fname}")
    print("[Mock CSV] Run training: python pc_software/ml_classifier.py --train")


# ─────────────────────────────────────────────
#  CLI Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Synthetic EEG signal generator for local testing"
    )
    parser.add_argument(
        "--csv", action="store_true",
        help="Generate a labeled CSV file directly (no COM port required)"
    )
    parser.add_argument(
        "--port", type=str, default=None,
        help="Virtual serial COM port to stream to (e.g. COM10)"
    )
    parser.add_argument(
        "--duration", type=int, default=120,
        help="Duration in seconds for CSV generation (default: 120)"
    )
    args = parser.parse_args()

    if args.csv:
        run_csv(duration_sec=args.duration)
    elif args.port:
        run_serial(args.port)
    else:
        print("Usage:")
        print("  python mock_eeg_device.py --csv                 # Create test dataset")
        print("  python mock_eeg_device.py --csv --duration 180  # Create 3-minute dataset")
        print("  python mock_eeg_device.py --port COM10          # Stream via serial")
        print()
        print("Quick start:")
        print("  1. python mock_eeg_device.py --csv")
        print("  2. python pc_software/ml_classifier.py --train")
