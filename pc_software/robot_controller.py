"""
EEG Robot Car UDP Controller
=============================
Receives real-time classification states from ml_classifier.py
and broadcasts motion commands to the Robot Car over WiFi UDP.

Command mappings:
  - eyes_closed -> STOP
  - eyes_open   -> FORWARD

Requirements:
  Robot Car must be powered on and broadcasting WiFi AP:
    SSID:     EEG_Robot
    Password: neuralink123
    IP:       192.168.4.1 (default ESP32 AP gateway)
    Port:     5555
"""

import socket
import json
import time

ROBOT_IP = "192.168.4.1"
ROBOT_PORT = 5555


class RobotClient:
    def __init__(self, ip=ROBOT_IP, port=ROBOT_PORT):
        self.ip = ip
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.2)

    def send_command(self, cmd: str, confidence: float):
        payload = json.dumps({
            "cmd": cmd.upper(),
            "confidence": round(confidence, 2)
        }).encode("utf-8")

        try:
            self.sock.sendto(payload, (self.ip, self.port))
        except Exception as e:
            print(f"[UDP Error] Failed to send: {e}")

    def close(self):
        self.sock.close()


def main():
    print(f"Connecting to Robot Car on {ROBOT_IP}:{ROBOT_PORT}...")
    client = RobotClient()

    try:
        while True:
            # Interactive test console
            user_in = input("Enter command (FORWARD/BACKWARD/LEFT/RIGHT/STOP) or 'q' to quit: ").strip().upper()
            if user_in == 'Q':
                break
            client.send_command(user_in, confidence=1.0)
            print(f"Sent: {user_in}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        client.send_command("STOP", confidence=1.0)
        client.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()

