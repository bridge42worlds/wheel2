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
//
// Форма кривой: |f|²/g² = 1 + k² + 2k·cos φ — точная синусоида по углу тяжести.
// За оборот |f|² раскладывается по углу (вес — пройденный угол): k по форме,
// постоянная часть, фаза тяжести. Сдвиг фазы от оборота к обороту — ошибка
// масштаба гироскопа; на ровных оборотах угол сам подстраивается (gs), чтобы
// «оборот» был настоящим: верны обороты и среднее (тяжесть уходит в ноль).
// Конец оборота и окно 2π — между отсчётами (без дрожания на один отсчёт).
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
  float kc;         // k по форме кривой (половина первой гармоники |f|²)
  float a0;         // постоянная часть |f|², g²: должна быть 1 + k²
  float ph1;        // фаза тяжести в угле гироскопа, рад
  float eps;        // ошибка масштаба гироскопа, оставшаяся после поправки (NAN — прошлого оборота подряд не было)
  float gs;         // поправка масштаба гироскопа после этого оборота
  float h2;         // вторая гармоника к первой: у ровного колеса ≈ 0
  float m[3];       // среднее ускорение по осям датчика, g: (ω²r/g)·к центру + смещение нуля
  bool  flat;       // ровный оборот (по нему подстраивается масштаб гироскопа)
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
  float w_now() const { return w_now_; }        // рад/с вокруг оси колеса (с поправкой масштаба)
  uint32_t revs() const { return revs_; }
  float gs() const { return gs_; }

  static constexpr float W_MIN = 0.3f;     // рад/с: медленнее — колесо стоит
  static constexpr float AXIS_TAU = 2.0f;  // с: сглаживание оси
  static constexpr float GS_ALPHA = 0.2f;  // масштаб гироскопа: доля найденной ошибки за ровный оборот
  static constexpr float GS_MAX = 0.05f;   // поправка не больше ±5 %

 private:
  void reset_rev();
  void acc(const float f[3], float m2, float th, float wgt);   // показание в суммы оборота с весом-углом
  float r_mm_;
  float axis_[3] = {0, 0, 0};
  float radial_[3] = {0, 0, 0};
  bool  has_radial_ = false;
  float theta_ = 0, w_now_ = 0;
  // Время — double: во float32 шаг 5 мс тонет в округлении уже через час работы (ulp 0,5 мс при 4096 с),
  // и период оборота уходил на проценты, а после 36 ч переставал расти (найдено 25.10.2026)
  double t_ = 0, t_rev0_ = -1;
  float sum_f_[3];
  float h_[6];                                // |f|² по углу: сумма, cos θ, sin θ, cos 2θ, sin 2θ; весь угол
  uint32_t n_;
  float fmin_, fmax_;
  uint32_t revs_ = 0;
  float ph_prev_ = 0, rpm_prev_ = -1, gs_ = 0;
  bool  has_ph_ = false;
};
