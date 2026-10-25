#include "wheel.h"
#include <math.h>

static const float G = 9.80665f;

static inline float norm3(const float v[3]) { return sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); }
static inline float dot3(const float a[3], const float b[3]) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

void WheelTracker::reset_rev() {
  sum_f_[0] = sum_f_[1] = sum_f_[2] = 0;
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

bool WheelTracker::add(const float f[3], const float w[3], float dt, WheelRev& out) {
  t_ += dt;
  const float a = dt / AXIS_TAU < 1.0f ? dt / AXIS_TAU : 1.0f;
  if (norm3(w) > W_MIN) {                 // ось — только пока колесо крутится
    const float s = dot3(w, axis_) >= 0 ? 1.0f : -1.0f;   // знак оси постоянный
    for (int i = 0; i < 3; ++i) axis_[i] = (1 - a) * axis_[i] + a * s * w[i];
  }
  float ua[3];
  const bool have_axis = axis_unit(ua);
  w_now_ = have_axis ? dot3(w, ua) : 0.0f;
  if (!have_axis || fabsf(w_now_) < W_MIN) {   // стоит — оборот не считается
    t_rev0_ = -1;
    theta_ = 0;
    reset_rev();
    return false;
  }
  if (t_rev0_ < 0) t_rev0_ = t_;
  theta_ += w_now_ * dt;
  for (int i = 0; i < 3; ++i) sum_f_[i] += f[i];
  ++n_;
  const float m = norm3(f);
  if (m < fmin_) fmin_ = m;
  if (m > fmax_) fmax_ = m;
  if (fabsf(theta_) < 2.0f * (float)PI) return false;

  const float period = (float)(t_ - t_rev0_);      // разность — секунды, во float точна; само время копится в double
  float mean[3] = {sum_f_[0] / n_, sum_f_[1] / n_, sum_f_[2] / n_};
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
  ++revs_;
  theta_ -= theta_ > 0 ? 2.0f * (float)PI : -2.0f * (float)PI;
  t_rev0_ = t_;
  reset_rev();
  return true;
}
