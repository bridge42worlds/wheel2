// entropy_probe — разовый замер источников шума ESP32-S3.
//
// Печатает строки PROBE (скорость и статистика каждого источника) и DUMP
// (сырые 32-битные слова аппаратного генератора, для проверки линейной
// сложности на компьютере: tools/probe_report.py).
// Порядок: всё при выключенном Wi-Fi, затем Wi-Fi включаем и повторяем.
//
// Источники:
//   rng_api — esp_random(): регистр генератора с паузой 1778 тактов шины;
//   rng_raw — тот же регистр подряд, без паузы;
//   adc     — АЦП на свободной ножке D0 (GPIO1): без подтяжек и с подтяжками;
//   jit     — счёт тактов кварца за N периодов внутреннего RC-генератора 136 кГц;
//   temp    — датчик температуры кристалла;
//   csi     — сырые замеры радиоканала Wi-Fi (CSI) по каждому принятому пакету:
//             по паре чисел (мнимая, действительная часть) на поднесущую.

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <agent_common.h>
#include "soc/wdev_reg.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/rtc.h"
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "ping/ping_sock.h"
#if __has_include("version.h")
#include "version.h"
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "entropy_probe"
#endif

const int ADC_PIN = 1;      // D0 = GPIO1 = ADC1_CH0, ничего не подключено
const int N = 4096;
static uint32_t v[N];
static uint16_t hist[8192];

// Статистика выборки: среднее, разброс, доля единиц в младшем бите, доля
// совпадений младшего бита с предыдущим (0.5 — без памяти) и оценка
// минимальной энтропии на отсчёт по самому частому значению (NIST SP 800-90B 6.3.1).
static void report(const char* name, const char* phase, double rate) {
  double sum = 0, sq = 0;
  uint32_t mn = UINT32_MAX, mx = 0, ones = 0, eq = 0, pmax = 0, distinct = 0;
  memset(hist, 0, sizeof hist);
  for (int i = 0; i < N; ++i) {
    uint32_t x = v[i];
    sum += x;
    sq += (double)x * x;
    if (x < mn) mn = x;
    if (x > mx) mx = x;
    ones += x & 1;
    if (i && ((x ^ v[i - 1]) & 1) == 0) ++eq;
    ++hist[x & 8191];
  }
  for (int i = 0; i < 8192; ++i)
    if (hist[i]) { ++distinct; if (hist[i] > pmax) pmax = hist[i]; }
  double mean = sum / N, sd = sqrt(fmax(0.0, sq / N - mean * mean));
  double p = (double)pmax / N, pu = fmin(1.0, p + 2.576 * sqrt(p * (1 - p) / (N - 1)));
  Serial.printf("PROBE %s phase=%s rate=%.0f mean=%.2f sd=%.3f min=%lu max=%lu lsb1=%.3f lag1eq=%.3f "
                "hmin=%.2f distinct=%lu\n", name, phase, rate, mean, sd, (unsigned long)mn,
                (unsigned long)mx, (double)ones / N, (double)eq / (N - 1), -log2(pu), (unsigned long)distinct);
}

// Для 32-битных слов генератора: доля единиц по всем битам.
static void report_words(const char* name, const char* phase, double rate) {
  uint64_t ones = 0;
  uint32_t same = 0;
  for (int i = 0; i < N; ++i) {
    ones += __builtin_popcount(v[i]);
    if (i && v[i] == v[i - 1]) ++same;
  }
  Serial.printf("PROBE %s phase=%s rate=%.0f ones=%.4f repeats=%lu\n", name, phase, rate,
                (double)ones / (32.0 * N), (unsigned long)same);
}

static void dump(const char* tag) {
  for (int i = 0; i < N; i += 8) {
    Serial.printf("DUMP %s %d", tag, i);
    for (int j = 0; j < 8; ++j) Serial.printf(" %08lx", (unsigned long)v[i + j]);
    Serial.print('\n');
  }
}

static void flags(const char* phase) {
  uint32_t clk = REG_READ(RTC_CNTL_CLK_CONF_REG);
  wifi_mode_t m = WIFI_MODE_NULL;
  wifi_ps_type_t ps = WIFI_PS_NONE;
  bool wifi = esp_wifi_get_mode(&m) == ESP_OK && m != WIFI_MODE_NULL;
  if (wifi) esp_wifi_get_ps(&ps);
  Serial.printf("FLAGS phase=%s wifi=%d ps=%d dig_clk8m=%d dig_clk8m_d256=%d enb_ck8m=%d clk_conf=%08lx\n",
                phase, wifi, (int)ps, (clk & RTC_CNTL_DIG_CLK8M_EN) ? 1 : 0,
                (clk & RTC_CNTL_DIG_CLK8M_D256_EN) ? 1 : 0, (clk & RTC_CNTL_ENB_CK8M) ? 1 : 0,
                (unsigned long)clk);
}

static void probe_rng(const char* phase) {
  char tag[24];
  uint32_t t0 = micros();
  for (int i = 0; i < N; ++i) v[i] = esp_random();
  double rate = N * 1e6 / (micros() - t0);
  report_words("rng_api", phase, rate);
  snprintf(tag, sizeof tag, "api_%s", phase);
  dump(tag);

  t0 = micros();
  for (int i = 0; i < N; ++i) v[i] = REG_READ(WDEV_RND_REG);
  rate = N * 1e6 / (micros() - t0);
  report_words("rng_raw", phase, rate);
  snprintf(tag, sizeof tag, "raw_%s", phase);
  dump(tag);
}

// То же, но с отключённой подачей RC_FAST в цифровую часть (бит DIG_CLK8M_EN):
// по TRM генератор подмешивает дрожание RC_FAST, если она включена. Без неё и
// без радио остаётся только внутренний генератор — проверяем, не линейный ли он.
static void probe_rng_noclk(const char* phase) {
  char ph[24];
  snprintf(ph, sizeof ph, "%s_noclk", phase);
  REG_CLR_BIT(RTC_CNTL_CLK_CONF_REG, RTC_CNTL_DIG_CLK8M_EN);
  delay(5);
  flags(ph);
  probe_rng(ph);
  REG_SET_BIT(RTC_CNTL_CLK_CONF_REG, RTC_CNTL_DIG_CLK8M_EN);
  delay(5);
}

static void probe_adc(const char* phase, const char* mode) {
  analogRead(ADC_PIN);   // первая выборка настраивает канал
  if (!strcmp(mode, "pull_gpio")) { gpio_pullup_en((gpio_num_t)ADC_PIN); gpio_pulldown_en((gpio_num_t)ADC_PIN); }
  if (!strcmp(mode, "pull_rtc"))  { rtc_gpio_pullup_en((gpio_num_t)ADC_PIN); rtc_gpio_pulldown_en((gpio_num_t)ADC_PIN); }
  delay(20);
  uint32_t t0 = micros();
  for (int i = 0; i < N; ++i) v[i] = analogRead(ADC_PIN);
  double rate = N * 1e6 / (micros() - t0);
  char name[32];
  snprintf(name, sizeof name, "adc_%s", mode);
  report(name, phase, rate);
  if (!strcmp(mode, "pull_gpio")) dump("adc_pull");   // отсчёты для разбора на компьютере
  gpio_pullup_dis((gpio_num_t)ADC_PIN);
  gpio_pulldown_dis((gpio_num_t)ADC_PIN);
  rtc_gpio_pullup_dis((gpio_num_t)ADC_PIN);
  rtc_gpio_pulldown_dis((gpio_num_t)ADC_PIN);
}

// rtc_clk_cal_ratio возвращает (тактов кварца << 19) / periods.
static void probe_jit(const char* phase, uint32_t periods) {
  int shift = 19 - __builtin_ctz(periods);   // periods — степень двойки
  uint32_t t0 = micros();
  for (int i = 0; i < N; ++i) v[i] = rtc_clk_cal_ratio(RTC_CAL_RTC_MUX, periods) >> shift;
  double rate = N * 1e6 / (micros() - t0);
  char name[24];
  snprintf(name, sizeof name, "jit_%lu", (unsigned long)periods);
  report(name, phase, rate);
  if (periods == 8 && !strcmp(phase, "off")) dump("jit8");
}

static void probe_temp(const char* phase) {
  const int n = 256;
  uint32_t t0 = micros();
  for (int i = 0; i < n; ++i) v[i] = (uint32_t)lroundf(agent::chip_temp() * 100.0f);
  double rate = n * 1e6 / (micros() - t0);
  for (int i = n; i < N; ++i) v[i] = v[i % n];   // report() считает по N отсчётам
  report("temp_x100", phase, rate);
}

// ---------- CSI: шум Wi-Fi напрямую ----------
struct CsiRec { uint32_t t; uint8_t mac[6]; int8_t rssi, nf; uint16_t len; uint8_t inv; int8_t d[128]; };
const int CSI_MAX = 300;
static CsiRec csi[CSI_MAX];
static volatile int csi_n = 0;
static volatile uint32_t csi_total = 0;

static void csi_cb(void*, wifi_csi_info_t* info) {   // вызывается из задачи Wi-Fi
  ++csi_total;
  int i = csi_n;
  if (i >= CSI_MAX || !info || !info->buf) return;
  CsiRec& r = csi[i];
  r.t = info->rx_ctrl.timestamp;
  memcpy(r.mac, info->mac, 6);
  r.rssi = info->rx_ctrl.rssi;
  r.nf = info->rx_ctrl.noise_floor;
  r.len = info->len;
  r.inv = info->first_word_invalid;
  int n = info->len < 128 ? info->len : 128;   // первые 128 байт — LLTF, 64 поднесущие
  memcpy(r.d, info->buf, n);
  if (n < 128) memset(r.d + n, 0, 128 - n);
  csi_n = i + 1;
}

static volatile uint32_t ping_ok = 0;
static void on_ping_ok(esp_ping_handle_t, void*) { ++ping_ok; }

// prom — слушать весь эфир; ping — 100 раз в секунду пинговать роутер, чтобы
// шёл ровный поток ответов от одного отправителя.
static void probe_csi(bool prom, bool ping) {
  csi_n = 0;
  csi_total = 0;
  ping_ok = 0;
  esp_ping_handle_t ph = nullptr;
  if (ping) {
    IPAddress gw = WiFi.gatewayIP();
    esp_ping_config_t pc = ESP_PING_DEFAULT_CONFIG();
    IP_ADDR4(&pc.target_addr, gw[0], gw[1], gw[2], gw[3]);
    pc.count = ESP_PING_COUNT_INFINITE;
    pc.interval_ms = 10;
    pc.timeout_ms = 500;
    esp_ping_callbacks_t cb = {};
    cb.on_ping_success = on_ping_ok;
    if (esp_ping_new_session(&pc, &cb, &ph) == ESP_OK) esp_ping_start(ph);
  }
  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = true;
  cfg.stbc_htltf2_en = true;
  cfg.ltf_merge_en = true;
  cfg.channel_filter_en = false;   // без сглаживания: соседние поднесущие независимы
  cfg.manu_scale = false;
  esp_err_t e1 = esp_wifi_set_csi_config(&cfg);
  esp_err_t e2 = esp_wifi_set_csi_rx_cb(csi_cb, nullptr);
  if (prom) {
    wifi_promiscuous_filter_t f = {};
    f.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL;
    esp_wifi_set_promiscuous_filter(&f);
    esp_wifi_set_promiscuous(true);
  }
  esp_err_t e3 = esp_wifi_set_csi(true);
  uint32_t t0 = millis();
  delay(4000);
  esp_wifi_set_csi(false);
  if (prom) esp_wifi_set_promiscuous(false);
  if (ph) { esp_ping_stop(ph); esp_ping_delete_session(ph); }
  double secs = (millis() - t0) / 1000.0;
  Serial.printf("PROBE csi prom=%d ping=%d pong=%lu err=%d/%d/%d packets=%lu rate=%.1f stored=%d\n", prom, ping, (unsigned long)ping_ok, e1, e2, e3,
                (unsigned long)csi_total, csi_total / secs, csi_n);
  for (int i = 0; i < csi_n; ++i) {
    const CsiRec& r = csi[i];
    Serial.printf("CSI %d %d %lu %02x%02x%02x%02x%02x%02x %d %d %u %u ", prom | (ping << 1), i, (unsigned long)r.t,
                  r.mac[0], r.mac[1], r.mac[2], r.mac[3], r.mac[4], r.mac[5], r.rssi, r.nf, r.len, r.inv);
    for (int j = 0; j < 128; ++j) Serial.printf("%02x", (uint8_t)r.d[j]);
    Serial.print('\n');
  }
}

static void run_phase(const char* phase, bool full) {
  flags(phase);
  probe_rng(phase);
  probe_rng_noclk(phase);
  probe_adc(phase, "float");
  if (full) {
    probe_adc(phase, "pull_gpio");
    probe_adc(phase, "pull_rtc");
  }
  for (uint32_t p : {1u, 8u, 32u}) probe_jit(phase, p);
  probe_temp(phase);
}

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);
  agent::identity_init();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d\n", FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id());
  analogReadResolution(12);
  analogSetPinAttenuation(ADC_PIN, ADC_11db);

  run_phase("off", true);

  WiFi.mode(WIFI_STA);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
  WiFi.begin(agent::wifi_ssid(), agent::wifi_pass());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(100);
  WiFi.setSleep(false);
  Serial.printf("WIFI %s rssi=%d\n", WiFi.status() == WL_CONNECTED ? "ok" : "fail", WiFi.RSSI());
  delay(500);

  run_phase("on", false);
  probe_csi(false, false);
  probe_csi(true, false);
  probe_csi(false, true);
  Serial.println("PROBE done");
}

void loop() {
  agent::led_set(true);
  delay(100);
  agent::led_set(false);
  delay(1900);
}
