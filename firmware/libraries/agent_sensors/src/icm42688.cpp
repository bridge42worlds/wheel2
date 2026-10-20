#include "icm42688.h"

// Регистры банка 0 (ICM-42688-P, DS-000347).
static const uint8_t R_DEVICE_CONFIG = 0x11;       // [0] SOFT_RESET_CONFIG
static const uint8_t R_FIFO_CONFIG = 0x16;         // [7:6] FIFO_MODE: 01 — поток
static const uint8_t R_TEMP_DATA1 = 0x1D;          // далее TEMP0, ACCEL X1 X0 Y1 Y0 Z1 Z0, GYRO X1 … Z0
static const uint8_t R_INT_STATUS = 0x2D;          // [3] DATA_RDY_INT, [1] FIFO_FULL_INT
static const uint8_t R_FIFO_COUNTH = 0x2E;         // далее FIFO_COUNTL: байт в FIFO
static const uint8_t R_FIFO_DATA = 0x30;
static const uint8_t R_SIGNAL_PATH_RESET = 0x4B;   // [1] FIFO_FLUSH
static const uint8_t R_INTF_CONFIG0 = 0x4C;        // [6] счёт FIFO в записях, [5] и [4] — старший байт первым
static const uint8_t R_PWR_MGMT0 = 0x4E;           // [3:2] GYRO_MODE, [1:0] ACCEL_MODE (11 — малый шум)
static const uint8_t R_GYRO_CONFIG0 = 0x4F;
static const uint8_t R_ACCEL_CONFIG0 = 0x50;
static const uint8_t R_GYRO_ACCEL_CONFIG0 = 0x52;  // [7:4] полоса акселерометра, [3:0] гироскопа
static const uint8_t R_ACCEL_CONFIG1 = 0x53;       // [4:3] порядок фильтра: 00 — 1, 01 — 2, 10 — 3
static const uint8_t R_FIFO_CONFIG1 = 0x5F;        // [0] ускорение, [2] температура
static const uint8_t R_WHO_AM_I = 0x75;
static const uint8_t R_REG_BANK_SEL = 0x76;

bool Icm42688::wr(uint8_t reg, uint8_t v) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  w_->write(v);
  if (w_->endTransmission() != 0) {
    ++err_;
    return false;
  }
  return true;
}

bool Icm42688::rd(uint8_t reg, uint8_t* buf, size_t n) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  if (w_->endTransmission(false) != 0) {
    ++err_;
    return false;
  }
  if (w_->requestFrom((int)addr_, (int)n) != (int)n) {
    ++err_;
    return false;
  }
  for (size_t i = 0; i < n; ++i) buf[i] = (uint8_t)w_->read();
  return true;
}

bool Icm42688::begin(TwoWire& w, uint8_t addr) {
  w_ = &w;
  addr_ = addr;
  uint8_t id = 0;
  if (!rd(R_WHO_AM_I, &id, 1)) return false;
  who_ = id;
  if (id != WHO) return false;
  wr(R_REG_BANK_SEL, 0);
  wr(R_DEVICE_CONFIG, 0x01);          // программный сброс; после него — 1 мс
  delay(2);
  wr(R_REG_BANK_SEL, 0);
  return rd(R_WHO_AM_I, &id, 1) && id == WHO;
}

bool Icm42688::configure(AccelFs afs, GyroFs gfs, Odr odr, uint8_t bw, bool gyro_on) {
  static const float A_LSB[4] = {2048.0f, 4096.0f, 8192.0f, 16384.0f};
  static const float G_LSB[4] = {16.4f, 32.8f, 65.5f, 131.0f};
  a_lsb_ = A_LSB[afs & 3];
  g_lsb_ = G_LSB[gfs & 3];
  bool ok = wr(R_PWR_MGMT0, 0x00);    // настраивать — выключенным
  delayMicroseconds(300);
  ok &= wr(R_GYRO_CONFIG0, (uint8_t)((gfs & 7) << 5 | (odr & 0x0F)));
  ok &= wr(R_ACCEL_CONFIG0, (uint8_t)((afs & 7) << 5 | (odr & 0x0F)));
  ok &= wr(R_GYRO_ACCEL_CONFIG0, (uint8_t)((bw & 0x0F) << 4 | (bw & 0x0F)));
  ok &= wr(R_PWR_MGMT0, gyro_on ? 0x0F : 0x03);   // малый шум; температура включена
  delay(50);                          // гироскоп запускается до 45 мс
  return ok;
}

bool Icm42688::data_ready() {
  uint8_t s = 0;
  return rd(R_INT_STATUS, &s, 1) && (s & 0x08);
}

bool Icm42688::read(Sample& s) {
  uint8_t b[14];
  if (!rd(R_TEMP_DATA1, b, sizeof b)) return false;
  const int16_t t = (int16_t)(b[0] << 8 | b[1]);
  for (int i = 0; i < 6; ++i) s.raw[i] = (int16_t)(b[2 + 2 * i] << 8 | b[3 + 2 * i]);
  const float rad = (float)(PI / 180.0) / g_lsb_;
  for (int i = 0; i < 3; ++i) {
    s.a[i] = s.raw[i] / a_lsb_;
    s.w[i] = s.raw[3 + i] * rad;
  }
  s.temp = t / 132.48f + 25.0f;
  return true;
}

bool Icm42688::read_accel(Sample& s) {
  uint8_t b[8];
  if (!rd(R_TEMP_DATA1, b, sizeof b)) return false;
  const int16_t t = (int16_t)(b[0] << 8 | b[1]);
  for (int i = 0; i < 3; ++i) {
    s.raw[i] = (int16_t)(b[2 + 2 * i] << 8 | b[3 + 2 * i]);
    s.a[i] = s.raw[i] / a_lsb_;
    s.raw[3 + i] = 0;
    s.w[i] = 0;
  }
  s.temp = t / 132.48f + 25.0f;
  return true;
}

void Icm42688::readback(uint8_t out[4]) {
  static const uint8_t regs[4] = {R_PWR_MGMT0, R_GYRO_CONFIG0, R_ACCEL_CONFIG0, R_GYRO_ACCEL_CONFIG0};
  for (int i = 0; i < 4; ++i)
    if (!rd(regs[i], &out[i], 1)) out[i] = 0xFF;
}

bool Icm42688::accel_filter_order(uint8_t ord) {
  uint8_t v = 0;
  if (ord < 1 || ord > 3 || !rd(R_ACCEL_CONFIG1, &v, 1)) return false;
  return wr(R_ACCEL_CONFIG1, (uint8_t)((v & ~0x18) | ((ord - 1) << 3)));
}

bool Icm42688::fifo_begin() {
  uint8_t v = 0;
  bool ok = rd(R_INTF_CONFIG0, &v, 1);
  ok &= wr(R_INTF_CONFIG0, (uint8_t)((v & ~0x40) | 0x30));   // счёт — в байтах, старший байт первым
  ok &= wr(R_FIFO_CONFIG1, 0x05);                             // ускорение и температура: пакет 1
  ok &= wr(R_FIFO_CONFIG, 0x40);                              // поток
  ok &= wr(R_SIGNAL_PATH_RESET, 0x02);                        // выбросить накопленное
  delay(2);
  uint8_t s = 0;
  rd(R_INT_STATUS, &s, 1);                                    // сбросить флаги
  ovf_ = 0;
  return ok;
}

bool Icm42688::fifo_read(int16_t (*a)[3], int8_t* t, int max, int& n) {
  n = 0;
  uint8_t s = 0;
  if (rd(R_INT_STATUS, &s, 1) && (s & 0x02)) ++ovf_;         // FIFO переполнялся — отсчёты пропали
  uint8_t c[2];
  if (!rd(R_FIFO_COUNTH, c, 2)) return false;
  int left = ((c[0] << 8) | c[1]) / 8;                        // целых пакетов
  if (left > max) left = max;
  uint8_t b[15 * 8];                                          // буфер Wire — 128 байт
  while (left > 0) {
    const int k = left > 15 ? 15 : left;
    if (!rd(R_FIFO_DATA, b, k * 8)) return false;
    left -= k;
    for (int i = 0; i < k; ++i) {
      const uint8_t* p = b + 8 * i;
      if (p[0] & 0x80) {                                      // FIFO пуст
        left = 0;
        break;
      }
      if (!(p[0] & 0x40)) continue;                           // в пакете нет ускорения
      for (int j = 0; j < 3; ++j) a[n][j] = (int16_t)(p[1 + 2 * j] << 8 | p[2 + 2 * j]);
      t[n] = (int8_t)p[7];
      ++n;
    }
  }
  return true;
}

void Icm42688::readback_fifo(uint8_t out[3]) {
  static const uint8_t regs[3] = {R_FIFO_CONFIG, R_FIFO_CONFIG1, R_ACCEL_CONFIG1};
  for (int i = 0; i < 3; ++i)
    if (!rd(regs[i], &out[i], 1)) out[i] = 0xFF;
}
