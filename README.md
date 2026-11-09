# Neuralink MindRide 🧠🚗

An end-to-end EEG-based Brain-Computer Interface (BCI) system designed to acquire raw brainwave signals, perform real-time digital filtering and frequency-band feature extraction, and classify mental states to control a motorized robot car.

## 📌 Architecture Overview

```
[ Scalp Electrodes (O1, A1, GND) ]
               │
               ▼
   [ AD8232 Analog Front-End ]
               │
               ▼
   [ ADS1115 16-Bit ADC (I2C) ]
               │
               ▼
[ ESP32-S3 DAQ Firmware (250 Hz) ]
               │ USB Serial (CDC)
               ▼
   [ Python Real-Time Pipeline ]
      ├── Signal Processing (50 Hz Notch, 8-13 Hz Alpha, 13-30 Hz Beta)
      ├── Real-Time Visualization (Matplotlib dashboard)
      └── ML Classification (Random Forest / SVM)
               │ WiFi UDP Commands
               ▼
[ ESP32-S3 Robot Car Controller ]
               │ PWM
               ▼
[ L298N Driver + Dual DC Motors ]
```

---

## 📂 Project Structure

```
├── esp32_firmware_pio/     # PlatformIO C++ firmware for EEG acquisition (ESP32-S3)
│   ├── platformio.ini
│   └── src/main.cpp
├── robot_car_pio/          # PlatformIO C++ firmware for robot car motor control
│   ├── platformio.ini
│   └── src/main.cpp
├── esp32_firmware/         # MicroPython fallback firmware
│   ├── boot.py
│   └── main.py
├── pc_software/            # PC-side data visualization and machine learning
│   ├── eeg_visualizer.py   # Real-time multi-panel EEG dashboard
│   ├── ml_classifier.py    # Feature extraction, model training, live inference
│   ├── mock_eeg_device.py  # Synthetic signal simulator (for offline testing)
│   ├── robot_controller.py # UDP bridge between ML engine and Robot Car
│   └── requirements.txt    # Python dependencies
├── flash_esp32.py          # Automated MicroPython firmware flashing script
└── verify_setup.py         # Local environment and dependency verification script
```

---

## ⚡ Quick Start

### 1. Set Up Python Environment
```bash
python -m venv venv
venv\Scripts\activate
pip install -r pc_software/requirements.txt
```

Verify your setup:
```bash
python verify_setup.py
```

### 2. Test Offline (Without Hardware)
Generate a synthetic EEG dataset and train the classifier:
```bash
python pc_software/mock_eeg_device.py --csv --duration 120
python pc_software/ml_classifier.py --train
```

### 3. Flash ESP32-S3 EEG Firmware
Using PlatformIO CLI:
```bash
cd esp32_firmware_pio
pio run --target upload
```

### 4. Launch Real-Time Monitor
```bash
python pc_software/eeg_visualizer.py
```
- Press `[O]` to label "Eyes Open" segments.
- Press `[C]` to label "Eyes Closed" segments.
- Press `[Q]` to quit.

---

## 🛡️ Safety Considerations
- **Isolation:** Always power the ESP32-S3 acquisition unit from an isolated USB battery pack when electrodes are connected to a human subject.
- **Laptop Power:** Disconnect the host PC/laptop from mains AC power (run on battery mode) during data collection sessions to minimize electrical risk and 50 Hz powerline hum.

---

## 📄 License
MIT License. Developed for undergraduate BCI research.
