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
    """

    W_MIN = 0.3                  # рад/с: медленнее — считаем, что колесо стоит
    AXIS_TAU = 2.0               # с: сглаживание оси колеса

    def __init__(self, r_mm=450.0):
        self.r_mm = r_mm
        self.axis = (0.0, 0.0, 0.0)          # сглаженный вектор гироскопа
        self.radial = None                   # единичный к центру (по прошлому обороту)
        self.theta = 0.0
        self.t = 0.0
        self.t_rev0 = None
        self._reset_rev()
        self.revs = []                       # законченные обороты
        self.w_now = 0.0

    def _reset_rev(self):
        self.sum_f = [0.0, 0.0, 0.0]
        self.n = 0
        self.fmin = float("inf")
        self.fmax = 0.0
        self.th_min = 0.0

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
        self.w_now = _dot(w, ua) if ua else 0.0
        if ua is None or abs(self.w_now) < self.W_MIN:
            self.t_rev0 = None               # стоит — оборот не считается
            self.theta = 0.0
            self._reset_rev()
            return None
        if self.t_rev0 is None:
            self.t_rev0 = self.t
        self.theta += self.w_now * dt
        for i in range(3):
            self.sum_f[i] += f[i]
        self.n += 1
        m = _norm(f)
        if m < self.fmin:
            self.fmin, self.th_min = m, self.theta
        self.fmax = max(self.fmax, m)
        if abs(self.theta) < 2 * math.pi:
            return None
        period = self.t - self.t_rev0
        mean = [x / self.n for x in self.sum_f]
        k = _norm(mean)
        if k > 1e-6:
            self.radial = tuple(x / k for x in mean)
        omega = 2 * math.pi / period
        rev = {"t": self.t, "period": period, "rpm": 60.0 / period, "w": omega, "k": k,
               "k_r": omega * omega * self.r_mm / 1000.0 / G,   # то же по заданному радиусу
               "r_mm": k * G / (omega * omega) * 1000.0,        # радиус по измерению
               "fmin": self.fmin, "fmax": self.fmax, "n": self.n}
        self.revs.append(rev)
        self.theta -= math.copysign(2 * math.pi, self.theta)
        self.t_rev0 = self.t
        self._reset_rev()
        return rev


def critical_rpm(r_mm):
    """Обороты невесомости в верхней точке для радиуса r."""
    return 60.0 / (2 * math.pi) * math.sqrt(G / (r_mm / 1000.0))
