#include "ina219.h"

// Шина 16 В (BRNG 0), шунт ±320 мВ (PG /8), шина и шунт — 12 бит × 128 замеров, непрерывно
static const uint16_t CFG = 0x1FFF;
static const uint16_t CFG_RESET = 0x399F;     // так регистр настройки читается после включения
static const float    SHUNT_OHM = 0.1f;       // R100 на модуле

bool Ina219::wr(uint8_t reg, uint16_t v) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  w_->write((uint8_t)(v >> 8));
  w_->write((uint8_t)(v & 0xff));
  if (w_->endTransmission() != 0) {
    ++err_;
    return false;
  }
  return true;
}

bool Ina219::rd(uint8_t reg, uint16_t& v) {
  w_->beginTransmission(addr_);
  w_->write(reg);
  if (w_->endTransmission(false) != 0 || w_->requestFrom(addr_, (uint8_t)2) != 2) {
    ++err_;
    return false;
  }
  v = (uint16_t)w_->read() << 8;
  v |= w_->read();
  return true;
}

bool Ina219::begin(TwoWire& w, uint8_t addr) {
  w_ = &w;
  addr_ = addr;
  uint16_t c = 0;
  // свой — только по регистру настройки: после включения 0x399F, после нашего begin (перезапуск агента) — CFG
  if (!rd(0x00, c) || (c != CFG_RESET && c != CFG)) return false;
  if (!wr(0x00, CFG)) return false;
  err_ = 0;                                   // промахи поиска по чужим адресам — не ошибки датчика
  return true;
}

bool Ina219::read(float& v_bat, float& i_ma) {
  uint16_t s = 0, b = 0;
  if (!rd(0x01, s) || !rd(0x02, b)) return false;
  if (b & 1) {                                // OVF: ток вне предела шунта — показание неверное
    ++err_;
    return false;
  }
  const float vsh = (int16_t)s * 10e-6f;      // шунт: 10 мкВ на единицу
  i_ma = vsh / SHUNT_OHM * 1000.0f;
  v_bat = (b >> 3) * 0.004f + vsh;            // шина (VIN− к GND): 4 мВ на единицу, биты 3..15; + падение на шунте
  return true;
}
