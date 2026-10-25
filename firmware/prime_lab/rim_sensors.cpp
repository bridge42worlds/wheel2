#include "rim_sensors.h"
#include <Wire.h>
#include <icm42688.h>
#include <mlx90393.h>
#include "esp_timer.h"
#include "entropy.h"

namespace rim {

static const int SDA_PIN = 5, SCL_PIN = 6;      // XIAO ESP32-S3: D4 — SDA, D5 — SCL
static const uint8_t MAG_ADDRS[] = {0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x18, 0x19, 0x1A, 0x1B};

static Icm42688     s_imu;
static Mlx90393     s_mag;
static uint8_t      s_present = 0;
static uint8_t      s_imu_cfg[4] = {};
static WheelTracker s_wheel;
static volatile float s_radius = 450.0f;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

// Накопленное для телеметрии (под s_mux): забирается и обнуляется add_imu / add_mag.
struct ImuAcc {
  uint32_t n = 0, revs = 0;
  float    w = 0, temp = 0;
  int      nrev = 0;
  WheelRev rev[NREV];
  bool     frame = false;
  float    axis[3] = {}, radial[3] = {};
  int      ns = 0, head = 0;                    // кольцо последних NS показаний
  int16_t  s[NS][4];                            // к центру, по ходу, вдоль оси, |f| — мg
  int16_t  raw[NS][6];                          // сырые отсчёты ax ay az gx gy gz
};
struct MagAcc {
  uint32_t n = 0;
  double   sum[3] = {}, sumabs = 0;
  float    bmin = 0, bmax = 0, temp = 0;
  int      ns = 0, head = 0;
  int16_t  s[NS][3];                            // мкТл × 10
};
static ImuAcc   s_ia, s_isnap;
static MagAcc   s_ma;
static int64_t  s_imu_take_t = 0;

template <typename T>
static int ring_put(T& acc) {                   // место для нового показания
  const int i = (acc.head + acc.ns) % NS;
  if (acc.ns < NS) ++acc.ns;
  else acc.head = (acc.head + 1) % NS;
  return i;
}

static inline int16_t clamp16(float v) {
  if (v > 32767.0f) return 32767;
  if (v < -32768.0f) return -32768;
  return (int16_t)lroundf(v);
}

static void task(void*) {
  TickType_t last = xTaskGetTickCount();
  int64_t t_prev = esp_timer_get_time();
  float r_used = s_radius;
  for (uint32_t k = 0;; ++k) {
    vTaskDelayUntil(&last, pdMS_TO_TICKS(5));   // 200 Гц
    const int64_t now = esp_timer_get_time();
    if (s_present & HAS_IMU) {
      Icm42688::Sample smp;
      if (s_imu.read(smp)) {
        if (entropy_wants(SI_ACC)) {           // шум с акселерометра выбран: младшие байты шести осей — в запас
          uint8_t lo[6];
          for (int j = 0; j < 6; ++j) lo[j] = (uint8_t)smp.raw[j];
          entropy_feed_sensor(SI_ACC, lo, sizeof lo);
        }
        const float dt = (now - t_prev) / 1e6f;
        t_prev = now;
        if (s_radius != r_used) {
          r_used = s_radius;
          s_wheel.set_radius(r_used);
        }
        WheelRev rev;
        const bool done = s_wheel.add(smp.a, smp.w, dt, rev);
        float wf[4];
        const bool frame = s_wheel.wheel_frame(smp.a, wf);
        float ua[3] = {};
        if (frame) s_wheel.axis_unit(ua);
        portENTER_CRITICAL(&s_mux);
        ++s_ia.n;
        s_ia.w = s_wheel.w_now();
        s_ia.temp = smp.temp;
        s_ia.revs = s_wheel.revs();
        if (done) {                             // последние NREV оборотов
          if (s_ia.nrev == NREV) {
            memmove(s_ia.rev, s_ia.rev + 1, sizeof(WheelRev) * (NREV - 1));
            --s_ia.nrev;
          }
          s_ia.rev[s_ia.nrev++] = rev;
        }
        s_ia.frame = frame;
        if (frame) {
          memcpy(s_ia.axis, ua, sizeof ua);
          memcpy(s_ia.radial, s_wheel.radial(), sizeof s_ia.radial);
        }
        if (k % 10 == 0) {                      // 20 Гц — в пакет
          const int i = ring_put(s_ia);
          for (int j = 0; j < 4; ++j) s_ia.s[i][j] = frame ? clamp16(wf[j] * 1000.0f) : 0;
          if (!frame)
            s_ia.s[i][3] = clamp16(sqrtf(smp.a[0] * smp.a[0] + smp.a[1] * smp.a[1] + smp.a[2] * smp.a[2]) * 1000.0f);
          memcpy(s_ia.raw[i], smp.raw, sizeof smp.raw);
        }
        portEXIT_CRITICAL(&s_mux);
      }
    }
    // MLX90393, 20 Гц: начать, через 30 мс забрать. Замер x, y, z и температуры при OSR 0 и DIG_FILT 5 идёт
    // ~21 мс (на ось 0,067 + 0,192·(2 + 2^5) ≈ 6,6 мс); 21.10.2026 забирали через 10 мс — получали прошлый
    // замер, а около 4 % раз ошибку «не готово» (err в tel рос на ~0,8 в секунду).
    if (s_present & HAS_MAG) {
      const uint32_t ph = k % 10;
      if (ph == 0) {
        s_mag.start();
      } else if (ph == 6) {
        Mlx90393::Sample m;
        if (s_mag.fetch(m)) {
          if (entropy_wants(SI_MAG)) {         // шум с магнитометра выбран: младшие байты трёх осей
            uint8_t lo[3] = {(uint8_t)m.raw[0], (uint8_t)m.raw[1], (uint8_t)m.raw[2]};
            entropy_feed_sensor(SI_MAG, lo, sizeof lo);
          }
          const float a = sqrtf(m.b[0] * m.b[0] + m.b[1] * m.b[1] + m.b[2] * m.b[2]);
          portENTER_CRITICAL(&s_mux);
          if (s_ma.n == 0 || a < s_ma.bmin) s_ma.bmin = a;
          if (s_ma.n == 0 || a > s_ma.bmax) s_ma.bmax = a;
          ++s_ma.n;
          for (int j = 0; j < 3; ++j) s_ma.sum[j] += m.b[j];
          s_ma.sumabs += a;
          s_ma.temp = m.temp;
          const int i = ring_put(s_ma);
          for (int j = 0; j < 3; ++j) s_ma.s[i][j] = clamp16(m.b[j] * 10.0f);
          portEXIT_CRITICAL(&s_mux);
          s_mag.auto_gain(m);                   // следующее измерение — с подобранным усилением
        }
      }
    }
  }
}

// Кто вообще отвечает на шине (до 8 адресов) — в журнал и в tel "i2c": «никто» — питание или провода
// (SDA/SCL перепутаны, CS не на 3V3), «отвечает другой адрес» — подключено верно, датчик не тот или не узнан.
static uint8_t s_i2c[8];
static int     s_i2c_n = 0;

// Чтение WHO_AM_I (0x75) напрямую: коды ошибок шины — для разбора, когда адрес отвечает, а чтение нет
static uint8_t who_at(uint8_t a, int& e, int& n) {
  Wire.beginTransmission(a);
  Wire.write(0x75);
  e = Wire.endTransmission(false);
  n = e == 0 ? Wire.requestFrom(a, (uint8_t)1) : 0;
  return n == 1 ? Wire.read() : 0xff;
}

// Найти ICM-42688 и MLX90393 на шине (на текущей скорости)
static void detect() {
  for (uint8_t a : {(uint8_t)0x68, (uint8_t)0x69})
    if (s_imu.begin(Wire, a)) {
      // ±16 g, ±2000 °/с, 200 Гц, полоса max(400, ODR)/10 = 40 Гц, гироскоп включён
      if (s_imu.configure(Icm42688::A16G, Icm42688::G2000, Icm42688::ODR_200, 4, true)) s_present |= HAS_IMU;
      s_imu.readback(s_imu_cfg);
      Serial.printf("RIM ICM-42688 at 0x%02x who=0x%02x cfg=%02x %02x %02x %02x\n", a, s_imu.who(), s_imu_cfg[0],
                    s_imu_cfg[1], s_imu_cfg[2], s_imu_cfg[3]);
      break;
    }
  for (uint8_t a : MAG_ADDRS)
    if (s_mag.begin(Wire, a)) {
      s_present |= HAS_MAG;
      Serial.printf("RIM MLX90393 at 0x%02x\n", a);
      break;
    }
}

void begin() {
  Wire.begin(SDA_PIN, SCL_PIN, 400000);
  for (uint8_t a = 1; a < 127; ++a) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0 && s_i2c_n < 8) s_i2c[s_i2c_n++] = a;
  }
  Serial.print("RIM i2c:");
  for (int i = 0; i < s_i2c_n; ++i) Serial.printf(" 0x%02x", s_i2c[i]);
  Serial.println(s_i2c_n ? "" : " nothing answers (check 3V3, GND, SDA=D4, SCL=D5, CS=3V3)");
  for (int i = 0; i < s_i2c_n; ++i)                 // кто-то на адресе ICM: читается ли его номер и на какой скорости
    if (s_i2c[i] == 0x68 || s_i2c[i] == 0x69)
      for (uint32_t clk : {400000u, 100000u, 50000u}) {
        Wire.setClock(clk);
        Serial.printf("RIM who 0x%02x @%lu kHz:", s_i2c[i], (unsigned long)(clk / 1000));
        for (int k = 0; k < 3; ++k) {
          int e = 0, n = 0;
          const uint8_t w = who_at(s_i2c[i], e, n);
          Serial.printf(" 0x%02x(e%d n%d)", w, e, n);
        }
        Serial.println();
      }
  // Поиск датчиков: сначала на 400 кГц; не нашлись — на 100 кГц (слабые подтяжки шины: адрес отвечает, чтение нет)
  for (uint32_t clk : {400000u, 100000u}) {
    Wire.setClock(clk);
    detect();
    if (s_present) {
      Serial.printf("RIM bus %lu kHz\n", (unsigned long)(clk / 1000));
      break;
    }
  }
  if (!s_present) {
    for (int i = 0; i < s_i2c_n; ++i)               // точный акселерометр меряет только в роли S
      if (s_i2c[i] == 0x1D || s_i2c[i] == 0x53) Serial.printf("RIM 0x%02x: ADXL355? measures in role S only\n", s_i2c[i]);
    Serial.println("RIM no sensors");
    return;
  }
  entropy_sensor_present(SI_ACC, s_present & HAS_IMU);
  entropy_sensor_present(SI_MAG, s_present & HAS_MAG);
  s_imu_take_t = esp_timer_get_time();
  xTaskCreatePinnedToCore(task, "rim", 4096, nullptr, 1, nullptr, 0);   // ядро связи, ниже протокола
}

void set_radius(float r_mm) { s_radius = r_mm; }

uint8_t present() { return s_present; }

void add_tel(Out& o) {
  add(o, ",\"sens\":%u,\"i2c\":[", s_present);
  for (int i = 0; i < s_i2c_n; ++i) add(o, "%s%u", i ? "," : "", s_i2c[i]);
  add(o, "]");
  if (s_present & HAS_IMU)
    add(o, ",\"imu\":{\"addr\":%u,\"who\":%u,\"cfg\":[%u,%u,%u,%u],\"err\":%lu}", s_imu.addr(), s_imu.who(),
        s_imu_cfg[0], s_imu_cfg[1], s_imu_cfg[2], s_imu_cfg[3], (unsigned long)s_imu.errors());
  if (s_present & HAS_MAG)
    add(o, ",\"mag\":{\"addr\":%u,\"gain\":%u,\"err\":%lu}", s_mag.addr(), s_mag.gain(),
        (unsigned long)s_mag.errors());
}

void add_imu(Out& o) {
  portENTER_CRITICAL(&s_mux);
  s_isnap = s_ia;
  s_ia.n = 0;
  s_ia.nrev = 0;
  s_ia.ns = s_ia.head = 0;
  portEXIT_CRITICAL(&s_mux);
  const int64_t now = esp_timer_get_time();
  const float hz = now > s_imu_take_t ? s_isnap.n * 1e6f / (now - s_imu_take_t) : 0;
  s_imu_take_t = now;
  const ImuAcc& a = s_isnap;
  add(o, ",\"n\":%lu,\"hz\":%.1f,\"w\":%.4f,\"t\":%.1f,\"revs\":%lu,\"r_set\":%.0f,\"err\":%lu",
      (unsigned long)a.n, hz, a.w, a.temp, (unsigned long)a.revs, (double)s_radius, (unsigned long)s_imu.errors());
  add(o, ",\"rev\":[");                          // t, период, об/мин, k, k по радиусу, радиус, |f| верх, низ
  for (int i = 0; i < a.nrev; ++i) {
    const WheelRev& r = a.rev[i];
    add(o, "%s[%.2f,%.4f,%.3f,%.4f,%.4f,%.1f,%.4f,%.4f]", i ? "," : "", r.t, r.period, r.rpm, r.k, r.k_r, r.r_mm,
        r.fmin, r.fmax);
  }
  add(o, "]");
  if (a.frame)
    add(o, ",\"ax\":[%d,%d,%d],\"rd\":[%d,%d,%d]", (int)lroundf(a.axis[0] * 1000), (int)lroundf(a.axis[1] * 1000),
        (int)lroundf(a.axis[2] * 1000), (int)lroundf(a.radial[0] * 1000), (int)lroundf(a.radial[1] * 1000),
        (int)lroundf(a.radial[2] * 1000));
  add(o, ",\"frame\":%d,\"s\":[", a.frame ? 1 : 0);   // к центру, по ходу, вдоль оси, |f| — мg
  for (int i = 0; i < a.ns; ++i) {
    const int16_t* v = a.s[(a.head + i) % NS];
    add(o, "%s[%d,%d,%d,%d]", i ? "," : "", v[0], v[1], v[2], v[3]);
  }
  add(o, "]");
}

void add_imr(Out& o) {                          // из того же снимка, что add_imu
  const ImuAcc& a = s_isnap;
  add(o, ",\"alsb\":%.0f,\"glsb\":%.1f,\"raw\":[", s_imu.accel_lsb(), s_imu.gyro_lsb());
  for (int i = 0; i < a.ns; ++i) {
    const int16_t* v = a.raw[(a.head + i) % NS];
    add(o, "%s[%d,%d,%d,%d,%d,%d]", i ? "," : "", v[0], v[1], v[2], v[3], v[4], v[5]);
  }
  add(o, "]");
}

void add_mag(Out& o) {
  MagAcc m;
  portENTER_CRITICAL(&s_mux);
  m = s_ma;
  s_ma = MagAcc();
  portEXIT_CRITICAL(&s_mux);
  const float n = m.n ? (float)m.n : 1.0f;
  add(o, ",\"n\":%lu,\"gain\":%u,\"err\":%lu,\"b\":[%.2f,%.2f,%.2f],\"babs\":%.2f,\"bmin\":%.2f,\"bmax\":%.2f,\"t\":%.1f",
      (unsigned long)m.n, s_mag.gain(), (unsigned long)s_mag.errors(), m.sum[0] / n, m.sum[1] / n, m.sum[2] / n,
      m.sumabs / n, m.bmin, m.bmax, m.temp);
  add(o, ",\"s\":[");                            // мкТл × 10
  for (int i = 0; i < m.ns; ++i) {
    const int16_t* v = m.s[(m.head + i) % NS];
    add(o, "%s[%d,%d,%d]", i ? "," : "", v[0], v[1], v[2]);
  }
  add(o, "]");
}

}  // namespace rim
