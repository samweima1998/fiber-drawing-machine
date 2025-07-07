/*
  Nano 33 IoT — HX711 + DS18B20 combo (non‑blocking + remote tare)
  ----------------------------------------------------------------
  Streams CSV: temp_C, raw_grams, compensated_grams  (one line per loop)

  Protocol from Raspberry Pi → Arduino (USB‑Serial):
    - Send "T" or "tare" followed by newline → Arduino performs scale.tare();
      replies one‑shot "# TARE_OK".

  Wiring
  ------
  HX711 : DOUT → D2,  SCK → D3,  VCC/GND → 3V3/GND
  DS18B20: DQ  → D4 (+4.7 kΩ to 3V3), VDD → 3V3, GND → GND

  Notes
  -----
  • Uses non‑blocking temperature polling: starts a new conversion every 1 s and
    reads the previous conversion on the next pass.
  • HX711 sampled each loop; delay(100) gives ≈10 Hz output stream.
  • Compensation coefficients (TC_OFFSET_KG, TC_SPAN_REL) are examples—tune to
    your own calibration data.
*/

#include "HX711.h"
#include <OneWire.h>
#include <DallasTemperature.h>

// ----------- user‑configurable constants ------------
const byte HX_DOUT_PIN = 2;
const byte HX_SCK_PIN  = 3;
const byte ONEWIRE_PIN = 4;

// Calibration (edit for your hardware)
const float CALIBRATION_FACTOR = 18.5f;   // scale factor for grams
const float TEMP_ZERO_PT  = 25.0f;        // reference °C for compensation
const float TC_OFFSET_KG  = 0.000f;       // kg zero drift / °C
const float TC_SPAN_REL   = 0.0000f;     // relative span drift / °C

const unsigned long TEMP_INTERVAL_MS = 1000; // 1 s between temp polls
// -----------------------------------------------------

HX711 scale;
OneWire oneWire(ONEWIRE_PIN);
DallasTemperature ds18b20(&oneWire);

unsigned long nextTempPoll = 0;   // scheduler for DS18B20
float temperatureC = NAN;         // last good temp value

void setup() {
  Serial.begin(115200);
  while (!Serial) {/*wait for USB*/}

  // ---- DS18B20 init ----
  ds18b20.begin();
  ds18b20.setWaitForConversion(false);    // non‑blocking mode
  nextTempPoll = millis();

  // ---- HX711 init ----
  scale.begin(HX_DOUT_PIN, HX_SCK_PIN);
  scale.set_scale(CALIBRATION_FACTOR);
  scale.tare();

  // ---- Serial protocol ----
  Serial.setTimeout(5);     // tiny timeout for readStringUntil
  // Serial.println(F("# temp_C,raw_g,comp_g"));
  // Serial.println(F("# send 'T' or 'tare' to zero"));
}

// ---------------------------------------------------------------------------
//  helper: handle commands from Raspberry Pi
// ---------------------------------------------------------------------------
void handleSerialCommands() {
  if (!Serial.available()) return;
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  cmd.toLowerCase();
  if (cmd == "t" || cmd == "tare") {
    scale.tare();
    Serial.println(F("# TARE_OK"));
  }
}

// ---------------------------------------------------------------------------
//  helper: non‑blocking temperature polling
// ---------------------------------------------------------------------------
void pollTemperature() {
  unsigned long now = millis();
  if (now >= nextTempPoll) {
    // start new conversion and grab the result of the previous one
    ds18b20.requestTemperatures();
    temperatureC = ds18b20.getTempCByIndex(0);
    nextTempPoll += TEMP_INTERVAL_MS;
  }
}

void loop() {
  handleSerialCommands();
  pollTemperature();

  // -------- Load‑cell reading --------
  float raw_g = scale.get_units(5);          // grams

  // -------- Compensation (optional) ---
  float raw_kg       = raw_g / 1000.0f;
  float zero_corr_kg = TC_OFFSET_KG * (temperatureC - TEMP_ZERO_PT);
  float span_corr    = 1.0f + TC_SPAN_REL * (temperatureC - TEMP_ZERO_PT);
  float comp_kg      = (raw_kg - zero_corr_kg) / span_corr;
  float comp_g       = comp_kg * 1000.0f;

  // -------- Output line ---------------
  Serial.print(temperatureC, 2); Serial.print(',');
  Serial.print(raw_g, 1);        Serial.print(',');
  Serial.println(comp_g, 1);

  delay(100);   // ~10 Hz output (adjust if needed)
}
