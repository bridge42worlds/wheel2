"""Калибровка оборотов колеса по тахометру — оптическому, на отражающую метку.

Датчик на ободе меряет обороты гироскопом; масштаб гироскопа сам подстраивается по фазе тяжести (analysis.py,
WheelTracker), тахометр — независимая проверка. Точка: что показал тахометр и средние обороты обода за те же
секунды. Поправка — одним множителем через ноль: c = Σ tach·rim / Σ rim² (обороты по тахометру = c · по ободу).
Разброс — насколько точки расходятся с этим множителем, %. Применять или нет — галочкой: тогда обороты колеса
для моторов и для показа — с множителем, а радиус по ободу — делится на c² (он считан по ω гироскопа:
r = k·g/ω²), и невесомость остаётся там же, где k = 1.

Вторая галочка — радиус для невесомости по наклону (rim_fit): без смещения нуля акселерометра. Без неё — медиана
радиусов последних оборотов (как раньше)."""
import json, os, threading, time

WHEELS = ("1", "2")
MAX_PTS = 40


def _clean(w):
    w = str(w)
    if w not in WHEELS:
        raise ValueError(f"колесо «{w}»: 1 или 2")
    return w


class RimCal:
    def __init__(self, path_fn, log=print):
        self.path_fn = path_fn               # путь к файлу — функцией: каталог данных задаётся после импорта
        self.log = log
        self.lock = threading.Lock()
        self.st = {w: {"pts": [], "tach_on": False, "fit_on": False} for w in WHEELS}   # load() — после выбора каталога данных

    def load(self):
        try:
            d = json.load(open(self.path_fn(), encoding="utf-8"))
        except (OSError, ValueError):
            return
        for w in WHEELS:
            x = d.get(w) or {}
            self.st[w] = {"pts": list(x.get("pts") or [])[-MAX_PTS:], "tach_on": bool(x.get("tach_on")),
                          "fit_on": bool(x.get("fit_on"))}

    def _save(self):
        p = self.path_fn()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, ensure_ascii=False)
        os.replace(tmp, p)

    # ---------- точки ----------
    def add(self, wheel, tach, rim):
        """tach — показание тахометра, об/мин; rim — {rpm, sd_pct, n, gs, steady} по ободу за те же секунды."""
        w = _clean(wheel)
        t = float(tach)
        if not 1 <= t <= 300:
            raise ValueError("тахометр: 1–300 об/мин")
        if not rim or not rim.get("rpm"):
            raise ValueError("обод колеса сейчас не меряет обороты — точку не с чем сравнить")
        pt = {"t": round(time.time(), 1), "tach": round(t, 3), "rim": round(float(rim["rpm"]), 4),
              "sd_pct": rim.get("sd_pct"), "n": rim.get("n"), "gs": rim.get("gs"), "steady": bool(rim.get("steady"))}
        with self.lock:
            self.st[w]["pts"] = (self.st[w]["pts"] + [pt])[-MAX_PTS:]
            self._save()
        self.log(f"[rimcal] колесо {w}: тахометр {t:.2f}, обод {pt['rim']:.2f} об/мин")
        return pt

    def delete(self, wheel, i):
        w = _clean(wheel)
        with self.lock:
            pts = self.st[w]["pts"]
            if not 0 <= int(i) < len(pts):
                raise ValueError("нет такой точки")
            pts.pop(int(i))
            self._save()

    def clear(self, wheel):
        w = _clean(wheel)
        with self.lock:
            self.st[w]["pts"] = []
            self.st[w]["tach_on"] = False
            self._save()

    def set(self, wheel, tach_on=None, fit_on=None):
        w = _clean(wheel)
        with self.lock:
            if tach_on is not None:
                if tach_on and not self.st[w]["pts"]:
                    raise ValueError("сначала хотя бы одна точка тахометра")
                self.st[w]["tach_on"] = bool(tach_on)
            if fit_on is not None:
                self.st[w]["fit_on"] = bool(fit_on)
            self._save()

    # ---------- расчёт ----------
    @staticmethod
    def scale_of(pts):
        """Множитель c (тахометр = c · обод) и разброс точек около него, %; None — точек нет."""
        P = [p for p in pts if p.get("rim") and p.get("tach")]
        if not P:
            return None
        c = sum(p["tach"] * p["rim"] for p in P) / sum(p["rim"] ** 2 for p in P)
        res = [100.0 * (p["tach"] - c * p["rim"]) / p["tach"] for p in P]
        sd = (sum(r * r for r in res) / (len(res) - 1)) ** 0.5 if len(res) > 1 else None
        return {"c": c, "pct": 100.0 * (c - 1), "sd_pct": sd, "n": len(P), "res_pct": [round(r, 3) for r in res]}

    def factor(self, wheel):
        """Множитель оборотов колеса, если поправка по тахометру включена; иначе 1."""
        with self.lock:
            s = self.st.get(str(wheel))
            if not s or not s["tach_on"]:
                return 1.0
            sc = self.scale_of(s["pts"])
        return sc["c"] if sc else 1.0

    def fit_on(self, wheel):
        with self.lock:
            s = self.st.get(str(wheel))
            return bool(s and s["fit_on"])

    def brief(self):
        with self.lock:
            out = {}
            for w, s in self.st.items():
                sc = self.scale_of(s["pts"])
                out[w] = {"pts": [dict(p) for p in s["pts"]], "tach_on": s["tach_on"], "fit_on": s["fit_on"],
                          "scale": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in sc.items()} if sc else None}
            return out
