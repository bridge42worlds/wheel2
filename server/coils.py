# -*- coding: utf-8 -*-
"""Катушки колёс: агент роли M (та же прошивка, что у моторов: motor.h) на колесе — его мост «a» питает катушку.

Какой агент — катушка какого колеса, задаёт состав (compose: slot «coil», wheels — номер колеса). Сервер раз в
секунду шлёт ему MOT a=±уровень b=0 r=5000 w=5: + — ток в одну сторону, − — в другую, 0 — выкл. Смену знака
агент делает через остановку и паузу 1 с (так устроен motor.h); сторож — нет команд 5 с, мост выключается.
Поле задают: шаг программы (слой «поле катушек»: «+», «−», «выкл»), отдых и конец программы — выкл,
«Всё стоп» — выкл, и вручную с планшетки. Состояние — только в памяти: после перезапуска сервера поле выкл.

Поле по датчику (sense): магнитометр на колесе (MLX90393 агента колеса) раз в секунду присылает среднее поле
по осям. Секунды, когда катушка заведомо выключена и показания ровные, дают фон (Земля, железо рядом); секунды
с полем «+» — направление поля катушки на месте датчика. Показания минус фон — вектор поля самой катушки по осям
датчика, его проекция на направление «+» — величина со знаком; по ней видно, есть ли поле и куда оно на самом
деле, и совпадает ли это с командой. Датчик сдвинули — фон и направление переучиваются за несколько секунд.
"""
import math
import threading
import time

STATES = {"off": 0, "+": 1, "-": -1}
WORD = {"выкл": "off", "off": "off", "+": "+", "−": "-", "-": "-"}


class Coils:
    def __init__(self, agents_fn, send_fn, log=print):
        self.agents_fn = agents_fn            # () -> {колесо "1"/"2": [(mac, ip, alive), …]} — агенты катушек
        self.send_fn = send_fn
        self.log = log
        self.lock = threading.Lock()
        self.st = {w: {"state": "off", "level": 1000, "src": "", "t": 0.0} for w in ("1", "2")}
        self.sent = {}
        self.applied = {w: [] for w in self.st}      # когда агенту ушло новое значение: [(время, a), …]
        self.learn = {}                               # колесо -> фон и направление «+» по магнитометру
        self.sense = {}                               # колесо -> последнее «поле по датчику»

    def set(self, wheel, state, level=None, src="вручную"):
        if wheel not in self.st:
            raise ValueError("колесо: 1 или 2")
        state = WORD.get(state, state)
        if state not in STATES:
            raise ValueError("поле: off, + или -")
        with self.lock:
            s = self.st[wheel]
            if level is not None:
                s["level"] = int(min(1000, max(50, float(level))))
            if s["state"] != state:
                self.log(f"[coil] колесо {wheel}: {s['state']} → {state} ({src})")
            s["state"], s["src"], s["t"] = state, src, round(time.time(), 1)

    def set_field(self, field, src):
        """Поле из шага программы: {"1": "+", "2": "выкл"}; пропущенное колесо — выкл."""
        for w in self.st:
            self.set(w, (field or {}).get(w, "выкл"), src=src)

    def all_off(self, src):
        for w in self.st:
            self.set(w, "off", src=src)

    def tick(self):
        ags = self.agents_fn()
        with self.lock:
            cmds = {w: f"MOT a={STATES[s['state']] * s['level']} b=0 r=5000 w=5" for w, s in self.st.items()}
        for w, lst in ags.items():                    # всем агентам катушки колеса (их может быть несколько)
            for mac, ip, alive in (lst if isinstance(lst, list) else [lst]):
                if alive and ip and w in cmds:
                    self.send_fn(cmds[w], ip)
                    self.sent[w] = (round(time.time(), 1), cmds[w])
                    a = int(cmds[w].split()[1][2:])
                    with self.lock:
                        h = self.applied[w]
                        if not h or h[-1][1] != a:
                            h.append((time.time(), a))
                            del h[:-8]

    def _active(self, w, t0, t1):
        """Значение a, которое агент держал весь отрезок [t0, t1], или None — внутри было переключение.
        Смена знака у агента идёт через остановку на 1 с (motor.h) — первая секунда после неё не в счёт."""
        h, cur, prev = self.applied.get(w) or [], None, None
        for t, a in h:
            if t > t1:
                break
            if t > t0 - 0.1:
                return None
            prev, cur = cur, (t, a)
        if cur is None:
            return None
        if prev and prev[1] and cur[1] and (prev[1] > 0) != (cur[1] > 0) and t0 < cur[0] + 1.2:
            return None
        return cur[1]

    def field_sample(self, w, mac, b, spread, t):
        """Пакет магнитометра колеса w: b — среднее поле за секунду по осям (мкТл), spread — размах |B| внутри
        секунды, t — время приёма. Учит фон и направление «+», считает поле катушки по датчику."""
        if w not in self.st or not b or len(b) < 3:
            return
        with self.lock:
            L = self.learn.get(w)
            if L is None or L["mac"] != mac:
                L = self.learn[w] = {"mac": mac, "bg": None, "nbg": 0, "u": None, "nu": 0, "ref": None}
            babs = math.sqrt(sum(x * x for x in b))
            steady = spread is not None and spread <= max(25.0, 0.04 * babs)
            a = self._active(w, t - 1.15, t) if steady else None
            if a == 0:
                L["bg"] = list(b) if not L["bg"] else [g + 0.2 * (x - g) for g, x in zip(L["bg"], b)]
                L["nbg"] += 1
            elif a and L["bg"]:
                d = [x - g for x, g in zip(b, L["bg"])]
                n = math.sqrt(sum(x * x for x in d))
                if n > 10:
                    c = [(1 if a > 0 else -1) * x / n for x in d]
                    u = c if not L["u"] else [p + 0.2 * (q - p) for p, q in zip(L["u"], c)]
                    un = math.sqrt(sum(x * x for x in u)) or 1.0
                    L["u"] = [x / un for x in u]
                    L["ref"] = n if not L["ref"] else L["ref"] + 0.2 * (n - L["ref"])
                    L["nu"] += 1
            out = {"mac": mac, "t": round(t, 2), "b": [round(x, 1) for x in b], "babs": round(babs, 1),
                   "steady": steady, "nbg": L["nbg"], "nu": L["nu"]}
            if L["bg"]:
                d = [x - g for x, g in zip(b, L["bg"])]
                out["bg"] = [round(x, 1) for x in L["bg"]]
                out["d"] = [round(x, 1) for x in d]
                out["dabs"] = round(math.sqrt(sum(x * x for x in d)), 1)
                if L["u"]:
                    proj = sum(x * y for x, y in zip(d, L["u"]))
                    ref = L["ref"] or 1.0
                    out["proj"], out["ref"] = round(proj, 1), round(ref, 1)
                    out["seen"] = ("off" if out["dabs"] < max(15.0, 0.2 * ref) else "+" if proj > 0.5 * ref
                                   else "-" if proj < -0.5 * ref else "?")
            if a is not None:
                out["cmd"] = "off" if a == 0 else "+" if a > 0 else "-"
                if "seen" in out:
                    out["agree"] = out["seen"] == out["cmd"]
            self.sense[w] = out

    def run(self, stop=None):
        while not (stop and stop.is_set()):
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:                    # noqa: BLE001
                self.log(f"[coil] ошибка: {e!r}")
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    def brief(self):
        with self.lock:
            return {"wheels": {w: dict(s) for w, s in self.st.items()}, "sent": dict(self.sent),
                    "sense": {w: {**x, "age": round(time.time() - x["t"], 1)} for w, x in self.sense.items()}}
