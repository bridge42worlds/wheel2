#include "icm20948.h"

// Регистры ICM-20948 (DS-000189). Банк выбирается регистром 0x7F: номер банка в битах [5:4].
static const uint8_t R_BANK_SEL = 0x7F;
// --- банк 0 ---
static const uint8_t R_WHO_AM_I = 0x00;
static const uint8_t R_USER_CTRL = 0x03;        // [6] FIFO_EN, [5] I2C_MST_EN
static const uint8_t R_PWR_MGMT_1 = 0x06;       // [7] сброс, [6] сон, [2:0] тактирование (1 — лучшее из доступных)
static const uint8_t R_PWR_MGMT_2 = 0x07;       // 0 — ускорение и вращение включены
static const uint8_t R_INT_PIN_CFG = 0x0F;      // [1] BYPASS_EN: магнитометр виден на внешней шине
static const uint8_t R_ACCEL_XOUT_H = 0x2D;     // ax ay az gx gy gz температура — 14 байт, старшим вперёд
static const uint8_t R_FIFO_EN_2 = 0x67;        // [4] ускорение, [3:1] вращение z y x, [0] температура
static const uint8_t R_FIFO_RST = 0x68;         // записать 0x1F, затем 0 — буфер пуст
static const uint8_t R_FIFO_MODE = 0x69;        // 0 — по кругу, 0x1F — до заполнения
static const uint8_t R_FIFO_COUNTH = 0x70;      // далее FIFO_COUNTL
static const uint8_t R_FIFO_R_W = 0x72;
// --- банк 2 ---
static const uint8_t R2_GYRO_SMPLRT_DIV = 0x00;
static const uint8_t R2_GYRO_CONFIG_1 = 0x01;   // [5:3] полоса, [2:1] предел, [0] фильтр включён
static const uint8_t R2_ODR_ALIGN_EN = 0x09;    // [0] записи ускорения и вращения в один момент
static const uint8_t R2_ACCEL_SMPLRT_DIV_1 = 0x10;   // старшие 4 бита делителя, далее _2 — младшие 8
static const uint8_t R2_ACCEL_CONFIG = 0x14;    // [5:3] полоса, [2:1] предел, [0] фильтр включён
// --- AK09916 ---
static const uint8_t M_WIA2 = 0x01;
static const uint8_t M_ST1 = 0x10;              // [0] DRDY; далее HXL HXH HYL HYH HZL HZH, пусто, ST2
static const uint8_t M_CNTL2 = 0x31;            // 0x08 — непрерывно, 100 замеров в секунду
static const uint8_t M_CNTL3 = 0x32;            // [0] сброс

static const int CHUNK = 120;                   // байт за одно чтение буфера: 10 записей (буфер Wire — 128)

bool Icm20948::wr(uint8_t reg, uint8_t v) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  w_->write(v);
  if (w_->endTransmission() != 0) {
    ++err_;
    return false;
  }
  return true;
}

bool Icm20948::rd(uint8_t reg, uint8_t* buf, size_t n) {
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

bool Icm20948::mwr(uint8_t reg, uint8_t v) {
  w_->beginTransmission(MAG_ADDR);
  w_->write(reg);
  w_->write(v);
  if (w_->endTransmission() != 0) {
    ++err_;
    return false;
  }
  return true;
}

bool Icm20948::mrd(uint8_t reg, uint8_t* buf, size_t n) {
  w_->beginTransmission(MAG_ADDR);
  w_->write(reg);
  if (w_->endTransmission(false) != 0) {
    ++err_;
    return false;
  }
  if (w_->requestFrom((int)MAG_ADDR, (int)n) != (int)n) {
    ++err_;
    return false;
  }
  for (size_t i = 0; i < n; ++i) buf[i] = (uint8_t)w_->read();
  return true;
}

bool Icm20948::bank(uint8_t b) {
  if (bank_ == b) return true;
  if (!wr(R_BANK_SEL, (uint8_t)(b << 4))) {
    bank_ = 0xff;
    return false;
  }
  bank_ = b;
  return true;
}

bool Icm20948::begin(TwoWire& w, uint8_t addr) {
  w_ = &w;
  addr_ = addr;
  bank_ = 0xff;
  mag_ = false;
  uint8_t id = 0;
  if (!bank(0) || !rd(R_WHO_AM_I, &id, 1)) return false;
  who_ = id;
  if (id != WHO) return false;
  wr(R_PWR_MGMT_1, 0x80);               // сброс: регистры в исходное, датчик засыпает
  delay(100);
  bank_ = 0xff;
  if (!bank(0)) return false;
  if (!wr(R_PWR_MGMT_1, 0x01)) return false;   // проснуться, тактирование — лучшее из доступных
  delay(30);
  wr(R_PWR_MGMT_2, 0x00);               // ускорение и вращение включены
  return rd(R_WHO_AM_I, &id, 1) && id == WHO;
}

bool Icm20948::configure(AccelFs afs, GyroFs gfs, uint8_t div, uint8_t dlpf) {
  static const float A_LSB[4] = {16384.0f, 8192.0f, 4096.0f, 2048.0f};
  static const float G_LSB[4] = {131.0f, 65.5f, 32.8f, 16.4f};
  dlpf &= 7;
  bool ok = bank(2);
  ok = ok && wr(R2_GYRO_SMPLRT_DIV, div);
  ok = ok && wr(R2_GYRO_CONFIG_1, (uint8_t)(dlpf << 3 | gfs << 1 | 1));
  ok = ok && wr(R2_ACCEL_SMPLRT_DIV_1, 0);
  ok = ok && wr(R2_ACCEL_SMPLRT_DIV_1 + 1, div);
  ok = ok && wr(R2_ACCEL_CONFIG, (uint8_t)(dlpf << 3 | afs << 1 | 1));
  ok = ok && wr(R2_ODR_ALIGN_EN, 1);
  ok = bank(0) && ok;
  if (ok) {
    a_lsb_ = A_LSB[afs];
    g_lsb_ = G_LSB[gfs];
  }
  return ok;
}

bool Icm20948::read(Sample& s) {
  uint8_t b[14];
  if (!bank(0) || !rd(R_ACCEL_XOUT_H, b, sizeof b)) return false;
  for (int i = 0; i < 6; ++i) s.raw[i] = (int16_t)((uint16_t)b[2 * i] << 8 | b[2 * i + 1]);
  for (int i = 0; i < 3; ++i) {
    s.a[i] = s.raw[i] / a_lsb_;
    s.w[i] = s.raw[3 + i] / g_lsb_ * (float)(PI / 180.0);
  }
  const int16_t t = (int16_t)((uint16_t)b[12] << 8 | b[13]);
  s.temp = t / 333.87f + 21.0f;
  return true;
}

bool Icm20948::read_accel(int16_t a[3]) {
  uint8_t b[6];
  if (!bank(0) || !rd(R_ACCEL_XOUT_H, b, sizeof b)) return false;
  for (int i = 0; i < 3; ++i) a[i] = (int16_t)((uint16_t)b[2 * i] << 8 | b[2 * i + 1]);
  return true;
}

void Icm20948::readback(uint8_t out[6]) {
  memset(out, 0, 6);
  if (!bank(0)) return;
  rd(R_PWR_MGMT_1, &out[0], 1);
  rd(R_FIFO_EN_2, &out[4], 1);
  rd(R_FIFO_MODE, &out[5], 1);
  if (bank(2)) {
    rd(R2_GYRO_SMPLRT_DIV, &out[1], 1);
    rd(R2_GYRO_CONFIG_1, &out[2], 1);
    rd(R2_ACCEL_CONFIG, &out[3], 1);
  }
  bank(0);
}

bool Icm20948::fifo_begin(bool stream) {
  if (!bank(0)) return false;
  uint8_t uc = 0;
  if (!rd(R_USER_CTRL, &uc, 1)) return false;
  bool ok = wr(R_FIFO_EN_2, 0x00);                     // пока настраиваем — ничего не писать
  ok = ok && wr(R_FIFO_MODE, stream ? 0x00 : 0x1F);
  ok = ok && wr(R_USER_CTRL, (uint8_t)(uc | 0x40));    // буфер включён
  ok = ok && fifo_reset();
  ok = ok && wr(R_FIFO_EN_2, 0x1E);                    // ускорение и три оси вращения
  return ok;
}

bool Icm20948::fifo_reset() {
  return bank(0) && wr(R_FIFO_RST, 0x1F) && wr(R_FIFO_RST, 0x00);
}

int Icm20948::fifo_count() {
  uint8_t b[2];
  if (!bank(0) || !rd(R_FIFO_COUNTH, b, 2)) return -1;
  return (int)((uint16_t)(b[0] & 0x1F) << 8 | b[1]);
}

bool Icm20948::fifo_read(int16_t (*rec)[6], int max, int& n) {
  n = 0;
  const int cnt = fifo_count();
  if (cnt < 0) return false;
  int left = cnt / REC;
  if (left > max) left = max;
  uint8_t b[CHUNK];
  while (left > 0) {
    const int k = left * REC > CHUNK ? CHUNK / REC : left;
    if (!rd(R_FIFO_R_W, b, (size_t)(k * REC))) return false;
    for (int i = 0; i < k; ++i)
      for (int j = 0; j < 6; ++j)
        rec[n + i][j] = (int16_t)((uint16_t)b[i * REC + 2 * j] << 8 | b[i * REC + 2 * j + 1]);
    n += k;
    left -= k;
  }
  return true;
}

int Icm20948::fifo_measure(uint32_t wait_ms) {
  if (!fifo_reset()) return 0;
  int last = -1, same = 0;
  const uint32_t t0 = millis();
  while (millis() - t0 < wait_ms) {
    delay(20);
    const int c = fifo_count();
    if (c < 0) return 0;
    if (c == last) {
      if (++same >= 3) break;           // три раза подряд одно число — буфер полон
    } else {
      same = 0;
      last = c;
    }
  }
  fifo_reset();
  return last < 0 ? 0 : last;
}

bool Icm20948::mag_begin() {
  mag_ = false;
  if (!bank(0)) return false;
  uint8_t uc = 0;
  if (!rd(R_USER_CTRL, &uc, 1)) return false;
  if (!wr(R_USER_CTRL, (uint8_t)(uc & ~0x20))) return false;   // внутренний мастер выключен
  if (!wr(R_INT_PIN_CFG, 0x02)) return false;                  // режим «мимо»
  delay(2);
  uint8_t id = 0;
  if (!mrd(M_WIA2, &id, 1) || id != MAG_WHO) return false;
  mwr(M_CNTL3, 0x01);                   // сброс
  delay(2);
  if (!mwr(M_CNTL2, 0x08)) return false;   // непрерывно, 100 замеров в секунду
  mag_ = true;
  return true;
}

bool Icm20948::mag_read(Mag& m) {
  if (!mag_) return false;
  uint8_t b[9];                         // ST1, шесть байт осей (младшим вперёд), пусто, ST2
  if (!mrd(M_ST1, b, sizeof b)) return false;
  if (!(b[0] & 0x01)) return false;     // нового замера нет
  for (int i = 0; i < 3; ++i) {
    m.raw[i] = (int16_t)((uint16_t)b[2 + 2 * i] << 8 | b[1 + 2 * i]);
    m.b[i] = m.raw[i] * 0.15f;
  }
  m.ovf = (b[8] & 0x08) != 0;
  return true;
}
