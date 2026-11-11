/**
 * EEG Firmware - ESP32-S3 N16R8
 * ========================================================
 * Hardware : AD8232 (EEG) + ADS1115 (16-bit ADC) + 1.3" I2C OLED (U8g2)
 * Output   : USB Serial, 115200 baud, JSON lines
 * Rate     : 250 Hz (ADS1115 continuous mode + esp_timer trigger)
 * Display  : 1.3 inch I2C OLED (SH1106 128x64) via U8g2
 *
 * I2C Bus Sharing (GPIO 1 = SCL, GPIO 2 = SDA):
 *   - ADS1115 I2C Address: 0x48 (ADDR pin tied to GND)
 *   - 1.3" OLED I2C Address: 0x3C
 *   Both devices share the same SCL and SDA lines.
 *
 * Wiring Summary:
 *   ESP32-S3 3.3V  -> ADS1115 VDD, AD8232 3.3V, OLED VCC
 *   ESP32-S3 GND   -> ADS1115 GND, AD8232 GND, OLED GND, ADS1115 ADDR
 *   ESP32-S3 GPIO 1 -> ADS1115 SCL, OLED SCL
 *   ESP32-S3 GPIO 2 -> ADS1115 SDA, OLED SDA
 *   ESP32-S3 GPIO 4 <- AD8232 LO-  (Lead-Off detection -)
 *   ESP32-S3 GPIO 5 <- AD8232 LO+  (Lead-Off detection +)
 *   ADS1115 AIN0    <- AD8232 OUTPUT (Amplified EEG analog signal)
 */

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_ADS1X15.h>
#include <ArduinoJson.h>
#include <U8g2lib.h>

// ─────────────────────────────────────────────
//  Pin Definitions
// ─────────────────────────────────────────────

#define PIN_SDA         2    // I2C Data line
#define PIN_SCL         1    // I2C Clock line
#define PIN_LOD_MINUS   4    // AD8232 LO- (lead-off detect)
#define PIN_LOD_PLUS    5    // AD8232 LO+ (lead-off detect)
#define PIN_LED         48   // Built-in LED on ESP32-S3

// ─────────────────────────────────────────────
//  Sampling Configuration
// ─────────────────────────────────────────────

#define SAMPLE_RATE_HZ      250
#define SAMPLE_INTERVAL_US  (1000000UL / SAMPLE_RATE_HZ)   // 4000 us (4 ms)

// ADS1115 GAIN_ONE: +/- 4.096 V range -> 0.125 mV per LSB
#define ADS_GAIN            GAIN_ONE
#define ADS_MV_PER_BIT      0.125f

// ─────────────────────────────────────────────
//  Display Setup (1.3 inch I2C OLED - SH1106)
// ─────────────────────────────────────────────

// Standard 1.3 inch I2C OLED uses the SH1106 controller
// If your display uses SSD1306, change SH1106 to SSD1306 below.
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, /* reset= */ U8X8_PIN_NONE);

#define OLED_REFRESH_MS     100    // Update display at 10 FPS to preserve I2C bandwidth
#define WAVE_POINTS         128    // Width of OLED in pixels
#define WAVE_Y_MIN          26     // Top of waveform area
#define WAVE_Y_MAX          63     // Bottom of waveform area

uint8_t wave_buffer[WAVE_POINTS];
uint8_t wave_idx            = 0;
uint32_t last_oled_render   = 0;
float   latest_mv           = 0.0f;

// ─────────────────────────────────────────────
//  Globals & Timing
// ─────────────────────────────────────────────

Adafruit_ADS1115 ads;
esp_timer_handle_t sample_timer;

volatile bool     do_read      = false;
uint32_t          sample_index = 0;

bool     leads_ok          = false;
bool     leads_ok_last     = false;
uint32_t last_lod_ms       = 0;

char tx_buf[64];

// ─────────────────────────────────────────────
//  Hardware Timer Callback
// ─────────────────────────────────────────────

void IRAM_ATTR onSampleTimer(void* /*arg*/) {
    do_read = true;   // Flag loop() to read ADS1115 via I2C safely
}

// ─────────────────────────────────────────────
//  Serial Transmission Helpers
// ─────────────────────────────────────────────

void sendSample(uint32_t idx, float mv) {
    int n = snprintf(tx_buf, sizeof(tx_buf),
                     "{\"t\":\"d\",\"i\":%lu,\"v\":%.4f}\n",
                     (unsigned long)idx, mv);
    Serial.write((uint8_t*)tx_buf, n);
}

void sendJson(JsonDocument& doc) {
    serializeJson(doc, Serial);
    Serial.write('\n');
}

void sendInfo() {
    JsonDocument doc;
    doc["type"]                 = "info";
    doc["data"]["device"]       = "ESP32-S3 EEG";
    doc["data"]["version"]      = "2.1-oled";
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

void fatalBlink(int period_ms = 100) {
    while (true) {
        digitalWrite(PIN_LED, HIGH); delay(period_ms);
        digitalWrite(PIN_LED, LOW);  delay(period_ms);
    }
}

// ─────────────────────────────────────────────
//  OLED Drawing Routine
// ─────────────────────────────────────────────

void drawOLED(float current_mv, bool electrodes_connected) {
    u8g2.clearBuffer();

    // Header bar
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(0, 8, "MNDRIDE EEG");
    u8g2.drawStr(80, 8, "250 Hz");
    u8g2.drawHLine(0, 10, 128);

    // Status line: Voltage and Lead state
    char stat_str[32];
    snprintf(stat_str, sizeof(stat_str), "%6.1f mV", current_mv);
    u8g2.drawStr(0, 21, stat_str);

    if (electrodes_connected) {
        u8g2.drawStr(74, 21, "[LEADS OK]");
    } else {
        u8g2.drawStr(68, 21, "[DISCONN]");
    }

    u8g2.drawHLine(0, 24, 128);

    // Live waveform plot
    for (int x = 0; x < WAVE_POINTS - 1; x++) {
        int idx1 = (wave_idx + x) % WAVE_POINTS;
        int idx2 = (wave_idx + x + 1) % WAVE_POINTS;
        u8g2.drawLine(x, wave_buffer[idx1], x + 1, wave_buffer[idx2]);
    }

    u8g2.sendBuffer();
}

// ─────────────────────────────────────────────
//  Setup
// ─────────────────────────────────────────────

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}

    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, LOW);

    pinMode(PIN_LOD_MINUS, INPUT);
    pinMode(PIN_LOD_PLUS,  INPUT);

    // Initialize circular waveform buffer to center line
    for (int i = 0; i < WAVE_POINTS; i++) {
        wave_buffer[i] = (WAVE_Y_MIN + WAVE_Y_MAX) / 2;
    }

    // Initialize shared I2C bus at 400 kHz fast mode
    Wire.begin(PIN_SDA, PIN_SCL, 400000UL);

    // Initialize U8g2 1.3 inch OLED
    u8g2.begin();
    u8g2.clearBuffer();
    u8g2.setFont(u8g2_font_7x14_tf);
    u8g2.drawStr(10, 25, "Neuralink BCI");
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(15, 45, "Initializing...");
    u8g2.sendBuffer();

    // Initialize ADS1115 ADC
    if (!ads.begin(0x48, &Wire)) {
        sendError("ADS1115 not found on I2C bus");
        u8g2.clearBuffer();
        u8g2.drawStr(10, 30, "ERROR: ADS1115");
        u8g2.drawStr(10, 48, "Check wiring!");
        u8g2.sendBuffer();
        fatalBlink(100);
    }

    ads.setGain(ADS_GAIN);
    ads.setDataRate(RATE_ADS1115_250SPS);

    // Start continuous acquisition on single-ended channel 0
    ads.startADCReading(ADS1X15_REG_CONFIG_MUX_SINGLE_0, /*continuous=*/true);
    delay(10);

    sendInfo();

    // Start 250 Hz hardware timer
    esp_timer_create_args_t args = {};
    args.callback              = onSampleTimer;
    args.arg                   = nullptr;
    args.dispatch_method       = ESP_TIMER_TASK;
    args.name                  = "eeg_timer";
    args.skip_unhandled_events = true;

    esp_timer_create(&args, &sample_timer);
    esp_timer_start_periodic(sample_timer, SAMPLE_INTERVAL_US);

    digitalWrite(PIN_LED, HIGH);
}

// ─────────────────────────────────────────────
//  Main Loop
// ─────────────────────────────────────────────

void loop() {
    // 1. Process 250 Hz Sample
    if (do_read) {
        do_read = false;

        int16_t raw = ads.getLastConversionResults();
        latest_mv = raw * ADS_MV_PER_BIT;

        sendSample(sample_index++, latest_mv);

        // Map voltage around 1650 mV nominal midpoint to waveform Y pixels (26 to 63)
        // Sensitivity: +/- 150 mV full scale on display
        float diff = latest_mv - 1650.0f;
        int y = 45 - (int)(diff * 0.12f);
        if (y < WAVE_Y_MIN) y = WAVE_Y_MIN;
        if (y > WAVE_Y_MAX) y = WAVE_Y_MAX;

        wave_buffer[wave_idx] = (uint8_t)y;
        wave_idx = (wave_idx + 1) % WAVE_POINTS;
    }

    // 2. Lead-off check every 500 ms
    uint32_t now = millis();
    if (now - last_lod_ms >= 500) {
        last_lod_ms = now;

        bool ok = (digitalRead(PIN_LOD_MINUS) == LOW) &&
                  (digitalRead(PIN_LOD_PLUS)  == LOW);

        if (ok != leads_ok_last) {
            leads_ok_last = ok;
            leads_ok      = ok;
            sendLeads(ok);
            digitalWrite(PIN_LED, ok ? HIGH : LOW);
        }

        if (!leads_ok) {
            digitalWrite(PIN_LED, (now / 500) % 2);
        }
    }

    // 3. Refresh 1.3" OLED at 10 FPS
    if (now - last_oled_render >= OLED_REFRESH_MS) {
        last_oled_render = now;
        drawOLED(latest_mv, leads_ok);
    }
}
