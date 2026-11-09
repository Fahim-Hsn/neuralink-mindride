/**
 * EEG Firmware — ESP32-S3 N16R8
 * ================================
 * Hardware : AD8232 (EEG amplifier) + ADS1115 (16-bit ADC) + ESP32-S3
 * Output   : USB Serial, 115200 baud, JSON lines
 * Rate     : 250 Hz (ADS1115 continuous mode + esp_timer read trigger)
 * Protocol : {"t":"d","i":<index>,"v":<mV>}
 *
 * Why continuous mode?
 *   - Calling I2C inside an ISR is UNSAFE and causes crashes
 *   - Continuous mode: ADS1115 converts autonomously at 250 SPS
 *   - esp_timer callback only sets a flag (ISR-safe, single instruction)
 *   - loop() reads the result via I2C safely when the flag is set
 *
 * Wiring:
 *   ADS1115 SDA  → GPIO 2
 *   ADS1115 SCL  → GPIO 1
 *   ADS1115 ADDR → GND        (I2C address 0x48)
 *   ADS1115 AIN0 ← AD8232 OUTPUT
 *   ADS1115 ALRT → GPIO 6     (conversion-ready interrupt, optional)
 *   AD8232  LO-  → GPIO 4
 *   AD8232  LO+  → GPIO 5
 *   AD8232  3.3V ← ESP32 3.3V
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <ArduinoJson.h>

// ─────────────────────────────────────────────
//  Pin Definitions
// ─────────────────────────────────────────────

#define PIN_SDA       2
#define PIN_SCL       1
#define PIN_LOD_MINUS 4    // AD8232 LO-  (lead-off detect)
#define PIN_LOD_PLUS  5    // AD8232 LO+  (lead-off detect)
#define PIN_LED       48   // Built-in LED (ESP32-S3 DevKit)

// ─────────────────────────────────────────────
//  Sampling Configuration
// ─────────────────────────────────────────────

#define SAMPLE_RATE_HZ      250
#define SAMPLE_INTERVAL_US  (1000000UL / SAMPLE_RATE_HZ)   // 4000 µs

// ADS1115 GAIN_ONE = ±4.096 V range → 0.125 mV per LSB
// AD8232 output: biased at VCC/2 ≈ 1650 mV, swings ±several hundred mV
#define ADS_GAIN        GAIN_ONE
#define ADS_MV_PER_BIT  0.125f

// ─────────────────────────────────────────────
//  Globals
// ─────────────────────────────────────────────

Adafruit_ADS1115 ads;
esp_timer_handle_t sample_timer;

// ISR → loop() communication: only a flag (no I2C in ISR!)
volatile bool     do_read      = false;
uint32_t          sample_index = 0;

// Lead-off
bool     leads_ok          = false;
bool     leads_ok_last     = false;
uint32_t last_lod_ms       = 0;

// Serial output buffer (avoid String allocations in hot path)
char tx_buf[64];

// ─────────────────────────────────────────────
//  Timer Callback — runs in esp_timer task context
//  Safe to set a flag; NOT safe to do I2C here
// ─────────────────────────────────────────────

void IRAM_ATTR onSampleTimer(void* /*arg*/) {
    do_read = true;   // signal loop() to read ADS1115
}

// ─────────────────────────────────────────────
//  Serial Output Helpers
// ─────────────────────────────────────────────

// Hot path: hand-craft JSON without heap allocation
void sendSample(uint32_t idx, float mv) {
    // {"t":"d","i":12345,"v":1652.3421}
    int n = snprintf(tx_buf, sizeof(tx_buf),
                     "{\"t\":\"d\",\"i\":%lu,\"v\":%.4f}\n",
                     (unsigned long)idx, mv);
    Serial.write((uint8_t*)tx_buf, n);
}

// Cold path: use ArduinoJson for structured messages
void sendJson(JsonDocument& doc) {
    serializeJson(doc, Serial);
    Serial.write('\n');
}

void sendInfo() {
    JsonDocument doc;
    doc["type"]                 = "info";
    doc["data"]["device"]       = "ESP32-S3 EEG";
    doc["data"]["version"]      = "2.0-pio";
    doc["data"]["sample_rate"]  = SAMPLE_RATE_HZ;
    doc["data"]["units"]        = "mV";
    doc["data"]["adc_bits"]     = 16;
    doc["data"]["gain_mv"]      = 4096;
    sendJson(doc);
}

void sendLeads(bool connected) {
    JsonDocument doc;
    doc["type"]              = "leads";
    doc["data"]["connected"] = connected;
    sendJson(doc);
}

void sendError(const char* msg) {
    JsonDocument doc;
    doc["type"] = "error";
    doc["msg"]  = msg;
    sendJson(doc);
}

// ─────────────────────────────────────────────
//  Blink LED forever (fatal error indicator)
// ─────────────────────────────────────────────

void fatalBlink(int period_ms = 100) {
    while (true) {
        digitalWrite(PIN_LED, HIGH); delay(period_ms);
        digitalWrite(PIN_LED, LOW);  delay(period_ms);
    }
}

// ─────────────────────────────────────────────
//  Setup
// ─────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}  // wait for USB CDC

    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, LOW);

    // AD8232 lead-off pins: LOW = electrode connected, HIGH = off
    pinMode(PIN_LOD_MINUS, INPUT);
    pinMode(PIN_LOD_PLUS,  INPUT);

    // ── I2C ──
    Wire.begin(PIN_SDA, PIN_SCL, 400000UL);  // 400 kHz fast mode

    // ── ADS1115 ──
    if (!ads.begin(0x48, &Wire)) {
        sendError("ADS1115 not found — check SDA/SCL wiring");
        fatalBlink(100);  // fast blink = hardware error
    }

    ads.setGain(ADS_GAIN);
    ads.setDataRate(RATE_ADS1115_250SPS);  // 250 conversions/sec

    // Start CONTINUOUS conversion on AIN0 (single-ended vs GND)
    // ADS1115 will keep converting at 250 SPS automatically.
    // We just read getLastConversionResults() every 4 ms from loop().
    ads.startADCReading(ADS1X15_REG_CONFIG_MUX_SINGLE_0, /*continuous=*/true);

    // Brief settling time (let first conversion complete)
    delay(10);

    // ── Handshake ──
    sendInfo();

    // ── Hardware timer at 250 Hz ──
    // ESP_TIMER_TASK: callback runs in a dedicated FreeRTOS task (not ISR)
    // → safe for lightweight work, our flag set is instant
    esp_timer_create_args_t args = {};
    args.callback              = onSampleTimer;
    args.arg                   = nullptr;
    args.dispatch_method       = ESP_TIMER_TASK;
    args.name                  = "eeg";
    args.skip_unhandled_events = true;   // drop if loop() is behind

    esp_timer_create(&args, &sample_timer);
    esp_timer_start_periodic(sample_timer, SAMPLE_INTERVAL_US);

    digitalWrite(PIN_LED, HIGH);  // solid LED = running OK
}

// ─────────────────────────────────────────────
//  Main Loop
// ─────────────────────────────────────────────

void loop() {

    // ── 1. Sample path (runs ~250 times/sec) ──
    if (do_read) {
        do_read = false;

        // Safe I2C read — continuous mode means result is always fresh
        int16_t raw = ads.getLastConversionResults();
        float   mv  = raw * ADS_MV_PER_BIT;

        sendSample(sample_index++, mv);
    }

    // ── 2. Lead-off check (every 500 ms) ──
    uint32_t now = millis();
    if (now - last_lod_ms >= 500) {
        last_lod_ms = now;

        bool ok = (digitalRead(PIN_LOD_MINUS) == LOW) &&
                  (digitalRead(PIN_LOD_PLUS)  == LOW);

        if (ok != leads_ok_last) {
            leads_ok_last = ok;
            leads_ok      = ok;
            sendLeads(ok);

            // LED: solid=running+leads, slow blink=no leads
            if (!ok) {
                // slow blink handled next iteration — just turn off for now
                digitalWrite(PIN_LED, LOW);
            } else {
                digitalWrite(PIN_LED, HIGH);
            }
        }

        // Slow blink when leads are off (non-blocking)
        if (!leads_ok) {
            digitalWrite(PIN_LED, (now / 500) % 2);
        }
    }
}
