"""Проверка расчётов server/analysis.py на синтетических данных — без датчиков.

    python tools/test_analysis.py

1. Колесо радиусом 450 мм, плоскость вертикальна. Датчик повёрнут в трубке как
   угодно (случайный поворот), к показаниям добавлен шум и смещение гироскопа.
   Скорость растёт от 30 до 60 об/мин, проходя невесомость (44,6 об/мин).
   Алгоритм должен восстановить обороты, k = ω²r/g, радиус, наименьшее и
   наибольшее |f| за оборот — и оси колеса в осях датчика.
Статические акселерометры считает сервер — их проверка в tools/test_static_watch.py.
"""
import math, os, random, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from analysis import G, WheelTracker, critical_rpm   # noqa: E402

random.seed(7)
ok_all = True


def check(name, got, want, tol):
    global ok_all
    ok = abs(got - want) <= tol
    ok_all &= ok
    print(f"   {'✓' if ok else '✗'} {name}: {got:.4f} (ожидалось {want:.4f} ± {tol})")


def rot_random():
    """Случайный поворот (кватернион) — матрица 3×3."""
    q = [random.gauss(0, 1) for _ in range(4)]
    n = math.sqrt(sum(x * x for x in q))
    w, x, y, z = (c / n for c in q)
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def mul(m, v):
    return tuple(sum(m[i][j] * v[j] for j in range(3)) for i in range(3))


# ---------------- 1. колесо ----------------
print("1. Колесо, r = 450 мм, обороты невесомости", f"{critical_rpm(450):.2f}")
R = 0.45
fs = 100.0
dt = 1 / fs
rot = rot_random()
bias = (0.004, -0.003, 0.002)           # смещение гироскопа, рад/с
tr = WheelTracker(r_mm=450)
theta = 0.0
t = 0.0
revs = []
truth = []
while t < 120:
    rpm = 30 + 30 * t / 120                 # 30 → 60 об/мин за 2 минуты
    w = rpm / 60 * 2 * math.pi
    alpha = (30 / 120) / 60 * 2 * math.pi   # угловое ускорение, рад/с²
    theta += w * dt
    t += dt
    # Плоскость колеса — xy, ось z. Датчик на угле theta: к центру — -(cos, sin).
    c, s = math.cos(theta), math.sin(theta)
    a = (-w * w * R * c - alpha * R * s, -w * w * R * s + alpha * R * c, 0.0)   # м/с²
    f_lab = (a[0], a[1] + G, 0.0)            # показание = ускорение − тяжесть (тяжесть −y)
    e_r, e_t, e_a = (-c, -s, 0.0), (-s, c, 0.0), (0.0, 0.0, 1.0)
    f_body = (sum(f_lab[i] * e_r[i] for i in range(3)) / G,
              sum(f_lab[i] * e_t[i] for i in range(3)) / G, 0.0)
    f_s = tuple(v + random.gauss(0, 0.004) for v in mul(rot, f_body))         # шум 4 мg
    w_s = tuple(v + b + random.gauss(0, 0.002) for v, b in zip(mul(rot, (0, 0, w)), bias))
    r = tr.add(f_s, w_s, dt)
    if r:
        revs.append(r)
        # правда для этого оборота — по средней скорости
        wm = r["w"]
        truth.append({"k": wm * wm * R / G, "fmin": abs(1 - wm * wm * R / G), "fmax": 1 + wm * wm * R / G})

print(f"   оборотов найдено: {len(revs)}")
# сравнить в середине разгона: оборот, ближайший к невесомости
i = min(range(len(revs)), key=lambda j: abs(revs[j]["k"] - 1))
r, tt = revs[i], truth[i]
print(f"   оборот №{i + 1}: {r['rpm']:.2f} об/мин")
check("k по среднему ускорению", r["k"], tt["k"], 0.01)
check("k по заданному радиусу", r["k_r"], tt["k"], 0.01)
check("радиус по измерению, мм", r["r_mm"], 450, 6)
check("наименьшее |f| (верх), g", r["fmin"], tt["fmin"], 0.03)
check("наибольшее |f| (низ), g", r["fmax"], tt["fmax"], 0.03)
# оси колеса: ось вращения в осях датчика — это rot·z, к центру в среднем — rot·e_r
ua = tr.axis_unit()
true_axis = mul(rot, (0, 0, 1))
check("ось колеса: |cos угла с правдой|", abs(sum(a * b for a, b in zip(ua, true_axis))), 1.0, 0.001)
wf = tr.wheel_frame(tuple(mul(rot, (0.5, -0.2, 0.0))))
check("разложение по осям колеса: вдоль оси", wf[2], 0.0, 0.01)
# при 60 об/мин k = 0.9·... — последний оборот выше невесомости
check("последний оборот: обороты", revs[-1]["rpm"], 60, 0.6)

print("\nвсё сходится" if ok_all else "\nЕСТЬ РАСХОЖДЕНИЯ")
sys.exit(0 if ok_all else 1)
