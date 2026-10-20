#include "thermal.h"
#include <esp_wifi.h>

namespace thermal {

static volatile float s_tmax = 70.0f;
static volatile float s_work = 1.0f;
static volatile int   s_state = NORMAL;
static volatile bool  s_radio_sleep = false;
static int            s_hot_s = 0;       // секунд подряд выше предела при минимальной работе
// Сколько ядру ещё положено поспать — у каждого ядра свой долг. Замер 04.10.2026: при
// двух задачах счёта (два ядра) общий долг правили наперегонки с двух ядер, он уезжал в
// минус на десятки секунд, и торможение пропадало («работа 5 %», а темп полный).
static int64_t        s_debt_us[2] = {0, 0};

void  set_limit(float t) { s_tmax = t; }
float limit() { return s_tmax; }
float work() { return s_work; }
int   state() { return s_radio_sleep && s_state != PAUSED ? RADIO_SLEEP : s_state; }

static void radio_sleep(bool on) {
  if (on == s_radio_sleep) return;
  esp_wifi_set_ps(on ? WIFI_PS_MIN_MODEM : WIFI_PS_NONE);
  s_radio_sleep = on;
}

// Ступени:
//  1. Интегральный регулятор доли работы ядра счёта: каждую секунду доля меняется
//     на 4 % на градус отклонения от цели (предел − 1 °C), от 5 до 100 %.
//     Замер 03.10.2026: счёт греет кристалл на 4–8 °C сверх простоя; на простое
//     датчик в комнате показывает ~61 °C, так что предел ниже ~62 недостижим.
//  2. Доля уже минимальная, а кристалл 20 с подряд на 3 °C и больше выше предела —
//     радио засыпает между маяками роутера (шум приёмника в генератор и связь идут
//     с перерывами). Просыпается, когда кристалл на 3 °C ниже предела. Замер
//     03.10.2026: на простое датчик и так показывает ~61 °C, сон радио за 2 мин
//     не дал заметной разницы (шаг датчика 1 °C) — поэтому только как авария.
//  3. Выше предела на 8 °C три секунды подряд — авария: счёт стоит, радио спит, пока
//     кристалл не остынет на 3 °C ниже предела. Три секунды — потому что сразу после
//     перезапуска датчик показывает на 5–9 °C больше (05.10.2026, №6 после перезапуска
//     посреди каскада: 78,7 → 74,7 → 70,7 за 12 с при 69,7 до него) и одно такое
//     показание стоило 40 с аварийной паузы.
static int            s_crit_s = 0;      // секунд подряд на 8 °C выше предела
void update(float t) {
  if (isnan(t)) return;
  if (millis() < 12000) return;            // первые секунды после перезапуска датчик врёт вверх — не верить
  const float tmax = s_tmax;
  if (s_state == PAUSED) {
    if (t > tmax - 3.0f) return;           // ещё горячо — пауза продолжается
    s_work = 0.25f;                        // остыл: осторожно с четверти
    s_state = LIMITING;
    radio_sleep(false);
    s_hot_s = 0;
    s_crit_s = 0;
    s_debt_us[0] = s_debt_us[1] = 0;       // долг сна за время паузы не копить
    return;
  }
  s_crit_s = t >= tmax + 8.0f ? s_crit_s + 1 : 0;
  if (s_crit_s >= 3) {
    s_work = 0.0f;
    s_state = PAUSED;
    radio_sleep(true);
    return;
  }
  float w = s_work + 0.04f * ((tmax - 1.0f) - t);
  if (w > 1.0f) w = 1.0f;
  if (w < 0.05f) w = 0.05f;
  s_work = w;
  s_state = w < 0.999f ? LIMITING : NORMAL;
  s_hot_s = (w <= 0.051f && t >= tmax + 3.0f) ? s_hot_s + 1 : 0;
  if (s_hot_s >= 20) radio_sleep(true);
  else if (s_radio_sleep && t <= tmax - 3.0f) radio_sleep(false);
}

void pace(uint32_t busy_us, volatile bool* abort) {
  while (s_state == PAUSED && !(abort && *abort)) vTaskDelay(pdMS_TO_TICKS(50));
  const float w = s_work;
  int64_t& debt = s_debt_us[xPortGetCoreID() & 1];
  if (w >= 0.999f || w <= 0.0f) {
    debt = 0;
    return;
  }
  debt += (int64_t)(busy_us * (1.0f - w) / w);
  if (debt >= 2000) {                      // спим кусками от 2 мс — тик планировщика 1 мс
    uint32_t ms = debt / 1000;
    if (ms > 500) ms = 500;
    vTaskDelay(pdMS_TO_TICKS(ms));
    debt -= (int64_t)ms * 1000;
  }
}

}  // namespace thermal
