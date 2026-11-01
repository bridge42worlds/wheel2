// Ядро расчёта поля тороидальной катушки. Длины в мм, поле в единицах μ0·I/π [1/мм].
// Каждый виток — круговая петля в меридиональной плоскости (точная формула через
// эллиптические интегралы); дальние петли — диполем. Ход провода вдоль кольца
// (один «большой виток») — набор горизонтальных колец по поверхности трубки.
(function (root) {
  'use strict';

  // Эллиптические интегралы K(m), E(m): Абрамовиц–Стиган 17.3.34/17.3.36, ошибка ≤ 2e-8
  function loopField(A, rho, z, eps2, out) {
    const r2 = rho * rho + z * z;
    const s = A * A + r2;
    let a2 = s - 2 * A * rho;
    if (a2 < eps2) a2 = eps2;
    const b2 = s + 2 * A * rho;
    const beta = Math.sqrt(b2);
    let m1 = a2 / b2;
    if (m1 < 1e-15) m1 = 1e-15;
    const L = -Math.log(m1);
    const K = (1.38629436112 + m1 * (0.09666344259 + m1 * (0.03590092383 + m1 * (0.03742563713 + m1 * 0.01451196212))))
      + L * (0.5 + m1 * (0.12498593597 + m1 * (0.06880248576 + m1 * (0.03328355346 + m1 * 0.00441787012))));
    const E = (1 + m1 * (0.44325141463 + m1 * (0.06260601220 + m1 * (0.04757383546 + m1 * 0.01736506451))))
      + L * m1 * (0.24998368310 + m1 * (0.09200180037 + m1 * (0.04069697526 + m1 * 0.00526449639)));
    const pre = 1 / (2 * a2 * beta);
    out[0] = pre * ((A * A - r2) * E + a2 * K);                                   // вдоль оси петли
    out[1] = rho > 1e-6 * A ? pre * z / rho * (s * E - a2 * K) : 0;              // радиально от оси
  }

  // Детерминированный генератор случайных чисел
  function rng32(seed) {
    let t = seed >>> 0;
    return function () {
      t = (t + 0x6D2B79F5) >>> 0;
      let x = Math.imul(t ^ (t >>> 15), 1 | t);
      x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
  }

  const NPROF = 3600; // отсчётов профиля по кольцу

  // Утолщения: бугры с разбросом шага, ширины и высоты; 0 … 1, максимум ровно 1
  function thickProfile(count, irr, seed) {
    const rnd = rng32(seed * 7919 + 13);
    const F = new Float64Array(NPROF);
    if (count < 1) return F;
    const step = 2 * Math.PI / count, off = rnd() * step;
    const bumps = [];
    for (let j = 0; j < count; j++) {
      bumps.push({
        c: off + step * (j + irr * 0.45 * (2 * rnd() - 1)),
        w: step * 0.26 * (1 + irr * 0.5 * (2 * rnd() - 1)),
        h: 1 - irr * 0.55 * rnd(),
      });
    }
    let mx = 0;
    for (let i = 0; i < NPROF; i++) {
      const ph = 2 * Math.PI * i / NPROF;
      let v = 0;
      for (const b of bumps) {
        let d = ph - b.c;
        d -= 2 * Math.PI * Math.round(d / (2 * Math.PI));
        v += b.h * Math.exp(-d * d / (2 * b.w * b.w));
      }
      F[i] = v; if (v > mx) mx = v;
    }
    let mn = Infinity;
    for (let i = 0; i < NPROF; i++) if (F[i] < mn) mn = F[i];
    for (let i = 0; i < NPROF; i++) F[i] = (F[i] - mn) / (mx - mn || 1);
    return F;
  }

  // Плотность намотки: плавная случайная волна вокруг характерного числа волн; −1 … 1
  function densProfile(waves, irr, seed) {
    const rnd = rng32(seed * 104729 + 7);
    const G = new Float64Array(NPROF);
    const comps = [];
    const width = 0.35 + irr * Math.max(1, waves) * 0.6;
    for (let m = 1; m <= 40; m++) {
      const w = Math.exp(-0.5 * Math.pow((m - waves) / width, 2));
      if (w < 0.02) continue;
      const amp = irr > 0 ? w * (0.4 + 0.6 * rnd()) : w;
      comps.push({ m, amp, ph: 2 * Math.PI * rnd() });
    }
    if (!comps.length) return G;
    let mx = -Infinity, mn = Infinity;
    for (let i = 0; i < NPROF; i++) {
      const ph = 2 * Math.PI * i / NPROF;
      let v = 0;
      for (const c of comps) v += c.amp * Math.cos(c.m * ph + c.ph);
      G[i] = v; if (v > mx) mx = v; if (v < mn) mn = v;
    }
    const mid = (mx + mn) / 2, half = (mx - mn) / 2 || 1;
    for (let i = 0; i < NPROF; i++) G[i] = (G[i] - mid) / half;
    return G;
  }

  function sampleProf(P, phi) {
    let u = phi / (2 * Math.PI) * NPROF;
    u -= NPROF * Math.floor(u / NPROF);
    const i = Math.floor(u), f = u - i;
    return P[i % NPROF] * (1 - f) + P[(i + 1) % NPROF] * f;
  }

  // Геометрия катушки: профили, положения витков, петли
  function buildCoil(p) {
    const F = thickProfile(p.thickCount, p.thickIrr, p.seed);
    const G = densProfile(p.densWaves, p.densIrr, p.seed);
    const aRel = new Float64Array(NPROF), nRel = new Float64Array(NPROF);
    let mean = 0;
    for (let i = 0; i < NPROF; i++) {
      aRel[i] = 1 + p.thickAmp * F[i];
      nRel[i] = 1 + 0.5 * p.densAmp * G[i];
      mean += nRel[i];
    }
    mean /= NPROF;
    for (let i = 0; i < NPROF; i++) nRel[i] /= mean;
    // Накопленное число витков → угол каждого витка
    const N = p.N, cum = new Float64Array(NPROF + 1);
    for (let i = 0; i < NPROF; i++) cum[i + 1] = cum[i] + nRel[i];
    const scale = N / cum[NPROF];
    const phiK = new Float64Array(N);
    let j = 0;
    for (let k = 0; k < N; k++) {
      const target = (k + 0.5) / scale;
      while (cum[j + 1] < target) j++;
      const f = (target - cum[j]) / (cum[j + 1] - cum[j]);
      phiK[k] = 2 * Math.PI * (j + f) / NPROF;
    }
    const cx = new Float64Array(N), cy = new Float64Array(N), tx = new Float64Array(N), ty = new Float64Array(N);
    const A = new Float64Array(N), cut2 = new Float64Array(N), pdip = new Float64Array(N);
    for (let k = 0; k < N; k++) {
      const c = Math.cos(phiK[k]), s = Math.sin(phiK[k]);
      cx[k] = p.R * c; cy[k] = p.R * s; tx[k] = -s; ty[k] = c;
      A[k] = p.a * sampleProf(aRel, phiK[k]);
      cut2[k] = Math.pow(p.dipCut * A[k], 2);
      pdip[k] = Math.PI * A[k] * A[k] / 4;
    }
    // Ход провода вдоль кольца: ток I по поверхности трубки, NR горизонтальных колец
    const NR = p.helix ? 16 : 0;
    const ringR = new Float64Array(NR), ringZ = new Float64Array(NR);
    for (let j2 = 0; j2 < NR; j2++) {
      const th = 2 * Math.PI * (j2 + 0.5) / NR;
      ringR[j2] = p.R + p.a * Math.cos(th); ringZ[j2] = p.a * Math.sin(th);
    }
    return {
      p, aRel, nRel, phiK, cx, cy, tx, ty, A, cut2, pdip, ringR, ringZ,
      eps2: Math.pow(p.wire / 2, 2),
    };
  }

  // Поле в точке (x,y,z) в единицах μ0·I/π; out = [Bx, By, Bz]
  function fieldAt(coil, x, y, z, out, tmp) {
    const { cx, cy, tx, ty, A, cut2, pdip, eps2 } = coil;
    const N = A.length;
    let bx = 0, by = 0, bz = 0;
    for (let k = 0; k < N; k++) {
      const dx = x - cx[k], dy = y - cy[k];
      const d2 = dx * dx + dy * dy + z * z;
      const zl = dx * tx[k] + dy * ty[k];
      if (d2 > cut2[k]) {
        const inv2 = 1 / d2, inv3 = inv2 * Math.sqrt(inv2);
        const f = pdip[k] * inv3, g = 3 * zl * inv2;
        bx += f * (g * dx - tx[k]); by += f * (g * dy - ty[k]); bz += f * g * z;
      } else {
        const u = dx * ty[k] - dy * tx[k];            // проекция на радиальное направление кольца
        const rho = Math.sqrt(u * u + z * z);
        loopField(A[k], rho, zl, eps2, tmp);
        const br = rho > 0 ? tmp[1] / rho : 0;
        bx += tmp[0] * tx[k] + br * u * ty[k];
        by += tmp[0] * ty[k] - br * u * tx[k];
        bz += br * z;
      }
    }
    const NR = coil.ringR.length;
    if (NR) {
      const rr = Math.sqrt(x * x + y * y), w = 1 / NR;
      for (let j = 0; j < NR; j++) {
        loopField(coil.ringR[j], rr, z - coil.ringZ[j], eps2, tmp);
        const br = rr > 0 ? w * tmp[1] / rr : 0;
        bx += br * x; by += br * y; bz += w * tmp[0];
      }
    }
    out[0] = bx; out[1] = by; out[2] = bz;
  }

  // Поле на наборе точек: pts = Float64Array [x,y,z,...] → Float32Array [Bx,By,Bz,...]
  function fieldMany(coil, pts, from, to, res) {
    const out = [0, 0, 0], tmp = [0, 0];
    for (let i = from; i < to; i++) {
      fieldAt(coil, pts[3 * i], pts[3 * i + 1], pts[3 * i + 2], out, tmp);
      res[3 * i] = out[0]; res[3 * i + 1] = out[1]; res[3 * i + 2] = out[2];
    }
  }

  const api = { loopField, rng32, NPROF, thickProfile, densProfile, sampleProf, buildCoil, fieldAt, fieldMany };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TorCore = api;
})(typeof self !== 'undefined' ? self : this);
