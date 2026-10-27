// prime_lab — генератор простых чисел с управлением с сервера.
//
// Два режима (настройка mode):
//   каждый сам за себя — ядро 1 само берёт шум, собирает кандидата, отсеивает
//     делителями и проверяет Миллером — Рабином по первым nb базам;
//   сообща — агенты группы делают одно число на всех (group.h): кусочки шума
//     от каждого, мастер складывает и отсеивает, каждый проверяет своими базами,
//     нашедший свидетеля становится мастером, простое — эстафета по порядку.
//     Протокол — задача на ядре 0, ядро 1 считает по его заданиям.
// Ядро 0 (связь): Wi-Fi, поиск сервера по маяку "W2SRV <порт>" (UDP 5006),
// приём настроек строкой "CFG cid=.. src=.. bits=.. ..." на UDP 5007,
// подтверждение, телеметрия раз в секунду, пинг роутера для CSI, ESP-NOW группы,
// предел температуры кристалла (thermal.h).
//
// Роль S (статический акселерометр, static_role.h): та же прошивка, но агент
// только меряет и шлёт оси — счёта, группы, ESP-NOW и пинга нет. Роль S агент
// помнит в своей памяти (NVS) и стартует сразу с неё; смена S ↔ A/B — перезапуск.
//
// Счётчики в телеметрии накопительные с момента применения настроек (cid);
// темп сервер считает сам по разности пакетов — потеря пакета ничего не теряет.
// Пакет не длиннее 1460 байт (буфер NetworkUDP); статистика группы — отдельным
// пакетом "grp".

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <errno.h>
#include "esp_timer.h"
#include <agent_common.h>
#include "config.h"
#include "static_role.h"
#include "motor.h"
#include "entropy.h"
#include "primes.h"
#include "thermal.h"
#include "group.h"
#include "jsonout.h"
#include "rim_sensors.h"
#include "battery.h"
#include "prime_log.h"
#include "ota.h"
#if __has_include("version.h")
#include "version.h"
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "prime_lab"
#endif

const uint16_t BEACON_PORT = 5006;
const uint16_t CMD_PORT    = 5007;
const int      HIST_N      = 28;    // время поиска простого: корзины [2^k, 2^(k+1)) мкс

struct GenStats {
  GenConfig    cfg;
  PrimeStats   p;
  EntropyStats e;
  int64_t  t_apply_us = 0;          // когда применены настройки
  uint64_t tp_n = 0, tp_sum_us = 0; // найдено простых и суммарное время поиска
  uint32_t tp_min_us = 0, tp_max_us = 0;
  uint32_t tp_hist[HIST_N] = {};
  uint64_t stall = 0;               // ожиданий энтропии, прерванных новыми настройками
  uint64_t jobs = 0;                // заданий группы выполнено
  uint64_t cwait = 0, pwait = 0;    // два ядра: ядро счёта ждало кандидата / ядро связи ждало места в очереди
  char     last_hex[68] = "";       // последнее простое (для длины до 256 бит)
};

static GenStats          s_st;       // пишет только ядро 1
static GenStats          g_pub;      // снимок для ядра 0, под замком
static GenConfig         g_new;      // новые настройки от ядра 0, под замком
static volatile bool     g_pending = false;
static portMUX_TYPE      g_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool     g_sig_done = false;
static volatile int      g_selftest = -1;   // -1 не было, 0 ошибка, 1 норма
static volatile uint32_t g_udp_fail = 0;    // пакетов телеметрии, которые стек не отправил
static volatile int      g_udp_err = 0;     // errno последней неудачной отправки (12 ENOMEM — нет буферов передачи)
static volatile uint32_t g_udp_ok = 0;      // отправленных — сторожу Wi-Fi
static volatile uint32_t g_cmd_rx = 0, g_cmd_rej = 0;   // команд принято / отброшено (не с адреса сервера)
static volatile uint32_t g_ident_until = 0;             // «найти плату»: до этого момента (мс) светодиод мигает 5 раз в секунду
// Шаг на агенте (настройка dur): счёт с новой настройкой начался в s_step_t0; через dur секунд агент сам
// останавливает счёт (run 0 — счётчики замирают, как по команде), возвращает радио и телеметрию и сдаёт итоги:
// в tel "done" = номер законченного шага. Радио на шаге (ro 1): если эфир шагу не нужен — каждый сам, без сета B,
// шум не из эфира, — Wi-Fi выключается на всё время шага (ни кадра от платы; команды в это время не слышны,
// шаг кончится сам). ГСЧ чипа без радио слабее: в него не подмешивается шум приёмника.
static uint32_t          s_step_cid = 0;      // шаг с какой настройкой идёт (0 — никакого)
static uint32_t          s_step_t0 = 0;       // когда начался, мс
static uint32_t          s_done_cid = 0;      // последний шаг, законченный самим агентом
static volatile bool     s_radio_off = false; // Wi-Fi выключен на время шага
static uint32_t          s_radio_t0 = 0;      // когда выключен, мс
static uint32_t          s_radio_off_s = 0;   // сколько секунд радио было выключено на последнем шаге
static uint32_t          g_last_try = 0;      // когда последний раз пробовали переподключить Wi-Fi
// Запасная сеть (ssid2 в secrets/wifi.ini): основная не пускает 40 с — плата уходит на запасную и остаётся на
// связи (опечатка в пароле новой сети не отрезает плату — её не надо перешивать по USB). На запасной через 2 мин,
// потом раз в 10 мин, когда стоит, пробует основную. В телеметрии "net": 0 — основная, 1 — запасная.
static uint8_t           g_net = 0;
static uint32_t          g_net_down = 0;      // с какого момента нет связи (мс); 0 — связь есть
static uint32_t          g_net_try = 0;       // когда последний раз пробовали вернуться на основную
static uint32_t          g_bcn_n = 0, g_bcn_t = 0;      // маяков сервера принято, когда последний (мс)
static uint32_t          g_bcn_rst = 0;                 // переподключений из-за тишины маяка
static volatile uint32_t g_pending_t = 0;   // когда ядру 1 отданы новые настройки (мс): в телеметрии — сколько не принимает
static volatile uint32_t g_wifi_rst = 0;    // переподключений сторожем: связь «есть», а пакеты не уходят
static volatile uint32_t g_wifi_disc = 0;   // разрывов связи с роутером
static volatile int      g_wifi_reason = 0; // причина последнего разрыва (код IEEE 802.11)
// Перезапуск сторожем радио (radio_watchdog): причина переживает перезапуск в памяти RTC.
RTC_NOINIT_ATTR static uint32_t g_rbt_magic, g_rbt_why, g_rbt_n;
static const uint32_t RBT_MAGIC = 0x52425431;
enum { RBT_NONE = 0, RBT_RADIO = 1, RBT_WIFI = 2, RBT_ZOMBIE = 3, RBT_HEAP = 4 };   // радио не передаёт / Wi-Fi нет долго / «зомби» не лечится / мало памяти
// Самоочистка памяти (07.10.2026: у №5 и №3 свободная память за часы работы и «зомби» сползала с 61 до
// 54 КБ, в худший момент 2,5 КБ — прошивка по Wi-Fi не могла даже начаться). Перезапуск — только когда
// агент стоит (run 0, между шагами), память ниже порога и прошло больше минуты с запуска.
static const uint32_t HEAP_RESTART_BELOW = 30000;
static uint32_t          g_boot_why = RBT_NONE;      // почему перезапустились в этот раз (сторож)
static int               g_boot_rst = 0;             // причина сброса чипа (esp_reset_reason)
static IPAddress         g_srv_ip;
static uint16_t          g_srv_port = 0;
// Откуда приходили маяки за последние секунды: сервер с несколькими адресами (Wi-Fi + виртуальный
// адаптер) шлёт маяк с каждого, а команды — с одного; команды принимаем от любого недавнего.
struct SrvSeen { IPAddress ip; uint32_t t; };
static SrvSeen           g_srv_seen[4];

static void srv_note(IPAddress ip, uint32_t now) {
  int k = 0;
  for (int i = 0; i < 4; ++i) {
    if (g_srv_seen[i].ip == ip) { k = i; break; }
    if (g_srv_seen[i].t < g_srv_seen[k].t) k = i;   // самый старый — на замену
  }
  g_srv_seen[k].ip = ip;
  g_srv_seen[k].t = now;
}

static bool srv_known(IPAddress ip, uint32_t now) {
  if (ip == g_srv_ip) return true;
  for (int i = 0; i < 4; ++i)
    if (g_srv_seen[i].t && g_srv_seen[i].ip == ip && now - g_srv_seen[i].t < 10000) return true;
  return false;
}
static uint8_t           g_boot_role = ROLE_A;   // роль, с которой агент стартовал (из NVS)

static uint8_t tl_now();   // уровень телеметрии сейчас (молчание кончается само) — ниже

// Роль S хранится в памяти платы: после перезапуска агент сразу меряет, даже без сервера.
static uint8_t nvs_role() {
  Preferences p;
  uint8_t r = ROLE_A;
  if (p.begin("agent", true)) {
    r = p.getUChar("role", ROLE_A);
    p.end();
  }
  return r <= ROLE_M ? r : ROLE_A;
}

// Роли без счёта: статический акселерометр (S) и агент моторов (M) — свой короткий tel, с другой ролью — перезапуск.
static bool fixed_role(uint8_t r) { return r == ROLE_S || r == ROLE_M; }

static void nvs_set_role(uint8_t r) {
  Preferences p;
  if (p.begin("agent", false)) {
    p.putUChar("role", r);
    p.end();
  }
}

// ---------- ядро 1: счёт ----------
static uint32_t s_last_pub = 0;

// Два ядра (настройка dc, режим «каждый сам»): ядро связи в свободное от связи время
// набирает шум и отсеивает кандидатов делителями (задача prod_task, низший приоритет —
// связь её вытесняет), ядро счёта только проверяет Миллером — Рабином. Шум берёт одно
// ядро: пауза между словами генератора считается по тактам своего ядра, два ядра её
// собьют (randomness.md); аппаратный умножитель тоже один. Кандидаты идут через очередь,
// счётчики производителя сливаются в телеметрию при публикации.
struct Cand { uint32_t epoch; uint8_t b[256]; };
static QueueHandle_t     g_candq = nullptr;
static GenConfig         g_prod_cfg;                 // настройка для производителя (под g_mux)
static volatile uint32_t g_prod_gen = 0;             // растёт при каждой новой настройке
static volatile uint32_t g_epoch = 0;                // растёт при каждом сбросе счёта
static volatile bool     g_prod_idle = true;         // производитель стоит — шум не трогает
static volatile bool     g_prod_abort = false;       // производителю: бросить текущий кандидат
static volatile bool     g_prod_pause = false;       // производителю: стоять (таблица делителей перестраивается)
static PrimeStats        s_pp;                       // счётчики производителя: его рабочая копия…
static EntropyStats      s_pe;
static uint64_t          s_pwait = 0;
static PrimeStats        s_pp_pub;                   // …и опубликованная (под g_mux)
static EntropyStats      s_pe_pub;
static uint64_t          s_pwait_pub = 0;
static uint32_t          s_prod_last_pub = 0;

static void prod_pub(bool force) {
  const uint32_t now = millis();
  if (!force && now - s_prod_last_pub < 10) return;
  s_prod_last_pub = now;
  portENTER_CRITICAL(&g_mux);
  s_pp_pub = s_pp;
  s_pe_pub = s_pe;
  s_pwait_pub = s_pwait;
  portEXIT_CRITICAL(&g_mux);
}

static void prod_tick() { prod_pub(false); }

// Шум набирает ядро связи (производитель): «каждый сам» с двумя ядрами — кандидатов,
// эстафета и каскад с двумя ядрами — кусочки для группы. Иначе шум берёт ядро счёта.
static bool noise_on_core0(const GenConfig& c) {
  return c.run && c.dc && c.role == ROLE_A && (c.mode == MODE_SOLO || (c.mode == MODE_GROUP && c.gv));
}

static void publish(bool force) {
  uint32_t now = millis();
  if (!force && now - s_last_pub < 100) return;
  s_last_pub = now;
  portENTER_CRITICAL(&g_mux);
  g_pub = s_st;
  if (s_st.cfg.dc) {                                 // два ядра: прибавить сделанное производителем
    g_pub.p.cand += s_pp_pub.cand;
    g_pub.p.td_rej += s_pp_pub.td_rej;
    g_pub.p.td_cyc += s_pp_pub.td_cyc;
    for (int i = 0; i < SI_N; ++i) {
      SourceStats& d = g_pub.e.s[i];
      const SourceStats& s = s_pe_pub.s[i];
      d.units += s.units;
      d.bits += s.bits;
      d.us += s.us;
      d.ones += s.ones;
      d.total += s.total;
      d.same += s.same;
      d.pairs += s.pairs;
    }
    g_pub.e.sha_blocks += s_pe_pub.sha_blocks;
    g_pub.pwait += s_pwait_pub;
  }
  portEXIT_CRITICAL(&g_mux);
}

static void publish_tick() { publish(false); }

static void take_pending() {
  GenConfig c;
  portENTER_CRITICAL(&g_mux);
  c = g_new;
  g_pending = false;
  portEXIT_CRITICAL(&g_mux);
  if (keep_stats(c, s_st.cfg) && s_st.t_apply_us) {   // предел температуры или остановка: счёт не обнулять
    s_st.cfg = c;
    portENTER_CRITICAL(&g_mux);
    g_prod_cfg = c;                                    // производителю — та же настройка (run мог смениться)
    ++g_prod_gen;
    portEXIT_CRITICAL(&g_mux);
    publish(true);
    return;
  }
  const bool was_dc = noise_on_core0(s_st.cfg) && s_st.t_apply_us;
  s_st = GenStats();
  s_st.cfg = c;
  s_st.t_apply_us = esp_timer_get_time();
  // Производителя — на паузу, пока перестраивается таблица делителей: иначе он отсеивал бы по
  // полуперестроенной таблице (пропуск делимого поймает МР, но ложный отсев — нет).
  g_prod_pause = true;
  g_prod_abort = true;                                 // производитель бросает кандидата и встаёт
  for (int i = 0; was_dc && i < 400 && !g_prod_idle; ++i) vTaskDelay(pdMS_TO_TICKS(5));
  primes_configure(c);
  portENTER_CRITICAL(&g_mux);
  g_prod_cfg = c;
  ++g_prod_gen;
  ++g_epoch;
  s_pp_pub = PrimeStats();                             // его прошлые счётчики в новую настройку не попадают:
  s_pe_pub = EntropyStats();                           // иначе один пакет телеметрии выходил с кандидатами
  s_pwait_pub = 0;                                     // прошлого прогона и нулём простых
  portEXIT_CRITICAL(&g_mux);
  if (!noise_on_core0(c)) entropy_reset();             // шум берёт это ядро; иначе его сбросит производитель, увидев новую эпоху
  g_prod_pause = false;
  publish(true);
}

// ---------- ядро 0: производитель кандидатов (два ядра) ----------
static void prod_task(void*) {
  static uint8_t buf[640];                             // кандидат (до 256 байт) или набор кусочков группы (до 600)
  GenConfig c;
  uint32_t gen = 0, epoch = 0, last_yield = millis();
  for (;;) {
    if (g_prod_pause) {                                // новая настройка применяется: стоять
      g_prod_idle = true;
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    portENTER_CRITICAL(&g_mux);
    if (g_prod_gen != gen) {
      c = g_prod_cfg;
      gen = g_prod_gen;
    }
    const uint32_t e = g_epoch;
    portEXIT_CRITICAL(&g_mux);
    if (e != epoch) {                                  // счёт начат заново
      epoch = e;
      s_pp = PrimeStats();
      s_pe = EntropyStats();
      s_pwait = 0;
      prod_pub(true);
      g_prod_abort = false;
      if (noise_on_core0(c)) entropy_reset();          // шум теперь берёт это ядро
    }
    if (!noise_on_core0(c)) {
      g_prod_idle = true;
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    g_prod_idle = false;
    // Долг тепла — только за счёт (шум, отсев): ожидание места в очереди в него не входит,
    // иначе после затора производитель спал бы лишнее и темп колебался.
    if (c.mode == MODE_GROUP) {                        // эстафета, каскад: наборы кусочков шума для группы
      uint32_t gs = 0;
      const int n = group::prod_len(&gs);
      if (n <= 0 || n > (int)sizeof buf) {             // группа ещё не настроилась
        vTaskDelay(pdMS_TO_TICKS(2));
        continue;
      }
      const int64_t w0 = esp_timer_get_time();
      if (!entropy_fill(buf, n, noise_mask(c), s_pe, &g_prod_abort, prod_tick)) {
        vTaskDelay(1);
        continue;
      }
      const uint32_t busy = (uint32_t)(esp_timer_get_time() - w0);
      if (!group::prod_put(buf, n, gs, &g_prod_abort)) ++s_pwait;   // набор не взяли: настройка сменилась
      thermal::pace(busy, &g_prod_abort);
      prod_pub(false);
      if (millis() - last_yield >= 50) {
        vTaskDelay(1);
        last_yield = millis();
      }
      continue;
    }
    const int64_t w0 = esp_timer_get_time();
    if (!entropy_fill(buf, c.bits / 8, noise_mask(c), s_pe, &g_prod_abort, prod_tick)) {
      vTaskDelay(1);
      continue;
    }
    const bool keep = sieve_candidate(buf, c, s_pp);
    const uint32_t busy = (uint32_t)(esp_timer_get_time() - w0);
    if (keep) {
      Cand it;
      it.epoch = epoch;
      memcpy(it.b, buf, c.bits / 8);
      if (xQueueSend(g_candq, &it, 0) != pdTRUE) {     // очередь полна: ядро счёта не успевает
        ++s_pwait;
        while (!g_prod_abort && xQueueSend(g_candq, &it, pdMS_TO_TICKS(20)) != pdTRUE) {}
      }
    }
    thermal::pace(busy, &g_prod_abort);                // предел температуры — для обоих ядер
    prod_pub(false);
    if (millis() - last_yield >= 50) {                 // задача простоя ядра 0 должна дышать (её сторож)
      vTaskDelay(1);
      last_yield = millis();
    }
  }
}

// Задание группы: набрать шум, отсеять кандидатов или проверить своими базами.
static void run_job(const group::Job& j) {
  group::JobResult r = {};
  r.type = j.type;
  r.gs = j.gs;
  r.cycle = j.cycle;
  r.round = j.round;
  r.ok = true;
  r.value = -1;
  const GenConfig& c = s_st.cfg;
  uint8_t* d = group::job_data();
  int64_t t0 = esp_timer_get_time();
  if (g_pending) {                   // новые настройки ещё не приняты — задание повторят
    r.ok = false;
    group::job_done(r);
    return;
  }
  switch (j.type) {
    case group::JOB_PIECES:
      r.ok = entropy_fill(d, j.nbytes, noise_mask(c), s_st.e, &g_pending, publish_tick);
      break;
    case group::JOB_SIEVE: {         // все кандидаты; уцелевшие — подряд в начало буфера
      int n = 0;
      for (int k = 0; k < j.count; ++k) {
        uint8_t* b = d + k * j.nbytes;
        if (!sieve_candidate(b, c, s_st.p)) continue;
        if (n != k) memmove(d + n * j.nbytes, b, j.nbytes);
        ++n;
      }
      r.value = n;
      break;
    }
    case group::JOB_TEST: {
      int passed = 0;
      r.value = mr_bases(d, c, j.idx, j.nidx, true, s_st.p, &passed);
      r.tests = passed + (r.value >= 0 ? 1 : 0);
      break;
    }
    case group::JOB_RTEST: {         // эстафета, каскад: кандидаты подряд всеми базами, до первого простого
      int k = 0;
      while (k < j.count) {
        int passed = 0;
        const int w = mr_bases(d + k * j.nbytes, c, j.idx, j.nidx, !c.all, s_st.p, &passed);
        ++k;
        if (w == -2) { r.value = -2; break; }
        if (w == -1) {                // простое
          r.value = (int16_t)(k - 1);
          ++s_st.p.primes;
          break;
        }
        if (passed) ++s_st.p.dis;    // все базы: одни сказали «простое», другие «составное»
        if (g_pending) break;        // новые настройки — остальное не нужно
      }
      r.tests = (uint8_t)k;
      break;
    }
    case group::JOB_AUDIT: {         // сверка чужого простого одной базой
      int passed = 0;
      r.value = mr_bases(d, c, j.idx, j.nidx, true, s_st.p, &passed);
      r.tests = 1;
      break;
    }
  }
  r.us = esp_timer_get_time() - t0;
  ++s_st.jobs;
  group::job_done(r);
  thermal::pace(r.us, &g_pending);
}

static void gen_task(void*) {
  primes_init();
  g_selftest = primes_selftest() ? 1 : 0;
  Serial.printf("SELFTEST %s\n", g_selftest ? "ok" : "FAIL");
  static uint8_t buf[256], prime[256];
  static char hex[68];
  int64_t t_last = esp_timer_get_time();
  uint32_t last_yield = millis();
  for (;;) {
    if (g_pending) {
      take_pending();
      t_last = s_st.t_apply_us;
    }
    const GenConfig& c = s_st.cfg;
    if (!c.run) {                                    // стоим: ждём сессию, счётчики не трогаем
      vTaskDelay(pdMS_TO_TICKS(50));
      publish(false);
      continue;
    }
    if (c.mode == MODE_GROUP || c.role == ROLE_B) {  // сообща или рассеяние: по заданиям протокола
      group::Job j;
      if (group::take_job(j, pdMS_TO_TICKS(20))) run_job(j);
      publish(false);
      continue;
    }
    int64_t w0 = esp_timer_get_time();
    bool found;
    if (c.dc) {                                      // два ядра: кандидаты уже отсеяны ядром связи
      Cand it;
      if (xQueueReceive(g_candq, &it, pdMS_TO_TICKS(20)) != pdTRUE) {
        ++s_st.cwait;                                // ядро счёта ждало кандидата
        publish(false);
        continue;
      }
      if (it.epoch != g_epoch) continue;             // кандидат прошлой настройки
      w0 = esp_timer_get_time();
      found = test_sieved(it.b, c, s_st.p, c.bits <= 256 ? hex : nullptr, sizeof hex, (c.nbg || c.pl) ? prime : nullptr);
    } else {
      if (!entropy_fill(buf, c.bits / 8, noise_mask(c), s_st.e, &g_pending, publish_tick)) {
        ++s_st.stall;
        continue;
      }
      found = test_candidate(buf, c, s_st.p, c.bits <= 256 ? hex : nullptr, sizeof hex, (c.nbg || c.pl) ? prime : nullptr);
    }
    if (found) {
      if (c.nbg) group::announce_prime(prime, c.bits / 8);   // сет B есть — пусть рассеивает
      if (c.pl) plog::push(prime, c.bits / 8, 0);             // журнал простых
      int64_t now = esp_timer_get_time();
      uint64_t dt = now - t_last;            // от прошлого простого (или от новых настроек)
      t_last = now;
      ++s_st.tp_n;
      s_st.tp_sum_us += dt;
      uint32_t d32 = dt > UINT32_MAX ? UINT32_MAX : (uint32_t)dt;
      if (s_st.tp_n == 1 || d32 < s_st.tp_min_us) s_st.tp_min_us = d32;
      if (d32 > s_st.tp_max_us) s_st.tp_max_us = d32;
      int k = dt ? 63 - __builtin_clzll(dt) : 0;
      ++s_st.tp_hist[k < HIST_N ? k : HIST_N - 1];
      if (c.bits <= 256) strlcpy(s_st.last_hex, hex, sizeof s_st.last_hex);
    }
    publish(false);
    thermal::pace((uint32_t)(esp_timer_get_time() - w0), &g_pending);   // предел температуры
    if (millis() - last_yield >= 200) {      // дать поработать задаче простоя ядра 1
      vTaskDelay(1);
      last_yield = millis();
    }
  }
}

// ---------- заставка: отдельная задача, чтобы не задерживать связь ----------
static void sig_task(void*) {
  agent::boot_signature();
  g_sig_done = true;
  vTaskDelete(nullptr);
}

// ---------- ядро 0: связь ----------
// 1 — радио включено постоянно (шум приёмника идёт в генератор), 2 — радио со
// сном, 0 — Wi-Fi не запущен.
static int rf_state() {
  wifi_mode_t m = WIFI_MODE_NULL;
  if (esp_wifi_get_mode(&m) != ESP_OK || m == WIFI_MODE_NULL) return 0;
  wifi_ps_type_t ps = WIFI_PS_NONE;
  esp_wifi_get_ps(&ps);
  return ps == WIFI_PS_NONE ? 1 : 2;
}

// Температура в JSON: датчик отказал — null, а не «nan» (это не JSON, сервер отбросил бы пакет).
static const char* temp_json(float t) {
  static char b[16];
  if (isnan(t)) return "null";
  snprintf(b, sizeof b, "%.1f", t);
  return b;
}

static void add_head(Out& o, const char* type, uint32_t seq, uint32_t now) {
  add(o, "{\"v\":2,\"type\":\"%s\",\"mac\":\"%s\",\"id\":%d,\"fw\":\"%s\",\"sk\":\"%s\","
         "\"seq\":%lu,\"up\":%lu,\"ms\":%lu",
      type, agent::mac_str(), agent::device_id(), FW_VERSION, FW_SKETCH,
      (unsigned long)seq, (unsigned long)(now / 1000), (unsigned long)now);
}

static void build_tel(Out& o, const GenStats& g, const group::Stats& gs, uint32_t seq, uint32_t now,
                      float temp, const agent::CoreLoad& load, bool full) {
  const uint32_t mhz = ESP.getCpuFreqMHz();
  const GenConfig& c = g.cfg;
  const PrimeStats& p = g.p;
  add_head(o, "tel", seq, now);
  add(o, ",\"rssi\":%d,\"temp\":%s,\"heap\":%lu,\"cpu0\":%.1f,\"cpu1\":%.1f,\"rf\":%d,\"st\":%d",
      WiFi.RSSI(), temp_json(temp), (unsigned long)ESP.getFreeHeap(), load.busy0, load.busy1,
      rf_state(), (int)g_selftest);
  add(o, ",\"tmax\":%u,\"work\":%.1f,\"ts\":%d", c.tmax, thermal::work() * 100.0f, thermal::state());
  add(o, ",\"udpf\":%lu,\"wdisc\":%lu,\"wrsn\":%d,\"wrst\":%lu,\"nr\":%u", (unsigned long)g_udp_fail,
      (unsigned long)g_wifi_disc, g_wifi_reason, (unsigned long)g_wifi_rst, c.nrate);
  add(o, ",\"rbt\":[%d,%lu,%lu]", g_boot_rst, (unsigned long)g_boot_why, (unsigned long)g_rbt_n);
  // самодиагностика: errno последней неудачной отправки, команд принято/отброшено, сколько мс ядро 1
  // не принимает новые настройки, наименьшая свободная память с запуска
  add(o, ",\"udpe\":%d,\"cmd\":[%lu,%lu],\"pend\":%lu,\"heapmin\":%lu", g_udp_err, (unsigned long)g_cmd_rx,
      (unsigned long)g_cmd_rej, (unsigned long)(g_pending ? now - g_pending_t : 0), (unsigned long)ESP.getMinFreeHeap());
  add(o, ",\"net\":%u", g_net);                    // 0 — основная сеть, 1 — запасная
  add(o, ",\"bcn\":[%lu,%lu,%lu]", (unsigned long)g_bcn_n, (unsigned long)(g_bcn_t ? (now - g_bcn_t) / 1000 : 0),
      (unsigned long)g_bcn_rst);                       // маяков сервера, с давности последнего, переподключений из-за тишины
  int8_t txq = 0;
  esp_wifi_get_max_tx_power(&txq);
  add(o, ",\"k\":%u,\"pre\":%u,\"cpu\":%u,\"txp\":%u,\"txr\":%.2f,\"ping\":%u,\"led\":%u,\"tp\":%u",
      c.kmax, c.pre, c.cpu, c.txp, txq / 4.0f, c.ping, c.led, c.tp);
  add(o, ",\"tl\":%u,\"tq\":%u,\"tls\":%u,\"rmm\":%u,\"hb\":%u", c.tl, c.tq, tl_now(), c.rmm, c.hb);
  // шаг на агенте: длительность, радио на шаге, сколько секунд радио было выключено, какой шаг закончен сам
  add(o, ",\"dur\":%u,\"ro\":%u,\"roff\":%lu,\"done\":%lu", c.dur, c.ro, (unsigned long)s_radio_off_s,
      (unsigned long)s_done_cid);
  rim::add_tel(o);                                   // датчики на ободе: что найдено, режимы из регистров
  bat::add_tel(o);                                   // аккумуляторы контура (INA219), если подключён
  // рассеяние: роль, режим, функция, фильтр CSI; счётчики A (объявлено) и B (получено, выпущено…)
  add(o, ",\"role\":%u,\"sm\":%u,\"sf\":%u,\"cf\":%u,\"sh\":%u,\"bgr\":%u,\"ann\":%lu,\"adrop\":%lu,"
         "\"heard\":%lu,\"bclost\":%lu,\"rxdrop\":%lu,\"slost\":[%lu,%lu]",
      c.role, c.sm, c.sf, c.cf, c.sh, c.bgr, (unsigned long)gs.ann, (unsigned long)gs.ann_drop,
      (unsigned long)gs.heard, (unsigned long)bcast_lost(), (unsigned long)gs.rx_drop,
      (unsigned long)sensor_lost(SI_ACC), (unsigned long)sensor_lost(SI_MAG));   // показания датчиков, не взятые в шум (запас полон)
  // ESP-NOW этого агента: кадров в очередь, выброшено (очередь полна), не принято радио, без подтверждения
  add(o, ",\"etx\":[%lu,%lu,%lu,%lu]", (unsigned long)gs.tx, (unsigned long)gs.tx_drop, (unsigned long)gs.tx_err,
      (unsigned long)gs.tx_fail);
  add(o, ",\"pl\":%u,\"pln\":%lu,\"pll\":%lu", c.pl, (unsigned long)plog::total(), (unsigned long)plog::lost());   // журнал простых
  add(o, ",\"dc\":%u,\"dcq\":[%llu,%llu]", c.dc, g.cwait, g.pwait);   // два ядра: кто кого ждал
  add(o, ",\"gv\":%u,\"rk\":%u,\"pw\":%u,\"raw\":%u", c.gv, c.rk, c.pw, c.raw);         // вариант группы: эстафета, каскад
  // прошивка по Wi-Fi: состояние (0 нет, 1 загрузка, 2 записано, -1 ошибка), «ждёт подтверждения», раздел
  add(o, ",\"ota\":[%d,%d,\"%s\"]", ota::state(), ota::pending_verify() ? 1 : 0, ota::slot());
  if (c.role == ROLE_B)
    add(o, ",\"prx\":%lu,\"stx\":%lu,\"sdrop\":%lu,\"swait\":%lu,\"chs\":%lu,\"chf\":%lu,\"chd\":%lu,"
           "\"slat\":%llu,\"sn\":%lu,\"sbg\":%lu",
        (unsigned long)gs.prx, (unsigned long)gs.stx, (unsigned long)gs.sdrop, (unsigned long)gs.swait,
        (unsigned long)gs.chs, (unsigned long)gs.chf, (unsigned long)gs.chd, gs.slat, (unsigned long)gs.sn,
        (unsigned long)gs.sbg);
  add(o, ",\"cid\":%lu,\"run\":%u,\"src\":%u,\"bits\":%u,\"nb\":%u,\"all\":%d,\"td\":%u,\"mode\":%u,\"age\":%.1f",
      (unsigned long)c.cid, c.run, c.src, c.bits, c.nb, c.all ? 1 : 0, c.td, c.mode,
      (esp_timer_get_time() - g.t_apply_us) / 1e6);
  if (c.mode == MODE_GROUP) {
    add(o, ",\"grp\":[");
    for (int i = 0; i < c.ng; ++i) add(o, "%s%u", i ? "," : "", c.grp[i]);
    add(o, "]");
  }
  // отсев ядром связи в эстафете и каскаде (два ядра) — в общие счётчики кандидатов
  add(o, ",\"cand\":%llu,\"tdr\":%llu,\"mri\":%llu,\"pr\":%llu,\"fp\":%llu,\"dis\":%llu,"
         "\"err\":%llu,\"stall\":%llu,\"jobs\":%llu",
      p.cand + gs.sv_cand, p.td_rej + gs.sv_tdr, p.mr_in, p.primes, p.fp, p.dis, p.err, g.stall, g.jobs);
  add_arr(o, "bt", p.bt, c.nb, 1);
  add_arr(o, "br", p.br, c.nb, 1);
  add_arr(o, "bus", p.bcyc, c.nb, mhz);
  add(o, ",\"tdus\":%llu,\"mrus\":%llu", (p.td_cyc + gs.sv_tdcyc) / mhz, p.mr_cyc / mhz);
  add(o, ",\"tpn\":%llu,\"tps\":%llu,\"tpmin\":%lu,\"tpmax\":%lu",
      g.tp_n, g.tp_sum_us, (unsigned long)g.tp_min_us, (unsigned long)g.tp_max_us);
  if (full) {
    int last = HIST_N - 1;
    while (last > 0 && !g.tp_hist[last]) --last;
    add(o, ",\"tph\":[");
    for (int i = 0; i <= last; ++i) add(o, "%s%lu", i ? "," : "", (unsigned long)g.tp_hist[i]);
    add(o, "]");
  }
  add(o, ",\"e\":[");
  for (int i = 0; i < SI_N; ++i) {
    const SourceStats& s = g.e.s[i];
    add(o, "%s[%llu,%llu,%llu,%llu,%llu,%llu,%llu]", i ? "," : "",
        s.units, s.bits, s.us, s.ones, s.total, s.same, s.pairs);
  }
  uint32_t cd[4];
  csi_detail(cd);
  add(o, "],\"sha\":%llu,\"csi\":[%lu,%lu,%lu,%lu,%lu,%lu,%lu]", g.e.sha_blocks,
      (unsigned long)csi_received(), (unsigned long)csi_dropped(), (unsigned long)ping_replies(),
      (unsigned long)cd[0], (unsigned long)cd[1], (unsigned long)cd[2], (unsigned long)cd[3]);
  if (full && g.last_hex[0]) add(o, ",\"lp\":\"%s\"", g.last_hex);
  add(o, "}");
}

// Статистика группы — отдельный пакет: в основной не помещается.
static void build_grp(Out& o, const group::Stats& s, uint32_t cid, int ng, uint32_t seq, uint32_t now) {
  add_head(o, "grp", seq, now);
  add(o, ",\"cid\":%lu,\"on\":%u,\"role\":%u,\"master\":%u,\"phase\":%u,\"cycle\":%lu",
      (unsigned long)cid, s.active, s.role, s.master, s.phase, (unsigned long)s.cycle);
  add(o, ",\"cycles\":%lu,\"cycles_m\":%lu,\"primes_m\":%lu,\"regathers\":%lu,\"found\":%lu",
      (unsigned long)s.cycles, (unsigned long)s.cycles_m, (unsigned long)s.primes_m,
      (unsigned long)s.regathers, (unsigned long)s.found);
  add(o, ",\"gath\":%lu,\"cands\":%lu,\"surv\":%lu,\"rdrop\":%lu,\"starts\":%lu,\"fillw\":%lu,"
         "\"pre\":[%lu,%lu],\"early\":%lu,\"hop\":%lu",
      (unsigned long)s.gathers, (unsigned long)s.cands, (unsigned long)s.survivors,
      (unsigned long)s.rsv_drop, (unsigned long)s.starts, (unsigned long)s.fill_waits,
      (unsigned long)s.pre_hit, (unsigned long)s.pre_miss, (unsigned long)s.early, (unsigned long)s.hop_us);
  add(o, ",\"asks_tx\":%lu,\"asks_rx\":%lu,\"brk_pcs\":%lu,\"brk_res\":%lu,\"brk_hand\":%lu,"
         "\"resyncs\":%lu,\"conflicts\":%lu,\"stale\":%lu",
      (unsigned long)s.asks_tx, (unsigned long)s.asks_rx, (unsigned long)s.brk_pcs,
      (unsigned long)s.brk_res, (unsigned long)s.brk_hand, (unsigned long)s.resyncs,
      (unsigned long)s.conflicts, (unsigned long)s.stale);
  add(o, ",\"streak\":%lu,\"streak_max\":%lu,\"streak_ms\":%lu,\"streak_ms_max\":%lu,\"clean_ms\":%llu",
      (unsigned long)s.streak, (unsigned long)s.streak_max, (unsigned long)s.streak_ms,
      (unsigned long)s.streak_ms_max, s.clean_ms);
  add(o, ",\"t\":[%llu,%llu,%llu,%llu,%llu],\"c\":[%llu,%llu,%llu]",
      s.t_gather, s.t_sieve, s.t_test, s.t_hand, s.t_cycle, s.c_pieces, s.c_sieve, s.c_test);
  add(o, ",\"tx\":%lu,\"rx\":%lu,\"tx_fail\":%lu,\"espnow\":%lu",
      (unsigned long)s.tx, (unsigned long)s.rx, (unsigned long)s.tx_fail, (unsigned long)s.espnow_ver);
  add(o, ",\"tx_cb\":%lu,\"tx_drop\":%lu,\"txq_max\":%lu,\"tx_stuck\":%lu,\"jlost\":%lu",
      (unsigned long)s.tx_cb, (unsigned long)s.tx_drop, (unsigned long)s.txq_max, (unsigned long)s.tx_stuck,
      (unsigned long)s.job_lost);
  add(o, ",\"ok_to\":[");
  for (int i = 0; i < ng; ++i) add(o, "%s%lu", i ? "," : "", (unsigned long)s.ok_to[i]);
  add(o, "],\"fail_to\":[");
  for (int i = 0; i < ng; ++i) add(o, "%s%lu", i ? "," : "", (unsigned long)s.fail_to[i]);
  add(o, "],\"rssi_from\":[");
  for (int i = 0; i < ng; ++i) add(o, "%s%d", i ? "," : "", s.rssi_from[i]);
  add(o, "]");
  add(o, ",\"agr\":%lu,\"split\":%lu,\"dis\":%lu", (unsigned long)s.agree, (unsigned long)s.split,
      (unsigned long)s.disagree);
  // вариант группы; эстафета, каскад: кругов с заменой кусочков своим шумом и замен, запас сейчас
  add(o, ",\"gv\":%u,\"tr\":[%lu,%lu],\"rr\":%u", s.gv, (unsigned long)s.truncs, (unsigned long)s.trunc_pcs, s.rr);
  // замолкали участники / кто молчит (бит — место) / сколько; три числа — маска (в a957d37, 82b5b35 было [раз, сколько])
  add(o, ",\"mute\":[%lu,%u,%d]", (unsigned long)s.mute, s.mute_mask, __builtin_popcount(s.mute_mask));
  add(o, ",\"rest\":[%u,%u,%lu]", s.resting, s.rest_left, (unsigned long)s.rests);   // отдых: сейчас, осталось с, всего
  if (s.last_split[0]) add(o, ",\"gls\":\"%s\"", s.last_split);
  if (s.last_prime[0]) add(o, ",\"glp\":\"%s\"", s.last_prime);
  add(o, "}");
}

static bool send_out(WiFiUDP& udp, const Out& o) {
  if (!udp.beginPacket(g_srv_ip, g_srv_port)) { g_udp_err = errno; ++g_udp_fail; return false; }
  udp.write((const uint8_t*)o.b, o.n);
  if (!udp.endPacket()) { g_udp_err = errno; ++g_udp_fail; return false; }   // 12 ENOMEM — нет буферов передачи
  ++g_udp_ok;
  return true;
}

static void reboot_for(uint32_t why, const char* text);

// Сторож Wi-Fi. Замечено 04.10.2026: плата считает, что подключена (разрывов нет), а
// стек не отправляет ни одного пакета — так на десятки минут (№5) или полторы минуты (№2);
// команды тоже почти не доходят. Само проходит не всегда. Десять секунд подряд «ни одного
// ушедшего при наличии неушедших» — переподключаемся и считаем событие (wrst в телеметрии).
// По журналу 04–07.10.2026 так бывает только с CSI (пинг роутера 100/с). Три переподключения
// подряд без единого ушедшего пакета — перезапуск (rbt: 3): переподключение застрявшие буферы
// передачи не освобождает, а плата в таком виде бесполезна.
static void wifi_watchdog() {
  static uint32_t ok_prev = 0, fail_prev = 0, bad_s = 0, streak = 0;
  const uint32_t ok = g_udp_ok, fail = g_udp_fail;
  const bool stuck = fail != fail_prev && ok == ok_prev;
  if (ok != ok_prev) streak = 0;                       // хоть один пакет ушёл — зомби кончился
  ok_prev = ok;
  fail_prev = fail;
  bad_s = stuck ? bad_s + 1 : 0;
  if (bad_s < 10 || WiFi.status() != WL_CONNECTED) return;
  bad_s = 0;
  ++g_wifi_rst;
  ++streak;
  Serial.printf("WIFI stuck: 10 s without a sent packet (udpf=%lu errno=%d heap=%lu) -> reconnect #%lu, streak %lu\n",
                (unsigned long)fail, g_udp_err, (unsigned long)ESP.getFreeHeap(), (unsigned long)g_wifi_rst,
                (unsigned long)streak);
  if (streak >= 3) reboot_for(RBT_ZOMBIE, "wifi zombie: 3 reconnects, still no packet leaves");
  WiFi.disconnect();
  delay(200);
  WiFi.reconnect();
}

// Вторая форма «зомби» (07.10.2026, №4 после CSI): плата считает, что подключена, пакеты «уходят»
// без ошибок (udpf не растёт), а до сервера ничего не доходит и от него ничего не приходит — точка
// доступа уже забыла плату (последний разрыв — reason 8, ASSOC_LEAVE), а плата об этом не знает. Так
// №4 молчала 23 минуты, пока её не перезапустили по USB. Признак — тишина маяка: сервер шлёт его раз
// в 2 с, и пока связь есть, он приходит. 45 с без маяка при «подключённом» Wi-Fi — переподключение
// (bcn_rst в телеметрии); три подряд без единого маяка между ними — перезапуск (rbt 3). Когда сервер
// выключен, плата будет переподключаться каждые 45 с и перезапускаться раз в ~2,5 мин — это
// безвредно: без сервера опытов нет.
static void beacon_watchdog(uint32_t now) {
  static uint32_t streak = 0, bcn_prev = 0;
  if (g_bcn_n != bcn_prev) streak = 0;                 // маяк был — связь настоящая
  bcn_prev = g_bcn_n;
  if (!g_bcn_t || WiFi.status() != WL_CONNECTED || now - g_bcn_t < 45000) return;
  g_bcn_t = now;                                       // следующий отсчёт — ещё через 45 с
  ++g_bcn_rst;
  ++streak;
  Serial.printf("WIFI beacon silence 45 s (bcn=%lu udpf=%lu) -> reconnect #%lu, streak %lu\n", (unsigned long)g_bcn_n,
                (unsigned long)g_udp_fail, (unsigned long)g_bcn_rst, (unsigned long)streak);
  if (streak >= 3) reboot_for(RBT_ZOMBIE, "wifi zombie: no server beacon after 3 reconnects");
  WiFi.disconnect();
  delay(200);
  WiFi.reconnect();
}

static void reboot_for(uint32_t why, const char* text) {
  if (g_rbt_magic != RBT_MAGIC) {
    g_rbt_magic = RBT_MAGIC;
    g_rbt_n = 0;
  }
  g_rbt_why = why;
  ++g_rbt_n;
  Serial.printf("REBOOT %s (#%lu)\n", text, (unsigned long)g_rbt_n);
  Serial.flush();
  delay(100);
  esp_restart();
}

// Сторож радио. 05.10.2026, №6 в эстафете: в 19:42:26 разом перестали приходить отчёты радио
// об отправке кадров ESP-NOW и уходить пакеты на сервер; Wi-Fi вскоре разорвался и больше не
// поднялся — 150 попыток за 7 минут, «4-way handshake timeout»: радио не передаёт вовсе, сторож
// Wi-Fi (переподключение) не помогает. Ожило только после перезапуска. Поэтому: кадры ESP-NOW
// отправляются, а отчётов радио о них нет 15 с подряд — или Wi-Fi нет 90 с подряд — перезапуск.
// Причина — в телеметрии после него: "rbt":[причина сброса чипа, 1 радио / 2 Wi-Fi, раз всего].
static void radio_watchdog(bool up) {
  static uint32_t tx_prev = 0, cb_prev = 0, mute_s = 0, down_s = 0;
  static group::Stats gs;
  group::snapshot(gs);
  const bool sending = gs.tx != tx_prev, reported = gs.tx_cb != cb_prev;
  tx_prev = gs.tx;
  cb_prev = gs.tx_cb;
  // отчёты радио хоть раз уже были: без ESP-NOW (не поднялся) перезапуск не поможет
  mute_s = sending && !reported && gs.tx_cb > 0 ? mute_s + 1 : 0;
  down_s = up ? 0 : down_s + 1;
  if (mute_s >= 15) reboot_for(RBT_RADIO, "radio: ESP-NOW frames sent, no reports for 15 s");
  if (down_s >= 90) reboot_for(RBT_WIFI, "wifi: no connection for 90 s");
}

static void send_ack(WiFiUDP& udp, unsigned long cid, bool ok, const char* err) {
  if (!g_srv_port) return;
  char b[220];
  int n = snprintf(b, sizeof b,
                   "{\"v\":2,\"type\":\"ack\",\"mac\":\"%s\",\"id\":%d,\"cid\":%lu,\"ok\":%d,\"err\":\"%s\"}",
                   agent::mac_str(), agent::device_id(), cid, ok ? 1 : 0, err);
  udp.beginPacket(g_srv_ip, g_srv_port);
  udp.write((const uint8_t*)b, n);
  udp.endPacket();
}

static GenConfig s_cur;   // последние принятые настройки (ядро 0)
static uint32_t  s_quiet_t0 = 0;   // когда пришла команда молчать, мс

// Статический акселерометр: короткий tel — связь, настройки, датчик; счёта нет.
static void build_tel_static(Out& o, uint32_t seq, uint32_t now, float temp, const agent::CoreLoad& load) {
  const GenConfig& c = s_cur;
  int8_t txq = 0;
  esp_wifi_get_max_tx_power(&txq);
  add_head(o, "tel", seq, now);
  add(o, ",\"rssi\":%d,\"temp\":%s,\"heap\":%lu,\"cpu0\":%.1f,\"cpu1\":%.1f,\"udpf\":%lu,\"wdisc\":%lu,\"wrsn\":%d,"
         "\"wrst\":%lu",
      WiFi.RSSI(), temp_json(temp), (unsigned long)ESP.getFreeHeap(), load.busy0, load.busy1, (unsigned long)g_udp_fail,
      (unsigned long)g_wifi_disc, g_wifi_reason, (unsigned long)g_wifi_rst);
  add(o, ",\"rbt\":[%d,%lu,%lu],\"net\":%u", g_boot_rst, (unsigned long)g_boot_why, (unsigned long)g_rbt_n, g_net);
  add(o, ",\"udpe\":%d,\"cmd\":[%lu,%lu],\"heapmin\":%lu,\"bcn\":[%lu,%lu,%lu]", g_udp_err, (unsigned long)g_cmd_rx,
      (unsigned long)g_cmd_rej, (unsigned long)ESP.getMinFreeHeap(), (unsigned long)g_bcn_n,
      (unsigned long)(g_bcn_t ? (now - g_bcn_t) / 1000 : 0), (unsigned long)g_bcn_rst);
  add(o, ",\"role\":%u,\"cid\":%lu,\"run\":%u,\"cpu\":%u,\"txp\":%u,\"txr\":%.2f,\"led\":%u,\"tp\":%u,\"tl\":%u,"
         "\"tq\":%u,\"tls\":%u,\"afs\":%u,\"avg\":%u",
      g_boot_role, (unsigned long)c.cid, c.run, c.cpu, c.txp, txq / 4.0f, c.led, c.tp, c.tl, c.tq, tl_now(), c.afs, c.avg);
  add(o, ",\"ota\":[%d,%d,\"%s\"]", ota::state(), ota::pending_verify() ? 1 : 0, ota::slot());
  if (g_boot_role == ROLE_M) motor::add_tel(o);
  else sacc::add_tel(o);
  bat::add_tel(o);                                   // агент катушки: аккумуляторы катушки (INA219)
  add(o, "}");
}

// Сейчас молчим? Молчание на tq секунд кончается само.
static uint8_t tl_now() {
  if (s_cur.tl != TL_QUIET) return s_cur.tl;
  if (s_cur.tq && millis() - s_quiet_t0 >= (uint32_t)s_cur.tq * 1000) return TL_NORMAL;
  return TL_QUIET;
}

// Мощность передатчика (Wi-Fi и ESP-NOW — одно радио), единица — 0,25 дБм.
static void apply_txp(uint8_t dbm) {
  int8_t want = (int8_t)(dbm * 4), have = 0;
  if (esp_wifi_get_max_tx_power(&have) == ESP_OK && have == want) return;
  esp_wifi_set_max_tx_power(want);
}

static void apply_config(const GenConfig& c) {
  if (c.cpu != getCpuFrequencyMhz()) {         // до счёта по новым настройкам: замеры времени — уже на новой частоте
    setCpuFrequencyMhz(c.cpu);
    Serial.printf("CPU %lu MHz\n", (unsigned long)getCpuFrequencyMhz());
  }
  if (fixed_role(g_boot_role)) {               // статический или моторы: только мощность, телеметрия, режим датчика
    apply_txp(c.txp);
    if (g_boot_role == ROLE_S) sacc::set(c.afs, c.avg);
    if (c.tl == TL_QUIET && (s_cur.tl != TL_QUIET || c.tq != s_cur.tq)) s_quiet_t0 = millis();
    s_cur = c;
    s_cur.role = g_boot_role;
    return;
  }
  portENTER_CRITICAL(&g_mux);
  g_new = c;
  g_pending = true;
  g_pending_t = millis();
  portEXIT_CRITICAL(&g_mux);
  thermal::set_limit(c.tmax);
  apply_txp(c.txp);
  entropy_set_ping(c.ping);
  entropy_set_csi_filter(c.cf, c.bmac, c.has_bmac ? c.nbg : 0);
  rim::set_radius(c.rmm);
  entropy_select(c.run ? c.src : 0);           // стоим — CSI и пинг роутера 100/с не нужны (грели и шумели в эфире)
  group::set_alive_rate(c.hb);
  plog::configure(c.pl, c.cid);
  group::configure(c);
  if (c.tl == TL_QUIET && (s_cur.tl != TL_QUIET || c.tq != s_cur.tq)) s_quiet_t0 = millis();
  s_cur = c;
  // отсчёт шага: новая настройка со счётом — с нуля; та же (сменились параметры «на ходу») — продолжается; стоп — снят
  if (!c.run) s_step_cid = 0;
  else if (c.cid != s_step_cid) { s_step_cid = c.cid; s_step_t0 = millis(); }
}

// Wi-Fi: как в setup — только 802.11b/g (с 802.11n роутер отвечает кадрами, на которые чип строит CSI лишь
// в 25–60 % случаев), WPA, без сна радио (шум приёмника в генератор непрерывно, ESP-NOW не теряется).
static void wifi_begin() {
  WiFi.mode(WIFI_STA);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
  if (g_net && strlen(agent::wifi_ssid2())) WiFi.begin(agent::wifi_ssid2(), agent::wifi_pass2());
  else WiFi.begin(agent::wifi_ssid(), agent::wifi_pass());
  WiFi.setSleep(false);
}

static void net_switch(uint8_t to, const char* why) {
  g_net = to;
  Serial.printf("WIFI -> %s network (%s)\n", to ? "fallback" : "main", why);
  WiFi.disconnect();
  delay(100);
  wifi_begin();
  g_last_try = g_net_down = millis();
  g_bcn_t = millis();                           // тишина маяка на время переезда — не повод переподключаться
  if (to == 1) g_net_try = millis() - 8u * 60 * 1000;   // первая попытка вернуться на основную — через 2 мин
}

// Раз в секунду: нет связи 40 с — на другую сеть; на запасной через 2 мин, потом раз в 10 мин, стоя, — попробовать основную.
static void net_tick(uint32_t now) {
  if (s_radio_off || !strlen(agent::wifi_ssid2())) return;
  if (WiFi.status() == WL_CONNECTED) {
    g_net_down = 0;
    if (g_net == 1 && !s_cur.run && now - g_net_try > 10u * 60 * 1000) {
      g_net_try = now;
      net_switch(0, "retry main");
    }
    return;
  }
  if (!g_net_down) g_net_down = now;
  else if (now - g_net_down > 40000) net_switch(g_net ^ 1, "no connection 40 s");
}

// Эфир шагу нужен: группа или сет B (ESP-NOW), шум из эфира (CSI, обратный) или пинг роутера без CSI (cf 3).
static bool step_needs_radio(const GenConfig& c) {
  return c.mode != MODE_SOLO || c.role != ROLE_A || c.nbg > 0 || (c.src & (SRC_CSI | SRC_BC)) || c.cf == 3;
}

// Радио на шаге: выключить Wi-Fi целиком (драйвер стоит, кадров нет) или поднять обратно.
static void radio_set(bool off) {
  if (off == s_radio_off) return;
  s_radio_off = off;
  if (off) {
    s_radio_t0 = millis();
    group::radio_pause(true);
    WiFi.disconnect(true, false);
    WiFi.mode(WIFI_OFF);
    Serial.printf("RADIO off for step cid=%lu\n", (unsigned long)s_step_cid);
    return;
  }
  s_radio_off_s = (millis() - s_radio_t0) / 1000;
  wifi_begin();
  group::radio_pause(false);
  g_bcn_t = millis();                           // тишина маяка за время без радио — не повод переподключаться
  g_last_try = millis();
  Serial.printf("RADIO on after %lu s\n", (unsigned long)s_radio_off_s);
}

// Раз в секунду: пора ли выключить радио (шаг начался, настройка подтверждена телеметрией, эфир не нужен)
// и не кончился ли шаг (dur): тогда стоп с теми же настройками, телеметрия обычная, радио обратно, итоги в tel.
static void step_tick(uint32_t now) {
  if (s_step_cid && s_cur.dur && now - s_step_t0 >= (uint32_t)s_cur.dur * 1000) {
    GenConfig c = s_cur;
    c.run = 0;
    c.tl = TL_NORMAL;
    c.tq = 0;
    s_done_cid = s_step_cid;
    Serial.printf("STEP done cid=%lu after %lu s\n", (unsigned long)s_step_cid, (unsigned long)((now - s_step_t0) / 1000));
    apply_config(c);                            // как команда «стоп» с тем же номером: счётчики замирают
    radio_set(false);
    return;
  }
  if (s_step_cid && s_cur.dur && s_cur.ro && !s_radio_off && !step_needs_radio(s_cur) && !g_pending &&
      ota::state() != 1 && now - s_step_t0 >= 2u * s_cur.tp + 500)   // две телеметрии с новым номером уже ушли
    radio_set(true);
}

static void handle_cmd(char* line, IPAddress from, WiFiUDP& udp) {
  for (char* p = line; *p; ++p)
    if (*p == '\r' || *p == '\n') { *p = 0; break; }
  ++g_cmd_rx;
  if (g_srv_port && !srv_known(from, millis())) {    // команды только от своего сервера (любой его адрес с маяком)
    ++g_cmd_rej;
    Serial.printf("CMD rejected from %s (server %s): %.40s\n", from.toString().c_str(), g_srv_ip.toString().c_str(), line);
    return;
  }
  if (strncmp(line, "IDENT", 5) == 0) {              // найти плату: IDENT s=секунд — светодиод мигает, видно, какая это (0 — перестать)
    const char* sp = strstr(line, "s=");
    unsigned long secs = sp ? strtoul(sp + 2, nullptr, 10) : 20;
    if (secs > 120) secs = 120;
    g_ident_until = secs ? millis() + secs * 1000 : 0;
    if (!secs) agent::led_quiet(false);
    Serial.printf("CMD IDENT %lu\n", secs);
    send_ack(udp, s_cur.cid, true, "ident");
    return;
  }
  if (strncmp(line, "MOT", 3) == 0) {                // моторы: MOT a=‰ b=‰ r=разгон w=сторож (только роль M)
    if (g_boot_role != ROLE_M) {
      send_ack(udp, s_cur.cid, false, "not motor");
      return;
    }
    motor::command(line + 3);
    send_ack(udp, s_cur.cid, true, "mot");
    return;
  }
  if (strncmp(line, "REST", 4) == 0 || strncmp(line, "BACK", 4) == 0) {   // отдых по очереди: REST s=секунд / BACK
    const char* sp = strstr(line, "s=");
    const unsigned long secs = line[0] == 'R' && sp ? strtoul(sp + 2, nullptr, 10) : 0;
    group::rest((uint16_t)(secs > 600 ? 600 : secs));
    Serial.printf("CMD %s %lu\n", line[0] == 'R' ? "REST" : "BACK", secs);
    send_ack(udp, s_cur.cid, true, line[0] == 'R' ? "rest" : "back");
    return;
  }
  if (strncmp(line, "OTA ", 4) == 0) {               // прошивка по Wi-Fi: OTA url=… md5=… len=… ver=… t=… sig=…
    char url[160] = "", md5[33] = "", ver[40] = "", sig[65] = "";
    unsigned long len = 0, t = 0;
    char* save = nullptr;
    for (char* tok = strtok_r(line + 4, " ", &save); tok; tok = strtok_r(nullptr, " ", &save)) {
      if (!strncmp(tok, "url=", 4)) strlcpy(url, tok + 4, sizeof url);
      else if (!strncmp(tok, "md5=", 4)) strlcpy(md5, tok + 4, sizeof md5);
      else if (!strncmp(tok, "len=", 4)) len = strtoul(tok + 4, nullptr, 10);
      else if (!strncmp(tok, "ver=", 4)) strlcpy(ver, tok + 4, sizeof ver);
      else if (!strncmp(tok, "t=", 2)) t = strtoul(tok + 2, nullptr, 10);
      else if (!strncmp(tok, "sig=", 4)) strlcpy(sig, tok + 4, sizeof sig);
    }
    const char* why = ota::start(url, md5, (uint32_t)len, ver, (uint32_t)t, sig);
    if (!why) {                                      // стоять: счёт и ESP-NOW загрузке не мешают
      GenConfig c = s_cur;
      c.run = 0;
      apply_config(c);
    }
    Serial.printf("OTA %s -> %s\n", ver, why ? why : "start");
    send_ack(udp, 0, why == nullptr, why ? why : "ota");
    return;
  }
  const char* cp = strstr(line, "cid=");
  unsigned long cid = cp ? strtoul(cp + 4, nullptr, 10) : 0;
  char err[48];
  GenConfig c;
  bool ok = parse_config(line, s_cur, c, err, sizeof err);
  if (ok && c.role != g_boot_role && (fixed_role(c.role) || fixed_role(g_boot_role))) {   // в/из S или M: с новой ролью заново
    if (g_boot_role == ROLE_M) motor::stop();
    nvs_set_role(c.role);
    Serial.printf("CMD %s -> role %u, restart\n", line, c.role);
    send_ack(udp, cid, true, "restart");
    delay(300);
    ESP.restart();
  }
  if (ok && c.mode == MODE_GROUP && c.role == ROLE_A) {   // есть ли агент в списке группы (сет B в ней не бывает)
    bool in = false;
    for (int i = 0; i < c.ng && !in; ++i)
      in = c.has_gmac ? memcmp(c.gmac[i], agent::mac_bytes(), 6) == 0
                      : c.grp[i] == agent::device_id() && agent::device_id() > 0;
    if (!in) {
      ok = false;
      snprintf(err, sizeof err, "not in group");
    }
  }
  // Новый номер — новые настройки. Тот же номер — повтор; если в нём другие
  // параметры «на ходу» (мощность, пинг, предел…), применить их без сброса счёта.
  if (ok && (c.cid != s_cur.cid || soft_differs(c, s_cur))) apply_config(c);
  Serial.printf("CMD %s -> %s %s\n", line, ok ? "ok" : "rejected", err);
  send_ack(udp, cid, ok, err);
}

static void comms_task(void*) {
  WiFiUDP udp, beacon, cmd;
  beacon.begin(BEACON_PORT);
  cmd.begin(CMD_PORT);
  static Out out;
  uint32_t next = millis() + 1000, next_s = millis() + 1000, seq = 0;
  uint32_t acc_seq = 0, acc_last = 0, prm_seq = 0, prm_last = 0;
  uint32_t led_off_at = 0;                              // когда погасить вспышку телеметрии (мс)
  float temp = agent::chip_temp();
  uint64_t last_cand = 0, last_pr = 0;
  const bool fixed = fixed_role(g_boot_role);          // статический акселерометр или моторы: счёта нет

  for (;;) {
    int n = beacon.parsePacket();
    if (n > 0) {
      char b[32] = {0};
      beacon.read(b, sizeof b - 1);
      if (strncmp(b, "W2SRV ", 6) == 0) {
        IPAddress ip = beacon.remoteIP();
        uint16_t port = atoi(b + 6);
        if (ip != g_srv_ip || port != g_srv_port)
          Serial.printf("SRV found %s:%u (beacon)\n", ip.toString().c_str(), port);
        g_srv_ip = ip;
        g_srv_port = port;
        srv_note(ip, millis());
        ++g_bcn_n;
        g_bcn_t = millis();
      }
    }

    n = cmd.parsePacket();
    if (n > 0) {
      static char line[1280];          // два списка по 16 MAC
      memset(line, 0, sizeof line);
      cmd.read((uint8_t*)line, sizeof line - 1);
      handle_cmd(line, cmd.remoteIP(), udp);
    }

    if (!fixed) entropy_pump();

    uint32_t now = millis();
    // статический: отсчёты — как только набрался пакет (или не реже 0,3 с), не больше двух пакетов за проход
    if (fixed && WiFi.status() == WL_CONNECTED && g_srv_port && tl_now() != TL_QUIET) {
      for (int k = 0; k < 2; ++k) {
        const uint32_t w = sacc::pending();
        if (!w || (w < (uint32_t)sacc::PER_PACKET && now - acc_last < 300)) break;
        out.n = 0;
        out.over = false;
        add_head(out, "acc", acc_seq, now);
        const int n = sacc::build_acc(out);
        add(out, "}");
        if (!n || out.over || !send_out(udp, out)) break;
        sacc::commit(n);
        ++acc_seq;
        acc_last = now;
      }
    }
    // журнал простых: пакет, как набралось 8 простых или прошло 250 мс
    if (!fixed && WiFi.status() == WL_CONNECTED && g_srv_port && tl_now() != TL_QUIET && plog::pending() &&
        (plog::pending() >= 8 || now - prm_last >= 250)) {
      out.n = 0;
      out.over = false;
      add_head(out, "prm", prm_seq, now);
      add(out, ",\"cid\":%lu,\"bits\":%u,\"lost\":%lu,\"p\":[", (unsigned long)s_cur.cid, s_cur.bits,
          (unsigned long)plog::lost());
      const int n = plog::build(out);
      add(out, "]}");
      if (n && !out.over && send_out(udp, out)) plog::commit(n);
      ++prm_seq;
      prm_last = now;
    }
    if ((int32_t)(now - next_s) >= 0) {                      // раз в секунду, независимо от телеметрии
      next_s += 1000;
      if ((int32_t)(now - next_s) > 2000) next_s = now + 1000;
      temp = agent::chip_temp();
      thermal::update(temp);                                 // предел температуры кристалла
      if (!fixed) group::set_alive_info(temp, s_cur.run);    // в свой пульс соседям
      const bool up1 = WiFi.status() == WL_CONNECTED;
      if (up1) apply_txp(s_cur.txp);                         // после переподключения Wi-Fi — снова своя мощность
      wifi_watchdog();                                       // связь «есть», а пакеты не уходят — переподключиться
      beacon_watchdog(now);                                  // связь «есть», а маяка сервера нет — переподключиться
      radio_watchdog(up1 || s_radio_off);                    // радио не передаёт вовсе — перезапуск (выключенное — не в счёт)
      if (!fixed) step_tick(now);                            // шаг на агенте: радио выкл / шаг кончился
      net_tick(now);                                         // основная сеть не пускает — запасная
      if (!s_cur.run && now > 90000 && ESP.getFreeHeap() < HEAP_RESTART_BELOW && ota::state() != 1) {
        Serial.printf("HEAP low while idle: %lu free (min %lu)\n", (unsigned long)ESP.getFreeHeap(),
                      (unsigned long)ESP.getMinFreeHeap());
        reboot_for(RBT_HEAP, "heap: low while idle — restart to clean up");
      }
      ota::tick(up1 && g_srv_port != 0, now);                // новая прошивка: подтвердить или откатить
#ifdef OTA_ROLLBACK_TEST                                     // проверка отката: «сломанная» прошивка перезапускается
      if (now > 15000) {                                     // раньше подтверждения — загрузчик вернёт прежнюю
        Serial.println("OTA rollback test: restart before confirm");
        delay(100);
        ESP.restart();
      }
#endif
    }
    if ((int32_t)(now - next) >= 0) {
      const uint8_t tl = tl_now();
      const uint32_t tp = s_cur.tp * (tl == TL_RARE ? 10 : 1);
      next += tp;
      if ((int32_t)(now - next) > 2 * (int32_t)tp) next = now + tp;   // после долгой паузы не догонять
      static GenStats g;
      portENTER_CRITICAL(&g_mux);
      g = g_pub;
      portEXIT_CRITICAL(&g_mux);
      agent::CoreLoad load = agent::core_load();
      bool up = WiFi.status() == WL_CONNECTED;
      if (fixed) {                                       // статический: свой короткий tel
        if (up && g_srv_port && tl != TL_QUIET) {
          out.n = 0;
          out.over = false;
          build_tel_static(out, seq, now, temp, load);
          if (!out.over && send_out(udp, out) && g_sig_done && s_cur.led) {
            agent::led_quiet(true);              // гаснет ниже, без ожидания: цикл связи не стоит 20 мс
            led_off_at = now + 20;
          }
          ++seq;
        }
        if (seq % 10 == 1 || !up || !g_srv_port)
          Serial.printf("TEL static seq=%lu wifi=%d srv=%s cid=%lu sens=%d wait=%lu temp=%.1f udpf=%lu\n",
                        (unsigned long)seq, up, g_srv_port ? g_srv_ip.toString().c_str() : "-",
                        (unsigned long)s_cur.cid, sacc::present(), (unsigned long)sacc::pending(), temp,
                        (unsigned long)g_udp_fail);
      } else if (up && g_srv_port && tl != TL_QUIET) {   // молчание: в эфир ничего
        static group::Stats gs;
        group::snapshot(gs);
        out.n = 0;
        out.over = false;
        build_tel(out, g, gs, seq, now, temp, load, true);
        if (out.over) {                                     // не влезло — без гистограммы и числа
          out.n = 0;
          out.over = false;
          build_tel(out, g, gs, seq, now, temp, load, false);
        }
        if (!out.over) {
          send_out(udp, out);
          if (g_sig_done && g.cfg.run && g.cfg.led) {   // вспышка — только когда считает и не выключена
            agent::led_quiet(true);
            led_off_at = now + 20;
          }
        }
        if (rim::present() & rim::HAS_IMU) {        // обороты и невесомость: сводка, потом сырые оси
          out.n = 0;
          out.over = false;
          add_head(out, "imu", seq, now);
          rim::add_imu(out);
          add(out, "}");
          if (!out.over) send_out(udp, out);
          out.n = 0;
          out.over = false;
          add_head(out, "imr", seq, now);
          rim::add_imr(out);
          add(out, "}");
          if (!out.over) send_out(udp, out);
        }
        if (rim::present() & rim::HAS_MAG) {
          out.n = 0;
          out.over = false;
          add_head(out, "mag", seq, now);
          rim::add_mag(out);
          add(out, "}");
          if (!out.over) send_out(udp, out);
        }
        if (g.cfg.mode == MODE_GROUP && g.cfg.role == ROLE_A) {
          out.n = 0;
          out.over = false;
          build_grp(out, gs, g.cfg.cid, g.cfg.ng, seq, now);
          if (!out.over) send_out(udp, out);
        }
        if (seq % 5 == 2) {                          // соседи по пульсу: раз в 5 с [номер, сигнал, с давности, кадров, считает, °C]
          static group::Neighbor nb[group::NB_MAX];
          const int n = group::neighbors(nb, group::NB_MAX);
          const int64_t tn = esp_timer_get_time();
          out.n = 0;
          out.over = false;
          add_head(out, "nbr", seq, now);
          add(out, ",\"nb\":[");
          for (int i = 0; i < n; ++i)
            add(out, "%s[%u,%d,%lu,%lu,%u,%d]", i ? "," : "", nb[i].id, nb[i].rssi,
                (unsigned long)((tn - nb[i].last_us) / 1000000), (unsigned long)nb[i].n, nb[i].run, nb[i].temp);
          add(out, "]}");
          if (!out.over) send_out(udp, out);
        }
        ++seq;
      }
      if (g.p.cand < last_cand) last_cand = last_pr = 0;    // счётчики сброшены новыми настройками
      if (!fixed && (seq % 10 == 1 || !up || !g_srv_port)) {
        static group::Stats gd;
        group::snapshot(gd);
        Serial.printf("TEL seq=%lu wifi=%d srv=%s cid=%lu mode=%u bits=%u src=%u cand=+%llu pr=+%llu "
                      "temp=%.1f work=%.0f%% len=%d udpf=%lu wdisc=%lu/%d esp_tx=%lu cb=%lu q=%lu stuck=%lu "
                      "cycle=%lu\n",
                      (unsigned long)seq, up, g_srv_port ? g_srv_ip.toString().c_str() : "-",
                      (unsigned long)g.cfg.cid, g.cfg.mode, g.cfg.bits, g.cfg.src, g.p.cand - last_cand,
                      g.p.primes - last_pr, temp, thermal::work() * 100.0f, out.n,
                      (unsigned long)g_udp_fail, (unsigned long)g_wifi_disc, g_wifi_reason,
                      (unsigned long)gd.tx, (unsigned long)gd.tx_cb, (unsigned long)gd.txq_max,
                      (unsigned long)gd.tx_stuck, (unsigned long)gd.cycle);
      }
      last_cand = g.p.cand;
      last_pr = g.p.primes;
    }

    if (led_off_at && (int32_t)(millis() - led_off_at) >= 0) {   // вспышка кончилась
      agent::led_quiet(false);
      led_off_at = 0;
    }
    if (g_ident_until) {                                         // «найти плату»: мигание 5 раз в секунду поверх вспышек
      if ((int32_t)(millis() - g_ident_until) >= 0) { g_ident_until = 0; agent::led_quiet(false); }
      else agent::led_quiet(((millis() / 100) & 1) == 0);
    }
    if (!s_radio_off && WiFi.status() != WL_CONNECTED && millis() - g_last_try > 5000) {
      g_last_try = millis();
      Serial.println("WIFI reconnect");
      WiFi.reconnect();
    }
    vTaskDelay(5);                       // 5 мс: пинг для CSI до 200 раз в секунду
  }
}

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);
  agent::identity_init();
  g_boot_role = nvs_role();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d label=%s role=%u\n",
                FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id(), agent::device_label(), g_boot_role);
  g_boot_rst = (int)esp_reset_reason();
  if (g_rbt_magic != RBT_MAGIC) {                    // включили питание: память RTC — мусор
    g_rbt_magic = RBT_MAGIC;
    g_rbt_n = 0;
    g_rbt_why = RBT_NONE;
  }
  g_boot_why = g_boot_rst == ESP_RST_SW ? g_rbt_why : RBT_NONE;   // сторож перезапустил — или кто-то другой
  g_rbt_why = RBT_NONE;                              // следующий перезапуск (прошивка по Wi-Fi) — не наш
  Serial.printf("BOOT reset=%d watchdog=%lu (#%lu)\n", g_boot_rst, (unsigned long)g_boot_why, (unsigned long)g_rbt_n);

  xTaskCreatePinnedToCore(sig_task, "sig", 4096, nullptr, 1, nullptr, 0);

  WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
    if (s_radio_off) return;                       // выключили сами на шаге — не разрыв
    ++g_wifi_disc;
    g_wifi_reason = info.wifi_sta_disconnected.reason;
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  wifi_begin();            // 802.11b/g, WPA (наша сеть — WPA), радио без сна — см. wifi_begin
  uint32_t t0 = millis();
  // основную ждём 30 с: после быстрого перезапуска роутер какое-то время помнит прежнее подключение платы
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) delay(100);
  if (WiFi.status() != WL_CONNECTED && strlen(agent::wifi_ssid2())) {   // основная не пустила — запасная
    net_switch(1, "main failed at boot");
    t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(100);
  }
  Serial.printf("WIFI %s net=%s ip=%s rssi=%d ch=%d t=%lu ms\n",
                WiFi.status() == WL_CONNECTED ? "ok" : "fail", g_net ? "fallback" : "main",
                WiFi.localIP().toString().c_str(), WiFi.RSSI(), WiFi.channel(), millis() - t0);

  if (strlen(agent::server_host())) {
    g_srv_ip.fromString(agent::server_host());
    g_srv_port = 5005;
    Serial.printf("SRV fixed %s:%u\n", g_srv_ip.toString().c_str(), g_srv_port);
  }

  GenConfig def;           // заводские настройки до первой команды сервера
  if (fixed_role(g_boot_role)) {                 // статический акселерометр (меряет на ядре 1) или моторы
    def.role = g_boot_role;
    if (g_boot_role == ROLE_S) sacc::begin(def.afs, def.avg);
    else {
      motor::begin();
      bat::begin(true);              // INA219 на D4/D5 — аккумуляторы катушки (шину I2C запускает сам)
    }
    apply_config(def);
    xTaskCreatePinnedToCore(comms_task, "comms", 8192, nullptr, 2, nullptr, 0);
    return;
  }
  entropy_init();
  group::begin();          // ESP-NOW и задача протокола группы (ядро 0)
  rim::begin();            // датчики на ободе, если подключены (I2C: D4, D5)
  bat::begin(false);       // аккумуляторы шины (INA219) на той же I2C, если подключён
  apply_config(def);
  g_candq = xQueueCreate(16, sizeof(Cand));
  xTaskCreatePinnedToCore(prod_task, "prod", 6144, nullptr, 1, nullptr, 0);   // два ядра: производитель, ниже связи
  xTaskCreatePinnedToCore(gen_task, "gen", 8192, nullptr, 1, nullptr, 1);     // ядро счёта
  xTaskCreatePinnedToCore(comms_task, "comms", 8192, nullptr, 2, nullptr, 0); // ядро связи
}

void loop() { vTaskDelete(nullptr); }
