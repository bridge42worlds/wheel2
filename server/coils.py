# -*- coding: utf-8 -*-
"""Катушки колёс: агент роли M (та же прошивка, что у моторов: motor.h) на колесе — его мост «a» питает катушку.

Какой агент — катушка какого колеса, задаёт состав (compose: slot «coil», wheels — номер колеса). Сервер раз в
секунду шлёт ему MOT a=±уровень b=0 r=5000 w=5: + — ток в одну сторону, − — в другую, 0 — выкл. Смену знака
агент делает через остановку и паузу 1 с (так устроен motor.h); сторож — нет команд 5 с, мост выключается.
Поле задают: шаг программы (слой «поле катушек»: «+», «−», «выкл»), отдых и конец программы — выкл,
«Всё стоп» — выкл, и вручную с планшетки. Состояние — только в памяти: после перезапуска сервера поле выкл.
"""
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
            return {"wheels": {w: dict(s) for w, s in self.st.items()}, "sent": dict(self.sent)}
