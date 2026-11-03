#include "motor.h"

namespace motor {

static const uint8_t  PIN[2][2] = {{1, 2}, {3, 4}};   // XIAO ESP32-S3: D0=GPIO1, D1=GPIO2, D2=GPIO3, D3=GPIO4
static const uint32_t FREQ = 20000;                    // Гц
static const uint8_t  BITS = 10;
static const int      FULL = (1 << BITS) - 1;          // ledcWrite(FULL) — вход всё время 1
static const uint32_t REVERSE_PAUSE_MS = 1000;         // стоять перед сменой направления
static const uint32_t TICK_MS = 20;                    // 50 раз в секунду
static const uint32_t TICK_AC_MS = 1;                  // переменное: 1000 раз в секунду — 20 точек на период при 50 Гц
static const uint32_t F_MAX_MHZ = 50000;               // переменное — до 50 Гц
static const uint32_t SYNC_MAX_MHZ = 5000;             // фазу по серверу подстраивать до 5 Гц (выше задержка сети — заметная доля периода)

enum : uint8_t { ST_STOP = 0, ST_UP = 1, ST_RUN = 2, ST_DOWN = 3, ST_PAUSE = 4 };

// Переменное (катушки): мост ведёт не уровень, а огибающую — амплитуду со знаком (знак — фаза 0 или 180°);
// на выходе огибающая × форма: синус или мягкий меандр (полуволны синуса на фронтах длиной edge). Смена рода
// (постоянное ↔ переменное, синус ↔ меандр) — только через огибающую 0: сначала плавно спасть, потом сменить.
struct Mot {
  int      target = 0;        // ‰ со знаком — куда велено (переменное: амплитуда со знаком фазы)
  float    cur = 0;           // ‰ со знаком — что сейчас (переменное: огибающая)
  uint32_t hold_until = 0;    // пауза перед сменой направления, millis
  uint8_t  state = ST_STOP;
  uint32_t f = 0, want_f = 0;         // частота, мГц: действующая и заданная (0 — постоянное)
  uint8_t  k = 0, want_k = 0;         // форма: 0 — синус, 1 — мягкий меандр
  uint16_t edge = 100;                // меандр: фронт, мс
  double   ph = 0;                    // фаза, доли периода [0, 1)
  float    slew = 0;                  // подстройка фазы по серверу: долей периода в секунду
  float    q = -1;                    // фаза сервера из последней команды (доли периода), −1 — нет
  uint32_t q_ms = 0;                  //   когда пришла
  float    out = 0;                   // ‰ на мосте сейчас (огибающая × форма)
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

static double wrap1(double x) { return x - floor(x); }
static float  wrap_half(float x) { return x - floorf(x + 0.5f); }   // в [−0,5, 0,5)

// Форма в фазе u ∈ [0, 1): синус или мягкий меандр — +1 на первой половине, −1 на второй, фронты — полуволны
// синуса шириной w долей периода (производная на краях фронта — ноль: ток меняется без рывка).
static float wave(const Mot& m, double u) {
  if (m.k == 0) return sinf(2.0f * (float)M_PI * (float)u);
  float w = m.edge * (m.f / 1e6f);                     // фронт в долях периода
  if (w > 0.25f) w = 0.25f;
  if (w < 0.002f) w = 0.002f;
  const float x = (float)u;
  if (x < w / 2) return sinf((float)M_PI * x / w);
  if (x > 1 - w / 2) return sinf((float)M_PI * (x - 1) / w);
  if (fabsf(x - 0.5f) < w / 2) return -sinf((float)M_PI * (x - 0.5f) / w);
  return x < 0.5f ? 1.0f : -1.0f;
}

static bool any_ac() { return s_m[0].f || s_m[0].want_f || s_m[1].f || s_m[1].want_f; }

static void task(void*) {
  TickType_t last = xTaskGetTickCount();
  int64_t prev = esp_timer_get_time();
  for (;;) {
    vTaskDelayUntil(&last, pdMS_TO_TICKS(any_ac() ? TICK_AC_MS : TICK_MS));
    const int64_t t_us = esp_timer_get_time();
    const float dt = (float)(t_us - prev) / 1e6f;
    prev = t_us;
    const uint32_t now = millis();
    portENTER_CRITICAL(&s_mux);
    if (s_wd_ms && now - s_last_cmd > s_wd_ms && (s_m[0].target || s_m[1].target)) {   // сервер пропал — встать
      s_m[0].target = s_m[1].target = 0;
      s_wd_fired = true;
    }
    const float step = s_ramp * (dt > 0.1f ? 0.1f : dt);
    float out[2];
    for (int k = 0; k < 2; ++k) {
      Mot& m = s_m[k];
      if (m.hold_until && (int32_t)(now - m.hold_until) < 0) {
        m.state = ST_PAUSE;
        out[k] = m.out = m.cur = 0;
        continue;
      }
      m.hold_until = 0;
      // смена рода — через 0: постоянное ↔ переменное или другая форма; частота внутри переменного — на ходу
      const bool kind = (m.want_f == 0) != (m.f == 0) || (m.f && m.want_f && m.want_k != m.k);
      if (kind && m.cur == 0) {
        m.f = m.want_f;
        m.k = m.want_k;
        if (m.f && m.q >= 0) m.ph = wrap1(m.q + m.f / 1e3 * ((now - m.q_ms) / 1e3));   // начать в фазе сервера
        else if (m.f) m.ph = 0;
        m.slew = 0;
      } else if (!kind) {
        m.f = m.want_f;                                // частота внутри переменного — сразу, фаза непрерывна
        m.k = m.want_k;
      }
      const bool reverse = m.target && m.cur && ((m.target > 0) != (m.cur > 0));
      const float goal = reverse || kind ? 0.0f : (float)m.target;
      const float was = m.cur;
      if (fabsf(goal - m.cur) <= step) m.cur = goal;
      else m.cur += goal > m.cur ? step : -step;
      if (reverse && m.cur == 0 && was != 0) m.hold_until = now + REVERSE_PAUSE_MS;   // встали — пауза, потом в другую сторону
      if (m.cur == 0 && m.target == 0) m.state = ST_STOP;
      else if (m.cur == goal && !reverse && !kind) m.state = ST_RUN;
      else m.state = fabsf(m.cur) < fabsf(goal) && !reverse && !kind ? ST_UP : ST_DOWN;
      if (m.f) {
        m.ph = wrap1(m.ph + (m.f / 1e3) * dt + m.slew * dt);
        m.out = m.cur * wave(m, m.ph);
      } else {
        m.out = m.cur;
      }
      out[k] = m.out;
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

// Ключ — только целым словом («a=» не находит «fa=»).
static bool arg(const char* s, const char* key, long& v) {
  for (const char* p = strstr(s, key); p; p = strstr(p + 1, key))
    if (p == s || p[-1] == ' ') {
      v = strtol(p + strlen(key), nullptr, 10);
      return true;
    }
  return false;
}

// Переменное моста: частота f мГц, форма k, фронт e мс, фаза сервера q (тысячные периода, −1 — нет).
static void set_ac(Mot& m, bool hf, long f, bool hk, long k, bool he, long e, bool hq, long q, uint32_t now) {
  if (hf) m.want_f = (uint32_t)constrain(f, 0L, (long)F_MAX_MHZ);
  if (hk) m.want_k = k ? 1 : 0;
  if (he) m.edge = (uint16_t)constrain(e, 10L, 2000L);
  if (!hq || q < 0) return;
  m.q = constrain(q, 0L, 999L) / 1000.0f;
  m.q_ms = now;
  // подстройка: ошибку фазы убрать примерно за 2 с ровным ходом, без скачка формы. Частота могла смениться в этой
  // же команде (поле с вращением — каждую секунду): внутри переменного она меняется на ходу, фаза непрерывна
  if (m.f && m.want_f && m.want_k == m.k && m.want_f <= SYNC_MAX_MHZ) m.slew = 0.5f * wrap_half((float)(m.q - m.ph));
  else m.slew = 0;
}

void command(const char* args) {
  long a = 0, b = 0, r = 0, w = 0, fa = 0, fb = 0, ka = 0, kb = 0, ea = 0, eb = 0, qa = -1, qb = -1;
  const bool ha = arg(args, "a=", a), hb = arg(args, "b=", b), hr = arg(args, "r=", r), hw = arg(args, "w=", w);
  const bool hfa = arg(args, "fa=", fa), hfb = arg(args, "fb=", fb), hka = arg(args, "ka=", ka), hkb = arg(args, "kb=", kb);
  const bool hea = arg(args, "ea=", ea), heb = arg(args, "eb=", eb), hqa = arg(args, "qa=", qa), hqb = arg(args, "qb=", qb);
  const uint32_t now = millis();
  portENTER_CRITICAL(&s_mux);
  if (strstr(args, "stop")) s_m[0].target = s_m[1].target = 0;
  if (ha) s_m[0].target = (int)constrain(a, -1000, 1000);
  if (hb) s_m[1].target = (int)constrain(b, -1000, 1000);
  if (hr) s_ramp = (int)constrain(r, 50, 5000);
  if (hw) s_wd_ms = (uint32_t)constrain(w, 0, 60) * 1000;
  set_ac(s_m[0], hfa, fa, hka, ka, hea, ea, hqa, qa, now);
  set_ac(s_m[1], hfb, fb, hkb, kb, heb, eb, hqb, qb, now);
  s_last_cmd = now;
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
  // a, b: [цель ‰, ШИМ сейчас ‰ (переменное — огибающая), состояние 0 стоит / 1 разгон / 2 едет / 3 торможение / 4 пауза]
  add(o, ",\"mot\":{\"ok\":%d,\"a\":[%d,%d,%u],\"b\":[%d,%d,%u],\"r\":%d,\"wd\":%lu,\"age\":%.1f,\"wdf\":%d,\"n\":%lu,\"hz\":%lu",
      s_ok ? 1 : 0, m[0].target, (int)lroundf(m[0].cur), m[0].state, m[1].target, (int)lroundf(m[1].cur), m[1].state, ramp,
      (unsigned long)(wd / 1000), age / 1000.0f, fired ? 1 : 0, (unsigned long)cmds, (unsigned long)FREQ);
  // переменное: [частота мГц, форма 0 синус / 1 меандр, фронт мс, фаза в тысячных, ошибка фазы против сервера в тысячных]
  for (int k = 0; k < 2; ++k) {
    const float err = m[k].f && m[k].q >= 0 ? wrap_half((float)(m[k].q + m[k].f / 1e3 * ((millis() - m[k].q_ms) / 1e3) - m[k].ph)) : 0;
    add(o, ",\"%s\":[%lu,%u,%u,%d,%d]", k ? "acb" : "aca", (unsigned long)m[k].f, m[k].k, m[k].edge,
        (int)lround(m[k].ph * 1000), (int)lroundf(err * 1000));
  }
  add(o, "}");
}

}  // namespace motor
