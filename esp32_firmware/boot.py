"""
boot.py — ESP32-S3 Auto-start
Runs before main.py on every power-on.
Short delay allows Ctrl+C interrupt via serial REPL if needed.
"""
import time

# 2-second window to interrupt with Ctrl+C before main.py starts
time.sleep(2)

