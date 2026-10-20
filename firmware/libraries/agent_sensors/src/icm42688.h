// ICM-42688-P (TDK InvenSense): акселерометр и гироскоп, I2C.
//
// Урок LIS3DH: режим датчика читается обратно из регистров и уходит в
// телеметрию (readback), а показания — полной разрядности, по каждой оси.
#pragma once
#include <Arduino.h>
#include <Wire.h>

class Icm42688 {
 public:
  enum AccelFs : uint8_t { A16G = 0, A8G = 1, A4G = 2, A2G = 3 };      // ACCEL_CONFIG0[7:5]
  enum GyroFs : uint8_t { G2000 = 0, G1000 = 1, G500 = 2, G250 = 3 };  // GYRO_CONFIG0[7:5]
  enum Odr : uint8_t { ODR_1K = 0x6, ODR_200 = 0x7, ODR_100 = 0x8, ODR_500 = 0xF };   // [3:0]
  static const uint8_t WHO = 0x47;    // WHO_AM_I

  struct Sample {
    float   a[3];                     // ускорение (то, что показывает датчик), g
    float   w[3];                     // угловая скорость, рад/с
    float   temp;                     // °C
    int16_t raw[6];                   // сырые отсчёты: ax ay az gx gy gz
  };

  // Найти датчик по адресу и сбросить. false — не ответил или не ICM-42688.
  bool begin(TwoWire& w, uint8_t addr);
  // Пределы, частота выдачи, полоса фильтра (GYRO_ACCEL_CONFIG0: 0 — ODR/2,
  // 1 — max(400 Гц, ODR)/4, … 4 — /10, 7 — /40), гироскоп вкл/выкл.
  bool configure(AccelFs afs, GyroFs gfs, Odr odr, uint8_t bw, bool gyro_on);
  bool data_ready();                  // новые данные (флаг сбрасывается чтением)
  bool read(Sample& s);               // температура, ускорение, гироскоп
  bool read_accel(Sample& s);         // только температура и ускорение (быстрее)
  // Что реально записано в регистры: PWR_MGMT0, GYRO_CONFIG0, ACCEL_CONFIG0, GYRO_ACCEL_CONFIG0.
  void readback(uint8_t out[4]);

  // Порядок фильтра акселерометра (ACCEL_CONFIG1): 1, 2 или 3 — чем выше, тем
  // круче спад за полосой и меньше заворот частот при усреднении.
  bool accel_filter_order(uint8_t ord);
  // FIFO в режиме потока: только ускорение — пакет 8 байт (заголовок, x y z
  // старшим байтом вперёд, температура). Отсчёты не теряются, даже если их
  // забирают с задержкой: FIFO 2 КБ — 256 отсчётов.
  bool fifo_begin();
  // Забрать целые пакеты, не больше max: a — сырые отсчёты осей, t — температура
  // (°C = t / 2,07 + 25). false — ошибка шины.
  bool fifo_read(int16_t (*a)[3], int8_t* t, int max, int& n);
  // FIFO_CONFIG, FIFO_CONFIG1, ACCEL_CONFIG1 — как записано.
  void readback_fifo(uint8_t out[3]);
  uint32_t fifo_overflows() const { return ovf_; }

  uint8_t  addr() const { return addr_; }
  uint8_t  who() const { return who_; }
  float    accel_lsb() const { return a_lsb_; }   // отсчётов на g
  float    gyro_lsb() const { return g_lsb_; }    // отсчётов на °/с
  uint32_t errors() const { return err_; }

 private:
  bool wr(uint8_t reg, uint8_t v);
  bool rd(uint8_t reg, uint8_t* buf, size_t n);
  TwoWire* w_ = nullptr;
  uint8_t  addr_ = 0, who_ = 0;
  float    a_lsb_ = 2048.0f, g_lsb_ = 16.4f;
  uint32_t err_ = 0, ovf_ = 0;
};
