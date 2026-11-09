"""
EEG Data Acquisition Firmware for ESP32-S3
Hardware: AD8232 + ADS1115 + ESP32-S3 N16R8
Sampling Rate: 250 Hz
Protocol: USB Serial, JSON lines
  - Data sample : {"t":"d","i":<index>,"v":<mV>}
  - Lead status  : {"type":"leads","data":{"connected":<bool>}}
  - Info         : {"type":"info","data":{...}}
"""

import machine
import time
import json
import sys
from machine import Pin, SoftI2C

# ─────────────────────────────────────────────
#  ADS1115 Driver (lightweight, no extra libs)
# ─────────────────────────────────────────────

ADS1115_ADDR = 0x48          # ADDR pin -> GND
CONFIG_REG   = 0x01
CONVERT_REG  = 0x00

# Config register fields
MUX_AIN0_GND = 0x4000        # AIN0 single-ended vs GND
PGA_4096     = 0x0200        # +/-4.096 V range
MODE_SINGLE  = 0x0100        # Single-shot mode
DR_250SPS    = 0x00A0        # 250 samples/sec
COMP_QUE_DIS = 0x0003        # Disable comparator

START_CONV   = 0x8000        # OS bit: start conversion

CONFIG_VALUE = (START_CONV | MUX_AIN0_GND | PGA_4096 |
                MODE_SINGLE | DR_250SPS | COMP_QUE_DIS)


class ADS1115:
    def __init__(self, i2c, addr=ADS1115_ADDR):
        self.i2c   = i2c
        self.addr  = addr
        self._buf  = bytearray(2)

    def _write_reg(self, reg, value):
        self._buf[0] = (value >> 8) & 0xFF
        self._buf[1] = value & 0xFF
        self.i2c.writeto_mem(self.addr, reg, self._buf)

    def _read_reg(self, reg):
        data = self.i2c.readfrom_mem(self.addr, reg, 2)
        return (data[0] << 8) | data[1]

    def read_single(self):
        """Trigger one conversion on AIN0 and return raw 16-bit signed int."""
        self._write_reg(CONFIG_REG, CONFIG_VALUE)
        # 1/250 Hz = 4 ms conversion time, add margin
        time.sleep_ms(5)
        raw = self._read_reg(CONVERT_REG)
        if raw > 32767:
            raw -= 65536
        return raw

    def raw_to_mv(self, raw):
        """Convert raw ADC value to millivolts (+/-4096 mV range, 16-bit)."""
        return raw * 4096.0 / 32768.0


# ─────────────────────────────────────────────
#  Hardware Pin Configuration
# ─────────────────────────────────────────────

I2C_SCL_PIN   = 1    # GPIO1 -> ADS1115 SCL
I2C_SDA_PIN   = 2    # GPIO2 -> ADS1115 SDA
LOD_MINUS_PIN = 4    # GPIO4 -> AD8232 LO-
LOD_PLUS_PIN  = 5    # GPIO5 -> AD8232 LO+
LED_PIN       = 48   # ESP32-S3 built-in LED

# ─────────────────────────────────────────────
#  Sampling Configuration
# ─────────────────────────────────────────────

SAMPLE_INTERVAL_US = 4000   # 4000 us = 250 Hz


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def init_hardware():
    """Initialize I2C bus, ADS1115, and GPIO pins."""
    i2c = SoftI2C(
        scl=Pin(I2C_SCL_PIN),
        sda=Pin(I2C_SDA_PIN),
        freq=400_000
    )

    devices = i2c.scan()
    if ADS1115_ADDR not in devices:
        err = json.dumps({"type": "error", "msg": "ADS1115 not found on I2C bus"})
        print(err)
        sys.exit(1)

    ads       = ADS1115(i2c)
    lod_minus = Pin(LOD_MINUS_PIN, Pin.IN)
    lod_plus  = Pin(LOD_PLUS_PIN,  Pin.IN)
    led       = Pin(LED_PIN, Pin.OUT)

    return ads, lod_minus, lod_plus, led


def leads_connected(lod_minus, lod_plus):
    """Return True when both lead-off pins are LOW (electrodes on skin)."""
    return (lod_minus.value() == 0) and (lod_plus.value() == 0)


def send_status(msg_type, data):
    """Emit a JSON status message on USB serial."""
    print(json.dumps({"type": msg_type, "data": data}))


# ─────────────────────────────────────────────
#  Main Loop
# ─────────────────────────────────────────────

def main():
    ads, lod_minus, lod_plus, led = init_hardware()

    send_status("info", {
        "device":      "ESP32-S3 EEG",
        "version":     "1.0",
        "sample_rate": 250,
        "units":       "mV",
        "adc_bits":    16
    })

    sample_index   = 0
    leads_ok       = False
    last_leads_ms  = -1000

    while True:
        t_start = time.ticks_us()

        # Check lead-off status every 500 ms
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, last_leads_ms) >= 500:
            current = leads_connected(lod_minus, lod_plus)
            if current != leads_ok:
                leads_ok = current
                send_status("leads", {"connected": leads_ok})
            last_leads_ms = now_ms
            led.value(1 if leads_ok else 0)

        # Read and transmit one sample
        raw = ads.read_single()
        mv  = ads.raw_to_mv(raw)

        sys.stdout.write('{"t":"d","i":')
        sys.stdout.write(str(sample_index))
        sys.stdout.write(',"v":')
        sys.stdout.write("{:.4f}".format(mv))
        sys.stdout.write('}\n')

        sample_index += 1

        # Busy-wait for precise 4 ms period
        elapsed   = time.ticks_diff(time.ticks_us(), t_start)
        remaining = SAMPLE_INTERVAL_US - elapsed
        if remaining > 100:
            time.sleep_us(remaining - 50)


if __name__ == "__main__":
    main()
