# -*- coding: utf-8 -*-
"""Аккумуляторы контуров: INA219 у агента шины колеса или у агента катушки (прошивка: battery.h, docs/wiring.md).

Агент раз в секунду шлёт "bat": {v — напряжение пары 18650, В; i — ток, мА; q — мА·ч с включения агента}.
Здесь — по каждому агенту с INA219: сглаженные напряжение и ток, израсходовано с установки пары (ток × время;
новая пара — напряжение после перерыва выросло), заряд по напряжению ячейки, сколько осталось при нынешнем токе,
уровень: ок / пора менять / мало. «Мало» один раз на пару — сигнал серверу поставить программу на паузу.
Ёмкость пары — заданная (по умолчанию 2500 мА·ч) или измеренная: пара разрядилась от полной до «мало» —
израсходованное и есть её ёмкость (battery.json в папке данных).
"""
import json
import os
import threading
import time

# Ячейка Li-ion под небольшой нагрузкой: напряжение → заряд, %. Пара 2S — напряжение пополам
OCV = [(4.20, 100), (4.10, 90), (4.00, 80), (3.90, 65), (3.80, 50), (3.75, 42), (3.70, 33), (3.65, 24),
       (3.60, 16), (3.50, 8), (3.40, 4), (3.30, 0)]
WARN_V, LOW_V = 6.8, 6.5          # пара под нагрузкой: 3,4 и 3,25 В на ячейку
WARN_MIN = 15                     # осталось меньше — «пора менять»
NEW_PACK_DV = 0.25                # после перерыва напряжение выросло на столько — поставили другую пару
FULL_V = 8.1                      # пара с зарядки — от неё считается измеренная ёмкость


def soc(v):
    """Заряд пары по напряжению, %."""
    c = v / 2
    if c >= OCV[0][0]:
        return 100.0
    for (v1, p1), (v2, p2) in zip(OCV, OCV[1:]):
        if c >= v2:
            return p2 + (p1 - p2) * (c - v2) / (v1 - v2)
    return 0.0


class Batteries:
    def __init__(self, path_fn, cap_mah=2500, log=print):
        self.path_fn = path_fn            # () -> путь battery.json (папка данных задаётся при запуске)
        self.cap = cap_mah                # ёмкость пары по умолчанию, мА·ч
        self.log = log
        self.lock = threading.Lock()
        self.s = {}                       # mac -> пара: v, i (сглаженные), q, t0, vmax, alarm …
        self.measured = {}                # mac -> последняя измеренная ёмкость пары, мА·ч
        self.loaded = False

    def _load(self):
        self.loaded = True
        try:
            d = json.load(open(self.path_fn(), encoding="utf-8"))
            self.cap = int(d.get("cap") or self.cap)
            self.measured = {k: float(v) for k, v in (d.get("measured") or {}).items()}
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.path_fn(), "w", encoding="utf-8") as f:
                json.dump({"cap": self.cap, "measured": self.measured}, f, ensure_ascii=False)
        except OSError as e:
            self.log(f"[bat] не записал battery.json: {e}")

    def set_cap(self, mah):
        mah = int(mah)
        if not 300 <= mah <= 10000:
            raise ValueError("ёмкость пары: 300–10000 мА·ч")
        with self.lock:
            if not self.loaded:
                self._load()
            self.cap = mah
            self._save()

    def ingest(self, mac, b, t):
        v, i = b.get("v"), b.get("i")
        if not isinstance(v, (int, float)) or not isinstance(i, (int, float)) or v <= 1:
            return
        with self.lock:
            if not self.loaded:
                self._load()
            st = self.s.get(mac)
            if st is None or (t - st["t"] > 20 and v > st["v"] + NEW_PACK_DV):
                if st is not None and st["vmax"] >= FULL_V and st["v"] <= WARN_V and st["q"] > 100:
                    self.measured[mac] = round(st["q"])          # от полной до «пора менять» — это и есть ёмкость
                    self._save()
                    self.log(f"[bat] {mac}: пара отдала {st['q']:.0f} мА·ч — ёмкость записана")
                if st is not None:
                    self.log(f"[bat] {mac}: новая пара, {v:.2f} В")
                st = self.s[mac] = {"v": v, "i": i, "q": 0.0, "t0": t, "t": t, "vmax": v, "vmin": v,
                                    "alarm": 0, "n": 0}
            dt = t - st["t"]
            if 0 < dt <= 10:
                st["q"] += max(0.0, i) * dt / 3600.0
                st["v"] += 0.2 * (v - st["v"])                    # напряжение — за ~5 с
                st["i"] += min(1.0, dt / 60.0) * (i - st["i"])    # ток — за ~минуту: оценка «сколько осталось»
            elif dt > 10:                                         # перерыв: агент был выключен — сначала
                st["v"], st["i"] = v, i
            st["t"] = t
            st["n"] += 1
            st["vmax"] = max(st["vmax"], st["v"])
            st["vmin"] = min(st["vmin"], st["v"])

    def _row(self, mac, st, now):
        cap = self.measured.get(mac) or self.cap
        pct = soc(st["v"])
        left_mah = min(pct / 100.0 * cap, max(0.0, cap - st["q"])) if st["vmax"] >= FULL_V else pct / 100.0 * cap
        left_min = left_mah / st["i"] * 60.0 if st["i"] > 20 else None
        level = "low" if st["v"] <= LOW_V else "warn" if st["v"] <= WARN_V or (left_min is not None and left_min < WARN_MIN) else "ok"
        return {"mac": mac, "v": round(st["v"], 3), "i": round(st["i"], 1), "w": round(st["v"] * st["i"] / 1000.0, 2),
                "q": round(st["q"], 1), "soc": round(pct), "cap": cap, "measured": mac in self.measured,
                "left_min": round(left_min) if left_min is not None else None, "level": level,
                "age": round(now - st["t"], 1), "since": round(now - st["t0"])}

    def state(self, label_fn):
        """Строки по контурам. label_fn(mac) -> {"label", "wheel", "kind", "id"} — берёт замок сервера, поэтому
        зовётся без своего замка (порядок замков: сервер — потом этот)."""
        now = time.time()
        with self.lock:
            rows = [self._row(m, dict(st), now) for m, st in self.s.items()]
            cap = self.cap
        for r in rows:
            r.update(label_fn(r["mac"]))
        rows.sort(key=lambda r: (r.get("wheel") or "9", r.get("kind") != "bus", r.get("id") or 999))
        return {"cap": cap, "warn_v": WARN_V, "low_v": LOW_V, "list": rows}

    def check_low(self):
        """Пары, только что ставшие «мало» (по разу на пару) — для паузы программы: [(mac, v)]."""
        now, out = time.time(), []
        with self.lock:
            for m, st in self.s.items():
                if now - st["t"] < 10 and st["n"] >= 5 and st["v"] <= LOW_V and not st["alarm"]:
                    st["alarm"] = 1
                    out.append((m, round(st["v"], 2)))
        return out
