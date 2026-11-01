#include "wheel.h"
#include <math.h>

static const float G = 9.80665f;

static inline float norm3(const float v[3]) { return sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); }
static inline float dot3(const float a[3], const float b[3]) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

void WheelTracker::reset_rev() {
  sum_f_[0] = sum_f_[1] = sum_f_[2] = 0;
  for (int i = 0; i < 6; ++i) h_[i] = 0;
  n_ = 0;
  fmin_ = INFINITY;
  fmax_ = 0;
}

bool WheelTracker::axis_unit(float u[3]) const {
  const float n = norm3(axis_);
  if (n <= 1e-9f) return false;
  for (int i = 0; i < 3; ++i) u[i] = axis_[i] / n;
  return true;
}

bool WheelTracker::wheel_frame(const float f[3], float out[4]) const {
  float ua[3];
  if (!axis_unit(ua) || !has_radial_) return false;
  const float* ur = radial_;
  const float ut[3] = {ua[1] * ur[2] - ua[2] * ur[1], ua[2] * ur[0] - ua[0] * ur[2], ua[0] * ur[1] - ua[1] * ur[0]};
  out[0] = dot3(f, ur);
  out[1] = dot3(f, ut);
  out[2] = dot3(f, ua);
  out[3] = norm3(f);
  return true;
}

void WheelTracker::acc(const float f[3], float m2, float th, float wgt) {
  for (int i = 0; i < 3; ++i) sum_f_[i] += f[i] * wgt;     // среднее — по углу: разгон внутри оборота не перекашивает
  h_[0] += m2 * wgt;
  h_[1] += m2 * cosf(th) * wgt;
  h_[2] += m2 * sinf(th) * wgt;
  h_[3] += m2 * cosf(2 * th) * wgt;
  h_[4] += m2 * sinf(2 * th) * wgt;
  h_[5] += wgt;
}

bool WheelTracker::add(const float f[3], const float w[3], float dt, WheelRev& out) {
  t_ += dt;
  const float a = dt / AXIS_TAU < 1.0f ? dt / AXIS_TAU : 1.0f;
  if (norm3(w) > W_MIN) {                 // ось — только пока колесо крутится
    const float s = dot3(w, axis_) >= 0 ? 1.0f : -1.0f;   // знак оси постоянный
    for (int i = 0; i < 3; ++i) axis_[i] = (1 - a) * axis_[i] + a * s * w[i];
  }
  float ua[3];
  const bool have_axis = axis_unit(ua);
  w_now_ = have_axis ? dot3(w, ua) / (1 + gs_) : 0.0f;    // с поправкой масштаба гироскопа
  if (!have_axis || fabsf(w_now_) < W_MIN) {   // стоит — оборот не считается
    t_rev0_ = -1;
    theta_ = 0;
    has_ph_ = false;
    rpm_prev_ = -1;
    reset_rev();
    return false;
  }
  if (t_rev0_ < 0) t_rev0_ = t_;
  theta_ += w_now_ * dt;
  const float m = norm3(f), m2 = m * m;
  acc(f, m2, theta_, fabsf(w_now_ * dt));
  ++n_;
  if (m < fmin_) fmin_ = m;
  if (m > fmax_) fmax_ = m;
  if (fabsf(theta_) < 2.0f * (float)PI) return false;

  // Конец оборота — между отсчётами: угол перелетел 2π на over рад, это over/|ω| с назад (без этого период дрожит
  // на один отсчёт — ±0,3 % при 45 об/мин). Окно — ровно 2π: часть последнего отсчёта за 2π — в следующий оборот
  const float over = fabsf(theta_) - 2.0f * (float)PI;
  const double t_end = t_ - over / fabsf(w_now_);
  acc(f, m2, theta_, -over);
  const float period = (float)(t_end - t_rev0_);    // разность — секунды, во float точна; само время копится в double
  const float ang = h_[5] > 1e-9f ? h_[5] : 1e-9f;
  float mean[3] = {sum_f_[0] / ang, sum_f_[1] / ang, sum_f_[2] / ang};
  const float k = norm3(mean);
  if (k > 1e-6f) {
    for (int i = 0; i < 3; ++i) radial_[i] = mean[i] / k;
    has_radial_ = true;
  }
  const float omega = 2.0f * (float)PI / period;
  out.t = (float)t_;
  out.period = period;
  out.rpm = 60.0f / period;
  out.w = omega;
  out.k = k;
  out.k_r = omega * omega * r_mm_ / 1000.0f / G;
  out.r_mm = k * G / (omega * omega) * 1000.0f;
  out.fmin = fmin_;
  out.fmax = fmax_;
  out.n = n_;
  // форма кривой: |f|² = a0 + a1·cos θ + b1·sin θ + … по углу гироскопа θ
  const float a1 = 2 * h_[1] / ang, b1 = 2 * h_[2] / ang, a2 = 2 * h_[3] / ang, b2 = 2 * h_[4] / ang;
  const float B1 = sqrtf(a1 * a1 + b1 * b1);
  const float ph1 = atan2f(b1, a1);
  out.eps = NAN;
  if (has_ph_) {
    // гироскоп (с нынешней поправкой) врёт ещё в (1 + eps) раз: фаза тяжести сдвигается на 2π·eps/(1 + eps) за оборот
    float dph = ph1 - ph_prev_;
    while (dph > (float)PI) dph -= 2.0f * (float)PI;
    while (dph < -(float)PI) dph += 2.0f * (float)PI;
    const float x = (theta_ > 0 ? 1.0f : -1.0f) * dph / (2.0f * (float)PI);
    if (x < 0.5f) out.eps = x / (1 - x);
  }
  ph_prev_ = ph1;
  has_ph_ = true;
  const float h2 = sqrtf(a2 * a2 + b2 * b2) / (B1 > 1e-9f ? B1 : 1e-9f);
  // ровный оборот: скорость как у прошлого (±0,5 %), тяжесть видна (k ≥ 0,05), форма чистая
  const bool flat = rpm_prev_ > 0 && fabsf(out.rpm - rpm_prev_) <= 0.005f * out.rpm && B1 >= 0.1f && h2 < 0.2f;
  if (flat && !isnan(out.eps)) {
    float g2 = (1 + gs_) * (1 + GS_ALPHA * out.eps) - 1;
    gs_ = g2 > GS_MAX ? GS_MAX : g2 < -GS_MAX ? -GS_MAX : g2;
  }
  rpm_prev_ = out.rpm;
  out.kc = B1 / 2;
  out.a0 = h_[0] / ang;
  out.ph1 = ph1;
  out.gs = gs_;
  out.h2 = h2;
  for (int i = 0; i < 3; ++i) out.m[i] = mean[i];
  out.flat = flat;
  ++revs_;
  theta_ -= theta_ > 0 ? 2.0f * (float)PI : -2.0f * (float)PI;
  t_rev0_ = t_end;
  reset_rev();
  acc(f, m2, theta_, over);                 // перелёт — начало следующего оборота
  return true;
}
