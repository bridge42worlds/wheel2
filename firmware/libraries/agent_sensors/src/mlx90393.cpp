#include "mlx90393.h"

// Команды (MLX90393, раздел «Command list»). Младшие 4 бита — zyxt.
static const uint8_t C_SM = 0x30, C_RM = 0x40, C_RR = 0x50, C_WR = 0x60, C_EX = 0x80, C_RT = 0xF0;
static const uint8_t ZYXT = 0x0F;
static const uint8_t ST_ERROR = 0x10;              // бит ошибки в байте состояния

// Чувствительность, мкТл на отсчёт, при HALLCONF = 0xC и RES = 0 — по усилению 0..7.
static const float SENS_XY[8] = {0.751f, 0.601f, 0.451f, 0.376f, 0.300f, 0.250f, 0.200f, 0.150f};
static const float SENS_Z[8] = {1.210f, 0.968f, 0.726f, 0.605f, 0.484f, 0.403f, 0.323f, 0.242f};

bool Mlx90393::cmd(const uint8_t* tx, size_t ntx, uint8_t* rx, size_t nrx) {
  w_->beginTransmission(addr_);
  for (size_t i = 0; i < ntx; ++i) w_->write(tx[i]);
  if (w_->endTransmission(false) != 0) {
    ++err_;
    return false;
  }
  if (w_->requestFrom((int)addr_, (int)nrx) != (int)nrx) {
    ++err_;
    return false;
  }
  for (size_t i = 0; i < nrx; ++i) rx[i] = (uint8_t)w_->read();
  if (rx[0] & ST_ERROR) {
    ++err_;
    return false;
  }
  return true;
}

bool Mlx90393::read_reg(uint8_t reg, uint16_t& v) {
  const uint8_t tx[2] = {C_RR, (uint8_t)(reg << 2)};
  uint8_t rx[3];
  if (!cmd(tx, 2, rx, 3)) return false;
  v = (uint16_t)(rx[1] << 8 | rx[2]);
  return true;
}

bool Mlx90393::write_reg(uint8_t reg, uint16_t v) {
  const uint8_t tx[4] = {C_WR, (uint8_t)(v >> 8), (uint8_t)v, (uint8_t)(reg << 2)};
  uint8_t rx[1];
  return cmd(tx, 4, rx, 1);
}

bool Mlx90393::begin(TwoWire& w, uint8_t addr) {
  w_ = &w;
  addr_ = addr;
  uint8_t rx[1];
  const uint8_t ex = C_EX, rt = C_RT;
  cmd(&ex, 1, rx, 1);                 // выйти из непрерывных режимов, если были
  delay(2);
  if (!cmd(&rt, 1, rx, 1)) return false;   // сброс; не ответил — датчика нет
  delay(5);
  uint16_t r2 = 0;
  if (!read_reg(0x02, r2)) return false;
  // Рег. 2: OSR2 0, RES_Z/Y/X 0 (знаковые 16 бит), DIG_FILT 5, OSR 0.
  r2 = (uint16_t)((r2 & ~0x1FFF) | (5 << 2));
  if (!write_reg(0x02, r2)) return false;
  return set_gain(7);
}

bool Mlx90393::set_gain(uint8_t g) {
  if (g > 7) g = 7;
  uint16_t r0 = 0;
  if (!read_reg(0x00, r0)) return false;
  r0 = (uint16_t)((r0 & ~0x007F) | (g << 4) | 0x0C);   // GAIN_SEL [6:4], HALLCONF 0xC
  if (!write_reg(0x00, r0)) return false;
  gain_ = g;
  return true;
}

bool Mlx90393::start() {
  const uint8_t sm = C_SM | ZYXT;
  uint8_t rx[1];
  return cmd(&sm, 1, rx, 1);
}

bool Mlx90393::fetch(Sample& s) {
  const uint8_t rm = C_RM | ZYXT;
  uint8_t rx[9];                      // состояние, T, X, Y, Z — по 2 байта, старший первым
  if (!cmd(&rm, 1, rx, 9)) return false;
  const uint16_t t = (uint16_t)(rx[1] << 8 | rx[2]);
  for (int i = 0; i < 3; ++i) s.raw[i] = (int16_t)(rx[3 + 2 * i] << 8 | rx[4 + 2 * i]);
  s.b[0] = s.raw[0] * SENS_XY[gain_];
  s.b[1] = s.raw[1] * SENS_XY[gain_];
  s.b[2] = s.raw[2] * SENS_Z[gain_];
  s.temp = 35.0f + (t - 46244.0f) / 45.2f;   // типичная опора; точная — в регистре 0x24
  s.gain = gain_;
  return true;
}

void Mlx90393::auto_gain(const Sample& s) {
  int m = 0;
  for (int i = 0; i < 3; ++i) {
    const int a = abs((int)s.raw[i]);
    if (a > m) m = a;
  }
  if (m > 28000 && gain_ > 0) set_gain(gain_ - 1);        // у края — грубее
  else if (m < 2500 && gain_ < 7) set_gain(gain_ + 1);    // слишком мало — точнее
}
