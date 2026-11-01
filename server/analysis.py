"""Расчёты по датчикам — эталон на Python.

Те же алгоритмы работают в прошивке (C++): сначала проверяются здесь на
синтетических данных (tools/test_analysis.py), потом переносятся один в один.

Колесо (ICM-42688 на ободе): обороты, близость к невесомости, радиус, оси
колеса в координатах датчика. Статические акселерометры считает сервер сам —
static_watch.py.
"""
import math

G = 9.80665                      # м/с²


def _norm(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class WheelTracker:
    """Обороты и невесомость по акселерометру и гироскопу на ободе.

    На вход — показания в осях датчика: f — ускорение в g (то, что показывает
    акселерометр: ускорение минус тяжесть), w — угловая скорость в рад/с.

    Ось колеса — направление среднего вектора гироскопа. Угол поворота —
    интеграл скорости вокруг этой оси; полный оборот — 2π. За оборот тяжесть
    усредняется в ноль, остаётся центростремительное: его среднее направлено к
    центру и по модулю равно ω²r/g = k. k = 1 — невесомость в верхней точке.
    Отдельно по каждому обороту — наименьшее и наибольшее |f| (верх и низ).

    Форма кривой. |f| за оборот — не синусоида (при k = 1 это 2·|sin(φ/2)|, «галочка» до нуля наверху), но
    квадрат — точная синусоида: |f|²/g² = 1 + k² + 2k·cos φ (φ — угол тяжести в осях датчика). Поэтому за
    оборот |f|² раскладывается по углу гироскопа (вес — пройденный угол, разгон внутри оборота не мешает):
      kc  — k по форме (половина первой гармоники); с k по среднему не обязаны совпасть: смещение нуля
            акселерометра входит в них по-разному — расхождение его и выдаёт;
      a0  — постоянная часть, g²: должна быть 1 + k² (масштаб акселерометра);
      ph1 — фаза тяжести в угле гироскопа; dph — её сдвиг с прошлого оборота. Тяжесть делает ровно один оборот
            за оборот колеса, поэтому сдвиг — это ошибка масштаба гироскопа: eps = −dph/2π (знак — по ходу);
            rpm_g — обороты с этой поправкой (на ровной скорости; на разгоне сдвиг даёт и касательное ускорение);
      h2  — вторая гармоника к первой: у ровного колеса ≈ 0; растёт от биения, рывков, смещения датчика;
      m   — среднее ускорение по осям датчика, g: m = (ω²r/g)·к центру + смещение нуля — по оборотам на разных
            скоростях сервер разделяет радиус и смещение (rim_fit).
    """

    W_MIN = 0.3                  # рад/с: медленнее — считаем, что колесо стоит
    AXIS_TAU = 2.0               # с: сглаживание оси колеса
    GS_ALPHA = 0.2               # масштаб гироскопа: доля найденной ошибки, принимаемая за ровный оборот
    GS_MAX = 0.05                # поправка не больше ±5 %

    def __init__(self, r_mm=450.0):
        self.r_mm = r_mm
        self.axis = (0.0, 0.0, 0.0)          # сглаженный вектор гироскопа
        self.radial = None                   # единичный к центру (по прошлому обороту)
        self.theta = 0.0
        self.t = 0.0
        self.t_rev0 = None
        self.ph_prev = None                  # фаза тяжести прошлого оборота (если обороты шли подряд)
        self.rpm_prev = None
        # Самоподстройка масштаба гироскопа: угол считается как ∫ω·dt / (1 + gs). Ошибку, оставшуюся после
        # поправки, показывает сдвиг фазы тяжести; на ровных оборотах gs подтягивается к ней. С верным gs «оборот»
        # по гироскопу — настоящий оборот: обороты верны, и тяжесть за оборот усредняется в ноль (иначе при ошибке
        # 1,5 % в среднем остаётся ~15 мg, вращаясь от оборота к обороту, — радиус и смещение нуля не найти)
        self.gs = 0.0
        self._reset_rev()
        self.revs = []                       # законченные обороты
        self.w_now = 0.0

    def _reset_rev(self):
        self.sum_f = [0.0, 0.0, 0.0]
        self.n = 0
        self.fmin = float("inf")
        self.fmax = 0.0
        self.th_min = 0.0
        self.h = [0.0] * 6                   # |f|² по углу: сумма, cos θ, sin θ, cos 2θ, sin 2θ — с весом dθ; весь угол

    def axis_unit(self):
        n = _norm(self.axis)
        return tuple(x / n for x in self.axis) if n > 1e-9 else None

    def wheel_frame(self, f):
        """f в осях колеса: (к центру, по ходу, вдоль оси, |f|); None — оси ещё не известны."""
        ua = self.axis_unit()
        if ua is None or self.radial is None:
            return None
        ut = _cross(ua, self.radial)
        return (_dot(f, self.radial), _dot(f, ut), _dot(f, ua), _norm(f))

    def add(self, f, w, dt):
        """Одно показание; dt — с с прошлого. Возвращает законченный оборот или None."""
        self.t += dt
        a = min(1.0, dt / self.AXIS_TAU)
        if _norm(w) > self.W_MIN:            # ось — только пока колесо крутится
            # знак оси держим постоянным: иначе при смене направления среднее обнулится
            s = 1.0 if _dot(w, self.axis) >= 0 else -1.0
            self.axis = tuple((1 - a) * x + a * s * y for x, y in zip(self.axis, w))
        ua = self.axis_unit()
        self.w_now = _dot(w, ua) / (1 + self.gs) if ua else 0.0     # с поправкой масштаба гироскопа
        if ua is None or abs(self.w_now) < self.W_MIN:
            self.t_rev0 = None               # стоит — оборот не считается
            self.theta = 0.0
            self.ph_prev = self.rpm_prev = None
            self._reset_rev()
            return None
        if self.t_rev0 is None:
            self.t_rev0 = self.t
        self.theta += self.w_now * dt
        m = _norm(f)
        m2 = m * m
        self._acc(f, m2, self.theta, abs(self.w_now * dt))
        self.n += 1
        if m < self.fmin:
            self.fmin, self.th_min = m, self.theta
        self.fmax = max(self.fmax, m)
        if abs(self.theta) < 2 * math.pi:
            return None
        # Конец оборота — между отсчётами: угол перелетел 2π на over рад, это over/|ω| с назад. Без этого период
        # дрожит на один отсчёт (±0,3 % на 200 Гц при 45 об/мин) — ровные обороты кажутся неровными. Окно — ровно
        # 2π: часть последнего отсчёта за 2π уходит в следующий оборот (иначе гармоники размазываются на ~0,4 %)
        over = abs(self.theta) - 2 * math.pi
        t_end = self.t - over / abs(self.w_now)
        self._acc(f, m2, self.theta, -over)
        period = t_end - self.t_rev0
        mean = [x / max(self.h[5], 1e-9) for x in self.sum_f]
        k = _norm(mean)
        if k > 1e-6:
            self.radial = tuple(x / k for x in mean)
        omega = 2 * math.pi / period
        rev = {"t": self.t, "period": period, "rpm": 60.0 / period, "w": omega, "k": k,
               "k_r": omega * omega * self.r_mm / 1000.0 / G,   # то же по заданному радиусу
               "r_mm": k * G / (omega * omega) * 1000.0,        # радиус по измерению
               "fmin": self.fmin, "fmax": self.fmax, "n": self.n}
        # форма кривой: |f|² = a0 + a1·cos θ + b1·sin θ + … по углу гироскопа θ
        s0, c1, s1, c2, s2, ang = self.h
        ang = ang if ang > 1e-9 else 1e-9
        a1, b1, a2, b2 = 2 * c1 / ang, 2 * s1 / ang, 2 * c2 / ang, 2 * s2 / ang
        B1 = math.hypot(a1, b1)
        ph1 = math.atan2(b1, a1)
        dph = None if self.ph_prev is None else (ph1 - self.ph_prev + math.pi) % (2 * math.pi) - math.pi
        self.ph_prev = ph1
        # гироскоп (с нынешней поправкой) врёт ещё в (1 + eps) раз: «оборот» по нему — 2π/(1 + eps) настоящего,
        # фаза тяжести сдвигается на 2π·eps/(1 + eps) за оборот (по ходу колеса)
        x = None if dph is None else math.copysign(1.0, self.theta) * dph / (2 * math.pi)
        eps = None if x is None or x >= 0.5 else x / (1 - x)
        h2 = math.hypot(a2, b2) / max(B1, 1e-9)
        rpm = 60.0 / period
        # ровный оборот: скорость как у прошлого (±0,5 %), тяжесть видна (k ≥ 0,05), форма чистая
        flat = self.rpm_prev is not None and abs(rpm - self.rpm_prev) <= 0.005 * rpm and B1 >= 0.1 and h2 < 0.2
        if flat and eps is not None:
            self.gs = max(-self.GS_MAX, min(self.GS_MAX, (1 + self.gs) * (1 + self.GS_ALPHA * eps) - 1))
        self.rpm_prev = rpm
        rev.update({"kc": B1 / 2, "a0": s0 / ang, "ph1": ph1, "dph": dph, "eps": eps, "gs": self.gs, "flat": flat,
                    "h2": h2, "m": tuple(mean)})
        self.revs.append(rev)
        self.theta -= math.copysign(2 * math.pi, self.theta)
        self.t_rev0 = t_end
        self._reset_rev()
        self._acc(f, m2, self.theta, over)       # перелёт — начало следующего оборота
        return rev

    def _acc(self, f, m2, th, wgt):
        """В суммы оборота — показание с весом wgt (пройденный угол, рад)."""
        h = self.h
        for i in range(3):
            self.sum_f[i] += f[i] * wgt          # среднее — по углу: разгон внутри оборота не перекашивает
        h[0] += m2 * wgt
        h[1] += m2 * math.cos(th) * wgt
        h[2] += m2 * math.sin(th) * wgt
        h[3] += m2 * math.cos(2 * th) * wgt
        h[4] += m2 * math.sin(2 * th) * wgt
        h[5] += wgt


def critical_rpm(r_mm):
    """Обороты невесомости в верхней точке для радиуса r."""
    return 60.0 / (2 * math.pi) * math.sqrt(G / (r_mm / 1000.0))


def steady(revs, tol=0.005):
    """Обороты на ровной скорости: соседние отличаются не больше tol — там фазе тяжести и среднему можно верить."""
    out = []
    for i, r in enumerate(revs):
        nb = [revs[j]["rpm"] for j in (i - 1, i + 1) if 0 <= j < len(revs)]
        if nb and all(abs(x - r["rpm"]) <= tol * r["rpm"] for x in nb):
            out.append(r)
    return out


def gyro_eps(revs):
    """Масштаб гироскопа: поправка, к которой пришла самоподстройка (gs), и что осталось (медиана eps на ровных
    оборотах — должна быть около нуля); None — ровных оборотов мало."""
    F = [r for r in revs if r.get("flat") and r.get("eps") is not None]
    if len(F) < 5:
        return None
    e = sorted(r["eps"] for r in F[-30:])
    med = e[len(e) // 2]
    mad = sorted(abs(x - med) for x in e)[len(e) // 2]
    return {"gs": F[-1]["gs"], "eps_left": med, "spread": 1.4826 * mad, "n": len(F)}


def rim_fit(revs):
    """Радиус датчика и смещение нуля акселерометра по оборотам на разных скоростях.

    Среднее ускорение за оборот в осях датчика: m = ω²·(r/g)·(к центру) + b. По каждой оси — прямая по ω²:
    наклон даёт вектор (r/g)·(к центру), отрезок — смещение b. Обороты — уже с поправкой масштаба гироскопа
    (gs): только тогда тяжесть за оборот усредняется в ноль. Нужен разброс скоростей хотя бы ±15 % (иначе
    наклон и отрезок не разделить) — тогда None."""
    S = [r for r in revs if r.get("flat") and r.get("m") and abs(r.get("eps") or 0) < 0.002]   # подстройка сошлась
    if len(S) < 6:
        return None
    x = [r["w"] ** 2 for r in S]
    xm = sum(x) / len(x)
    vx = sum((v - xm) ** 2 for v in x) / len(x)
    if vx < (0.3 * xm) ** 2 / 3:                       # ω² разбросан меньше ±30 % (обороты — меньше ±15 %)
        return None
    c, b, res = [0.0] * 3, [0.0] * 3, 0.0
    for i in range(3):
        y = [r["m"][i] for r in S]
        ym = sum(y) / len(y)
        c[i] = sum((xv - xm) * (yv - ym) for xv, yv in zip(x, y)) / len(x) / vx
        b[i] = ym - c[i] * xm
        res += sum((yv - (c[i] * xv + b[i])) ** 2 for xv, yv in zip(x, y)) / len(x)
    cn = _norm(c)
    return {"r_mm": cn * G * 1000.0, "radial": tuple(v / cn for v in c) if cn > 0 else None,
            "bias": tuple(b), "bias_mg": _norm(b) * 1000.0, "rms_mg": math.sqrt(res) * 1000.0, "n": len(S)}
