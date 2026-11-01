#include "battery.h"
#include <Wire.h>
#include <ina219.h>
#include "esp_timer.h"

namespace bat {

static const int SDA_PIN = 5, SCL_PIN = 6;      // XIAO ESP32-S3: D4 — SDA, D5 — SCL
static const int SDA2_PIN = 7, SCL2_PIN = 8;    // вторая шина I2C (Wire1): D8 — SDA, D9 — SCL

// До двух INA219 на одном агенте: у агента катушки — свой контур и шина колеса. Шина — на своих выводах D8/D9
// (Wire1, адрес 0x40) или на тех же D4/D5 с перемычкой A0 (0x41)
static const int    MAXN = 2;
static Ina219       s_ina[MAXN];
static bool         s_w1[MAXN];                 // модуль на второй шине (D8/D9)
static int          s_cnt = 0;                  // сколько нашлось
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
struct Acc {
  float    v = 0, i = 0;
  double   q = 0;                               // мА·ч с включения агента
  uint32_t n = 0;                               // удачных замеров
  int64_t  t_prev = 0;
};
static Acc s_acc[MAXN];

// Раз в секунду: замер и мА·ч каждого. Шина I2C общая с датчиками обода — Wire сам держит её на время обмена
static void task(void*) {
  for (int k = 0; k < s_cnt; ++k) s_acc[k].t_prev = esp_timer_get_time();
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    for (int k = 0; k < s_cnt; ++k) {
      float v = 0, i = 0;
      if (!s_ina[k].read(v, i)) continue;
      const int64_t now = esp_timer_get_time();
      Acc& A = s_acc[k];
      const double h = (now - A.t_prev) / 3.6e9;  // часы с прошлого удачного замера
      A.t_prev = now;
      portENTER_CRITICAL(&s_mux);
      A.v = v;
      A.i = i;
      A.q += i * h;
      ++A.n;
      portEXIT_CRITICAL(&s_mux);
    }
  }
}

static void scan(TwoWire& w, bool w1) {
  for (uint8_t a = 0x40; a <= 0x4F && s_cnt < MAXN; ++a)
    if (s_ina[s_cnt].begin(w, a)) {
      Serial.printf("BAT INA219 at 0x%02x%s\n", a, w1 ? " (D8/D9)" : "");
      s_w1[s_cnt++] = w1;
    }
}

void begin(bool start_wire) {
  if (start_wire) Wire.begin(SDA_PIN, SCL_PIN, 400000);
  scan(Wire, false);
  if (s_cnt < MAXN) {                           // вторая шина: на D8/D9 больше ничего не подключают
    const int was = s_cnt;
    Wire1.begin(SDA2_PIN, SCL2_PIN, 400000);
    scan(Wire1, true);
    if (s_cnt == was) Wire1.end();              // никого — выводы свободны
  }
  if (s_cnt) xTaskCreatePinnedToCore(task, "bat", 3072, nullptr, 1, nullptr, 0);   // ядро связи, ниже протокола
}

bool present() { return s_cnt > 0; }

void add_tel(Out& o) {
  for (int k = 0; k < s_cnt; ++k) {
    portENTER_CRITICAL(&s_mux);
    const Acc A = s_acc[k];
    portEXIT_CRITICAL(&s_mux);
    add(o, ",\"%s\":{\"v\":%.3f,\"i\":%.1f,\"q\":%.2f,\"n\":%lu,\"a\":%u,\"err\":%lu%s}", k ? "bat2" : "bat", A.v, A.i,
        A.q, (unsigned long)A.n, s_ina[k].addr(), (unsigned long)s_ina[k].errors(), s_w1[k] ? ",\"w\":1" : "");
  }
}

}  // namespace bat
