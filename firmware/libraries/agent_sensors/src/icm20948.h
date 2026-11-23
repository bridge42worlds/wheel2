// ICM-20948 (TDK InvenSense): акселерометр, гироскоп и магнитометр AK09916 в одном корпусе, I2C.
//
// Фаза 3: по два датчика на агента, каждый на своём канале I2C. Показания полной разрядности, режим читается
// обратно из регистров (readback). Буфер датчика (FIFO): записи ускорения и вращения по 12 байт пишет сам
// датчик по своим часам; агент сбрасывает буфер при входе датчика в окно и забирает после окна.
// Магнитометр читается напрямую (режим «мимо»: AK09916 виден на той же шине по адресу 0x0C).
#pragma once
#include <Arduino.h>
#include <Wire.h>

class Icm20948 {
 public:
  enum AccelFs : uint8_t { A2G = 0, A4G = 1, A8G = 2, A16G = 3 };          // ACCEL_CONFIG[2:1]
  enum GyroFs : uint8_t { G250 = 0, G500 = 1, G1000 = 2, G2000 = 3 };      // GYRO_CONFIG_1[2:1]
  static const uint8_t WHO = 0xEA;      // WHO_AM_I
  static const uint8_t MAG_ADDR = 0x0C; // AK09916
  static const uint8_t MAG_WHO = 0x09;  // WIA2
  static const int REC = 12;            // запись буфера: ax ay az gx gy gz, старшим байтом вперёд

  struct Sample {
    float   a[3];                       // ускорение, g
    float   w[3];                       // угловая скорость, рад/с
    float   temp;                       // °C
    int16_t raw[6];                     // сырые отсчёты: ax ay az gx gy gz
  };
  struct Mag {
    float   b[3];                       // поле, мкТл
    int16_t raw[3];
    bool    ovf;                        // переполнение датчика (ST2.HOFL)
  };

  // Найти датчик по адресу, сбросить, разбудить. false — не ответил или не ICM-20948.
  bool begin(TwoWire& w, uint8_t addr);
  // Пределы и частота: 1125 / (1 + div) записей в секунду у ускорения и вращения (div 0 — 1125 Гц),
  // dlpf — полоса фильтра 0..7 (по описанию датчика 7 — самая широкая: ускорение 473 Гц, вращение 361 Гц;
  // 0 — ускорение 246 Гц, вращение 197 Гц). Проверить на живом датчике.
  bool configure(AccelFs afs, GyroFs gfs, uint8_t div, uint8_t dlpf);
  bool read(Sample& s);                 // ускорение, вращение, температура — одним чтением (14 байт)
  bool read_accel(int16_t a[3]);        // только ускорение (6 байт) — быстрый опрос в ожидании окна
  // Что записано в регистры: PWR_MGMT_1, GYRO_SMPLRT_DIV, GYRO_CONFIG_1, ACCEL_CONFIG, FIFO_EN_2, FIFO_MODE.
  void readback(uint8_t out[6]);

  // ---- буфер датчика ----
  // Включить запись ускорения и вращения в буфер. stream: true — по кругу (старое затирается),
  // false — до заполнения (дальше записи не принимаются).
  bool fifo_begin(bool stream);
  bool fifo_reset();                    // опустошить: с этого момента в буфере только новые записи
  int  fifo_count();                    // байт в буфере; -1 — ошибка шины
  // Забрать целые записи, не больше max. n — сколько забрано. false — ошибка шины.
  bool fifo_read(int16_t (*rec)[6], int max, int& n);
  // Измерить размер буфера: сбросить, ждать до wait_ms, пока счётчик не перестанет расти. Байт; 0 — ошибка.
  int  fifo_measure(uint32_t wait_ms);

  // ---- магнитометр ----
  bool mag_begin();                     // режим «мимо», сброс AK09916, 100 замеров в секунду
  bool mag_read(Mag& m);                // false — нового замера нет или ошибка шины
  bool has_mag() const { return mag_; }

  uint8_t  addr() const { return addr_; }
  uint8_t  who() const { return who_; }
  float    accel_lsb() const { return a_lsb_; }   // отсчётов на g
  float    gyro_lsb() const { return g_lsb_; }    // отсчётов на °/с
  uint32_t errors() const { return err_; }

 private:
  bool bank(uint8_t b);
  bool wr(uint8_t reg, uint8_t v);
  bool rd(uint8_t reg, uint8_t* buf, size_t n);
  bool mwr(uint8_t reg, uint8_t v);
  bool mrd(uint8_t reg, uint8_t* buf, size_t n);
  TwoWire* w_ = nullptr;
  uint8_t  addr_ = 0, who_ = 0, bank_ = 0xff;
  bool     mag_ = false;
  float    a_lsb_ = 8192.0f, g_lsb_ = 32.8f;
  uint32_t err_ = 0;
};
