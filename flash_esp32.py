"""
ESP32-S3 MicroPython Flash Helper
==================================
This script performs the following tasks:
1. Downloads the appropriate MicroPython firmware for ESP32-S3
2. Flashes the firmware to ESP32-S3
3. Uploads the EEG acquisition scripts

Usage: python flash_esp32.py
"""

import subprocess
import sys
import os
import urllib.request
import time

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

# Firmware for ESP32-S3 N16R8 (16MB Flash, 8MB PSRAM)
# SPIRAM build provides PSRAM support
FIRMWARE_URL = (
    "https://micropython.org/resources/firmware/"
    "ESP32_GENERIC_S3-SPIRAM_OCT-20241129-v1.24.1.bin"
)
FIRMWARE_FILE = "ESP32_GENERIC_S3_SPIRAM.bin"

BAUD_FLASH = 460800   # Flash baud rate
BAUD_SERIAL = 115200  # Normal serial communication baud rate


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def run(cmd, check=True):
    """Run a system command and print output."""
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False, text=True)
    if check and result.returncode != 0:
        print(f"\n[ERROR] Command failed (exit code {result.returncode})")
        sys.exit(1)
    return result


def find_esp32_port():
    """Auto-detect ESP32 serial COM port on Windows."""
    import serial.tools.list_ports
    ports = serial.tools.list_ports.comports()
    candidates = []
    for p in ports:
        desc = (p.description or "").upper()
        if any(kw in desc for kw in ["CP210", "CH340", "SILICON", "USB SERIAL", "UART"]):
            candidates.append(p.device)
    return candidates


def download_firmware():
    """Download MicroPython firmware if not already downloaded."""
    if os.path.exists(FIRMWARE_FILE):
        size = os.path.getsize(FIRMWARE_FILE)
        print(f"  [OK] Firmware already downloaded ({size // 1024} KB): {FIRMWARE_FILE}")
        return True

    print(f"  Downloading firmware from MicroPython.org...")
    print(f"  URL: {FIRMWARE_URL}")
    try:
        def progress(block, block_size, total):
            done = block * block_size
            pct = min(100, int(100 * done / total))
            bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
            print(f"\r  [{bar}] {pct}% ({done//1024}/{total//1024} KB)", end="")

        urllib.request.urlretrieve(FIRMWARE_URL, FIRMWARE_FILE, reporthook=progress)
        print(f"\n  [OK] Downloaded: {FIRMWARE_FILE}")
        return True
    except Exception as e:
        print(f"\n  [FAIL] Download failed: {e}")
        print("\n  Manual download link:")
        print(f"     {FIRMWARE_URL}")
        print(f"  Save as: {FIRMWARE_FILE}")
        return False


# ─────────────────────────────────────────────
#  Main Flash Procedure
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  ESP32-S3 MicroPython Flash Tool")
    print("  EEG BCI Project — Neuralink DIY")
    print("=" * 60)

    # Step 1: Check esptool
    print("\n[1/5] Checking esptool...")
    try:
        import esptool
        print(f"  [OK] esptool found")
    except ImportError:
        print("  [INFO] esptool not found. Installing...")
        subprocess.run([sys.executable, "-m", "pip", "install", "esptool"], check=True)

    # Step 2: Find COM port
    print("\n[2/5] Locating ESP32-S3 COM port...")
    try:
        import serial.tools.list_ports
        candidates = find_esp32_port()
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "pyserial"], check=True)
        import serial.tools.list_ports
        candidates = find_esp32_port()

    all_ports = list(serial.tools.list_ports.comports())
    print(f"  Available serial ports:")
    for p in all_ports:
        print(f"    {p.device:8s} - {p.description}")

    if candidates:
        port = candidates[0]
        print(f"\n  [OK] Auto-detected ESP32 on: {port}")
    else:
        print("\n  [WARN] Could not auto-detect. Please specify port manually:")
        port = input("  Port (e.g. COM3, COM5): ").strip()
        if not port:
            print("  [ERROR] No port entered. Aborting.")
            sys.exit(1)

    # Step 3: Download firmware
    print("\n[3/5] Verifying firmware image...")
    if not download_firmware():
        print("\n  Please download manually and run this script again.")
        sys.exit(1)

    # Step 4: Erase and Flash
    print(f"\n[4/5] Flashing MicroPython to ESP32-S3 on {port}...")
    print()
    print("  Note: If connection times out, hold the BOOT button on the ESP32,")
    print("  press and release RESET, then release the BOOT button.")
    print()
    input("  Press ENTER when ready to flash...")

    print("\n  Erasing flash...")
    run([
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "--port", port,
        "--baud", str(BAUD_FLASH),
        "erase_flash"
    ])

    time.sleep(2)

    print("\n  Writing MicroPython firmware...")
    run([
        sys.executable, "-m", "esptool",
        "--chip", "esp32s3",
        "--port", port,
        "--baud", str(BAUD_FLASH),
        "write_flash",
        "-z", "0x0",
        FIRMWARE_FILE
    ])

    print("\n  [OK] MicroPython flashed successfully!")

    # Step 5: Upload EEG firmware files
    print("\n[5/5] Uploading EEG firmware scripts...")
    print()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "mpremote", "version"],
            capture_output=True, text=True
        )
        has_mpremote = result.returncode == 0
    except Exception:
        has_mpremote = False

    if not has_mpremote:
        print("  Installing mpremote tool...")
        subprocess.run([sys.executable, "-m", "pip", "install", "mpremote"], check=True)

    firmware_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "esp32_firmware"
    )
    firmware_dir = os.path.normpath(firmware_dir)

    for fname in ["main.py", "boot.py"]:
        fpath = os.path.join(firmware_dir, fname)
        if os.path.exists(fpath):
            print(f"  Uploading {fname}...")
            run([
                sys.executable, "-m", "mpremote",
                "connect", port,
                "cp", fpath, f":{fname}"
            ])
            print(f"  [OK] {fname} uploaded")
        else:
            print(f"  [WARN] {fname} not found at {fpath}")

    print()
    print("=" * 60)
    print("  Flash and setup completed!")
    print("=" * 60)
    print()
    print("  Next steps:")
    print("  1. Connect electrodes as specified in wiring documentation")
    print("  2. Reset ESP32 board (press RESET button)")
    print("  3. Run visualizer: python pc_software/eeg_visualizer.py")
    print()
    print("  To open a raw serial monitor:")
    print(f"  python -m serial.tools.miniterm {port} 115200")


if __name__ == "__main__":
    main()
