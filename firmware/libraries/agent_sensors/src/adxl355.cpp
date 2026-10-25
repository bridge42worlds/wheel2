#include "adxl355.h"

// Регистры (ADXL354/ADXL355, Rev. A).
static const uint8_t R_DEVID_AD = 0x00;      // 0xAD, далее DEVID_MST 0x1D, PARTID 0xED
static const uint8_t R_STATUS = 0x04;        // [2] FIFO_OVR, [1] FIFO_FULL, [0] DATA_RDY; далее FIFO_ENTRIES
static const uint8_t R_TEMP2 = 0x06;         // [3:0] старшие разряды температуры, далее TEMP1
static const uint8_t R_FIFO_DATA = 0x11;     // 3 байта на ось, старший первым; чтение снимает из FIFO
static const uint8_t R_FILTER = 0x28;        // [6:4] HPF_CORNER (0 — выкл), [3:0] ODR_LPF
static const uint8_t R_RANGE = 0x2C;         // [7] I2C_HS (0 — до 1 МГц), [1:0] предел
static const uint8_t R_POWER_CTL = 0x2D;     // [2] DRDY_OFF, [1] TEMP_OFF, [0] STANDBY
static const uint8_t R_RESET = 0x2F;         // 0x52 — сброс как при включении

bool Adxl355::wr(uint8_t reg, uint8_t v) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  w_->write(v);
  if (w_->endTransmission() != 0) {
    ++err_;
    return false;
  }
  return true;
}

bool Adxl355::rd(uint8_t reg, uint8_t* buf, size_t n) {
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

bool Adxl355::begin(TwoWire& w, uint8_t addr) {
  w_ = &w;
  addr_ = addr;
  uint8_t id[3] = {};
  if (!rd(R_DEVID_AD, id, 3)) return false;
  pid_ = id[2];
  if (id[0] != DEVID_AD || id[2] != PARTID) return false;
  wr(R_RESET, 0x52);                  // после сброса — ожидание (включение < 10 мс)
  delay(20);
  return rd(R_DEVID_AD, id, 3) && id[0] == DEVID_AD && id[2] == PARTID;
}

bool Adxl355::configure(Range r, Odr odr) {
  bool ok = wr(R_POWER_CTL, 0x01);    // режим меняют в ожидании
  ok &= wr(R_RANGE, (uint8_t)r);      // I2C_HS = 0: шина 400 кГц
  ok &= wr(R_FILTER, (uint8_t)odr);   // фильтр высоких частот выключен
  lsb16_ = r == R2G ? 16000.0f : r == R4G ? 8000.0f : 4000.0f;
  ok &= wr(R_POWER_CTL, 0x00);        // измерение, температура включена
  delay(10);
  uint8_t junk[9];                    // FIFO с прошлого режима — выбросить
  uint8_t st[2];
  if (rd(R_STATUS, st, 2))
    for (int k = st[1] / 3; k > 0; --k) rd(R_FIFO_DATA, junk, 9);
  pi_ = -1;
  return ok;
}

bool Adxl355::fifo_read(int16_t (*a)[3], int max, int& n) {
  n = 0;
  uint8_t st[2];
  if (!rd(R_STATUS, st, 2)) return false;
  if (st[0] & 0x04) ++ovf_;
  int left = st[1] & 0x7F;            // осей в FIFO (до 96)
  if (left > max * 3) left = max * 3;
  uint8_t buf[117];                   // 13 троек — буфер Wire 128 байт
  while (left > 0) {
    const int take = left > 39 ? 39 : left;
    if (!rd(R_FIFO_DATA, buf, take * 3)) return false;
    left -= take;
    for (int i = 0; i < take; ++i) {
      const uint8_t* b = buf + 3 * i;
      if (b[2] & 0x02) {              // «FIFO было пусто»: не отсчёт
        ++bad_;
        pi_ = -1;
        continue;
      }
      int32_t v = ((int32_t)b[0] << 12) | ((int32_t)b[1] << 4) | (b[2] >> 4);
      if (v & 0x80000) v -= 0x100000;  // 20 разрядов со знаком
      v = (v + 8) >> 4;                // в int16 с округлением
      if (v > 32767) v = 32767;
      if (b[2] & 0x01) {              // метка оси x — начало тройки
        if (pi_ > 0) ++bad_;          // прошлая тройка оборвалась
        pi_ = 0;
      } else if (pi_ < 0) {           // y или z без своего x — сбились с тройки
        ++bad_;
        continue;
      }
      tri_[pi_++] = (int16_t)v;
      if (pi_ == 3) {
        if (n < max) memcpy(a[n++], tri_, sizeof tri_);
        pi_ = -1;
      }
    }
  }
  return true;
}

bool Adxl355::read_temp(float& t) {
  uint8_t b[2];
  if (!rd(R_TEMP2, b, 2)) return false;
  const int raw = ((b[0] & 0x0F) << 8) | b[1];
  t = 25.0f + (raw - 1852) / -9.05f;
  return true;
}

void Adxl355::readback(uint8_t out[4]) {
  out[0] = out[1] = out[2] = out[3] = 0;
  rd(R_FILTER, out + 0, 1);
  rd(R_RANGE, out + 1, 1);
  rd(R_POWER_CTL, out + 2, 1);
  rd(R_STATUS, out + 3, 1);
}
