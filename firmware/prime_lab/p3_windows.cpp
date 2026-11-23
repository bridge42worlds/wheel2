#include "p3_windows.h"
#include <Wire.h>
#include <icm20948.h>
#include "esp_timer.h"
#include "psram.h"

namespace p3 {

static const int SDA1_PIN = 5, SCL1_PIN = 6;    // XIAO ESP32-S3: D4 — SDA, D5 — SCL
static const int SDA2_PIN = 7, SCL2_PIN = 8;    //                D8 — SDA, D9 — SCL
static const float ODR_HZ = 1125.0f;            // записей ускорения и вращения в секунду (делитель 0)
static const float W_MIN = 3.0f;                // рад/с: медленнее 29 об/мин окон не ищем
static const int   NPOOL = 4;                   // окон в работе сразу: два датчика снимают, два ждут счёта
static const int64_t SLOW_US = 5000;            // опрос вдали от окна: 200 раз в секунду
static const int64_t FULL_US = 20000;           // полная запись (со скоростью вращения): 50 раз в секунду
static const int64_t MAG_GAP_US = 11000;        // магнитометр: 100 замеров в секунду — следующий через 10 мс

enum St : uint8_t { ST_WAIT = 0, ST_IN_FIFO = 1, ST_IN_DIRECT = 2, ST_MAG = 3 };

struct Sens {
  Icm20948 dev;
  bool     ok = false;
  uint8_t  bus = 0;
  uint8_t  cfg[6] = {};
  int      fifo_bytes = 0;            // измеренный размер буфера датчика, байт
  // --- задача датчиков ---
  St       st = ST_WAIT;
  int64_t  next_poll = 0, next_full = 0, t_end = 0, t_k = 0, t_mag = 0;
  float    w = 0;                     // скорость вращения, рад/с (сглажена)
  bool     w_ok = false;
  float    amax = 0;                  // наибольшее ускорение с прошлого окна, g
  float    k = 0;                     // ω²r/g этого датчика: наибольшее ускорение за оборот минус 1
  bool     k_ok = false;
  bool     armed = false;             // ускорение побывало заметно выше порога — следующий спуск ниже порога и есть вход
  uint8_t  below = 0;
  float    a_now = 0;
  Window*  win = nullptr;
  // --- счётчики (под s_mux) ---
  uint32_t wins = 0, drop = 0, trunc = 0, recs = 0, cands = 0, primes = 0;
  uint64_t us = 0;
  int32_t  last[8] = {};              // последнее окно: записей, мкс, вход мg, минимум мg, поле x y z, как читали
};

static Sens          s_s[NSENS];
static uint8_t       s_present = 0;
static portMUX_TYPE  s_mux = portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t s_free = nullptr, s_ready = nullptr;
static volatile bool    s_on = false;
static volatile uint8_t s_win = 30, s_fm = 1;
static volatile int8_t  s_wo = 0;
static Find          s_find[NFIND];
static uint32_t      s_nfind = 0;     // всего находок; в таблице — последние NFIND

static inline float rad(float deg) { return deg * (float)(PI / 180.0); }

static void give_back(Sens& x) {
  if (x.win) xQueueSend(s_free, &x.win, 0);
  x.win = nullptr;
  x.st = ST_WAIT;
}

static void push(Sens& x, int64_t now) {
  Window* w = x.win;
  x.win = nullptr;
  x.st = ST_WAIT;
  x.next_poll = now;
  float am = 1e9f;
  for (int i = 0; i < w->n; ++i) {
    const float ax = w->rec[i][0], ay = w->rec[i][1], az = w->rec[i][2];
    const float a2 = ax * ax + ay * ay + az * az;
    if (a2 < am) am = a2;
  }
  w->a_min = w->n ? sqrtf(am) / x.dev.accel_lsb() : 0;
  portENTER_CRITICAL(&s_mux);
  x.recs += w->n;
  x.last[0] = w->n;
  x.last[1] = (int32_t)w->dur_us;
  x.last[2] = (int32_t)lroundf(w->a_enter * 1000.0f);
  x.last[3] = (int32_t)lroundf(w->a_min * 1000.0f);
  x.last[4] = w->mag[0];
  x.last[5] = w->mag[1];
  x.last[6] = w->mag[2];
  x.last[7] = w->fm;
  portEXIT_CRITICAL(&s_mux);
  if (xQueueSend(s_ready, &w, 0) != pdTRUE) {   // счёт не успевает: окно пропало
    xQueueSend(s_free, &w, 0);
    portENTER_CRITICAL(&s_mux);
    ++x.drop;
    portEXIT_CRITICAL(&s_mux);
  }
}

static void enter(Sens& x, int i, int64_t now, float am) {
  x.armed = false;
  x.below = 0;
  x.k = x.amax - 1.0f;                // оборот кончился: наибольшее ускорение за него — внизу, 1 + ω²r/g
  x.amax = 0;
  x.t_k = now;
  Window* w = nullptr;
  if (xQueueReceive(s_free, &w, 0) != pdTRUE) {
    portENTER_CRITICAL(&s_mux);
    ++x.drop;
    portEXIT_CRITICAL(&s_mux);
    return;
  }
  float dur = rad((float)s_win) / x.w * 1e6f;
  if (dur < 5000.0f) dur = 5000.0f;
  if (dur > 400000.0f) dur = 400000.0f;
  w->sensor = (uint8_t)i;
  w->n = 0;
  w->t_enter_us = now;
  w->dur_us = (uint32_t)dur;
  w->w = x.w;
  w->a_enter = am;
  w->a_min = 0;
  w->has_mag = false;
  w->mag[0] = w->mag[1] = w->mag[2] = 0;
  portENTER_CRITICAL(&s_mux);
  w->seq = ++x.wins;
  portEXIT_CRITICAL(&s_mux);
  x.win = w;
  x.t_end = now + (int64_t)dur;
  if (s_fm && x.fifo_bytes >= Icm20948::REC) {
    w->fm = 1;
    if (!x.dev.fifo_reset()) {        // шина не ответила — окно не снимаем
      give_back(x);
      return;
    }
    x.st = ST_IN_FIFO;
  } else {
    w->fm = 0;
    x.st = ST_IN_DIRECT;
  }
}

static void wait_step(Sens& x, int i, int64_t now) {
  if (now < x.next_poll) return;
  float a2;
  if (now >= x.next_full) {           // полная запись: заодно скорость вращения
    Icm20948::Sample smp;
    if (!x.dev.read(smp)) {
      x.next_poll = now + SLOW_US;
      return;
    }
    const float wm = sqrtf(smp.w[0] * smp.w[0] + smp.w[1] * smp.w[1] + smp.w[2] * smp.w[2]);
    x.w = x.w_ok ? 0.8f * x.w + 0.2f * wm : wm;
    x.w_ok = true;
    a2 = smp.a[0] * smp.a[0] + smp.a[1] * smp.a[1] + smp.a[2] * smp.a[2];
    x.next_full = now + FULL_US;
  } else {
    int16_t a[3];
    if (!x.dev.read_accel(a)) {
      x.next_poll = now + SLOW_US;
      return;
    }
    const float l = x.dev.accel_lsb();
    const float ax = a[0] / l, ay = a[1] / l, az = a[2] / l;
    a2 = ax * ax + ay * ay + az * az;
  }
  const float am = sqrtf(a2);
  x.a_now = am;
  if (am > x.amax) x.amax = am;
  const bool turning = x.w_ok && x.w >= W_MIN;
  // Входа не было полтора оборота (первый оборот, сменилась скорость, окно выключено): k — заново
  if (turning && now - x.t_k > (int64_t)(1.5f * 2.0f * (float)PI / x.w * 1e6f)) {
    x.k = x.amax - 1.0f;
    x.k_ok = true;
    x.amax = am;
    x.t_k = now;
  }
  bool fast = false;
  if (s_on && turning && x.k_ok && x.k >= 0.3f && x.k <= 4.0f) {
    // Ускорение датчика на угле θ от верха: |f|² = k² + 1 − 2k·cos θ (в g²). Окно начинается за h до верха.
    float h = (float)s_win * 0.5f - (float)s_wo;
    if (h < 2.0f) h = 2.0f;
    if (h > 170.0f) h = 170.0f;
    const float thr2 = x.k * x.k + 1.0f - 2.0f * x.k * cosf(rad(h));
    const float thr = sqrtf(thr2 > 0 ? thr2 : 0);
    float gap = (x.k + 1.0f - thr) * 0.5f;      // до наибольшего ускорения (внизу) — половина пути, но не больше 0,35 g
    if (gap > 0.35f) gap = 0.35f;
    const float near = thr + gap;
    if (am > near) x.armed = true;
    if (x.armed && am < thr) {
      if (++x.below >= 2) {           // два опроса подряд ниже порога
        enter(x, i, now, am);
        if (x.st != ST_WAIT) return;
      }
    } else {
      x.below = 0;
    }
    fast = x.armed && am < near;
  }
  x.next_poll = now + (fast ? 0 : SLOW_US);     // у порога — каждый такт задачи (1 мс)
}

static void step(Sens& x, int i, int64_t now) {
  switch (x.st) {
    case ST_WAIT:
      wait_step(x, i, now);
      break;
    case ST_IN_FIFO: {
      if (now < x.t_end) break;       // всё окно к датчику не обращаемся
      Window* w = x.win;
      int n = 0;
      if (!x.dev.fifo_read(w->rec, MAXREC, n)) {
        give_back(x);
        break;
      }
      w->n = (uint16_t)n;
      w->dur_us = (uint32_t)(now - w->t_enter_us);
      if ((n + 1) * Icm20948::REC > x.fifo_bytes) {   // буфер заполнился раньше конца окна
        portENTER_CRITICAL(&s_mux);
        ++x.trunc;
        portEXIT_CRITICAL(&s_mux);
      }
      Icm20948::Mag m;
      x.dev.mag_read(m);              // замер, пролежавший окно, — выбросить; свежий — через 10 мс
      x.t_mag = now + MAG_GAP_US;
      x.st = ST_MAG;
      break;
    }
    case ST_IN_DIRECT: {
      Window* w = x.win;
      Icm20948::Sample smp;
      if (w->n < MAXREC && x.dev.read(smp)) memcpy(w->rec[w->n++], smp.raw, sizeof smp.raw);
      if (now < x.t_end && w->n < MAXREC) break;
      w->dur_us = (uint32_t)(now - w->t_enter_us);
      Icm20948::Mag m;
      x.dev.mag_read(m);
      x.t_mag = now + MAG_GAP_US;
      x.st = ST_MAG;
      break;
    }
    case ST_MAG: {
      if (now < x.t_mag) break;
      Window* w = x.win;
      Icm20948::Mag m;
      if (x.dev.mag_read(m)) {
        memcpy(w->mag, m.raw, sizeof w->mag);
        w->has_mag = true;
      }
      push(x, now);
      break;
    }
  }
}

static void task(void*) {
  for (;;) {
    vTaskDelay(1);
    const int64_t now = esp_timer_get_time();
    for (int i = 0; i < NSENS; ++i)
      if (s_s[i].ok) step(s_s[i], i, now);
  }
}

static bool find_on(Sens& x, TwoWire& bus, uint8_t nbus) {
  for (uint8_t a : {(uint8_t)0x69, (uint8_t)0x68}) {
    if (!x.dev.begin(bus, a)) continue;
    x.bus = nbus;
    // ±4 g (внизу 2 g и вибрация), ±1000 °/с (74 об/мин — 445 °/с), 1125 записей в секунду, самая широкая полоса
    if (!x.dev.configure(Icm20948::A4G, Icm20948::G1000, 0, 7)) return false;
    if (x.dev.fifo_begin(false)) x.fifo_bytes = x.dev.fifo_measure(1500);
    const bool mag = x.dev.mag_begin();
    x.dev.readback(x.cfg);
    Serial.printf("P3 ICM-20948 bus %u at 0x%02x fifo=%d mag=%d cfg=%02x %02x %02x %02x %02x %02x\n", nbus, a, x.fifo_bytes,
                  mag ? 1 : 0, x.cfg[0], x.cfg[1], x.cfg[2], x.cfg[3], x.cfg[4], x.cfg[5]);
    return true;
  }
  return false;
}

void begin(bool bus1_taken) {
  if (!bus1_taken) {                  // на D4/D5 уже работает датчик обода прежнего типа — шину не трогаем
    Wire.begin(SDA1_PIN, SCL1_PIN, 400000);
    s_s[0].ok = find_on(s_s[0], Wire, 0);
  }
  Wire1.begin(SDA2_PIN, SCL2_PIN, 400000);
  s_s[1].ok = find_on(s_s[1], Wire1, 1);
  s_present = (s_s[0].ok ? 1 : 0) | (s_s[1].ok ? 2 : 0);
  if (!s_present) {
    Serial.println("P3 no ICM-20948");
    return;
  }
  s_free = xQueueCreate(NPOOL, sizeof(Window*));
  s_ready = xQueueCreate(NPOOL, sizeof(Window*));
  for (int i = 0; i < NPOOL; ++i) {
    Window* w = (Window*)big_alloc(sizeof(Window));
    xQueueSend(s_free, &w, 0);
  }
  const int64_t now = esp_timer_get_time();
  for (Sens& x : s_s) x.t_k = now;
  xTaskCreatePinnedToCore(task, "p3", 4096, nullptr, 3, nullptr, 1);   // ядро счёта, выше счёта
}

uint8_t present() { return s_present; }

void configure(const GenConfig& c) {
  s_win = c.win;
  s_wo = c.wo;
  s_fm = c.fm;
  s_on = c.p3 && c.run;
}

bool take(Window*& w, TickType_t wait) {
  if (!s_ready) {
    vTaskDelay(wait);
    return false;
  }
  return xQueueReceive(s_ready, &w, wait) == pdTRUE;
}

void release(Window* w) {
  if (w) xQueueSend(s_free, &w, 0);
}

void record(const Window& w, int i, uint8_t out[RECLEN]) {
  memcpy(out, w.rec[i], 12);
  memcpy(out + 12, w.mag, 6);
}

void found(const Window& w, int i, const uint8_t* num) {
  Find f;
  memcpy(f.num, num, sizeof f.num);
  f.sensor = w.sensor;
  f.idx = (uint16_t)i;
  f.seq = w.seq;
  f.t_us = w.t_enter_us + (w.n ? (int64_t)w.dur_us * i / w.n : 0);
  portENTER_CRITICAL(&s_mux);
  s_find[s_nfind % NFIND] = f;
  ++s_nfind;
  portEXIT_CRITICAL(&s_mux);
}

void done(const Window& w, uint32_t cands, uint32_t primes, uint32_t us) {
  Sens& x = s_s[w.sensor < NSENS ? w.sensor : 0];
  portENTER_CRITICAL(&s_mux);
  x.cands += cands;
  x.primes += primes;
  x.us += us;
  portEXIT_CRITICAL(&s_mux);
}

int finds(Find* out, int max) {
  portENTER_CRITICAL(&s_mux);
  const uint32_t total = s_nfind;
  int n = total < (uint32_t)NFIND ? (int)total : NFIND;
  if (n > max) n = max;
  for (int i = 0; i < n; ++i) out[i] = s_find[(total - n + i) % NFIND];
  portEXIT_CRITICAL(&s_mux);
  return n;
}

void add_tel(Out& o) {
  add(o, ",\"on\":%d,\"win\":%u,\"wo\":%d,\"fm\":%u,\"free\":%u,\"finds\":%lu,\"s\":[", s_on ? 1 : 0, (unsigned)s_win,
      (int)s_wo, (unsigned)s_fm, s_free ? (unsigned)uxQueueMessagesWaiting(s_free) : 0, (unsigned long)s_nfind);
  bool first = true;
  for (int i = 0; i < NSENS; ++i) {
    Sens& x = s_s[i];
    if (!x.ok) continue;
    uint32_t wins, drop, trunc, recs, cands, primes;
    uint64_t us;
    int32_t last[8];
    portENTER_CRITICAL(&s_mux);
    wins = x.wins; drop = x.drop; trunc = x.trunc; recs = x.recs; cands = x.cands; primes = x.primes; us = x.us;
    memcpy(last, x.last, sizeof last);
    portEXIT_CRITICAL(&s_mux);
    add(o, "%s{\"i\":%d,\"bus\":%u,\"addr\":%u,\"who\":%u,\"fifo\":%d,\"mag\":%d,\"err\":%lu,\"cfg\":[%u,%u,%u,%u,%u,%u]",
        first ? "" : ",", i, x.bus, x.dev.addr(), x.dev.who(), x.fifo_bytes, x.dev.has_mag() ? 1 : 0,
        (unsigned long)x.dev.errors(), x.cfg[0], x.cfg[1], x.cfg[2], x.cfg[3], x.cfg[4], x.cfg[5]);
    add(o, ",\"st\":%u,\"w\":%.3f,\"k\":%.3f,\"a\":%.3f,\"wins\":%lu,\"drop\":%lu,\"trunc\":%lu,\"recs\":%lu,\"cands\":%lu,"
           "\"primes\":%lu,\"us\":%llu,\"last\":[%ld,%ld,%ld,%ld,%ld,%ld,%ld,%ld]}",
        (unsigned)x.st, x.w, x.k_ok ? x.k : 0.0f, x.a_now, (unsigned long)wins, (unsigned long)drop, (unsigned long)trunc,
        (unsigned long)recs, (unsigned long)cands, (unsigned long)primes, (unsigned long long)us, (long)last[0],
        (long)last[1], (long)last[2], (long)last[3], (long)last[4], (long)last[5], (long)last[6], (long)last[7]);
    first = false;
  }
  add(o, "]");
}

}  // namespace p3
