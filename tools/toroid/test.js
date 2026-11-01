const C = require('./core.js');
const base = { R: 150, a: 15, N: 1000, wire: 0.7, thickAmp: 0, thickCount: 8, thickIrr: 0, densAmp: 0, densWaves: 6, densIrr: 0, seed: 1, helix: false, dipCut: 10 };

function bphi(coil, x, y, z) {
  const o = [0, 0, 0], t = [0, 0];
  C.fieldAt(coil, x, y, z, o, t);
  const r = Math.hypot(x, y), ph = Math.atan2(y, x);
  const bp = -o[0] * Math.sin(ph) + o[1] * Math.cos(ph);
  const br = o[0] * Math.cos(ph) + o[1] * Math.sin(ph);
  return { b: bp * 2 * r / coil.p.N, mag: Math.hypot(o[0], o[1], o[2]) / (coil.p.N / (2 * coil.p.R)), br: br / bp, bz: o[2] / bp };
}

// 1. Идеальный тор: b = 1 внутри, 0 снаружи; диполь против точного
for (const cut of [1e9, 10, 6]) {
  const coil = C.buildCoil({ ...base, dipCut: cut });
  const pts = [[150, 0, 0], [160, 0, 5], [138, 0, -6], [150, 0, 12], [163, 0, 0], [137.5, 0, 0]];
  const out = [[175, 0, 0], [150, 0, 25], [0, 0, 0], [120, 0, 0], [200, 0, 40]];
  const t0 = Date.now();
  console.log('cut', cut, 'inside b:', pts.map(q => bphi(coil, ...q).b.toFixed(5)).join(' '),
    '| outside |B|/B0:', out.map(q => bphi(coil, ...q).mag.toExponential(2)).join(' '));
}

// 2. Одна гармоника плотности, m=6: линейная теория T = ka·K1(ka), ka = 0.6 → 0.782 × 10% = 7.8%
function scanCenter(p) {
  const coil = C.buildCoil(p);
  let mn = 9, mx = -9;
  for (let i = 0; i < 360; i++) {
    const ph = 2 * Math.PI * i / 360;
    const v = bphi(coil, 150 * Math.cos(ph), 150 * Math.sin(ph), 0).b;
    mn = Math.min(mn, v); mx = Math.max(mx, v);
  }
  return ((mx - mn) / 2 * 100).toFixed(2) + '% (min ' + mn.toFixed(4) + ', max ' + mx.toFixed(4) + ')';
}
console.log('плотность ±10%, m=6, амплитуда b на оси:', scanCenter({ ...base, densAmp: 0.2, densWaves: 6 }), '— теория ≈7.8%');
// 3. Утолщение — в тесте бугры, а не гармоника; просто смотрим порядок
console.log('утолщение 0…+20%, 6 бугров, амплитуда b на оси:', scanCenter({ ...base, thickAmp: 0.2, thickCount: 6 }));
// 4. Ход провода
console.log('ход провода, центр трубки b =', bphi(C.buildCoil({ ...base, helix: true }), 150, 0, 0));

// 5. Скорость
const coil = C.buildCoil({ ...base, thickAmp: 0.2, densAmp: 0.2, helix: true });
const n = 20000, pts = new Float64Array(3 * n), res = new Float32Array(3 * n);
for (let i = 0; i < n; i++) { const ph = 2 * Math.PI * i / n; pts[3 * i] = (150 + 10 * Math.sin(i)) * Math.cos(ph); pts[3 * i + 1] = (150 + 10 * Math.sin(i)) * Math.sin(ph); pts[3 * i + 2] = 8 * Math.cos(i * 1.3); }
let t0 = Date.now(); C.fieldMany(coil, pts, 0, n, res); console.log('20k точек:', Date.now() - t0, 'мс');
