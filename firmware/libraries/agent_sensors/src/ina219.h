// INA219 (TI): напряжение и ток по I2C — аккумуляторы контура (шина колеса или катушка).
//
// Модуль с шунтом 0,1 Ом стоит в плюсе между выключателем и DC-DC: VIN+ — от аккумуляторов, VIN− — на вход
// DC-DC (docs/wiring.md). Шунт ±320 мВ — до 3,2 А; шина до 16 В (у пары 18650 — 6–8,4 В); 12 бит, среднее по
// 128 замерам (≈ 68 мс) — броски передатчика сглажены.
#pragma once
#include <Arduino.h>
#include <Wire.h>

class Ina219 {
 public:
  bool begin(TwoWire& w, uint8_t addr);   // узнать по регистру настройки и настроить; false — не он или не ответил
  bool read(float& v_bat, float& i_ma);   // напряжение аккумуляторов (шина + падение на шунте), В; ток, мА
  uint8_t  addr() const { return addr_; }
  uint32_t errors() const { return err_; }

 private:
  bool rd(uint8_t reg, uint16_t& v);
  bool wr(uint8_t reg, uint16_t v);
  TwoWire* w_ = nullptr;
  uint8_t  addr_ = 0;
  uint32_t err_ = 0;
};
