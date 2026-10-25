#include "motor.h"

namespace motor {

static const uint8_t  PIN[2][2] = {{1, 2}, {3, 4}};   // XIAO ESP32-S3: D0=GPIO1, D1=GPIO2, D2=GPIO3, D3=GPIO4
static const uint32_t FREQ = 20000;                    // Гц
static const uint8_t  BITS = 10;
static const int      FULL = (1 << BITS) - 1;          // ledcWrite(FULL) — вход всё время 1
static const uint32_t REVERSE_PAUSE_MS = 1000;         // стоять перед сменой направления
static const uint32_t TICK_MS = 20;                    // 50 раз в секунду

enum : uint8_t { ST_STOP = 0, ST_UP = 1, ST_RUN = 2, ST_DOWN = 3, ST_PAUSE = 4 };

struct Mot {
  int      target = 0;        // ‰ со знаком — куда велено
  float    cur = 0;           // ‰ со знаком — что сейчас на мосте
  uint32_t hold_until = 0;    // пауза перед сменой направления, millis
  uint8_t  state = ST_STOP;
};

static Mot          s_m[2];
static int          s_ramp = 500;        // ‰ в секунду: 0 → 40 % за 0,8 с
static uint32_t     s_wd_ms = 5000;
static uint32_t     s_last_cmd = 0;
static uint32_t     s_cmds = 0;
static bool         s_wd_fired = false;
static bool         s_ok = false;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

// Мост k на ШИМ d (‰ со знаком): 0 — выбег (оба входа 0), иначе медленный спад.
static void drive(int k, float d) {
  const uint8_t a = PIN[k][0], b = PIN[k][1];
  const float m = fabsf(d) / 1000.0f;
  if (m < 0.002f) {
    ledcWrite(a, 0);
    ledcWrite(b, 0);
    return;
  }
  const int pwm = (int)lroundf((1.0f - (m > 1.0f ? 1.0f : m)) * FULL);   // доля «тормоза» в периоде
  if (d > 0) {
    ledcWrite(a, FULL);
    ledcWrite(b, pwm);
  } else {
    ledcWrite(a, pwm);
    ledcWrite(b, FULL);
  }
}

static void task(void*) {
  TickType_t last = xTaskGetTickCount();
  for (;;) {
    vTaskDelayUntil(&last, pdMS_TO_TICKS(TICK_MS));
    const uint32_t now = millis();
    portENTER_CRITICAL(&s_mux);
    if (s_wd_ms && now - s_last_cmd > s_wd_ms && (s_m[0].target || s_m[1].target)) {   // сервер пропал — встать
      s_m[0].target = s_m[1].target = 0;
      s_wd_fired = true;
    }
    const float step = s_ramp * (TICK_MS / 1000.0f);
    float out[2];
    for (int k = 0; k < 2; ++k) {
      Mot& m = s_m[k];
      if (m.hold_until && (int32_t)(now - m.hold_until) < 0) {
        m.state = ST_PAUSE;
        out[k] = m.cur = 0;
        continue;
      }
      m.hold_until = 0;
      const bool reverse = m.target && m.cur && ((m.target > 0) != (m.cur > 0));
      const float goal = reverse ? 0.0f : (float)m.target;
      const float was = m.cur;
      if (fabsf(goal - m.cur) <= step) m.cur = goal;
      else m.cur += goal > m.cur ? step : -step;
      if (reverse && m.cur == 0 && was != 0) m.hold_until = now + REVERSE_PAUSE_MS;   // встали — пауза, потом в другую сторону
      if (m.cur == 0 && m.target == 0) m.state = ST_STOP;
      else if (m.cur == goal && !reverse) m.state = ST_RUN;
      else m.state = fabsf(m.cur) < fabsf(goal) && !reverse ? ST_UP : ST_DOWN;
      out[k] = m.cur;
    }
    portEXIT_CRITICAL(&s_mux);
    for (int k = 0; k < 2; ++k) drive(k, out[k]);
  }
}

void begin() {
  bool ok = true;
  for (auto& p : PIN)
    for (uint8_t pin : p) {
      ok &= ledcAttach(pin, FREQ, BITS);
      ledcWrite(pin, 0);                    // выбег, пока не велено
    }
  s_ok = ok;
  s_last_cmd = millis();
  Serial.printf("MOTOR bridges D0/D1, D2/D3: PWM %lu Hz, %u bit %s\n", (unsigned long)FREQ, BITS, ok ? "ok" : "FAIL");
  xTaskCreatePinnedToCore(task, "motor", 3072, nullptr, 3, nullptr, 0);
}

static bool arg(const char* s, const char* key, long& v) {
  const char* p = strstr(s, key);
  if (!p) return false;
  v = strtol(p + strlen(key), nullptr, 10);
  return true;
}

void command(const char* args) {
  long a = 0, b = 0, r = 0, w = 0;
  const bool ha = arg(args, "a=", a), hb = arg(args, "b=", b), hr = arg(args, "r=", r), hw = arg(args, "w=", w);
  portENTER_CRITICAL(&s_mux);
  if (strstr(args, "stop")) s_m[0].target = s_m[1].target = 0;
  if (ha) s_m[0].target = (int)constrain(a, -1000, 1000);
  if (hb) s_m[1].target = (int)constrain(b, -1000, 1000);
  if (hr) s_ramp = (int)constrain(r, 50, 5000);
  if (hw) s_wd_ms = (uint32_t)constrain(w, 0, 60) * 1000;
  s_last_cmd = millis();
  s_wd_fired = false;
  ++s_cmds;
  portEXIT_CRITICAL(&s_mux);
}

void stop() { command("stop"); }

void add_tel(Out& o) {
  Mot m[2];
  int ramp;
  uint32_t wd, age, cmds;
  bool fired;
  portENTER_CRITICAL(&s_mux);
  m[0] = s_m[0];
  m[1] = s_m[1];
  ramp = s_ramp;
  wd = s_wd_ms;
  age = millis() - s_last_cmd;
  cmds = s_cmds;
  fired = s_wd_fired;
  portEXIT_CRITICAL(&s_mux);
  // a, b: [цель ‰, ШИМ сейчас ‰, состояние 0 стоит / 1 разгон / 2 едет / 3 торможение / 4 пауза перед реверсом]
  add(o, ",\"mot\":{\"ok\":%d,\"a\":[%d,%d,%u],\"b\":[%d,%d,%u],\"r\":%d,\"wd\":%lu,\"age\":%.1f,\"wdf\":%d,\"n\":%lu,\"hz\":%lu}",
      s_ok ? 1 : 0, m[0].target, (int)lroundf(m[0].cur), m[0].state, m[1].target, (int)lroundf(m[1].cur), m[1].state, ramp,
      (unsigned long)(wd / 1000), age / 1000.0f, fired ? 1 : 0, (unsigned long)cmds, (unsigned long)FREQ);
}

}  // namespace motor
