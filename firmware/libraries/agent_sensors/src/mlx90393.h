// MLX90393 (Melexis): трёхосный магнитометр на эффекте Холла, I2C.
//
// Ток катушки заранее неизвестен — усиление подбирается само: показание
// подошло к краю диапазона — усиление ниже, слишком мало — выше. Поле всегда
// пересчитывается в микротеслы по таблице чувствительности текущего усиления.
#pragma once
#include <Arduino.h>
#include <Wire.h>

class Mlx90393 {
 public:
  struct Sample {
    float   b[3];                     // поле, мкТл (x, y, z)
    float   temp;                     // °C
    int16_t raw[3];                   // сырые отсчёты
    uint8_t gain;                     // усиление, с которым измерено (0..7)
  };

  bool begin(TwoWire& w, uint8_t addr);   // сброс и настройка; false — не ответил
  bool set_gain(uint8_t g);               // 0 (грубо, до ~±50 мТл) … 7 (точно, ~±5 мТл)
  bool start();                           // начать одно измерение x, y, z, t
  bool fetch(Sample& s);                  // забрать (через CONV_MS после start)
  void auto_gain(const Sample& s);        // подобрать усиление к следующему измерению
  uint8_t  gain() const { return gain_; }
  uint8_t  addr() const { return addr_; }
  uint32_t errors() const { return err_; }
  static const uint16_t CONV_MS = 10;     // DIG_FILT 5, OSR 0: около 7,3 мс на x, y, z, t

 private:
  bool cmd(const uint8_t* tx, size_t ntx, uint8_t* rx, size_t nrx);
  bool read_reg(uint8_t reg, uint16_t& v);
  bool write_reg(uint8_t reg, uint16_t v);
  TwoWire* w_ = nullptr;
  uint8_t  addr_ = 0, gain_ = 7;
  uint32_t err_ = 0;
};
