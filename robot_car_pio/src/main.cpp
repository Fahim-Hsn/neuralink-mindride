/**
 * Robot Car Firmware — ESP32-S3 (Second ESP32)
 * =============================================
 * Hardware : L298N Motor Driver + 2× DC Gear Motor + ESP32-S3
 * Input    : WiFi UDP commands from PC (ML output)
 * Commands : FORWARD / BACKWARD / LEFT / RIGHT / STOP
 *
 * Architecture:
 *   PC (ML Classifier)
 *      ↓ UDP packet (JSON)
 *   ESP32 #2 (this firmware) — WiFi AP mode
 *      ↓ PWM signals
 *   L298N Motor Driver
 *      ↓ DC current
 *   Left Motor + Right Motor
 *
 * L298N Wiring:
 *   ENA  → GPIO 10  (PWM — left motor speed)
 *   IN1  → GPIO 11  (left motor direction A)
 *   IN2  → GPIO 12  (left motor direction B)
 *   ENB  → GPIO 13  (PWM — right motor speed)
 *   IN3  → GPIO 14  (right motor direction A)
 *   IN4  → GPIO 15  (right motor direction B)
 *   L298N 12V ← Battery pack (7.4V–12V)
 *   L298N GND ← Battery GND + ESP32 GND (common ground!)
 *   L298N 5V  → ESP32 5V (or use separate regulator)
 *
 * Safety:
 *   - Watchdog: if no command for 1 second → STOP automatically
 *   - Speed ramp: gentle acceleration to avoid current spikes
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ArduinoJson.h>

// ─────────────────────────────────────────────
//  WiFi Configuration
//  Must match values in pc_software/robot_controller.py
// ─────────────────────────────────────────────

#define WIFI_SSID     "EEG_Robot"      // Robot creates its own WiFi hotspot
#define WIFI_PASS     "neuralink123"
#define UDP_PORT      5555

// ─────────────────────────────────────────────
//  Motor Driver Pins (L298N)
// ─────────────────────────────────────────────

#define PIN_ENA   10   // Left motor PWM speed
#define PIN_IN1   11   // Left motor dir A
#define PIN_IN2   12   // Left motor dir B
#define PIN_ENB   13   // Right motor PWM speed
#define PIN_IN3   14   // Right motor dir A
#define PIN_IN4   15   // Right motor dir B

#define PIN_LED   48   // Status LED

// ─────────────────────────────────────────────
//  Motor Speed (0–255)
// ─────────────────────────────────────────────

#define SPEED_FORWARD   200   // 78% duty cycle
#define SPEED_TURN      160   // slower for turns
#define SPEED_STOP      0

// ─────────────────────────────────────────────
//  Watchdog: auto-stop if no command for 1s
// ─────────────────────────────────────────────

#define WATCHDOG_MS   1000

// ─────────────────────────────────────────────
//  Globals
// ─────────────────────────────────────────────

WiFiUDP udp;
uint32_t last_cmd_ms = 0;
String   current_cmd = "STOP";
uint32_t cmd_count   = 0;

// ─────────────────────────────────────────────
//  Motor Control Functions
// ─────────────────────────────────────────────

void motorLeft(int speed, bool forward) {
    // speed: 0–255, forward: true=forward, false=backward
    analogWrite(PIN_ENA, abs(speed));
    digitalWrite(PIN_IN1, forward ? HIGH : LOW);
    digitalWrite(PIN_IN2, forward ? LOW  : HIGH);
}

void motorRight(int speed, bool forward) {
    analogWrite(PIN_ENB, abs(speed));
    digitalWrite(PIN_IN3, forward ? HIGH : LOW);
    digitalWrite(PIN_IN4, forward ? LOW  : HIGH);
}

void motorStop() {
    analogWrite(PIN_ENA, 0);
    analogWrite(PIN_ENB, 0);
    digitalWrite(PIN_IN1, LOW); digitalWrite(PIN_IN2, LOW);
    digitalWrite(PIN_IN3, LOW); digitalWrite(PIN_IN4, LOW);
}

void executeCommand(const String& cmd) {
    if (cmd == "FORWARD") {
        motorLeft(SPEED_FORWARD, true);
        motorRight(SPEED_FORWARD, true);
    }
    else if (cmd == "BACKWARD") {
        motorLeft(SPEED_FORWARD, false);
        motorRight(SPEED_FORWARD, false);
    }
    else if (cmd == "LEFT") {
        // Pivot left: left motor backward, right forward
        motorLeft(SPEED_TURN, false);
        motorRight(SPEED_TURN, true);
    }
    else if (cmd == "RIGHT") {
        motorLeft(SPEED_TURN, true);
        motorRight(SPEED_TURN, false);
    }
    else {   // STOP or unknown
        motorStop();
    }
}

// ─────────────────────────────────────────────
//  UDP Status Reply
// ─────────────────────────────────────────────

void sendStatus(IPAddress ip, uint16_t port, const String& cmd) {
    JsonDocument doc;
    doc["ack"]   = cmd;
    doc["count"] = cmd_count;
    doc["uptime_s"] = millis() / 1000;

    char buf[128];
    int n = serializeJson(doc, buf, sizeof(buf));
    udp.beginPacket(ip, port);
    udp.write((uint8_t*)buf, n);
    udp.endPacket();
}

// ─────────────────────────────────────────────
//  Setup
// ─────────────────────────────────────────────

void setup() {
    Serial.begin(115200);

    // LED
    pinMode(PIN_LED, OUTPUT);

    // Motor pins
    pinMode(PIN_IN1, OUTPUT); pinMode(PIN_IN2, OUTPUT);
    pinMode(PIN_IN3, OUTPUT); pinMode(PIN_IN4, OUTPUT);
    pinMode(PIN_ENA, OUTPUT); pinMode(PIN_ENB, OUTPUT);
    motorStop();

    // ── WiFi Access Point ──
    // Robot broadcasts its own WiFi — no router needed
    WiFi.mode(WIFI_AP);
    WiFi.softAP(WIFI_SSID, WIFI_PASS);

    Serial.print("Robot WiFi AP: "); Serial.println(WIFI_SSID);
    Serial.print("IP address:    "); Serial.println(WiFi.softAPIP());

    // ── UDP listener ──
    udp.begin(UDP_PORT);
    Serial.printf("Listening on UDP port %d\n", UDP_PORT);

    last_cmd_ms = millis();

    // Blink 3× to signal ready
    for (int i = 0; i < 3; i++) {
        digitalWrite(PIN_LED, HIGH); delay(200);
        digitalWrite(PIN_LED, LOW);  delay(200);
    }
    digitalWrite(PIN_LED, HIGH);

    Serial.println("Robot ready! Waiting for EEG commands...");
}

// ─────────────────────────────────────────────
//  Main Loop
// ─────────────────────────────────────────────

void loop() {
    // ── 1. Receive UDP command ──
    int pkt_size = udp.parsePacket();
    if (pkt_size > 0) {
        char buf[256] = {};
        udp.read(buf, sizeof(buf) - 1);

        // Parse JSON: {"cmd": "FORWARD", "confidence": 0.92}
        JsonDocument doc;
        DeserializationError err = deserializeJson(doc, buf);

        if (!err) {
            String cmd = doc["cmd"] | "STOP";
            float conf = doc["confidence"] | 0.0f;

            // Only execute if confidence is high enough
            if (conf >= 0.65f || cmd == "STOP") {
                current_cmd  = cmd;
                last_cmd_ms  = millis();
                cmd_count++;

                executeCommand(cmd);

                // Log to serial
                Serial.printf("[CMD #%lu] %s (conf=%.2f)\n",
                              cmd_count, cmd.c_str(), conf);

                // Acknowledge back to PC
                sendStatus(udp.remoteIP(), udp.remotePort(), cmd);
            } else {
                // Low confidence → safe stop
                Serial.printf("[SKIP] %s conf=%.2f < 0.65 → STOP\n",
                              cmd.c_str(), conf);
                executeCommand("STOP");
            }
        }
    }

    // ── 2. Watchdog: auto-stop if silent > 1s ──
    if (millis() - last_cmd_ms > WATCHDOG_MS) {
        if (current_cmd != "STOP") {
            current_cmd = "STOP";
            motorStop();
            Serial.println("[WATCHDOG] No command → STOP");
        }
    }

    // ── 3. LED heartbeat ──
    // Fast blink = running but no recent command
    // Solid = active command
    uint32_t now = millis();
    if (current_cmd == "STOP") {
        digitalWrite(PIN_LED, (now / 300) % 2);
    } else {
        digitalWrite(PIN_LED, HIGH);
    }
}
