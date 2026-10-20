// Обороты и невесомость по акселерометру и гироскопу на ободе.
//
// Точная копия WheelTracker из server/analysis.py — алгоритм проверен там на
// синтетическом колесе (tools/test_analysis.py). Менять — в обоих местах.
//
// Вход: f — показание акселерометра в осях датчика, g; w — гироскоп, рад/с.
// Ось колеса — направление сглаженного вектора гироскопа. Угол — интеграл
// скорости вокруг оси; полный оборот — 2π. За оборот тяжесть усредняется в
// ноль, остаётся центростремительное: его среднее направлено к центру и по
// модулю равно k = ω²r/g. k = 1 — невесомость в верхней точке.
#pragma once
#include <Arduino.h>

struct WheelRev {
  float t;          // с от начала: когда оборот закончился
  float period;     // с
  float rpm;
  float w;          // средняя угловая скорость, рад/с
  float k;          // ω²r/g по среднему ускорению за оборот
  float k_r;        // то же по заданному радиусу
  float r_mm;       // радиус по измерению
  float fmin;       // наименьшее |f| за оборот (верхняя точка), g
  float fmax;       // наибольшее (нижняя), g
  uint32_t n;       // показаний за оборот
};

class WheelTracker {
 public:
  explicit WheelTracker(float r_mm = 450.0f) : r_mm_(r_mm) { reset_rev(); }
  void set_radius(float r_mm) { r_mm_ = r_mm; }
  // Одно показание; dt — с с прошлого. true — закончился оборот (он в out).
  bool add(const float f[3], const float w[3], float dt, WheelRev& out);
  // f в осях колеса: к центру, по ходу, вдоль оси, |f|. false — оси ещё не известны.
  bool wheel_frame(const float f[3], float out[4]) const;
  bool axis_unit(float u[3]) const;
  bool has_radial() const { return has_radial_; }
  const float* radial() const { return radial_; }
  float w_now() const { return w_now_; }        // рад/с вокруг оси колеса
  uint32_t revs() const { return revs_; }

  static constexpr float W_MIN = 0.3f;     // рад/с: медленнее — колесо стоит
  static constexpr float AXIS_TAU = 2.0f;  // с: сглаживание оси

 private:
  void reset_rev();
  float r_mm_;
  float axis_[3] = {0, 0, 0};
  float radial_[3] = {0, 0, 0};
  bool  has_radial_ = false;
  float theta_ = 0, t_ = 0, t_rev0_ = -1, w_now_ = 0;
  float sum_f_[3];
  uint32_t n_;
  float fmin_, fmax_;
  uint32_t revs_ = 0;
};
