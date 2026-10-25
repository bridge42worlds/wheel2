#include "static_role.h"
#include <Wire.h>
#include <adxl355.h>
#include <icm42688.h>
#include "esp_timer.h"
#include "mbedtls/base64.h"

namespace sacc {

enum : uint8_t { DEV_NONE = 0, DEV_ICM = 1, DEV_ADXL = 2 };

static const int SDA_PIN = 5, SCL_PIN = 6;    // XIAO ESP32-S3: D4 — SDA, D5 — SCL
static const int ODR = 1000;                  // Гц — частота датчика
static const int READ_MS = 10;                // забирать FIFO раз в 10 мс (~10 отсчётов)

static Icm42688     s_imu;
static Adxl355      s_adx;
static uint8_t      s_dev = DEV_NONE;
static bool         s_ok = false;
static uint8_t      s_cfg[4] = {}, s_fifo[3] = {};
static uint8_t      s_afs = 2, s_avg = 2;
static int16_t    (*s_ring)[3] = nullptr;     // отсчёты после усреднения
static uint32_t     s_cap = 0;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
// под s_mux:
static uint32_t     s_head = 0;               // отсчётов (после усреднения) всего
static uint32_t     s_mark_i = 0, s_mark_ms = 0;   // номер отсчёта и время последнего чтения FIFO
static uint32_t     s_cut = 0;                // отсчёты до этого номера — в прежнем режиме: не слать
// пишет только ядро 1:
static volatile uint32_t s_raw = 0, s_bad = 0, s_rderr = 0;
static volatile float    s_temp = 0, s_hz = 0;
// только ядро 0:
static uint32_t     s_sent = 0, s_lost = 0;
static volatile bool s_reconf = false;
static volatile uint8_t s_new_afs = 2, s_new_avg = 2;

static Icm42688::AccelFs afs_code(uint8_t g) {
  return g >= 16 ? Icm42688::A16G : g >= 8 ? Icm42688::A8G : g >= 4 ? Icm42688::A4G : Icm42688::A2G;
}

// Полоса фильтра датчика под усреднение: ниже половины выходной частоты.
// Коды GYRO_ACCEL_CONFIG0 при 1000 Гц: 1 — 250 Гц, 3 — 125, 5 — 62,5, 7 — 25.
static uint8_t bw_code(uint8_t avg) { return avg >= 8 ? 7 : avg >= 4 ? 5 : avg >= 2 ? 3 : 1; }

// ADXL355 усредняет и фильтрует сам: его частота выдачи — сразу 1000/avg (фильтр — четверть её),
// своё усреднение — по 1. Предела ±16 g у него нет — берётся ±8.
static uint8_t s_div = 2;                     // по скольким отсчётам датчика среднее на ядре 1

static bool adx_setup(uint8_t afs, uint8_t avg) {
  const Adxl355::Range r = afs >= 8 ? Adxl355::R8G : afs >= 4 ? Adxl355::R4G : Adxl355::R2G;
  const Adxl355::Odr odr = avg >= 8   ? Adxl355::ODR_125
                           : avg >= 4 ? Adxl355::ODR_250
                           : avg >= 2 ? Adxl355::ODR_500
                                      : Adxl355::ODR_1000;
  const bool ok = s_adx.configure(r, odr);
  s_adx.readback(s_cfg);
  s_afs = afs >= 8 ? 8 : afs;
  s_avg = avg;
  s_div = 1;
  Serial.printf("SACC ADXL355 cfg %s: filter %02x range %02x power %02x status %02x, ±%u g, %d Hz\n", ok ? "ok" : "FAIL",
                s_cfg[0], s_cfg[1], s_cfg[2], s_cfg[3], s_afs, ODR / avg);
  return ok;
}

static bool imu_setup(uint8_t afs, uint8_t avg) {
  if (s_dev == DEV_ADXL) return adx_setup(afs, avg);
  bool ok = s_imu.configure(afs_code(afs), Icm42688::G2000, Icm42688::ODR_1K, bw_code(avg), false);
  ok &= s_imu.accel_filter_order(3);
  ok &= s_imu.fifo_begin();
  s_imu.readback(s_cfg);
  s_imu.readback_fifo(s_fifo);
  s_afs = afs;
  s_avg = avg;
  s_div = avg;
  Serial.printf("SACC cfg %s: %02x %02x %02x %02x fifo %02x %02x %02x, ±%u g, avg %u\n", ok ? "ok" : "FAIL",
                s_cfg[0], s_cfg[1], s_cfg[2], s_cfg[3], s_fifo[0], s_fifo[1], s_fifo[2], afs, avg);
  return ok;
}

static void task(void*) {
  static int16_t a[64][3];
  static int8_t  tt[64];
  int32_t acc[3] = {0, 0, 0};
  int k = 0;
  int64_t t0 = 0;
  uint32_t raw0 = 0;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(READ_MS));
    if (s_reconf) {                          // новый предел или усреднение
      s_reconf = false;
      s_ok = imu_setup(s_new_afs, s_new_avg);
      acc[0] = acc[1] = acc[2] = 0;
      k = 0;
      t0 = 0;
      s_hz = 0;
      portENTER_CRITICAL(&s_mux);            // неотправленное — в прежнем режиме: подписать нечем, выбросить
      s_cut = s_head;
      portEXIT_CRITICAL(&s_mux);
    }
    int n = 0;
    const bool rd_ok = s_dev == DEV_ADXL ? s_adx.fifo_read(a, 64, n) : s_imu.fifo_read(a, tt, 64, n);
    if (!rd_ok) {
      ++s_rderr;
      continue;
    }
    if (!n) continue;
    const int64_t now = esp_timer_get_time();
    if (!t0) {
      t0 = now;
      raw0 = s_raw;
    }
    const int avg = s_div;
    float tsum = 0;
    for (int i = 0; i < n; ++i) {
      if (a[i][0] == -32768 || a[i][1] == -32768 || a[i][2] == -32768) {   // FIFO: отсчёт недействителен
        ++s_bad;
        continue;
      }
      if (s_dev == DEV_ICM) tsum += tt[i];
      for (int j = 0; j < 3; ++j) acc[j] += a[i][j];
      if (++k < avg) continue;
      int16_t v[3];
      for (int j = 0; j < 3; ++j) v[j] = (int16_t)((acc[j] + (acc[j] >= 0 ? avg / 2 : -(avg / 2))) / avg);
      acc[0] = acc[1] = acc[2] = 0;
      k = 0;
      portENTER_CRITICAL(&s_mux);
      memcpy(s_ring[s_head % s_cap], v, sizeof v);
      ++s_head;
      portEXIT_CRITICAL(&s_mux);
    }
    s_raw += n;
    portENTER_CRITICAL(&s_mux);
    s_mark_i = s_head;
    s_mark_ms = millis();
    portEXIT_CRITICAL(&s_mux);
    if (s_dev == DEV_ADXL) {
      float t;
      if (s_adx.read_temp(t)) s_temp = t;
    } else {
      s_temp = tsum / n / 2.07f + 25.0f;
    }
    if (now - t0 > 2000000) s_hz = (s_raw - raw0) * 1e6f / (float)(now - t0) / avg;   // по часам чипа
  }
}

bool begin(uint8_t afs, uint8_t avg) {
  for (uint32_t cap = 12288; cap >= 1024 && !s_ring; cap /= 2) {   // ~25 с при 500 Гц, сколько дадут
    s_ring = (int16_t(*)[3])malloc(sizeof(int16_t) * 3 * cap);
    if (s_ring) s_cap = cap;
  }
  if (!s_ring) {
    Serial.println("SACC no memory");
    return false;
  }
  Wire.begin(SDA_PIN, SCL_PIN, 400000);
  // Точный ADXL355 — первым: стоят оба — меряет он
  for (uint8_t addr : {(uint8_t)0x1D, (uint8_t)0x53})
    if (s_adx.begin(Wire, addr)) {
      Serial.printf("SACC ADXL355 at 0x%02x, ring %lu\n", addr, (unsigned long)s_cap);
      s_dev = DEV_ADXL;
      break;
    }
  if (s_dev == DEV_NONE)
    for (uint8_t addr : {(uint8_t)0x68, (uint8_t)0x69})
      if (s_imu.begin(Wire, addr)) {
        Serial.printf("SACC ICM-42688 at 0x%02x who=0x%02x, ring %lu\n", addr, s_imu.who(), (unsigned long)s_cap);
        s_dev = DEV_ICM;
        break;
      }
  if (s_dev == DEV_NONE) {
    Serial.println("SACC no accelerometer (ADXL355 0x1d/0x53, ICM-42688 0x68/0x69)");
    return false;
  }
  s_ok = imu_setup(afs, avg);
  xTaskCreatePinnedToCore(task, "sacc", 6144, nullptr, 2, nullptr, 1);   // ядро 1 — только измерение
  return s_ok;
}

void set(uint8_t afs, uint8_t avg) {
  if (afs == s_afs && avg == s_avg) return;
  s_new_afs = afs;
  s_new_avg = avg;
  s_reconf = true;
}

bool present() { return s_ok; }

uint32_t pending() {
  portENTER_CRITICAL(&s_mux);
  const uint32_t h = s_head;
  portEXIT_CRITICAL(&s_mux);
  const uint32_t d = h - s_sent;
  return d > s_cap ? s_cap : d;
}

static float lsb() { return s_dev == DEV_ADXL ? s_adx.lsb16() : s_imu.accel_lsb(); }

void add_tel(Out& o) {
  add(o, ",\"sens\":%u,\"dev\":\"%s\"", s_dev != DEV_NONE ? 1 : 0,
      s_dev == DEV_ADXL ? "adxl355" : s_dev == DEV_ICM ? "icm42688" : "");
  if (s_dev == DEV_ADXL)                       // регистры FILTER, RANGE, POWER_CTL, STATUS
    add(o, ",\"adxl\":{\"addr\":%u,\"part\":%u,\"cfg\":[%u,%u,%u,%u],\"err\":%lu,\"ovf\":%lu,\"bad\":%lu}",
        s_adx.addr(), s_adx.partid(), s_cfg[0], s_cfg[1], s_cfg[2], s_cfg[3], (unsigned long)s_adx.errors(),
        (unsigned long)s_adx.overflows(), (unsigned long)s_adx.bad());
  if (s_dev == DEV_ICM)
    add(o, ",\"imu\":{\"addr\":%u,\"who\":%u,\"cfg\":[%u,%u,%u,%u],\"fifo\":[%u,%u,%u],\"err\":%lu,\"ovf\":%lu}",
        s_imu.addr(), s_imu.who(), s_cfg[0], s_cfg[1], s_cfg[2], s_cfg[3], s_fifo[0], s_fifo[1], s_fifo[2],
        (unsigned long)s_imu.errors(), (unsigned long)s_imu.fifo_overflows());
  uint32_t head;
  portENTER_CRITICAL(&s_mux);
  head = s_head;
  portEXIT_CRITICAL(&s_mux);
  add(o, ",\"acc\":{\"ok\":%d,\"odr\":%d,\"avg\":%u,\"afs\":%u,\"lsb\":%.0f,\"hz\":%.3f,\"raw\":%lu,\"out\":%lu,"
         "\"bad\":%lu,\"rderr\":%lu,\"cap\":%lu,\"wait\":%lu,\"lost\":%lu,\"t\":%.1f}",
      s_ok ? 1 : 0, ODR, s_avg, s_afs, lsb(), s_hz, (unsigned long)s_raw, (unsigned long)head,
      (unsigned long)s_bad, (unsigned long)s_rderr, (unsigned long)s_cap, (unsigned long)pending(),
      (unsigned long)s_lost, s_temp);
}

int build_acc(Out& o) {
  static int16_t buf[PER_PACKET][3];
  uint32_t n = 0, i0 = 0, mi = 0, mms = 0;
  portENTER_CRITICAL(&s_mux);
  if (s_head - s_sent > s_cap) {             // молчали дольше, чем держит кольцо: старое затёрто
    s_lost += s_head - s_cap - s_sent;
    s_sent = s_head - s_cap;
  }
  if ((int32_t)(s_cut - s_sent) > 0) {       // сменился режим датчика: прежнее не слать
    s_lost += s_cut - s_sent;
    s_sent = s_cut;
  }
  n = s_head - s_sent;
  if (n > (uint32_t)PER_PACKET) n = PER_PACKET;
  i0 = s_sent;
  for (uint32_t i = 0; i < n; ++i) memcpy(buf[i], s_ring[(i0 + i) % s_cap], sizeof buf[i]);
  mi = s_mark_i;
  mms = s_mark_ms;
  portEXIT_CRITICAL(&s_mux);
  if (!n) return 0;
  // номер первого отсчёта, сколько, частота (по часам чипа), усреднение, предел, отсчётов на g,
  // метка: отсчёт mi прочитан в mms (millis агента) — по ней сервер ставит отсчёты на время
  add(o, ",\"i0\":%lu,\"n\":%lu,\"hz\":%.3f,\"avg\":%u,\"afs\":%u,\"lsb\":%.0f,\"mi\":%lu,\"mms\":%lu,\"t\":%.1f,"
         "\"lost\":%lu,\"b64\":\"",
      (unsigned long)i0, (unsigned long)n, s_hz, s_avg, s_afs, lsb(), (unsigned long)mi,
      (unsigned long)mms, s_temp, (unsigned long)s_lost);
  if (o.over) return 0;
  size_t len = 0;
  if (mbedtls_base64_encode((unsigned char*)o.b + o.n, sizeof o.b - o.n, &len, (const unsigned char*)buf,
                            n * sizeof buf[0]) != 0) {
    o.over = true;
    return 0;
  }
  o.n += (int)len;
  add(o, "\"");
  return o.over ? 0 : (int)n;
}

void commit(int n) { s_sent += (uint32_t)n; }

}  // namespace sacc
