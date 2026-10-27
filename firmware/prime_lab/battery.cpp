#include "battery.h"
#include <Wire.h>
#include <ina219.h>
#include "esp_timer.h"

namespace bat {

static const int SDA_PIN = 5, SCL_PIN = 6;      // XIAO ESP32-S3: D4 — SDA, D5 — SCL

static Ina219       s_ina;
static bool         s_ok = false;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static float        s_v = 0, s_i = 0;
static double       s_q = 0;                    // мА·ч с включения агента
static uint32_t     s_n = 0;                    // удачных замеров

// Раз в секунду: замер и мА·ч. Шина I2C общая с датчиками обода — Wire сам держит её на время обмена
static void task(void*) {
  int64_t t_prev = esp_timer_get_time();
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    float v = 0, i = 0;
    if (!s_ina.read(v, i)) continue;
    const int64_t now = esp_timer_get_time();
    const double h = (now - t_prev) / 3.6e9;    // часы с прошлого удачного замера
    t_prev = now;
    portENTER_CRITICAL(&s_mux);
    s_v = v;
    s_i = i;
    s_q += i * h;
    ++s_n;
    portEXIT_CRITICAL(&s_mux);
  }
}

void begin(bool start_wire) {
  if (start_wire) Wire.begin(SDA_PIN, SCL_PIN, 400000);
  for (uint8_t a = 0x40; a <= 0x4F && !s_ok; ++a) s_ok = s_ina.begin(Wire, a);
  if (!s_ok) return;
  Serial.printf("BAT INA219 at 0x%02x\n", s_ina.addr());
  xTaskCreatePinnedToCore(task, "bat", 3072, nullptr, 1, nullptr, 0);   // ядро связи, ниже протокола
}

bool present() { return s_ok; }

void add_tel(Out& o) {
  if (!s_ok) return;
  portENTER_CRITICAL(&s_mux);
  const float v = s_v, i = s_i;
  const double q = s_q;
  const uint32_t n = s_n;
  portEXIT_CRITICAL(&s_mux);
  add(o, ",\"bat\":{\"v\":%.3f,\"i\":%.1f,\"q\":%.2f,\"n\":%lu,\"a\":%u,\"err\":%lu}", v, i, q, (unsigned long)n,
      s_ina.addr(), (unsigned long)s_ina.errors());
}

}  // namespace bat
