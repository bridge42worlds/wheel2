// 07.11: набор шума и слова регистра — горячий путь производителя кандидатов; -Os здесь жмёт скорость
#pragma GCC optimize ("O3")
#include "entropy.h"
#include "psram.h"
#include <WiFi.h>
#include <esp_wifi.h>
#include "esp_cpu.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "soc/rtc.h"
#include "soc/wdev_reg.h"
#include "driver/gpio.h"
#include "freertos/queue.h"
#include "lwip/sockets.h"
#include "lwip/inet_chksum.h"
#include "lwip/prot/icmp.h"

// Сколько бит засчитываем. Замер 03.10.2026 (tools/probe_report.py, logs/probe4.log):
//   CSI: 104 числа на пакет, мин-энтропия двух младших бит ~1.5 на число,
//        ~150 бит на пакет — зачитываем 32;
//   АЦП: разброс 8 единиц, но сильная связь соседних отсчётов (наводка) —
//        1/4 бита на отсчёт;
//   дрожание: мин-энтропия ~1 бит на отсчёт, медленный дрейф — 1/4 бита.
static const uint32_t CSI_BITS_PER_PACKET = 32;
static const uint32_t ADC_SAMPLES_PER_BIT = 4;
static const uint32_t JIT_SAMPLES_PER_BIT = 4;
static const uint32_t JIT_PERIODS = 8;     // отсчёт — тактов кварца за 8 периодов RC 136 кГц
static const int      ADC_PIN = 1;         // D0 = GPIO1 = ADC1_CH0; ничего к ней не подключать
static const uint32_t BLOCK_BITS = 256;
// Регистр ГСЧ напрямую (SRC_RRG): соседние чтения связаны — в SHA-256 засчитываем 1 бит на rw слов (настройка rw,
// 1..4; было всегда 4).
static inline uint32_t rrg_word() { return REG_READ(WDEV_RND_REG); }

static inline uint32_t cyc() { return esp_cpu_get_cycle_count(); }

// ---------- CSI: замеры канала по каждому принятому пакету ----------
struct CsiPkt { int8_t d[128]; };         // LLTF: 64 поднесущие × (мнимая, действительная)
static QueueHandle_t     s_q = nullptr;
static volatile uint32_t s_rx = 0, s_drop = 0, s_pong = 0;
static volatile uint32_t s_all = 0, s_short = 0, s_ht = 0, s_from_gw = 0;   // для разбора потока
static volatile uint8_t  s_csi_mode = 0;      // 0 — все кадры, 1 — только роутер, 2 — только сет B
static uint8_t           s_csi_macs[MAX_GROUP][6];
static volatile int      s_csi_nmac = 0;
static uint8_t           s_gw_mac[6];
static bool              s_csi_on = false;
static int               s_icmp = -1;
static uint16_t          s_seq = 0;

static void csi_cb(void*, wifi_csi_info_t* info) {   // задача Wi-Fi, ядро 0
  ++s_all;
  if (!info || !info->buf) return;
  if (info->rx_ctrl.sig_mode) ++s_ht;                // пакет 802.11n
  const bool gw = memcmp(info->mac, s_gw_mac, 6) == 0;
  if (gw) ++s_from_gw;
  if (s_csi_mode == 1 && !gw) return;                // только роутер: кадры других плат и сета B — мимо
  if (s_csi_mode == 2) {                             // только сет B
    bool b = false;
    for (int i = 0; i < s_csi_nmac && !b; ++i) b = memcmp(info->mac, s_csi_macs[i], 6) == 0;
    if (!b) return;
  }
  if (info->len < 128) { ++s_short; return; }
  CsiPkt p;
  memcpy(p.d, info->buf, sizeof p.d);
  if (info->first_word_invalid) memset(p.d, 0, 4);  // аппаратное ограничение чипа
  ++s_rx;
  if (xQueueSend(s_q, &p, 0) != pdTRUE) ++s_drop;
}

void entropy_init() { s_q = xQueueCreate(32, sizeof(CsiPkt)); }

static bool s_ping_on = false;              // пинг роутера идёт (обычно вместе с CSI)

// Разбор «зомби» (07.10.2026): с CSI платы теряли буферы передачи. Чтобы развести сбор CSI и
// пинг роутера, есть два опытных положения: cf = 3 — пинг идёт, а CSI не включается (при любом
// источнике шума); ping = 0 — CSI включён, пинга нет (CSI только с маяков и чужих кадров).
static volatile uint8_t s_want = 0;            // источники, выбранные сейчас (для датчиков на ободе)

bool entropy_wants(int si) { return s_want & (si == SI_ACC ? SRC_ACC : si == SI_MAG ? SRC_MAG : 0); }

void entropy_select(uint8_t mask) {
  s_want = mask;
  const bool csi = (mask & SRC_CSI) && s_csi_mode != 3;
  const bool ping = mask && ((mask & SRC_CSI) || s_csi_mode == 3);   // mask 0 — агент стоит: ни CSI, ни пинга
  if (csi != s_csi_on) {
    if (csi) {
      wifi_csi_config_t cfg = {};
      cfg.lltf_en = true;
      cfg.htltf_en = true;
      cfg.stbc_htltf2_en = true;
      cfg.ltf_merge_en = true;
      cfg.channel_filter_en = false;   // без сглаживания: соседние поднесущие независимы
      cfg.manu_scale = false;
      esp_wifi_set_csi_config(&cfg);
      esp_wifi_set_csi_rx_cb(csi_cb, nullptr);
      esp_wifi_set_csi(true);
      wifi_ap_record_t ap;
      if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) memcpy(s_gw_mac, ap.bssid, 6);
    } else {
      esp_wifi_set_csi(false);
      xQueueReset(s_q);
    }
    s_csi_on = csi;
  }
  if (ping && s_icmp < 0) {
    s_icmp = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
    if (s_icmp >= 0) fcntl(s_icmp, F_SETFL, O_NONBLOCK);
  }
  s_ping_on = ping;
}

// Без пинга CSI почти не приходит: пакеты для нас редки. Ответы роутера на
// эхо-запрос (по умолчанию 100 раз в секунду) дают ровный поток от одного
// отправителя. Свой эхо-запрос, а не esp_ping: та заводит задачу без привязки
// к ядру, и она могла бы попасть на ядро счёта.
static int64_t s_ping_period_us = 10000, s_ping_next = 0;

void entropy_set_csi_filter(uint8_t mode, const uint8_t (*macs)[6], int n) {
  s_csi_nmac = 0;
  for (int i = 0; i < n && i < MAX_GROUP; ++i) memcpy(s_csi_macs[i], macs[i], 6);
  s_csi_nmac = n < MAX_GROUP ? n : MAX_GROUP;
  s_csi_mode = (mode == 2 && s_csi_nmac == 0) ? 0 : mode;   // «только сет B» без сета B отбрасывал бы всё
}

// ---------- эфир сета B: запас байт из услышанных кадров (пишет ядро 0, читает ядро 1) ----------
static uint8_t           s_bc[2048];                 // степень двойки: счётчики переполняются ровно
static volatile uint32_t s_bc_w = 0, s_bc_r = 0;
static volatile uint32_t s_bc_lost = 0;
static portMUX_TYPE      s_bc_mux = portMUX_INITIALIZER_UNLOCKED;

void entropy_feed_bcast(const uint8_t* d, size_t n) {
  portENTER_CRITICAL(&s_bc_mux);
  for (size_t i = 0; i < n; ++i) {
    if (s_bc_w - s_bc_r >= sizeof s_bc) {            // запас полон: счёт не успевает брать
      s_bc_lost += n - i;
      break;
    }
    s_bc[s_bc_w++ % sizeof s_bc] = d[i];
  }
  portEXIT_CRITICAL(&s_bc_mux);
}

uint32_t bcast_lost() { return s_bc_lost; }

// ---------- «информация» (07.11): файл с сервера (код, музыка) по кругу — вместо эфира B в источнике SRC_BC ----------
// INFO off|clr|on|<смещение> <hex>: блок до INFO_CAP байт в PSRAM; пока on и блок не пуст — bc_take читает его по кругу,
// эфир B не нужен. Чистая информация: src = 16; смесь с регистром ГСЧ: src = 144 (XOR байт в байт, raw 1).
static const uint32_t    INFO_CAP = 256 * 1024;
static uint8_t*          s_info = nullptr;
static volatile uint32_t s_info_len = 0, s_info_r = 0;
static volatile bool     s_info_on = false;

bool entropy_info_put(uint32_t off, const uint8_t* d, size_t n) {
  if (!s_info) s_info = (uint8_t*)big_alloc(INFO_CAP);
  if (!s_info || off + n > INFO_CAP) return false;
  memcpy(s_info + off, d, n);
  if (off + n > s_info_len) s_info_len = off + n;
  return true;
}
void entropy_info_clear() { s_info_len = 0; s_info_r = 0; s_info_on = false; }
void entropy_info_on(bool on) { s_info_on = on; s_info_r = 0; }
uint32_t entropy_info_len() { return s_info_len; }
bool entropy_info_active() { return s_info_on && s_info_len > 0; }

static size_t bc_take(uint8_t* out, size_t want) {
  if (s_info_on && s_info_len > 0) {                 // информация по кругу — всегда есть, сколько просят
    for (size_t i = 0; i < want; ++i) out[i] = s_info[s_info_r++ % s_info_len];
    return want;
  }
  portENTER_CRITICAL(&s_bc_mux);
  size_t n = s_bc_w - s_bc_r;
  if (n > want) n = want;
  for (size_t i = 0; i < n; ++i) out[i] = s_bc[s_bc_r++ % sizeof s_bc];
  portEXIT_CRITICAL(&s_bc_mux);
  return n;
}

// ---------- датчики на ободе: запас младших байт показаний (пишет задача датчиков, читает ядро 1) ----------
struct SensRing {
  uint8_t           b[4096];                         // 09.11: записи целиком (18 байт на отсчёт акселерометра) — запас больше
  volatile uint32_t w = 0, r = 0, lost = 0;
  volatile bool     present = false;
  volatile uint16_t bpb256 = 256;                    // зачёт, 1/256 бита на байт запаса (entropy_sensor_format)
};
static SensRing     s_sr[2];                         // 0 — акселерометр, 1 — магнитометр
static portMUX_TYPE s_sr_mux = portMUX_INITIALIZER_UNLOCKED;
static const uint32_t SENS_BITS_PER_BYTE[2] = {1, 2};   // зачёт младших байт (без хеша): ICM — 1 бит на байт, MLX — 2
static volatile bool s_hashed = true;                // шум через SHA-256 (raw 0) — записи датчиков целиком

static inline int sr_idx(int si) { return si == SI_MAG ? 1 : 0; }

void entropy_sensor_present(int si, bool present) { s_sr[sr_idx(si)].present = present; }

static volatile bool s_share_rx = false;            // поток датчиков колеса принимается (WS): датчик «есть» всегда
void entropy_share_rx(bool on) { s_share_rx = on; }
void entropy_sensor_format(int si, uint16_t bpb256) { if (bpb256) s_sr[sr_idx(si)].bpb256 = bpb256; }
bool entropy_hashed() { return s_hashed; }
static inline bool sens_here(int si) { return s_share_rx || s_sr[sr_idx(si)].present; }

void entropy_feed_sensor(int si, const uint8_t* d, size_t n) {
  SensRing& q = s_sr[sr_idx(si)];
  portENTER_CRITICAL(&s_sr_mux);
  for (size_t i = 0; i < n; ++i) {
    if (q.w - q.r >= sizeof q.b) {                   // запас полон: счёт не успевает брать
      q.lost += n - i;
      break;
    }
    q.b[q.w++ % sizeof q.b] = d[i];
  }
  portEXIT_CRITICAL(&s_sr_mux);
}

uint32_t sensor_lost(int si) { return s_sr[sr_idx(si)].lost; }

static size_t sr_take(int si, uint8_t* out, size_t want) {
  SensRing& q = s_sr[sr_idx(si)];
  portENTER_CRITICAL(&s_sr_mux);
  size_t n = q.w - q.r;
  if (n > want) n = want;
  for (size_t i = 0; i < n; ++i) out[i] = q.b[q.r++ % sizeof q.b];
  portEXIT_CRITICAL(&s_sr_mux);
  return n;
}

void entropy_set_ping(uint8_t per_s) {
  if (per_s == 0) {                    // без пинга: CSI только с маяков и чужих кадров (опыт)
    s_ping_period_us = 0;
    return;
  }
  if (per_s < 10) per_s = 10;
  if (per_s > 200) per_s = 200;
  s_ping_period_us = 1000000 / per_s;
}

void entropy_pump() {
  if (!s_ping_on || s_icmp < 0) return;
  uint8_t in[64];
  while (recv(s_icmp, in, sizeof in, MSG_DONTWAIT) > 0) ++s_pong;
  if (WiFi.status() != WL_CONNECTED || !s_ping_period_us) return;
  const int64_t now = esp_timer_get_time();
  static int64_t gw_at = 0;            // адрес роутера — раз в секунду: после переподключения он мог смениться
  if (now - gw_at > 1000000) {         // (брался один раз при включении CSI — и с cf=1 всё шло мимо)
    gw_at = now;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) memcpy(s_gw_mac, ap.bssid, 6);
  }
  if (now < s_ping_next) return;
  s_ping_next = now - s_ping_next > 3 * s_ping_period_us ? now + s_ping_period_us : s_ping_next + s_ping_period_us;
  uint32_t gw = (uint32_t)WiFi.gatewayIP();
  if (!gw) return;
  struct { struct icmp_echo_hdr h; uint8_t pad[16]; } pkt;
  memset(&pkt, 0, sizeof pkt);
  pkt.h.type = ICMP_ECHO;
  pkt.h.id = htons(0x5732);
  pkt.h.seqno = htons(++s_seq);
  pkt.h.chksum = inet_chksum(&pkt, sizeof pkt);
  struct sockaddr_in to = {};
  to.sin_family = AF_INET;
  to.sin_addr.s_addr = gw;
  sendto(s_icmp, &pkt, sizeof pkt, 0, (struct sockaddr*)&to, sizeof to);
}

uint32_t csi_received() { return s_rx; }
uint32_t csi_dropped()  { return s_drop; }
uint32_t ping_replies() { return s_pong; }
void csi_detail(uint32_t out[4]) {
  out[0] = s_all;
  out[1] = s_short;
  out[2] = s_ht;
  out[3] = s_from_gw;
}

// ---------- ядро 1 ----------
static uint8_t  s_pool[32];
static int      s_pool_left = 0;
static uint32_t s_cpu_mhz = 240;
static bool     s_adc_ready = false;
static uint32_t s_prev[SI_N];
static bool     s_have_prev[SI_N];
static int8_t   s_csi_prev[128];
static uint32_t s_rng_cyc_rest = 0;      // такты быстрого пути, ещё не переведённые в мкс
struct BitQ { uint8_t b[256]; int r, n; };   // шум без хеша: очередь бит источника, по биту в байте
static BitQ     s_rq[SI_N];

void entropy_reset() {
  s_pool_left = 0;
  for (int i = 0; i < SI_N; ++i) s_rq[i].r = s_rq[i].n = 0;   // сырьё прошлой настройки не брать
  portENTER_CRITICAL(&s_bc_mux);                     // эфир сета B: байты прошлой настройки не брать,
  s_bc_r = s_bc_w;                                   // потери считать заново
  s_bc_lost = 0;
  portEXIT_CRITICAL(&s_bc_mux);
  portENTER_CRITICAL(&s_sr_mux);                     // показания датчиков прошлой настройки не брать, потери — с настройки
  for (auto& q : s_sr) { q.r = q.w; q.lost = 0; }
  portEXIT_CRITICAL(&s_sr_mux);
  memset(s_have_prev, 0, sizeof s_have_prev);
  memset(s_csi_prev, 0, sizeof s_csi_prev);
  s_rng_cyc_rest = 0;
  s_cpu_mhz = ESP.getCpuFreqMHz();
}

static inline void lsb(SourceStats& s, int si, uint32_t v) {
  s.ones += v & 1;
  ++s.total;
  if (s_have_prev[si]) {
    s.same += ((v ^ s_prev[si]) & 1) == 0;
    ++s.pairs;
  }
  s_prev[si] = v;
  s_have_prev[si] = true;
}

static void adc_prepare() {
  analogReadResolution(12);
  analogSetPinAttenuation(ADC_PIN, ADC_11db);
  analogRead(ADC_PIN);                      // первая выборка настраивает канал
  gpio_pullup_en((gpio_num_t)ADC_PIN);      // обе подтяжки держат ножку у середины
  gpio_pulldown_en((gpio_num_t)ADC_PIN);    // питания; без них она висит у нуля
  delay(5);
  s_adc_ready = true;
}

static void add_rng(mbedtls_sha256_context& ctx, EntropyStats& st) {
  SourceStats& s = st.s[SI_RNG];
  int64_t t0 = esp_timer_get_time();
  uint32_t w[BLOCK_BITS / 32];
  for (uint32_t& x : w) {
    x = esp_random();
    lsb(s, SI_RNG, x);
  }
  mbedtls_sha256_update(&ctx, (const uint8_t*)w, sizeof w);
  s.units += BLOCK_BITS / 32;
  s.bits += BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
}

static void add_rrg(mbedtls_sha256_context& ctx, EntropyStats& st, uint32_t wpb) {
  SourceStats& s = st.s[SI_RRG];
  int64_t t0 = esp_timer_get_time();
  uint32_t w[64];
  for (uint32_t k = 0; k < BLOCK_BITS * wpb; k += 64) {
    for (uint32_t& x : w) {
      x = rrg_word();
      lsb(s, SI_RRG, x);
    }
    mbedtls_sha256_update(&ctx, (const uint8_t*)w, sizeof w);
  }
  s.units += BLOCK_BITS * wpb;
  s.bits += BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
}

static bool add_csi(mbedtls_sha256_context& ctx, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  SourceStats& s = st.s[SI_CSI];
  int64_t t0 = esp_timer_get_time();
  uint32_t got = 0;
  CsiPkt p;
  bool ok = true;
  while (got < BLOCK_BITS) {
    if (xQueueReceive(s_q, &p, pdMS_TO_TICKS(100)) != pdTRUE) {
      if (tick) tick();
      if (*abort) { ok = false; break; }
      continue;
    }
    mbedtls_sha256_update(&ctx, (const uint8_t*)p.d, sizeof p.d);
    for (int j = 0; j < 128; ++j) {
      if (!p.d[j]) continue;
      s.ones += p.d[j] & 1;
      ++s.total;
      if (s_csi_prev[j]) {
        s.same += ((p.d[j] ^ s_csi_prev[j]) & 1) == 0;
        ++s.pairs;
      }
    }
    memcpy(s_csi_prev, p.d, sizeof s_csi_prev);
    ++s.units;
    got += CSI_BITS_PER_PACKET;
    if (tick) tick();             // счётчики видны и пока блок ещё собирается
  }
  s.bits += ok ? BLOCK_BITS : 0;
  s.us += esp_timer_get_time() - t0;
  return ok;
}

static void add_adc(mbedtls_sha256_context& ctx, EntropyStats& st) {
  if (!s_adc_ready) adc_prepare();
  SourceStats& s = st.s[SI_ADC];
  int64_t t0 = esp_timer_get_time();
  uint16_t v[64];
  for (uint32_t k = 0; k < BLOCK_BITS * ADC_SAMPLES_PER_BIT; k += 64) {
    for (uint16_t& x : v) {
      x = analogRead(ADC_PIN);
      lsb(s, SI_ADC, x);
    }
    mbedtls_sha256_update(&ctx, (const uint8_t*)v, sizeof v);
  }
  s.units += BLOCK_BITS * ADC_SAMPLES_PER_BIT;
  s.bits += BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
}

static void add_jit(mbedtls_sha256_context& ctx, EntropyStats& st) {
  SourceStats& s = st.s[SI_JIT];
  int64_t t0 = esp_timer_get_time();
  const int shift = 19 - __builtin_ctz(JIT_PERIODS);   // rtc_clk_cal_ratio: (такты << 19) / периоды
  uint16_t v[64];
  for (uint32_t k = 0; k < BLOCK_BITS * JIT_SAMPLES_PER_BIT; k += 64) {
    for (uint16_t& x : v) {
      x = (uint16_t)(rtc_clk_cal_ratio(RTC_CAL_RTC_MUX, JIT_PERIODS) >> shift);
      lsb(s, SI_JIT, x);
    }
    mbedtls_sha256_update(&ctx, (const uint8_t*)v, sizeof v);
  }
  s.units += BLOCK_BITS * JIT_SAMPLES_PER_BIT;
  s.bits += BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
}

// Эфир сета B: байты услышанных кадров рассеяния. Засчитываем 4 бита на байт —
// в кадре шум агента B, но он мог пройти через функцию рассеяния вместе с числом.
static const uint32_t BC_BITS_PER_BYTE = 4;
static bool s_bc_mixed = false;                      // эфир B не единственный источник блока — не ждать его

static bool add_bc(mbedtls_sha256_context& ctx, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  SourceStats& s = st.s[SI_BC];
  int64_t t0 = esp_timer_get_time();
  uint32_t got = 0;
  uint8_t b[64];
  bool ok = true;
  while (got < BLOCK_BITS) {
    size_t need = (BLOCK_BITS - got + BC_BITS_PER_BYTE - 1) / BC_BITS_PER_BYTE;
    size_t n = bc_take(b, need < sizeof b ? need : sizeof b);
    if (!n) {                                        // эфир молчит — ждём; с другими источниками — берём что есть
      if (s_bc_mixed) break;                         // (07.11: иначе «круг» ждёт соседей, а соседи — его)
      vTaskDelay(pdMS_TO_TICKS(5));
      if (tick) tick();
      if (*abort) { ok = false; break; }
      continue;
    }
    mbedtls_sha256_update(&ctx, b, n);
    for (size_t i = 0; i < n; ++i) lsb(s, SI_BC, b[i]);
    s.units += n;
    got += n * BC_BITS_PER_BYTE;
  }
  s.bits += ok ? BLOCK_BITS : 0;
  s.us += esp_timer_get_time() - t0;
  return ok;
}

// Датчик на ободе в блок. need — блок ждёт 256 зачтённых бит (датчик — единственный источник блока);
// иначе — подмешать накопленное (не больше 256 бит). Датчика нет и ждать нечего — вместо него ГСЧ;
// при раздаче шума колеса (WS) ждём поток с датчиков колеса, ГСЧ не подставляем.
static bool add_sensor(int si, bool need, mbedtls_sha256_context& ctx, EntropyStats& st, volatile bool* abort,
                       void (*tick)()) {
  if (need && !sens_here(si)) {
    add_rng(ctx, st);
    return true;
  }
  SourceStats& s = st.s[si];
  const uint32_t bpb = s_sr[sr_idx(si)].bpb256;        // 1/256 бита на байт: записи целиком — 6 бит на запись
  const int64_t t0 = esp_timer_get_time();
  uint32_t got = 0;                                    // в 1/256 бита
  uint8_t b[64];
  bool ok = true;
  while (got < BLOCK_BITS * 256) {
    const size_t want = (BLOCK_BITS * 256 - got + bpb - 1) / bpb;
    const size_t n = sr_take(si, b, want < sizeof b ? want : sizeof b);
    if (!n) {
      if (!need) break;                              // подмешать — сколько есть
      vTaskDelay(pdMS_TO_TICKS(5));                   // единственный — ждать
      if (tick) tick();
      if (*abort) { ok = false; break; }
      continue;
    }
    mbedtls_sha256_update(&ctx, b, n);
    for (size_t i = 0; i < n; ++i) lsb(s, si, b[i]);
    s.units += n;
    got += n * bpb;
  }
  got /= 256;
  if (ok) s.bits += got < BLOCK_BITS ? got : BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
  return ok;
}

static bool make_block(uint16_t mask, uint32_t rw, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  s_bc_mixed = (mask & SRC_MASK & ~SRC_BC) != 0;
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  bool ok = true;
  if (mask & SRC_RNG) add_rng(ctx, st);
  if (mask & SRC_RRG) add_rrg(ctx, st, rw);
  if (mask & SRC_CSI) ok = add_csi(ctx, st, abort, tick);
  if (ok && (mask & SRC_ADC)) { add_adc(ctx, st); if (tick) tick(); }
  if (ok && (mask & SRC_JIT)) { add_jit(ctx, st); if (tick) tick(); }
  if (ok && (mask & SRC_BC)) ok = add_bc(ctx, st, abort, tick);
  const bool alone = !(mask & (SRC_RNG | SRC_CSI | SRC_ADC | SRC_JIT | SRC_BC | SRC_RRG));   // только датчики
  if (ok && (mask & SRC_ACC)) ok = add_sensor(SI_ACC, alone, ctx, st, abort, tick);
  // 09.11: с акселерометром магнитометр — добавка (кладёт накопленное): блок ждёт только акселерометр, иначе счёт
  // шёл бы по 2 с на блок (магнитометр — 20 замеров в секунду)
  if (ok && (mask & SRC_MAG)) ok = add_sensor(SI_MAG, alone && !(mask & SRC_ACC), ctx, st, abort, tick);
  if (ok) {
    mbedtls_sha256_finish(&ctx, s_pool);
    s_pool_left = sizeof s_pool;
    ++st.sha_blocks;
  }
  mbedtls_sha256_free(&ctx);
  return ok;
}

// ---------- шум без хеша (raw) ----------
// raw = 1: младшие биты сырья — прямо в число (ГСЧ — слова целиком, эфир B — байты целиком):
// в числе остаётся структура источника (перекосы, связь соседних отсчётов), которую хеш размешал бы.
// raw = 2: то же с выравниванием фон Неймана — пара бит 01 → 0, 10 → 1, 00 и 11 — мимо: снимает
// перекос единиц и нулей, но не связь соседних отсчётов. Несколько источников — побитный XOR.
// raw = 3: бит числа — XOR xf бит сырья подряд (лемма о накоплении: перекос e → 2^(xf-1)·e^xf у независимых
// бит); связь соседних при чётном xf сама даёт перекос — сервер это предсказывает (nullmodel.py).
// raw = 4: экстрактор Тёплица — xf·256 бит сырья (как в raw = 1) → 256 бит числа (ниже, tz_block).
static inline void bq_push(int si, uint32_t v, int nbits) {   // младшие nbits из v, младший первым
  BitQ& q = s_rq[si];
  for (int i = 0; i < nbits && q.n < 256; ++i) {
    q.b[(q.r + q.n) & 255] = (v >> i) & 1;
    ++q.n;
  }
}

// Пополнить очередь источника сырьём. false — ожидание прервано (новые настройки).
static bool raw_refill(int si, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  SourceStats& s = st.s[si];
  const int64_t t0 = esp_timer_get_time();
  bool ok = true;
  switch (si) {
    case SI_RNG:
      for (int k = 0; k < 4; ++k) {
        const uint32_t w = esp_random();
        lsb(s, SI_RNG, w);
        bq_push(si, w, 32);
      }
      s.units += 4;
      break;
    case SI_CSI: {
      CsiPkt p;
      while (xQueueReceive(s_q, &p, pdMS_TO_TICKS(100)) != pdTRUE) {
        if (tick) tick();
        if (*abort) { ok = false; break; }
      }
      if (!ok) break;
      // поднесущие с данными: 2…26 и 38…63 (0 — нулевая, 27…37 — защитные, у 1-й чип иногда портит слово)
      for (int sc = 2; sc < 64; ++sc) {
        if (sc > 26 && sc < 38) continue;
        for (int k = 0; k < 2; ++k) {
          const int j = 2 * sc + k;
          const uint8_t v = (uint8_t)p.d[j];
          bq_push(si, v, 1);
          s.ones += v & 1;
          ++s.total;
          s.same += ((v ^ (uint8_t)s_csi_prev[j]) & 1) == 0;
          ++s.pairs;
        }
      }
      memcpy(s_csi_prev, p.d, sizeof s_csi_prev);
      ++s.units;
      if (tick) tick();
      break;
    }
    case SI_ADC:
      if (!s_adc_ready) adc_prepare();
      for (int k = 0; k < 64; ++k) {
        const uint16_t x = analogRead(ADC_PIN);
        lsb(s, SI_ADC, x);
        bq_push(si, x, 1);
      }
      s.units += 64;
      break;
    case SI_JIT: {
      const int shift = 19 - __builtin_ctz(JIT_PERIODS);
      for (int k = 0; k < 64; ++k) {
        const uint16_t x = (uint16_t)(rtc_clk_cal_ratio(RTC_CAL_RTC_MUX, JIT_PERIODS) >> shift);
        lsb(s, SI_JIT, x);
        bq_push(si, x, 1);
      }
      s.units += 64;
      break;
    }
    case SI_RRG:                                     // регистр ГСЧ — слова целиком, как ГСЧ
      for (int k = 0; k < 4; ++k) {
        const uint32_t w = rrg_word();
        lsb(s, SI_RRG, w);
        bq_push(si, w, 32);
      }
      s.units += 4;
      break;
    case SI_ACC:
    case SI_MAG: {
      if (!sens_here(si)) {                          // датчика нет (и потока колеса нет) — вместо него ГСЧ
        const uint32_t w = esp_random();
        lsb(s, si, w);
        bq_push(si, w, 32);
        ++s.units;
        break;
      }
      uint8_t b[16];
      size_t n;
      while (!(n = sr_take(si, b, sizeof b))) {      // ждём показаний
        vTaskDelay(pdMS_TO_TICKS(5));
        if (tick) tick();
        if (*abort) { ok = false; break; }
      }
      if (!ok) break;
      const int bits = (int)SENS_BITS_PER_BYTE[sr_idx(si)];
      for (size_t i = 0; i < n; ++i) {
        lsb(s, si, b[i]);
        bq_push(si, b[i], bits);
      }
      s.units += n;
      break;
    }
    case SI_BC: {
      uint8_t b[16];
      size_t n;
      while (!(n = bc_take(b, sizeof b))) {          // эфир молчит — ждём
        vTaskDelay(pdMS_TO_TICKS(5));
        if (tick) tick();
        if (*abort) { ok = false; break; }
      }
      if (!ok) break;
      for (size_t i = 0; i < n; ++i) {
        lsb(s, SI_BC, b[i]);
        bq_push(si, b[i], 8);
      }
      s.units += n;
      break;
    }
  }
  s.us += esp_timer_get_time() - t0;
  return ok;
}

static bool raw_bit(int si, bool vn, EntropyStats& st, volatile bool* abort, void (*tick)(), uint8_t& bit) {
  uint32_t dropped = 0;                              // пар 00/11 подряд: источник с застывшим младшим битом
  for (;;) {
    BitQ& q = s_rq[si];
    if (q.n < (vn ? 2 : 1)) {
      if (!raw_refill(si, st, abort, tick)) return false;
      continue;
    }
    const uint8_t a = q.b[q.r];
    q.r = (q.r + 1) & 255;
    --q.n;
    if (!vn) { bit = a; return true; }
    const uint8_t b = q.b[q.r];
    q.r = (q.r + 1) & 255;
    --q.n;
    if (a != b) { bit = a; return true; }            // 10 → 1, 01 → 0; 00 и 11 — мимо
    // фон Нейман на источнике без разброса (D0 замкнут на землю или питание) крутился бы вечно,
    // и новая настройка — даже «стоп» — не применялась: раз в 4096 пустых пар слушаем команды
    if ((++dropped & 4095) == 0) {
      if (tick) tick();
      if (*abort) return false;
    }
  }
}

// raw = 3: XOR k бит сырья подряд.
static bool fold_bit(int si, int k, EntropyStats& st, volatile bool* abort, void (*tick)(), uint8_t& bit) {
  uint8_t x = 0;
  for (int i = 0; i < k; ++i) {
    uint8_t b;
    if (!raw_bit(si, false, st, abort, tick, b)) return false;
    x ^= b;
  }
  bit = x;
  return true;
}

static const uint8_t BIT_OF[SI_N] = {SRC_RNG, SRC_CSI, SRC_ADC, SRC_JIT, SRC_BC, SRC_ACC, SRC_MAG, SRC_RRG};

// 08.11: медленные источники (дрожание часов, АЦП) в смеси с быстрым (ГСЧ, регистр, информация) не держат темп:
// у каждого — 64-битное состояние, в которое по одному отсчёту на SLOW_EVERY чисел вплетается новый бит; состояние
// входит в каждое число. Каждое число зависит от всех включённых источников, скорость задают быстрые. В одиночку
// медленный источник идёт честно, бит за битом (raw_bit).
static const uint32_t SLOW_EVERY = 8;
static uint64_t s_slow_state[SI_N];
static bool     s_slow_init[SI_N];
static uint32_t s_slow_n = 0;

static uint8_t slow_sample(int si, EntropyStats& st) {
  SourceStats& s = st.s[si];
  const int64_t t0 = esp_timer_get_time();
  uint16_t x;
  if (si == SI_ADC) {
    if (!s_adc_ready) adc_prepare();
    x = analogRead(ADC_PIN);
  } else {
    x = (uint16_t)(rtc_clk_cal_ratio(RTC_CAL_RTC_MUX, JIT_PERIODS) >> (19 - __builtin_ctz(JIT_PERIODS)));
  }
  lsb(s, si, x);
  ++s.units;
  s.us += esp_timer_get_time() - t0;
  return x & 1;
}

static bool raw_fill(uint8_t* out, size_t n, uint16_t mask, uint8_t raw, uint8_t xf, EntropyStats& st,
                     volatile bool* abort, void (*tick)()) {
  memset(out, 0, n);
  const bool fast = (mask & (SRC_RNG | SRC_RRG)) || ((mask & SRC_BC) && entropy_info_active());
  const bool fresh = (s_slow_n++ % SLOW_EVERY) == 0;
  for (int si = 0; si < SI_N; ++si) {
    if (!(mask & BIT_OF[si])) continue;
    if (fast && (si == SI_JIT || si == SI_ADC)) {     // медленный в смеси: текущее состояние, новый бит — раз в SLOW_EVERY
      uint64_t& z = s_slow_state[si];
      if (!s_slow_init[si]) {
        for (int k = 0; k < 64; ++k) z = (z << 1) | slow_sample(si, st);
        s_slow_init[si] = true;
      } else if (fresh) {
        z = ((z << 1) | (z >> 63)) ^ slow_sample(si, st);
      }
      for (size_t i = 0; i < n; ++i) out[i] ^= (uint8_t)(z >> (8 * (i & 7)));
      st.s[si].bits += n * 8;
      continue;
    }
    if (si == SI_BC && (mask & SRC_MASK & ~SRC_BC)) {   // эфир B вместе с другими источниками: подмешиваем, что есть,
      SourceStats& s = st.s[SI_BC];                    // не ждём (07.11: «круг» ждал байты соседей, соседи — его: 0 простых)
      uint8_t b[64];
      size_t got = 0;
      while (got < n) {
        const size_t k = bc_take(b, (n - got) < sizeof b ? (n - got) : sizeof b);
        if (!k) break;
        for (size_t i = 0; i < k; ++i) { lsb(s, SI_BC, b[i]); out[got + i] ^= b[i]; }
        got += k;
      }
      s.units += got;
      s.bits += got * 8;
      continue;
    }
    for (size_t i = 0; i < n; ++i) {
      uint8_t byte = 0;
      for (int k = 0; k < 8; ++k) {
        uint8_t bit;
        const bool ok = raw == 3 ? fold_bit(si, xf, st, abort, tick, bit) : raw_bit(si, raw == 2, st, abort, tick, bit);
        if (!ok) return false;
        byte |= (uint8_t)(bit << k);
      }
      out[i] ^= byte;
    }
    st.s[si].bits += n * 8;                          // бит этого источника в числе
  }
  return true;
}

// ---------- экстрактор Тёплица (raw = 4) ----------
// y = T·x над GF(2): x — m = xf·256 бит сырья (как в raw = 1: младшие биты, несколько источников — XOR), y — 256
// бит. T — матрица Тёплица 256×m, T[i][j] = s[i - j + m - 1] (по диагоналям постоянна), s — открытое зерно: m + 255
// бит splitmix64 с начальным словом 0x243F6A8885A308D3 (цифры π), бит i — бит (i % 64) слова i / 64. Лемма об
// остатке хеширования: если в m битах сырья не меньше 256 + 2·log2(1/ε) бит мин-энтропии, y отличается от
// равномерного не больше чем на ε — при любом зерне, выбранном независимо от шума. Зерно открыто: любой блок
// можно пересчитать (server/nullmodel.py, toeplitz).
static const int TZ_MAX_M = 16 * BLOCK_BITS;
static uint32_t  s_tz_seed[(TZ_MAX_M + BLOCK_BITS) / 32 + 2];   // + запас: окно читает слово за краем
static bool      s_tz_ready = false;

static uint64_t splitmix64(uint64_t& x) {
  uint64_t z = (x += 0x9E3779B97F4A7C15ULL);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

static void tz_prepare() {
  uint64_t x = 0x243F6A8885A308D3ULL;
  for (size_t i = 0; i + 1 < sizeof s_tz_seed / sizeof s_tz_seed[0]; i += 2) {
    const uint64_t w = splitmix64(x);
    s_tz_seed[i] = (uint32_t)w;
    s_tz_seed[i + 1] = (uint32_t)(w >> 32);
  }
  s_tz_ready = true;
}

static inline void tz_xor_window(uint32_t acc[8], int o) {   // acc ^= s[o … o + 255] — столбец m - 1 - o
  const uint32_t* p = s_tz_seed + (o >> 5);
  const int r = o & 31;
  if (!r) {
    for (int i = 0; i < 8; ++i) acc[i] ^= p[i];
    return;
  }
  for (int i = 0; i < 8; ++i) acc[i] ^= (p[i] >> r) | (p[i + 1] << (32 - r));
}

// Тёплиц по словам: один источник — ГСЧ или регистр ГСЧ. Те же биты в том же порядке, что raw_bit (слово за словом,
// младший бит первым; сначала — остаток очереди), но окно матрицы — только на единичные биты, без очереди по биту.
static void tz_words(int si, int m, uint32_t acc[8], EntropyStats& st) {
  SourceStats& s = st.s[si];
  BitQ& q = s_rq[si];
  int j = 0;
  for (; q.n && j < m; ++j) {                       // остаток очереди от прежнего блока
    const uint8_t a = q.b[q.r];
    q.r = (q.r + 1) & 255;
    --q.n;
    if (a) tz_xor_window(acc, m - 1 - j);
  }
  const int64_t t0 = esp_timer_get_time();
  uint32_t nw = 0;
  while (j < m) {
    uint32_t w = si == SI_RRG ? rrg_word() : esp_random();
    lsb(s, si, w);
    ++nw;
    const int k = m - j < 32 ? m - j : 32;           // слово не целиком — лишние биты в очередь, как у raw_bit
    if (k < 32) bq_push(si, w >> k, 32 - k);
    w = k < 32 ? w & ((1u << k) - 1) : w;
    while (w) {
      const int b = __builtin_ctz(w);
      w &= w - 1;
      tz_xor_window(acc, m - 1 - (j + b));
    }
    j += k;
  }
  s.units += nw;
  s.us += esp_timer_get_time() - t0;
}

static bool tz_block(uint16_t mask, int xf, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  if (!s_tz_ready) tz_prepare();
  const int m = xf * BLOCK_BITS;
  if (mask == SRC_RRG || mask == SRC_RNG) {          // один источник-генератор — по словам
    uint32_t acc[8] = {};
    tz_words(mask == SRC_RRG ? SI_RRG : SI_RNG, m, acc, st);
    memcpy(s_pool, acc, sizeof acc);
    s_pool_left = sizeof s_pool;
    st.s[mask == SRC_RRG ? SI_RRG : SI_RNG].bits += BLOCK_BITS;
    return true;
  }
  uint32_t acc[8] = {};
  for (int j = 0; j < m; ++j) {
    uint8_t x = 0;
    for (int si = 0; si < SI_N; ++si) {
      if (!(mask & BIT_OF[si])) continue;
      uint8_t b;
      if (!raw_bit(si, false, st, abort, tick, b)) return false;
      x ^= b;
    }
    if (x) tz_xor_window(acc, m - 1 - j);
  }
  memcpy(s_pool, acc, sizeof acc);                   // бит i выхода — бит (i % 8) байта i / 8, как в raw = 1
  s_pool_left = sizeof s_pool;
  for (int si = 0; si < SI_N; ++si)
    if (mask & BIT_OF[si]) st.s[si].bits += BLOCK_BITS;
  return true;
}

bool entropy_selftest() {
  // эталон: python server/nullmodel.py — вход — биты splitmix64 с начальным словом 0x0123456789ABCDEF
  static const uint32_t WANT[2][8] = {
    {0x73B35AE1, 0xE8DC34E0, 0xAAD0151A, 0x8FCDE12D, 0x2CE4DC76, 0xEAE8119A, 0x1DC55D9C, 0xC85AFBD5},   // xf = 4
    {0x71BA7801, 0xCD4B63B8, 0xE2D94789, 0x4B97C311, 0x7C6BC13C, 0xFBACDC32, 0xBDADF8AD, 0x2F447590}};  // xf = 16
  if (!s_tz_ready) tz_prepare();
  static const int XF[2] = {4, 16};
  for (int t = 0; t < 2; ++t) {
    const int m = XF[t] * BLOCK_BITS;
    uint32_t acc[8] = {};
    uint64_t x = 0x0123456789ABCDEFULL, w = 0;
    for (int j = 0; j < m; ++j) {
      if ((j & 63) == 0) w = splitmix64(x);
      if ((w >> (j & 63)) & 1) tz_xor_window(acc, m - 1 - j);
    }
    if (memcmp(acc, WANT[t], sizeof acc)) return false;
  }
  return true;
}

// Слова генератора как есть: ГСЧ (esp_random) или регистр ГСЧ напрямую.
static void words_fill(uint8_t* out, size_t n, int si, EntropyStats& st) {
  SourceStats& s = st.s[si];
  uint32_t c0 = cyc();
  for (size_t i = 0; i < n; i += 4) {
    uint32_t w = si == SI_RRG ? rrg_word() : esp_random();
    memcpy(out + i, &w, 4);
    lsb(s, si, w);
  }
  s_rng_cyc_rest += cyc() - c0;
  s.us += s_rng_cyc_rest / s_cpu_mhz;
  s_rng_cyc_rest %= s_cpu_mhz;
  s.units += n / 4;
  s.bits += n * 8;
}

bool entropy_fill(uint8_t* out, size_t n, uint32_t mask, EntropyStats& st,
                  volatile bool* abort, void (*tick)()) {
  const uint8_t raw = (mask >> 8) & 7;               // как брать шум и во сколько сжимать — в старших битах маски
  const uint8_t xf = (mask >> 12) & 31;              // (noise_mask)
  const uint16_t src = mask & SRC_MASK;
  const uint32_t rw = (mask >> 17) & 7 ? (mask >> 17) & 7 : 4;   // слов регистра ГСЧ на бит SHA-256
  s_want = (uint8_t)src;                             // и по команде NS: датчики кладут то, что берёт счёт
  s_hashed = raw == 0;
  if (raw <= 1 && (src == SRC_RNG || (raw == 1 && src == SRC_RRG))) {   // быстрый путь: слова генератора как есть
    words_fill(out, n, src == SRC_RRG ? SI_RRG : SI_RNG, st);
    return true;
  }
  if (raw >= 1 && raw <= 3) return raw_fill(out, n, src, raw, xf, st, abort, tick);   // без хеша
  for (size_t off = 0; off < n;) {                  // блоки SHA-256 (raw 0) или Тёплица (raw 4); остаток — дальше
    if (s_pool_left == 0 && !(raw == 4 ? tz_block(src, xf, st, abort, tick) : make_block(src, rw, st, abort, tick)))
      return false;
    size_t take = min((size_t)s_pool_left, n - off);
    memcpy(out + off, s_pool + (sizeof s_pool - s_pool_left), take);
    s_pool_left -= take;
    off += take;
  }
  return true;
}
