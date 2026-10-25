#include "entropy.h"
#include <WiFi.h>
#include <esp_wifi.h>
#include "esp_cpu.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "soc/rtc.h"
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

static size_t bc_take(uint8_t* out, size_t want) {
  portENTER_CRITICAL(&s_bc_mux);
  size_t n = s_bc_w - s_bc_r;
  if (n > want) n = want;
  for (size_t i = 0; i < n; ++i) out[i] = s_bc[s_bc_r++ % sizeof s_bc];
  portEXIT_CRITICAL(&s_bc_mux);
  return n;
}

// ---------- датчики на ободе: запас младших байт показаний (пишет задача датчиков, читает ядро 1) ----------
struct SensRing {
  uint8_t           b[1024];
  volatile uint32_t w = 0, r = 0, lost = 0;
  volatile bool     present = false;
};
static SensRing     s_sr[2];                         // 0 — акселерометр, 1 — магнитометр
static portMUX_TYPE s_sr_mux = portMUX_INITIALIZER_UNLOCKED;
static const uint32_t SENS_BITS_PER_BYTE[2] = {1, 2};   // зачёт: ICM — 1 бит на байт, MLX — 2

static inline int sr_idx(int si) { return si == SI_MAG ? 1 : 0; }

void entropy_sensor_present(int si, bool present) { s_sr[sr_idx(si)].present = present; }

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

static bool add_bc(mbedtls_sha256_context& ctx, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  SourceStats& s = st.s[SI_BC];
  int64_t t0 = esp_timer_get_time();
  uint32_t got = 0;
  uint8_t b[64];
  bool ok = true;
  while (got < BLOCK_BITS) {
    size_t need = (BLOCK_BITS - got + BC_BITS_PER_BYTE - 1) / BC_BITS_PER_BYTE;
    size_t n = bc_take(b, need < sizeof b ? need : sizeof b);
    if (!n) {                                        // эфир молчит — ждём
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
// иначе — подмешать накопленное (не больше 256 бит). Датчика нет и ждать нечего — вместо него ГСЧ.
static bool add_sensor(int si, bool need, mbedtls_sha256_context& ctx, EntropyStats& st, volatile bool* abort,
                       void (*tick)()) {
  if (need && !s_sr[sr_idx(si)].present) {
    add_rng(ctx, st);
    return true;
  }
  SourceStats& s = st.s[si];
  const uint32_t bpb = SENS_BITS_PER_BYTE[sr_idx(si)];
  const int64_t t0 = esp_timer_get_time();
  uint32_t got = 0;
  uint8_t b[64];
  bool ok = true;
  while (got < BLOCK_BITS) {
    const size_t want = (BLOCK_BITS - got + bpb - 1) / bpb;
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
  if (ok) s.bits += got < BLOCK_BITS ? got : BLOCK_BITS;
  s.us += esp_timer_get_time() - t0;
  return ok;
}

static bool make_block(uint16_t mask, EntropyStats& st, volatile bool* abort, void (*tick)()) {
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  bool ok = true;
  if (mask & SRC_RNG) add_rng(ctx, st);
  if (mask & SRC_CSI) ok = add_csi(ctx, st, abort, tick);
  if (ok && (mask & SRC_ADC)) { add_adc(ctx, st); if (tick) tick(); }
  if (ok && (mask & SRC_JIT)) { add_jit(ctx, st); if (tick) tick(); }
  if (ok && (mask & SRC_BC)) ok = add_bc(ctx, st, abort, tick);
  const bool alone = !(mask & (SRC_RNG | SRC_CSI | SRC_ADC | SRC_JIT | SRC_BC));   // только датчики
  if (ok && (mask & SRC_ACC)) ok = add_sensor(SI_ACC, alone, ctx, st, abort, tick);
  if (ok && (mask & SRC_MAG)) ok = add_sensor(SI_MAG, alone, ctx, st, abort, tick);
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
    case SI_ACC:
    case SI_MAG: {
      if (!s_sr[sr_idx(si)].present) {               // датчика нет — вместо него ГСЧ
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

static bool raw_fill(uint8_t* out, size_t n, uint16_t mask, bool vn, EntropyStats& st,
                     volatile bool* abort, void (*tick)()) {
  static const uint8_t BIT_OF[SI_N] = {SRC_RNG, SRC_CSI, SRC_ADC, SRC_JIT, SRC_BC, SRC_ACC, SRC_MAG};
  memset(out, 0, n);
  for (int si = 0; si < SI_N; ++si) {
    if (!(mask & BIT_OF[si])) continue;
    for (size_t i = 0; i < n; ++i) {
      uint8_t byte = 0;
      for (int k = 0; k < 8; ++k) {
        uint8_t bit;
        if (!raw_bit(si, vn, st, abort, tick, bit)) return false;
        byte |= (uint8_t)(bit << k);
      }
      out[i] ^= byte;
    }
    st.s[si].bits += n * 8;                          // бит этого источника в числе
  }
  return true;
}

bool entropy_fill(uint8_t* out, size_t n, uint16_t mask, EntropyStats& st,
                  volatile bool* abort, void (*tick)()) {
  const uint8_t raw = (mask >> 8) & 3;               // как брать шум — в старших битах маски (noise_mask)
  mask &= SRC_MASK;
  if (raw && !(raw == 1 && mask == SRC_RNG))         // без хеша (один ГСЧ без выравнивания — и так как есть)
    return raw_fill(out, n, mask, raw == 2, st, abort, tick);
  if (mask == SRC_RNG) {                 // быстрый путь: слова генератора как есть
    SourceStats& s = st.s[SI_RNG];
    uint32_t c0 = cyc();
    for (size_t i = 0; i < n; i += 4) {
      uint32_t w = esp_random();
      memcpy(out + i, &w, 4);
      lsb(s, SI_RNG, w);
    }
    s_rng_cyc_rest += cyc() - c0;
    s.us += s_rng_cyc_rest / s_cpu_mhz;
    s_rng_cyc_rest %= s_cpu_mhz;
    s.units += n / 4;
    s.bits += n * 8;
    return true;
  }
  for (size_t off = 0; off < n;) {
    if (s_pool_left == 0 && !make_block(mask, st, abort, tick)) return false;
    size_t take = min((size_t)s_pool_left, n - off);
    memcpy(out + off, s_pool + (sizeof s_pool - s_pool_left), take);
    s_pool_left -= take;
    off += take;
  }
  return true;
}
