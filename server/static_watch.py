# -*- coding: utf-8 -*-
"""Статические акселерометры: эталон шума и слежение за спектром.

Агент со статическим акселерометром (роль S) только меряет: ICM-42688 1000 Гц,
среднее по avg отсчётам — на сервер идут оси x, y, z (при avg = 2 — 500 Гц)
пакетами "acc". Всё остальное считается здесь.

Окно — 10 с. По каждому окну и оси:
  спектр       — Уэлч: отрезки 1024 отсчёта (при 500 Гц — 2 с, шаг частоты
                 0,49 Гц), окно Ханна, перекрытие наполовину; плотность, g²/Гц;
  полосы       — мощность в октавах 1–2, 2–4, … Гц, дБ;
  форма шума   — асимметрия и эксцесс после вычитания среднего каждой секунды
                 (у гауссова шума оба около нуля);
  квантование  — доля значений, кратных 2, 4, … 256: у живого шума 1/2, 1/4, …;
                 «гребёнка» (урок LIS3DH) — все значения кратны одному шагу;
  среднее      — наклон или сдвиг датчика; зашкал; пропуски отсчётов.
По каждой секунде — среднее, разброс, крайние: графики и толчки.

Эталон — те же показатели за несколько минут, пока основные группы стоят:
форма спектра (среднее и разброс по окнам в каждом бине), уровни полос, форма
шума, квантование, разброс за секунду, среднее.

Каждое окно сравнивается с эталоном:
  линия        — бин выше эталона больше чем на Z_LINE его разбросов;
  полоса       — мощность полосы ушла больше чем на Z_BAND;
  форма шума   — асимметрия или эксцесс ушли больше чем на Z_GAUSS;
  квантование  — доля кратных ушла больше чем на Z_QUANT — это прибор;
  толчок       — разброс секунды в KNOCK_X раз выше обычного или выброс в
                 KNOCK_PEAK разбросов;
  сдвиг        — среднее ушло больше чем на MOVED_UG: датчик сдвинули, наклонили;
  пропуски, зашкал.
Линия рядом с известным источником (сеть 50 Гц, пинг CSI агентов, маяки
роутера, своя телеметрия, обороты колеса) помечается им и тревогой не считается.

Вывод по отрезку времени (шаг программы, сессия, последние минуты):
  ↻ повторить  — толчок, сдвиг, сбой прибора: данные отрезка испорчены;
  ⚠ проверить  — отклонение от эталона без известной причины держится не
                 меньше PERSIST окон подряд;
  ✓ продолжать — всё как в эталоне.

Отсчёты хранятся как есть: data/accel/<дата>/<mac>.bin (int16 x y z подряд) и
<mac>.idx.jsonl (по строке на пакет: номер первого отсчёта, сколько, частота,
метка времени). Эталоны — data/static/baselines/, отклонения — data/static/events.jsonl.

Нужен numpy.
"""
import base64, json, math, os, threading, time
from collections import deque

import numpy as np

WIN_S = 10                 # окно анализа, с
NPERSEG = 1024             # отрезок Уэлча
KEEP_S = 15 * 60           # отсчётов в памяти: 15 минут
KEEP_WIN = 720             # окон в памяти: 2 часа
KEEP_SEC = 7200            # секунд в памяти: 2 часа
OCTAVES = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 256), (256, 512)]
F_MIN = 1.0                # ниже — дрейф, линии не ищем
Z_LINE = 6.0
Z_BAND = 5.0
Z_GAUSS = 5.0
Z_QUANT = 6.0
KNOCK_X = 4.0              # разброс секунды выше обычного во столько раз — толчок
KNOCK_PEAK = 8.0           # или выброс в столько обычных разбросов (у гаусса — никогда)
MOVED_UG = 5000.0          # среднее ушло на 5 мg — сдвинули или наклонили (дрейф от тепла меньше)
PERSIST = 2                # окон подряд — уже не случайность
MIN_BASE_WIN = 6           # эталон — не меньше минуты
QBITS = 8                  # квантование: шаги 2 … 256

# вид отклонения -> класс: помеха (повторить), прибор (повторить), отклонение (проверить)
CLS = {"knock": "dist", "spikes": "dist", "moved": "dist", "quant": "inst", "gap": "inst", "sat": "inst", "mode": "inst",
       "line": "dev", "gone": "dev", "band": "dev", "gauss": "dev"}
KIND_NAMES = {"knock": "толчок", "spikes": "выбросы", "moved": "сдвиг", "quant": "квантование", "gap": "пропуск отсчётов",
              "sat": "зашкал", "mode": "режим датчика", "line": "линия", "gone": "линия пропала",
              "band": "полоса", "gauss": "форма шума"}
AXES = "xyz"
DB = 10.0 / math.log(10.0)  # 4,343: разброс логарифма средней по n отрезкам — DB/√n


def band_name(b):
    lo, hi = OCTAVES[b]
    return f"{lo}–{hi} Гц"


def fnum(x, d=1):
    return f"{x:.{d}f}".replace(".", ",")


def alias(f, fs):
    """Частота, на которую ляжет f при отсчётах fs."""
    return abs(((f + fs / 2) % fs) - fs / 2)


def runmed(a, h):
    """Скользящая медиана по ±h соседям."""
    a = np.asarray(a, dtype=np.float64)
    return np.array([np.median(a[max(0, i - h):i + h + 1]) for i in range(len(a))])


def quant_check(q, sd_lsb, n, e_base=None):
    """Квантование по одной оси. q[k-1] — доля значений, кратных 2^k. Проверяются только шаги
    не больше половины разброса: у узкого распределения доля кратных крупному шагу честно
    больше 2^-k (рядом с нулём — сам ноль). Грубый режим всё равно виден: при нём все значения
    кратны шагу, а значит, и 2, и 4. Возвращает (z худшего шага, номер шага, крупнейший шаг,
    которому кратны почти все значения) или None, если проверять нечего."""
    q = np.asarray(q, dtype=np.float64)
    step = max([2 ** (k + 1) for k in range(QBITS) if q[k] > 0.9], default=1)
    kmax = int(math.floor(math.log2(max(sd_lsb, 1e-9) / 2))) if sd_lsb >= 4 else (1 if step > 1 else 0)
    kmax = min(QBITS, kmax)
    if kmax < 1:
        return None
    e = 0.5 ** np.arange(1, kmax + 1)
    if e_base is not None:
        e = np.minimum(np.asarray(e_base[:kmax], dtype=np.float64), 0.999)
    z = (q[:kmax] - e) / np.sqrt(np.maximum(e * (1 - e), 1e-6) / max(1, n))
    k = int(np.argmax(np.abs(z)))
    return float(z[k]), k + 1, step


class Sensor:
    def __init__(self, mac):
        self.mac = mac
        self.mode = None           # (avg, afs, lsb) — смена режима датчика начинает поток заново
        self.hz = None             # номинальная частота отсчётов: 1000/avg
        self.fs = None             # по часам чипа (из пакетов)
        self.lsb = None
        self.cap = 0
        self.buf = None
        self.ok = None
        self.head = None           # номер следующего ожидаемого отсчёта
        self.win_next = None
        self.mark = None           # (номер отсчёта, время сервера)
        self.lost = 0
        self.restarts = 0
        self.windows = deque(maxlen=KEEP_WIN)
        self.seconds = deque(maxlen=KEEP_SEC)
        self.base = None
        self.conds = {}            # отклонения, которые держатся: ключ -> запись
        self.done = deque(maxlen=300)
        self.last_rx = None
        self.rx_n = 0
        self.temp = None

    @property
    def W(self):
        return int(round(WIN_S * self.hz))

    def t_of(self, i):
        mi, tm = self.mark
        return tm + (i - mi) / (self.fs or self.hz)

    def start(self, i0, hz, mode):
        self.mode, self.hz, self.lsb = mode, hz, float(mode[2] or 16384)
        cap = int(KEEP_S * hz)
        if cap != self.cap:
            self.cap = cap
            self.buf = np.zeros((cap, 3), np.int16)
            self.ok = np.zeros(cap, bool)
        else:
            self.ok[:] = False
        self.head = i0
        W = self.W
        self.win_next = (i0 + W - 1) // W * W      # окна — по номерам, кратным W
        self.conds = {}


class Watch:
    def __init__(self, data_dir, put=None, log=print):
        self.dir = os.path.join(data_dir, "static")
        self.acc_dir = os.path.join(data_dir, "accel")
        os.makedirs(os.path.join(self.dir, "baselines"), exist_ok=True)
        self.put = put or (lambda *a: None)
        self.log = log
        self.lock = threading.RLock()
        self.sensors = {}
        self.rec = None            # запись эталона
        self.known_fn = lambda: []
        self.busy_fn = lambda: []
        self.files = {}
        self.last_rec = None
        self._load_baselines()

    # ---------------- приём ----------------
    def add(self, mac, p, rx):
        """Пакет "acc": отсчёты в кольцо, на диск; посчитать окна, которые набрались."""
        try:
            a = np.frombuffer(base64.b64decode(p["b64"]), dtype="<i2").reshape(-1, 3)
            i0 = int(p["i0"])
            avg = int(p.get("avg") or 2)
        except Exception:
            return
        if not len(a):
            return
        hz = 1000.0 / avg
        mode = (avg, int(p.get("afs") or 2), float(p.get("lsb") or 16384))
        self._store(mac, p, rx, a)
        with self.lock:
            s = self.sensors.get(mac) or self.sensors.setdefault(mac, Sensor(mac))
            s.last_rx, s.rx_n, s.temp = rx, s.rx_n + 1, p.get("t")
            if p.get("hz"):
                s.fs = float(p["hz"]) if not s.fs else s.fs + 0.05 * (float(p["hz"]) - s.fs)
            fresh = s.head is None or mode != s.mode
            if not fresh and i0 + len(a) <= s.head:
                if s.head - (i0 + len(a)) < 5 * hz:
                    return                              # повтор
                fresh = True                            # номера пошли заново — агент перезапускался
                s.restarts += 1
            if fresh:
                if s.mode is not None and mode != s.mode:
                    self._event(s, {"kind": "mode", "t": rx, "text": f"режим датчика: ±{mode[1]} g, "
                                    f"{fnum(1000 / mode[0], 0)} Гц (было ±{s.mode[1]} g, {fnum(1000 / s.mode[0], 0)} Гц)"})
                s.fs = float(p["hz"]) if p.get("hz") else None
                s.start(i0, hz, mode)
            if i0 < s.head:                             # частично уже было
                a = a[s.head - i0:]
                i0 = s.head
            if i0 > s.head:                             # пропуск
                gap = i0 - s.head
                s.lost += gap
                if gap >= s.cap:
                    s.ok[:] = False
                else:
                    s.ok[np.arange(s.head, i0) % s.cap] = False
            idx = np.arange(i0, i0 + len(a)) % s.cap
            s.buf[idx] = a
            s.ok[idx] = True
            s.head = i0 + len(a)
            s.mark = (int(p.get("mi", s.head)), rx - ((p.get("ms") or 0) - (p.get("mms") or 0)) / 1000.0)
            while s.head >= s.win_next + s.W:
                self._window(s, s.win_next)
                s.win_next += s.W

    def _store(self, mac, p, rx, a):
        day = time.strftime("%Y-%m-%d", time.localtime(rx))
        d = os.path.join(self.acc_dir, day)
        key = (mac, day)
        f = self.files.get(key)
        if f is None:
            for k in [k for k in self.files if k[0] == mac]:
                for h in self.files.pop(k):
                    h.close()
            os.makedirs(d, exist_ok=True)
            name = mac.replace(":", "")
            f = self.files[key] = (open(os.path.join(d, name + ".bin"), "ab"),
                                   open(os.path.join(d, name + ".idx.jsonl"), "a", encoding="utf-8"))
        f[0].write(a.astype("<i2").tobytes())
        f[0].flush()
        f[1].write(json.dumps({"rx": round(rx, 3), **{k: p.get(k) for k in ("i0", "n", "hz", "avg", "afs", "lsb",
                                                                           "mi", "mms", "ms", "seq", "t", "lost")}})
                   + "\n")
        f[1].flush()

    # ---------------- окно ----------------
    def _window(self, s, i0):
        W, hz = s.W, (s.fs or s.hz)
        idx = np.arange(i0, i0 + W) % s.cap
        raw, ok = s.buf[idx].astype(np.int32), s.ok[idx]
        w = {"i0": i0, "t0": s.t_of(i0), "t1": s.t_of(i0 + W), "n": int(ok.sum()), "gap": int(W - ok.sum()),
             "hz": hz, "mode": s.mode, "knocks": [], "finds": []}
        g = raw / s.lsb
        S = int(round(s.hz))
        hp, secs = [], []
        for k in range(W // S):                         # секунды
            sl = slice(k * S, (k + 1) * S)
            if not ok[sl].all():
                continue
            x = g[sl]
            m = x.mean(0)
            d = x - m
            sd = d.std(0)
            sec = {"t": s.t_of(i0 + k * S + S // 2), "mean": m * 1e6, "sd": sd * 1e6,
                   "min": x.min(0) * 1e6, "max": x.max(0) * 1e6, "peak": np.abs(d).max(0) * 1e6}
            secs.append(sec)
            hp.append(d)
        nper = min(NPERSEG, W)
        if hp:
            d = np.concatenate(hp)
            m2 = (d ** 2).mean(0)
            w["sd"] = np.sqrt(m2) * 1e6
            w["skew"] = (d ** 3).mean(0) / np.maximum(m2, 1e-30) ** 1.5
            w["kurt"] = (d ** 4).mean(0) / np.maximum(m2, 1e-30) ** 2 - 3
            w["mean"] = np.mean([q["mean"] for q in secs], axis=0)
            rv = raw[ok]
            w["q"] = np.array([[float(((rv[:, ax] & ((1 << k) - 1)) == 0).mean()) for k in range(1, QBITS + 1)]
                               for ax in range(3)])
            w["sat"] = int(((rv >= 32767) | (rv <= -32767)).any(1).sum())
            w["nq"] = int(len(rv))
        win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nper) / nper)
        P, ns = np.zeros((3, nper // 2 + 1)), 0
        for st in range(0, W - nper + 1, nper // 2):    # Уэлч
            if not ok[st:st + nper].all():
                continue
            seg = g[st:st + nper]
            X = np.fft.rfft((seg - seg.mean(0)) * win[:, None], axis=0)
            P += (np.abs(X) ** 2).T
            ns += 1
        if ns:
            psd = P / ns / (hz * (win ** 2).sum())
            psd[:, 1:-1] *= 2                            # односторонняя
            w["psd"], w["ns"], w["df"] = psd.astype(np.float32), ns, hz / nper
            f = np.arange(psd.shape[1]) * w["df"]
            bands = []
            for lo, hi in OCTAVES:
                sel = (f >= lo) & (f < min(hi, hz / 2))
                if lo >= hz / 2 or not sel.any():
                    break
                bands.append(10 * np.log10(np.maximum(psd[:, sel].sum(1) * w["df"], 1e-30)))
            w["band_db"] = np.array(bands).T if bands else None
            w["band_nb"] = [int(((f >= lo) & (f < min(hi, hz / 2))).sum()) for lo, hi in OCTAVES[:len(bands)]]
        # толчки — против эталона или, без него, против обычного за последние две минуты
        ref = self._sec_ref(s)
        for q in secs:
            if ref is None:
                break
            x = np.maximum(q["sd"] / ref, q["peak"] / ref / KNOCK_PEAK * KNOCK_X)
            if (x > KNOCK_X).any():
                ax = int(np.argmax(x))
                w["knocks"].append({"t": q["t"], "ax": ax, "x": float(x[ax])})
        if secs and len(w["knocks"]) >= max(3, len(secs) // 2):
            # выбросы почти каждую секунду — не толчки, а сплошная импульсная помеха: шум окна изменился весь,
            # его и спектр, и форму сравнивать можно и нужно
            w["spikes"] = {"n": len(w["knocks"]), "of": len(secs), "x": max(k["x"] for k in w["knocks"])}
            w["knocks"] = []
        for q in secs:
            s.seconds.append(q)
            for i, ax in enumerate(AXES):
                self.put(s.mac, f"s_sd_{ax}", round(q["t"], 3), round(float(q["sd"][i]), 1))
                self.put(s.mac, f"s_mean_{ax}", round(q["t"], 3), round(float(q["mean"][i]), 1))
        w["finds"] = self._compare(s, w)
        self._track(s, w)
        s.windows.append(w)
        tm = round(w["t1"], 3)
        if w.get("band_db") is not None:
            for i, ax in enumerate(AXES):
                for b in range(w["band_db"].shape[1]):
                    self.put(s.mac, f"s_p{b}_{ax}", tm, round(float(w["band_db"][i, b]), 2))
        if not w["knocks"]:                              # в окне с толчком отклонение — от толчка; у него свой график
            dev = [f["z"] for f in w["finds"] if CLS[f["kind"]] == "dev" and not f.get("known")]
            self.put(s.mac, "s_zmax", tm, round(max([abs(z) for z in dev], default=0.0), 2))
        if "kurt" in w:
            for i, ax in enumerate(AXES):
                self.put(s.mac, f"s_kurt_{ax}", tm, round(float(w["kurt"][i]), 4))

    def _sec_ref(self, s):
        b = self._base(s)
        if b is not None:
            return np.array(b["sec_sd"])
        recent = [q["sd"] for q in list(s.seconds)[-120:]]
        if len(recent) < 20:
            return None
        return np.median(np.array(recent), axis=0)

    def _base(self, s):
        """Эталон, если он для того же режима датчика."""
        b = s.base
        if b is None or tuple(b["mode"]) != tuple(s.mode):
            return None
        return b

    # ---------------- сравнение с эталоном ----------------
    def _compare(self, s, w):
        out = []
        for k in w["knocks"]:
            out.append({"kind": "knock", "ax": k["ax"], "t": k["t"], "z": k["x"],
                        "text": f"толчок по {AXES[k['ax']]}: разброс ×{fnum(k['x'])}"})
        if w.get("spikes"):
            sp = w["spikes"]
            out.append({"kind": "spikes", "z": sp["x"], "text": f"выбросы в {sp['n']} из {sp['of']} секунд — "
                                                                 f"импульсная помеха (наводка, искра, мотор?)"})
        if w["gap"]:
            out.append({"kind": "gap", "z": w["gap"], "text": f"пропало {w['gap']} отсчётов"})
        if w.get("sat"):
            out.append({"kind": "sat", "z": w["sat"], "text": f"зашкал: {w['sat']} отсчётов на пределе"})
        b = self._base(s)
        if "q" in w:                                    # квантование, зависание
            for ax in range(3):
                sd_lsb = w["sd"][ax] / 1e6 * s.lsb
                if sd_lsb < 0.5:
                    out.append({"kind": "quant", "ax": ax, "z": 99.0, "step": 0,
                                "text": f"значения по {AXES[ax]} не меняются — датчик завис?"})
                    continue
                r = quant_check(w["q"][ax], sd_lsb, w["nq"], b["q"][ax] if b else None)
                if r and abs(r[0]) > Z_QUANT:
                    z, k, step = r
                    txt = (f"значения по {AXES[ax]} кратны {step} — грубый шаг, режим датчика?" if step > 1 else
                           f"квантование по {AXES[ax]}: кратных {2 ** k} — {fnum(100 * w['q'][ax][k - 1])} %")
                    out.append({"kind": "quant", "ax": ax, "z": z, "step": step, "text": txt})
            if any(f["kind"] == "quant" for f in out):  # грубый шаг сам скачет разбросом — это не толчки, а прибор
                out = [f for f in out if f["kind"] not in ("knock", "spikes")]
        if b is None or w.get("psd") is None or len(b["db_mean"][0]) != w["psd"].shape[1]:
            return out
        if w["knocks"]:                                 # окно с толчком: спектр и форма шума — от толчка, не сравнивать
            return out
        known = self._known_lines(s, w["hz"])
        n_b = b["n"]
        for ax in range(3):
            psd = w["psd"][ax].astype(np.float64)
            L = 10 * np.log10(np.maximum(psd, 1e-30))
            lines = self._lines(b, ax, L, self._sde(b, ax, w["ns"]) * math.sqrt(1 + 1 / n_b), w["df"], known)
            out += lines
            out += self._band_finds(b, ax, psd, w["df"], w["hz"], known, lines, w["ns"], 1)
            if "skew" in w:
                g = self._gauss(b, ax, w["skew"][ax], w["kurt"][ax], w["nq"], 1)
                if g:
                    out.append(g)
                dm = float(w["mean"][ax] - b["dc"][ax])
                if abs(dm) > MOVED_UG:
                    out.append({"kind": "moved", "ax": ax, "z": dm / 1000,
                                "text": f"среднее по {AXES[ax]} ушло на {fnum(dm / 1000, 1)} мg — сдвинули или наклонили?"})
        return out

    @staticmethod
    def _sde(b, ax, ns):
        """Разброс логарифма спектра в каждом бине: по эталону, сглаженный по соседям, не меньше теории."""
        return np.maximum(np.maximum(np.array(b["db_sd"][ax]), np.array(b["db_sdm"][ax])),
                          0.9 * DB / math.sqrt(max(1, ns)))

    def _lines(self, b, ax, L, sde, df, known, tag=""):
        """Линии выше эталона и пропавшие линии эталона по одной оси."""
        out = []
        m = np.array(b["db_mean"][ax])
        z = (L - m) / sde
        z[: int(math.ceil(F_MIN / df))] = 0
        peaks = np.array(b["peak"][ax], dtype=bool)
        for sign, kind in ((1, "line"), (-1, "gone")):
            hit = np.where(sign * z > Z_LINE)[0]
            if kind == "gone":
                hit = hit[peaks[hit]]                   # пропасть может только линия эталона
            for grp in np.split(hit, np.where(np.diff(hit) > 1)[0] + 1) if len(hit) else []:
                k = int(grp[np.argmax(sign * z[grp])])
                f = k * df
                lab = next((n for fk, n in known if abs(fk - f) <= 1.5 * df), None)
                ddb = float(L[k] - m[k])
                out.append({"kind": kind, "ax": ax, "f": f, "bin": k, "z": float(z[k]), "db": ddb, "known": lab,
                            "text": (f"линия {fnum(f, 2)} Гц по {AXES[ax]}: {'+' if ddb > 0 else ''}{fnum(ddb)} дБ"
                                     if kind == "line" else
                                     f"пропала линия эталона {fnum(f, 2)} Гц по {AXES[ax]}: {fnum(ddb)} дБ")
                            + tag + (f" — {lab}" if lab else "")})
        return out

    @staticmethod
    def _levels(psd, df, hz, mask):
        """Мощность в октавах, дБ, без бинов mask; (уровень, бинов) или (None, 0)."""
        f = np.arange(len(psd)) * df
        out = []
        for lo, hi in OCTAVES:
            if lo >= hz / 2:
                break
            sel = (f >= lo) & (f < min(hi, hz / 2)) & ~mask
            out.append((10 * math.log10(max(float(psd[sel].sum()) * df, 1e-30)), int(sel.sum())) if sel.any()
                       else (None, 0))
        return out

    def _band_finds(self, b, ax, psd, df, hz, known, lines, ns, nwin, tag=""):
        """Полосы — широкий шум: частоты известных и найденных линий (±2 бина) не входят ни в окно, ни в
        эталон — иначе одна линия тревожила бы дважды, а известная ещё и поднимала бы полосу."""
        mask = np.zeros(len(psd), bool)
        for fk in [fk for fk, _ in known] + [x["f"] for x in lines]:
            k = int(round(fk / df))
            mask[max(0, k - 2):k + 3] = True
        lw = self._levels(psd, df, hz, mask)
        lb = self._levels(np.array(b["psd_mean"][ax]), df, hz, mask)
        out = []
        for bi, ((x, nb), (y, _)) in enumerate(zip(lw, lb)):
            if x is None or y is None or bi >= len(b["band_sd"][ax]):
                continue
            sd = max(b["band_sd"][ax][bi], 0.8 * DB / math.sqrt(ns * max(1, nb)))
            ddb = x - y
            zb = ddb / (sd * math.sqrt(1 / nwin + 1 / b["n"]))
            if abs(zb) > Z_BAND:
                out.append({"kind": "band", "ax": ax, "band": bi, "z": zb, "db": ddb,
                            "text": f"полоса {band_name(bi)} по {AXES[ax]}: {'+' if ddb > 0 else ''}{fnum(ddb)} дБ" + tag})
        return out

    @staticmethod
    def _gauss(b, ax, skew, kurt, n, nwin, tag=""):
        k = math.sqrt(1 / nwin + 1 / b["n"])
        zs = (skew - b["skew"][ax][0]) / (max(b["skew"][ax][1], math.sqrt(6 / max(1, n))) * k)
        zk = (kurt - b["kurt"][ax][0]) / (max(b["kurt"][ax][1], math.sqrt(24 / max(1, n))) * k)
        if max(abs(zs), abs(zk)) <= Z_GAUSS:
            return None
        return {"kind": "gauss", "ax": ax, "z": float(zk if abs(zk) >= abs(zs) else zs),
                "text": f"форма шума по {AXES[ax]}: эксцесс {fnum(kurt, 2)}, асимметрия {fnum(skew, 2)} "
                        f"(в эталоне {fnum(b['kurt'][ax][0], 2)}, {fnum(b['skew'][ax][0], 2)})" + tag}

    def _integral(self, s, ws):
        """Весь отрезок против эталона: средние по окнам. Слабая линия, невидимая в одном окне,
        за отрезок из n окон видна в √n раз лучше."""
        b = self._base(s)
        ws = [w for w in ws if w.get("psd") is not None and "skew" in w and not w["knocks"]]
        if b is None or len(ws) < 2 or any(len(b["db_mean"][0]) != w["psd"].shape[1] for w in ws):
            return []
        n = len(ws)
        ns = float(np.median([w["ns"] for w in ws]))
        known = self._known_lines(s, ws[-1]["hz"])
        tag = f" (за отрезок, окон {n})"
        out = []
        df, hz = ws[-1]["df"], ws[-1]["hz"]
        for ax in range(3):
            L = np.mean([10 * np.log10(np.maximum(w["psd"][ax].astype(np.float64), 1e-30)) for w in ws], axis=0)
            lines = self._lines(b, ax, L, self._sde(b, ax, ns) * math.sqrt(1 / n + 1 / b["n"]), df, known, tag)
            out += lines
            psd = np.mean([w["psd"][ax].astype(np.float64) for w in ws], axis=0)
            out += self._band_finds(b, ax, psd, df, hz, known, lines, ns, n, tag)
            g = self._gauss(b, ax, float(np.mean([w["skew"][ax] for w in ws])),
                            float(np.mean([w["kurt"][ax] for w in ws])), int(np.median([w["nq"] for w in ws])), n, tag)
            if g:
                out.append(g)
        return out

    def _known_lines(self, s, fs):
        """Известные частоты (с гармониками и заворотом за половину частоты отсчётов)."""
        out = []
        for src in self.known_fn():
            f0, name, nh = src["f"], src["name"], src.get("harm", 5)
            if not f0 or f0 <= 0:
                continue
            for h in range(1, nh + 1):
                out.append((alias(f0 * h, fs), name if h == 1 else f"{name}, {h}-я гармоника"))
        out.append((alias(fs / 140 * 1, fs), "пакеты отсчётов этого агента"))
        return out

    # ---------------- отклонения, которые держатся ----------------
    def _key(self, s, f):
        if f["kind"] in ("line", "gone"):
            for dk in (0, -1, 1):
                k = (f["kind"], f["ax"], f["bin"] + dk)
                if k in s.conds:
                    return k
            return (f["kind"], f["ax"], f["bin"])
        if f["kind"] == "band":
            return ("band", f["ax"], f["band"], f["z"] > 0)
        if f["kind"] in ("knock", "spikes", "gap", "sat"):
            return (f["kind"],)
        return (f["kind"], f.get("ax"))

    def _track(self, s, w):
        seen = set()
        for f in w["finds"]:
            key = self._key(s, f)
            c = s.conds.get(key)
            if c is None:
                c = s.conds[key] = {"mac": s.mac, "kind": f["kind"], "cls": CLS[f["kind"]], "ax": f.get("ax"),
                                    "f": f.get("f"), "band": f.get("band"), "t0": w["t0"], "n": 0, "zmax": 0.0,
                                    "known": f.get("known")}
            c["t1"], c["text"] = w["t1"], f["text"]
            if key not in seen:
                c["n"] += 1
            seen.add(key)
            if abs(f["z"]) >= abs(c["zmax"]):
                c["zmax"] = f["z"]
        for key in [k for k in s.conds if k not in seen]:
            c = s.conds.pop(key)
            c["end"] = w["t0"]
            s.done.append(c)
            self._event(s, c)

    def _event(self, s, c):
        try:
            with open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"mac": s.mac, **{k: v for k, v in c.items() if k != "mac"}},
                                   ensure_ascii=False, default=float) + "\n")
        except OSError:
            pass
        if "kind" in c and "n" not in c:               # разовое событие (смена режима)
            s.done.append({"mac": s.mac, "cls": CLS.get(c["kind"], "inst"), "t0": c["t"], "t1": c["t"], "end": c["t"],
                           "n": 1, "zmax": 0.0, **c})

    # ---------------- эталон ----------------
    def rec_start(self, macs, minutes):
        with self.lock:
            if self.rec:
                raise ValueError("эталон уже записывается")
            macs = [m for m in (macs if macs not in (None, "all") else list(self.sensors)) if m in self.sensors]
            if not macs:
                raise ValueError("нет статических акселерометров с данными")
            minutes = float(minutes)
            if not 1 <= minutes <= 120:
                raise ValueError("эталон — от 1 до 120 минут")
            t0 = time.time()
            self.rec = {"t0": t0, "t1": t0 + minutes * 60, "minutes": minutes, "macs": macs, "busy": []}
            self.log(f"[static] эталон: {minutes} мин, {len(macs)} датчиков")
            return self.rec

    def rec_stop(self):
        with self.lock:
            if not self.rec:
                raise ValueError("эталон не записывается")
            self.rec["t1"] = time.time()
            return self._finish()

    def rec_clear(self, macs):
        with self.lock:
            for m in (macs if macs not in (None, "all") else list(self.sensors)):
                if m in self.sensors:
                    self.sensors[m].base = None
                    try:
                        os.remove(self._bpath(m))
                    except OSError:
                        pass

    def tick(self, now=None):
        """Раз в секунду: запись эталона — заметить работающие генераторы, вовремя закончить."""
        now = now or time.time()
        with self.lock:
            r = self.rec
            if not r:
                return
            for name in self.busy_fn():
                if name not in r["busy"]:
                    r["busy"].append(name)
            if now >= r["t1"] + 3:                      # последнее окно уже посчитано
                self._finish()

    def _finish(self):
        r, self.rec = self.rec, None
        out = {"t0": r["t0"], "t1": r["t1"], "busy": r["busy"], "sensors": {}}
        for mac in r["macs"]:
            s = self.sensors.get(mac)
            ws = [w for w in (s.windows if s else []) if w["t0"] >= r["t0"] - 0.5 and w["t1"] <= r["t1"] + 0.5]
            b, why = self._build(s, ws, r)
            if b:
                s.base = b
                with open(self._bpath(mac), "w", encoding="utf-8") as f:
                    json.dump(b, f, ensure_ascii=False)
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(r["t0"]))
                with open(self._bpath(mac).replace(".json", f"_{stamp}.json"), "w", encoding="utf-8") as f:
                    json.dump(b, f, ensure_ascii=False)
                s.conds = {}
            out["sensors"][mac] = {"ok": bool(b), "why": why, "quality": b["quality"] if b else None}
        self.last_rec = out
        self.log(f"[static] эталон записан: " + ", ".join(f"{m}: {'✓' if v['ok'] else '✗ ' + v['why']}"
                                                         for m, v in out["sensors"].items()))
        return out

    def _build(self, s, ws, r):
        if not s:
            return None, "нет данных"
        clean = [w for w in ws if w.get("psd") is not None and not w["knocks"] and not w["gap"] and "q" in w
                 and tuple(w["mode"]) == tuple(s.mode)]
        if len(clean) < MIN_BASE_WIN:
            return None, f"мало чистых окон: {len(clean)} из {len(ws)} (нужно {MIN_BASE_WIN}; толчки и пропуски не берём)"
        L = 10 * np.log10(np.maximum(np.stack([w["psd"] for w in clean]).astype(np.float64), 1e-30))  # [N, 3, бины]
        nb = min(w["band_db"].shape[1] for w in clean)
        B = np.stack([w["band_db"][:, :nb] for w in clean])
        sk = np.stack([w["skew"] for w in clean])
        ku = np.stack([w["kurt"] for w in clean])
        sec = [q["sd"] for q in s.seconds if r["t0"] <= q["t"] <= r["t1"]]
        sec_sd = np.median(np.array(sec), axis=0) if sec else np.stack([w["sd"] for w in clean]).mean(0)
        n = len(clean)
        lm, ls = L.mean(0), L.std(0, ddof=1)
        b = {"mac": s.mac, "t0": r["t0"], "t1": r["t1"], "n": n, "of": len(ws), "mode": list(s.mode), "hz": s.hz,
             "fs": s.fs, "df": clean[0]["df"], "ns": int(np.median([w["ns"] for w in clean])),
             "db_mean": np.round(lm, 3).tolist(), "db_sd": np.round(ls, 3).tolist(),
             "psd_mean": [[float(f"{x:.5g}") for x in np.stack([w["psd"][ax] for w in clean]).astype(np.float64).mean(0)]
                          for ax in range(3)],
             # разброс, сглаженный по соседним бинам: по немногим окнам у отдельного бина он бывает мал случайно
             "db_sdm": [np.round(runmed(ls[ax], 8), 3).tolist() for ax in range(3)],
             # линии эталона — бины выше соседей на 6 дБ: только они могут «пропасть»
             "peak": [(lm[ax] - runmed(lm[ax], 16) >= 6).astype(int).tolist() for ax in range(3)],
             "band_mean": np.round(B.mean(0), 3).tolist(), "band_sd": np.round(B.std(0, ddof=1), 3).tolist(),
             "skew": [[float(sk[:, ax].mean()), float(sk[:, ax].std(ddof=1))] for ax in range(3)],
             "kurt": [[float(ku[:, ax].mean()), float(ku[:, ax].std(ddof=1))] for ax in range(3)],
             "q": np.round(np.stack([w["q"] for w in clean]).mean(0), 6).tolist(),
             "dc": np.stack([w["mean"] for w in clean]).mean(0).tolist(),
             "sec_sd": np.asarray(sec_sd).tolist(), "busy": r["busy"]}
        b["quality"] = self._quality(b, L, B, clean)
        return b, ""

    def _quality(self, b, L, B, clean):
        """Годится ли эталон: шум гауссов, фон ровный, квантование мелкое, генераторы стояли."""
        q = []
        f = np.arange(len(b["db_mean"][0])) * b["df"]
        sel = (f >= 10) & (f <= min(100, b["hz"] / 2 * 0.8))
        dens = [math.sqrt(10 ** (np.median(np.array(b["db_mean"][ax])[sel]) / 10)) * 1e6 for ax in range(3)]
        q.append({"ok": True, "text": "шум " + " · ".join(f"{a} {fnum(d, 0)}" for a, d in zip(AXES, dens)) +
                  " мкg/√Гц (10–100 Гц; у ICM-42688 по паспорту 70)"})
        ku = [b["kurt"][ax][0] for ax in range(3)]
        sk = [b["skew"][ax][0] for ax in range(3)]
        g_ok = max(abs(x) for x in ku) < 0.2 and max(abs(x) for x in sk) < 0.1
        q.append({"ok": g_ok, "text": ("шум гауссов" if g_ok else "шум не совсем гауссов") +
                  f": эксцесс {' · '.join(fnum(x, 2) for x in ku)}, асимметрия {' · '.join(fnum(x, 2) for x in sk)}"})
        h = len(clean) // 2
        if h >= 3:                                      # половины записи: разница больше случайной (4 её разброса) и 1 дБ
            d = np.abs(B[:h].mean(0) - B[h:].mean(0))
            sd_d = np.std(B, axis=0, ddof=1) * math.sqrt(1 / h + 1 / (len(clean) - h))
            nb = d.shape[1]
            hi = [bi for bi in range(nb) if OCTAVES[bi][0] >= 4]
            bad = [(float(d[ax, bi]), ax, bi) for ax in range(3) for bi in hi
                   if d[ax, bi] > max(1.0, 4 * sd_d[ax, bi])]
            worst = max(bad, default=None)
            q.append({"ok": not bad, "text": "фон ровный: первая и вторая половина записи совпадают" if not bad else
                      f"фон менялся: полоса {band_name(worst[2])} по {AXES[worst[1]]} на {fnum(worst[0])} дБ "
                      f"между половинами"})
        bad = []
        for ax in range(3):
            sd_lsb = float(np.mean([w["sd"][ax] for w in clean])) / 1e6 * b["mode"][2]
            r = quant_check(b["q"][ax], sd_lsb, int(np.median([w["nq"] for w in clean])) * len(clean))
            if r and abs(r[0]) > Z_QUANT:
                bad.append(f"{AXES[ax]} (кратны {r[2]})" if r[2] > 1 else AXES[ax])
        q.append({"ok": not bad, "text": "квантование мелкое: доли чётных, кратных 4, … как у живого шума" if not bad
                  else "квантование грубое: " + ", ".join(bad) + " — проверьте режим датчика"})
        if b["of"] > b["n"]:
            q.append({"ok": True, "text": f"окон с толчками и пропусками не взято: {b['of'] - b['n']}"})
        q.append({"ok": not b["busy"], "text": "основные группы стояли" if not b["busy"] else
                  "во время записи работали: " + ", ".join(b["busy"]) + " — эталон не чистый"})
        return q

    def _bpath(self, mac):
        return os.path.join(self.dir, "baselines", mac.replace(":", "") + ".json")

    def _load_baselines(self):
        d = os.path.join(self.dir, "baselines")
        for fn in os.listdir(d):
            if fn.endswith(".json") and "_" not in fn:
                try:
                    b = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                    s = self.sensors.setdefault(b["mac"], Sensor(b["mac"]))
                    s.base = b
                except Exception as e:
                    self.log(f"[static] эталон {fn} не прочитан: {e}")

    # ---------------- выводы ----------------
    def _wins(self, s, t0, t1):
        return [w for w in s.windows if w["t0"] >= t0 - 0.5 and w["t1"] <= t1 + 0.5]

    def verdict(self, t0, t1, macs=None):
        """Вывод по отрезку: ok — продолжать, repeat — повторить (помеха, прибор), check — проверить
        (отклонение от эталона держится), nobase — нет эталона, nodata — нет данных."""
        with self.lock:
            per = {}
            for mac, s in self.sensors.items():
                if macs and mac not in macs:
                    continue
                ws = self._wins(s, t0, t1)
                if not ws:
                    if s.last_rx and s.last_rx >= t0:
                        per[mac] = {"verdict": "nodata", "reasons": ["окна ещё не набрались"], "windows": 0}
                    continue
                finds = [f for w in ws for f in w["finds"]]
                rs = []
                dist = [f for f in finds if CLS[f["kind"]] == "dist"]
                inst = [f for f in finds if CLS[f["kind"]] == "inst"]
                for kind in ("knock", "spikes", "moved", "quant", "gap", "sat"):
                    fs = [f for f in finds if f["kind"] == kind]
                    if fs:
                        rs.append(f"{KIND_NAMES[kind]} ×{len(fs)}: {fs[0]['text']}")
                v = "repeat" if dist or inst else None
                runs = {}                                  # отклонения без известной причины: сколько окон подряд
                for i, w in enumerate(ws):
                    for f in w["finds"]:
                        if CLS[f["kind"]] != "dev" or f.get("known"):
                            continue
                        k = (f["kind"], f.get("ax"), f.get("band"), round(f.get("f") or 0, 0))
                        r = runs.setdefault(k, {"best": 0, "cur": 0, "last": -2, "f": f})
                        r["cur"] = r["cur"] + 1 if r["last"] == i - 1 else 1
                        r["last"] = i
                        r["best"] = max(r["best"], r["cur"])
                        if abs(f["z"]) > abs(r["f"]["z"]):
                            r["f"] = f
                need = min(PERSIST, len(ws))
                held = sorted([r for r in runs.values() if r["best"] >= need], key=lambda r: -abs(r["f"]["z"]))
                for r in held[:4]:
                    rs.append(f"{r['f']['text']} (z {fnum(r['f']['z'])}, окон подряд {r['best']})")
                integ = [f for f in self._integral(s, ws) if not f.get("known")]
                had = {(r["f"]["kind"], r["f"].get("ax"), r["f"].get("band"), round(r["f"].get("f") or 0)) for r in held}
                for f in sorted(integ, key=lambda f: -abs(f["z"]))[:4]:
                    if (f["kind"], f.get("ax"), f.get("band"), round(f.get("f") or 0)) not in had:
                        rs.append(f"{f['text']} (z {fnum(f['z'])})")
                held = held + [{"f": f} for f in integ]
                known = {f["known"] for f in finds if f.get("known")}
                if not v:
                    v = "check" if held else ("ok" if self._base(s) is not None else "nobase")
                per[mac] = {"verdict": v, "reasons": rs, "windows": len(ws), "known": sorted(known),
                            "base": self._base(s) is not None}
            order = ["repeat", "check", "ok", "nobase", "nodata"]
            vs = [p["verdict"] for p in per.values()]
            overall = next((x for x in order if x in vs), "nodata")
            notes = []
            # толчок у нескольких датчиков в одну секунду — снаружи: шаги, дверь, здание
            kt = sorted((k["t"], mac) for mac, s in self.sensors.items() if mac in per
                        for w in self._wins(s, t0, t1) for k in w["knocks"])
            groups, cur = [], []
            for t, mac in kt:
                if cur and t - cur[0][0] > 1.5:
                    groups.append(cur)
                    cur = []
                cur.append((t, mac))
            if cur:
                groups.append(cur)
            together = [g for g in groups if len({m for _, m in g}) >= 2]
            if together:
                notes.append(f"толчок сразу у {max(len({m for _, m in g}) for g in together)} датчиков "
                             f"(раз: {len(together)}) — помеха снаружи: шаги, дверь, здание")
            return {"t0": t0, "t1": t1, "verdict": overall, "sensors": per, "notes": notes}

    def metrics(self, t0, t1):
        """Показатели каждого статического за отрезок — для сравнения шагов программы с контрольными:
        мощность полос (дБ, среднее по окнам), разброс и среднее (мкg), эксцесс и асимметрия."""
        with self.lock:
            out = {}
            for mac, s in self.sensors.items():
                ws = [w for w in self._wins(s, t0, t1) if w.get("psd") is not None and "sd" in w]
                if not ws:
                    out[mac] = None
                    continue
                m = {"n": len(ws)}
                nb = min(w["band_db"].shape[1] for w in ws if w.get("band_db") is not None) if any(
                    w.get("band_db") is not None for w in ws) else 0
                for i, ax in enumerate(AXES):
                    m["sd_" + ax] = float(np.mean([w["sd"][i] for w in ws]))
                    m["mean_" + ax] = float(np.mean([w["mean"][i] for w in ws]))
                    m["kurt_" + ax] = float(np.mean([w["kurt"][i] for w in ws]))
                    m["skew_" + ax] = float(np.mean([w["skew"][i] for w in ws]))
                    for b in range(nb):
                        p = np.mean([10 ** (w["band_db"][i, b] / 10) for w in ws if w.get("band_db") is not None])
                        m[f"p{b}_{ax}"] = float(10 * np.log10(p))
                out[mac] = m
            return out

    # ---------------- для панели ----------------
    def status(self, s, now):
        b = self._base(s)
        active = list(s.conds.values())
        held = [c for c in active if c["cls"] == "dev" and not c.get("known") and c["n"] >= PERSIST]
        if s.last_rx is None or now - s.last_rx > 10:
            return "none", "✗ нет данных"
        if not s.windows:
            return "none", f"◌ ждём первое окно ({WIN_S} с)"
        if any(c["cls"] == "dist" for c in active):
            return "dist", "↻ помеха: " + "; ".join(c["text"] for c in active if c["cls"] == "dist")[:160]
        if any(c["cls"] == "inst" for c in active):
            return "inst", "⚠ прибор: " + "; ".join(c["text"] for c in active if c["cls"] == "inst")[:160]
        if s.base is not None and b is None:
            return "nobase", f"◌ эталон записан для другого режима (±{s.base['mode'][1]} g, " \
                             f"{fnum(1000 / s.base['mode'][0], 0)} Гц) — перепишите"
        if b is None:
            return "nobase", "◌ нет эталона — сравнивать не с чем"
        if held:
            return "dev", "⚠ отклонение: " + "; ".join(c["text"] for c in held)[:200]
        if any(c["cls"] == "dev" and not c.get("known") for c in active):
            return "maybe", "◐ разовое отклонение — смотрим следующее окно"
        if any(c.get("known") for c in active):
            return "known", "◐ как эталон, кроме известных источников: " + ", ".join(
                sorted({c["known"] for c in active if c.get("known")}))[:160]
        return "ok", "✓ как эталон"

    def summary(self, now=None, macs_live=None):
        now = now or time.time()
        with self.lock:
            out = []
            for mac, s in self.sensors.items():
                st, text = self.status(s, now)
                w = s.windows[-1] if s.windows else None
                b = s.base
                out.append({
                    "mac": mac, "status": st, "text": text, "hz": s.hz, "fs": s.fs, "mode": s.mode,
                    "lost": s.lost, "restarts": s.restarts, "age": None if s.last_rx is None else round(now - s.last_rx, 1),
                    "temp": s.temp, "buffered_s": None if s.head is None else round(min(s.head, s.cap) / s.hz),
                    "last": None if not w else {
                        "t1": w["t1"], "sd": _l(w.get("sd")), "mean": _l(w.get("mean")), "kurt": _l(w.get("kurt"), 3),
                        "skew": _l(w.get("skew"), 3), "gap": w["gap"], "sat": w.get("sat"), "ns": w.get("ns"),
                        "q": None if "q" not in w else [[round(float(x), 4) for x in r] for r in w["q"]],
                        "band_db": None if w.get("band_db") is None else np.round(w["band_db"], 2).tolist(),
                        "finds": [{k: v for k, v in f.items() if k != "bin"} for f in w["finds"]]},
                    "base": None if not b else {k: b.get(k) for k in ("t0", "t1", "n", "of", "mode", "hz", "quality",
                                                                     "busy", "band_mean", "band_sd", "dc", "sec_sd")}
                                               | {"fits": self._base(s) is not None},
                    "conds": [_cond(c) for c in s.conds.values()],
                })
            events = sorted([_cond(c) for s in self.sensors.values() for c in s.done], key=lambda c: -c["t1"])[:150]
            rec = None
            if self.rec:
                rec = {k: self.rec[k] for k in ("t0", "t1", "minutes", "macs", "busy")} | {"now": now}
            return {"sensors": sorted(out, key=lambda x: x["mac"]), "events": events, "rec": rec,
                    "last_rec": self.last_rec, "bands": [band_name(b) for b in range(len(OCTAVES))],
                    "thresholds": {"line": Z_LINE, "band": Z_BAND, "gauss": Z_GAUSS, "quant": Z_QUANT,
                                   "knock": KNOCK_X, "moved_mg": MOVED_UG / 1000, "persist": PERSIST, "win_s": WIN_S}}

    def spectrum(self, mac, nwin=6):
        """Спектр для графика: среднее последних nwin окон и эталон (среднее, ±3 разброса)."""
        with self.lock:
            s = self.sensors.get(mac)
            if not s or not s.windows:
                return {"mac": mac, "f": [], "axes": []}
            ws = [w for w in list(s.windows)[-nwin:] if w.get("psd") is not None and tuple(w["mode"]) == tuple(s.mode)]
            if not ws:
                return {"mac": mac, "f": [], "axes": []}
            P = np.mean(np.stack([w["psd"] for w in ws]).astype(np.float64), axis=0)
            df = ws[-1]["df"]
            b = self._base(s)
            if b is not None and len(b["db_mean"][0]) != P.shape[1]:
                b = None
            k0 = max(1, int(math.ceil(0.5 / df)))
            k1 = P.shape[1] - 1                         # без бина Найквиста: он не удваивается — честный провал на 3 дБ
            f = (np.arange(P.shape[1]) * df)[k0:k1]
            axes = []
            for ax in range(3):
                cur = 10 * np.log10(np.maximum(P[ax], 1e-30))[k0:k1]
                a = {"cur": np.round(cur, 2).tolist()}
                if b is not None:
                    m = np.array(b["db_mean"][ax])[k0:k1]
                    sd_n = self._sde(b, ax, b["ns"])[k0:k1] / math.sqrt(len(ws))   # среднее nwin окон — разброс меньше
                    a.update({"base": np.round(m, 2).tolist(), "lo": np.round(m - 3 * sd_n, 2).tolist(),
                              "hi": np.round(m + 3 * sd_n, 2).tolist()})
                axes.append(a)
            finds = [f2 for f2 in ws[-1]["finds"] if f2["kind"] in ("line", "gone")]
            return {"mac": mac, "f": np.round(f, 3).tolist(), "axes": axes, "nwin": len(ws), "t1": ws[-1]["t1"],
                    "finds": [{k: v for k, v in x.items() if k != "bin"} for x in finds]}


def _l(v, d=1):
    return None if v is None else [round(float(x), d) for x in v]


def _cond(c):
    out = {k: c.get(k) for k in ("mac", "kind", "cls", "ax", "f", "band", "t0", "t1", "end", "n", "zmax", "known",
                                 "text")}
    for k in ("f", "zmax"):
        if out[k] is not None:
            out[k] = round(float(out[k]), 3)
    return out
