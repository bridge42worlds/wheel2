// ADXL355 (Analog Devices): точный акселерометр, I2C (адрес 0x1D — ASEL на GND, 0x53 — на 3V3).
//
// Шум 25 мкg/√Гц по каждой оси (у ICM-42688 — 65–70), 20 разрядов: ±2 g — 256000
// отсчётов на g (3,9 мкg). Своя частота выдачи (ODR) с фильтром ODR/4 внутри
// датчика и FIFO на 32 тройки x y z — отсчёты не теряются, если забирать хотя бы
// раз в 30 мс при 1000 Гц. Режим читается обратно из регистров (урок LIS3DH).
#pragma once
#include <Arduino.h>
#include <Wire.h>

class Adxl355 {
 public:
  enum Range : uint8_t { R2G = 1, R4G = 2, R8G = 3 };                  // RANGE[1:0]
  // FILTER[3:0] ODR_LPF: частота выдачи / полоса фильтра, Гц
  enum Odr : uint8_t { ODR_4000 = 0, ODR_2000 = 1, ODR_1000 = 2, ODR_500 = 3, ODR_250 = 4, ODR_125 = 5 };
  static const uint8_t DEVID_AD = 0xAD, DEVID_MST = 0x1D, PARTID = 0xED;

  // Найти датчик по адресу (DEVID_AD, PARTID) и сбросить. false — не ответил или не ADXL355.
  bool begin(TwoWire& w, uint8_t addr);
  // Предел, частота выдачи; включает измерение (и температуру). FIFO копит само.
  bool configure(Range r, Odr odr);
  // Забрать из FIFO целые тройки, не больше max: оси — 20 разрядов, сдвинутые
  // на 4 вправо (int16: ±2 g — 16000 на g, ступень 62,5 мкg — шум при 1000 Гц
  // ~0,5 мg, ступень его не портит). Тройки без метки оси x или с меткой «пусто»
  // отбрасываются и считаются в bad(). false — ошибка шины.
  bool fifo_read(int16_t (*a)[3], int max, int& n);
  bool read_temp(float& t);           // °C: 1852 отсчёта при 25 °C, −9,05 на °C
  // Что реально записано: FILTER (0x28), RANGE (0x2C), POWER_CTL (0x2D), STATUS (0x04).
  void readback(uint8_t out[4]);

  uint8_t  addr() const { return addr_; }
  uint8_t  partid() const { return pid_; }
  float    lsb16() const { return lsb16_; }    // отсчётов int16 на g (после сдвига на 4)
  uint32_t errors() const { return err_; }
  uint32_t bad() const { return bad_; }        // отброшенных троек FIFO
  uint32_t overflows() const { return ovf_; }  // FIFO было полно: отсчёты потеряны

 private:
  bool wr(uint8_t reg, uint8_t v);
  bool rd(uint8_t reg, uint8_t* buf, size_t n);
  TwoWire* w_ = nullptr;
  uint8_t  addr_ = 0, pid_ = 0;
  float    lsb16_ = 16000.0f;
  uint32_t err_ = 0, bad_ = 0, ovf_ = 0;
  int16_t  tri_[3] = {};              // начатая тройка (x пришёл, y или z — в следующем чтении)
  int      pi_ = -1;                  // сколько осей тройки уже есть; -1 — ждём метку x
};
