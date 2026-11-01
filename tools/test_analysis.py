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
from analysis import G, WheelTracker, critical_rpm, gyro_eps, rim_fit, steady   # noqa: E402

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
check("k по форме кривой (разгон: k за оборот меняется на 1,5 %)", r["kc"], tt["k"], 0.015)
check("постоянная часть |f|² = 1 + k²", r["a0"], 1 + tt["k"] ** 2, 0.03)


# ---------------- 2. ошибки приборов: масштаб гироскопа, смещение нуля акселерометра ----------------
def run(segments, eps, abias, direction, seed, noise=0.004, vib=0.0, ripple=0.0, wander=0.0):
    """Колесо ступенями скорости (об/мин, секунд) с плавными переходами; гироскоп врёт на eps, у акселерометра —
    смещение abias (g, в осях датчика). direction −1 — колесо крутится назад. Возвращает обороты и правду по ним.
    Неровное колесо: noise — шум акселерометра, g; vib — вибрация 23 и 61 Гц, g; ripple — пульсация скорости раз
    за оборот (доля); wander — медленное блуждание скорости (доля, время 5 с)."""
    random.seed(seed)
    rot2, tr2, th, t2, out = rot_random(), WheelTracker(r_mm=450), 0.0, 0.0, []
    wd, w_prev = 0.0, None
    plan = []
    for i, (rpm, sec) in enumerate(segments):
        prev = segments[i - 1][0] if i else rpm
        plan.append((prev, rpm, 4.0))                  # 4 с переход
        plan.append((rpm, rpm, sec))
    for r0, r1, sec in plan:
        n = int(sec * fs)
        for j in range(n):
            wd += -wd * dt / 5.0 + wander * math.sqrt(2 * dt / 5.0) * random.gauss(0, 1)
            rpm = (r0 + (r1 - r0) * j / n) * (1 + ripple * math.sin(th) + wd)
            w = direction * rpm / 60 * 2 * math.pi
            alpha = (w - w_prev) / dt if w_prev is not None else 0.0
            w_prev = w
            th += w * dt
            t2 += dt
            c, s = math.cos(th), math.sin(th)
            a = (-w * w * R * c - alpha * R * s, -w * w * R * s + alpha * R * c, 0.0)
            f_lab = (a[0], a[1] + G, 0.0)
            e_r, e_t = (-c, -s, 0.0), (-s, c, 0.0)
            f_body = (sum(f_lab[i] * e_r[i] for i in range(3)) / G, sum(f_lab[i] * e_t[i] for i in range(3)) / G, 0.0)
            vb = vib * (math.sin(2 * math.pi * 23 * t2) + 0.6 * math.sin(2 * math.pi * 61 * t2 + 1.0))
            f_s = tuple(v + b + vb * (0.5, -0.3, 0.8)[i] + random.gauss(0, noise)
                        for i, (v, b) in enumerate(zip(mul(rot2, f_body), abias)))
            w_s = tuple((1 + eps) * v + b + random.gauss(0, 0.002) for v, b in zip(mul(rot2, (0, 0, w)), bias))
            rv = tr2.add(f_s, w_s, dt)
            if rv:
                rv["true_rpm"], rv["true_k"] = abs(rpm), (rpm / 60 * 2 * math.pi) ** 2 * R / G
                out.append(rv)
    return out, rot2


SEG = [(30, 25), (38, 25), (44.6, 25), (52, 25)]
EPS, AB = 0.015, (0.020, -0.015, 0.010)
for direction in (1, -1):
    print(f"\n2. Гироскоп врёт на +{EPS * 100:g} %, смещение нуля акселерометра {tuple(round(v * 1000) for v in AB)} мg, "
          f"ступени {', '.join(f'{s[0]:g}' for s in SEG)} об/мин, колесо {'вперёд' if direction > 0 else 'назад'}")
    rv, rot2 = run(SEG, EPS, AB, direction, 11 if direction > 0 else 12)
    st = steady(rv)
    print(f"   оборотов {len(rv)}, ровных {len(st)}")
    ge = gyro_eps(rv)
    print(f"   первый оборот: обороты врут на {100 * (rv[0]['rpm'] / rv[0]['true_rpm'] - 1):+.2f} % (подстройки ещё нет)")
    check("масштаб гироскопа: самоподстройка (смещение нуля гироскопа даёт ±0,2 %)", ge["gs"], EPS, 0.002)
    check("что осталось после подстройки", ge["eps_left"], 0.0, 0.0005)
    last = [x for x in st if abs(x["true_rpm"] - 44.6) < 0.05][-1]
    check("обороты на невесомости, % ошибки", 100 * (last["rpm"] / last["true_rpm"] - 1), 0.0, 0.1)
    fit = rim_fit(rv)
    check("радиус по скоростям, мм", fit["r_mm"], 450, 3)
    for i, ax in enumerate("xyz"):
        check(f"смещение нуля {ax}, мg", fit["bias"][i] * 1000, AB[i] * 1000, 3)
    kc = math.sqrt(sum((m - b) ** 2 for m, b in zip(last["m"], fit["bias"])))
    print(f"   на невесомости: k по среднему {last['k']:.4f}, по форме {last['kc']:.4f}, правда {last['true_k']:.4f}")
    check("k по среднему без смещения нуля", kc, last["true_k"], 0.005)

# ---------------- 3. исправные приборы, ровная скорость: k по форме и по среднему — одно ----------------
print("\n3. Исправные приборы, 44,6 об/мин ровно")
rv, _ = run([(44.6, 40)], 0.0, (0.0, 0.0, 0.0), 1, 21)
last = steady(rv)[-1]
check("k по среднему", last["k"], last["true_k"], 0.003)
check("k по форме кривой", last["kc"], last["true_k"], 0.003)
check("форма чистая: вторая гармоника к первой", last["h2"], 0.0, 0.05)

# ---------------- 4. неровное колесо: шум, вибрация, пульсация и блуждание скорости ----------------
print("\n4. Неровное колесо: шум 20 мg, вибрация 23 и 61 Гц 50 мg, пульсация скорости ±0,3 % за оборот, "
      "блуждание ±0,1 %; гироскоп +1,5 %, смещение нуля как в п. 2")
SEG4 = [(30, 40), (38, 40), (44.6, 40), (52, 40)]
rv, _ = run(SEG4, EPS, AB, 1, 31, noise=0.020, vib=0.05, ripple=0.003, wander=0.001)
ge = gyro_eps(rv)
print(f"   оборотов {len(rv)}, ровных по признаку flat {sum(1 for x in rv if x['flat'])}")
check("масштаб гироскопа: самоподстройка", ge["gs"], EPS, 0.003)
last = [x for x in rv if x["flat"] and abs(x["true_rpm"] - 44.6) < 0.3][-1]
check("обороты на невесомости, % ошибки", 100 * (last["rpm"] / last["true_rpm"] - 1), 0.0, 0.3)
fit = rim_fit(rv)
check("радиус по скоростям, мм", fit["r_mm"], 450, 6)
for i, ax in enumerate("xyz"):
    check(f"смещение нуля {ax}, мg", fit["bias"][i] * 1000, AB[i] * 1000, 6)
print(f"   разброс среднего около прямой {fit['rms_mg']:.1f} мg, вторая гармоника {last['h2']:.3f}")

print("\nвсё сходится" if ok_all else "\nЕСТЬ РАСХОЖДЕНИЯ")
sys.exit(0 if ok_all else 1)
