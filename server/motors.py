# -*- coding: utf-8 -*-
"""Моторы колёс: агент роли M (прошивка prime_lab, motor.h) крутит два щёточных мотора 36GP-555 через DRV8871.

Сервер раз в секунду шлёт агенту моторов MOT a=… b=… r=… w=… — ШИМ колеса 1 (a) и 2 (b) в ‰ со знаком
направления. Эта же команда — сторож: перестанет приходить (сервер упал, сеть пропала) — агент сам плавно
остановит моторы через w секунд.

Как крутятся колёса (pair):
  free     — по отдельности: у каждого свой режим;
  same     — вместе, в одну сторону: оба по настройкам колеса 1;
  opposite — навстречу: колесо 2 — по настройкам колеса 1, направление обратное;
  only1 / only2 — крутится одно колесо (по настройкам колеса 1), другое стоит.
Режим колеса (mode):
  off   — стоит;
  pwm   — заданный ШИМ, ‰;
  rpm   — заданные обороты колеса;
  zerog — невесомость наверху: обороты, при которых k = ω²r/g = 1 — по радиусу, который меряет датчик на
          ободе (ICM-42688, обороты пакета imu); нет замера — радиус из настроек.
В rpm и zerog: ШИМ = прямая подача (об/мин / RPM_FULL) + поправка ПИ по оборотам с обода; нет оборотов с
обода — только прямая подача. Смена направления каждые flip минут (0 — не менять); сам переход агент
делает через остановку и паузу 1 с. Состояние — только в памяти: после перезапуска сервера колёса стоят.
"""
import math
import threading
import time

RPM_FULL = 112.0          # об/мин на выходе редуктора при 100 % ШИМ без нагрузки (36GP-555, 12 В); колесо на валу мотора
KP, KI = 4.0, 2.0         # ПИ: ‰ ШИМ на 1 об/мин ошибки; ‰ на (об/мин · с)
I_MAX = 300.0             # предел накопленной поправки, ‰
G = 9.80665
WHEELS = {"a": "1", "b": "2"}
PAIRS = ("free", "same", "opposite", "only1", "only2")
MODES = ("off", "pwm", "rpm", "zerog")


def zero_g_rpm(r_mm):
    """Обороты, при которых наверху невесомость: ω²r = g."""
    return 60.0 / (2 * math.pi) * math.sqrt(G / (r_mm / 1000.0))


class Motors:
    def __init__(self, agents_fn, send_fn, rpm_fn, radius_fn=None, rmm_fn=None, log=print):
        self.agents_fn = agents_fn            # () -> [(mac, ip, alive)] агентов роли M
        self.send_fn = send_fn                # (line, ip)
        self.rpm_fn = rpm_fn                  # (колесо "1"/"2") -> обороты с обода или None
        self.radius_fn = radius_fn or (lambda w: (None, None))   # (колесо) -> (радиус мм, k последнего оборота)
        self.rmm_fn = rmm_fn or (lambda: 450.0)                  # радиус из настроек, мм
        self.log = log
        self.lock = threading.Lock()
        self.ramp = 500                       # ‰ в секунду
        self.wd = 5                           # с
        self.pair = "free"
        self.st = {w: {"mode": "off", "dir": 1, "pwm": 0, "rpm": 44.6, "flip": 0.0, "t_flip": 0.0,
                       "i": 0.0, "duty": 0, "meas": None, "fb": False, "target": None, "r": None, "k": None,
                       "r_src": None, "eff": "off", "edir": 1} for w in WHEELS}
        self.sent = None

    # ---------- команды ----------
    def set(self, body):
        """POST /api/motors: {pair} и/или {wheel: a|b, mode, dir, pwm, rpm, flip}, {ramp, wd} или {stop: true}."""
        with self.lock:
            if body.get("stop"):
                for s in self.st.values():
                    s["mode"], s["i"], s["flip"] = "off", 0.0, 0.0
                self.log("[mot] стоп")
                return self.brief_locked()
            if "ramp" in body:
                self.ramp = int(min(5000, max(50, float(body["ramp"]))))
            if "wd" in body:
                self.wd = int(min(60, max(2, float(body["wd"]))))
            if "pair" in body:
                if body["pair"] not in PAIRS:
                    raise ValueError("pair: free, same, opposite, only1 или only2")
                if body["pair"] != self.pair:
                    for s in self.st.values():
                        s["i"] = 0.0
                self.pair = body["pair"]
                self.log(f"[mot] колёса: {self.pair}")
            w = body.get("wheel")
            if w is not None:
                if w not in self.st:
                    raise ValueError("wheel: a (колесо 1) или b (колесо 2)")
                s = self.st[w]
                if "mode" in body:
                    if body["mode"] not in MODES:
                        raise ValueError("mode: off, pwm, rpm или zerog")
                    if body["mode"] != s["mode"]:
                        s["i"] = 0.0
                        if s["mode"] == "off":          # колесо трогается: смена направления — через flip минут ДВИЖЕНИЯ,
                            s["t_flip"] = time.time()   # а не от момента, когда таймер задали стоящему колесу
                    s["mode"] = body["mode"]
                if "dir" in body:
                    d = 1 if int(body["dir"]) >= 0 else -1
                    if d != s["dir"]:
                        s["i"] = 0.0
                    s["dir"] = d
                if "pwm" in body:
                    s["pwm"] = int(min(1000, max(0, float(body["pwm"]))))
                if "rpm" in body:
                    s["rpm"] = float(min(RPM_FULL, max(0.0, float(body["rpm"]))))
                if "flip" in body:
                    s["flip"] = float(min(240, max(0, float(body["flip"]))))
                    s["t_flip"] = time.time()
                self.log(f"[mot] колесо {WHEELS[w]}: {s['mode']} {'вперёд' if s['dir'] > 0 else 'назад'} "
                         f"ШИМ {s['pwm']} ‰ / {s['rpm']} об/мин, смена направления {s['flip'] or 'нет'} мин")
            return self.brief_locked()

    def apply_spec(self, spec, src=""):
        """Шаг программы (серия с планшетки): {pair, mode, dir, rpm, pwm} — связка колёс и режим ведущего
        колеса 1 (в same / opposite / only1 / only2 второе колесо идёт по нему); смена направления по таймеру — нет."""
        self.set({"pair": spec["pair"]})
        body = {"wheel": "a", "mode": spec["mode"], "dir": spec.get("dir", 1), "flip": 0}
        for k in ("rpm", "pwm"):
            if spec.get(k) is not None:
                body[k] = spec[k]
        self.set(body)
        if src:
            self.log(f"[mot] {src}: {spec['pair']} {spec['mode']}")
        return self.brief()

    def _eff(self, w):
        """Что колесо w делает на самом деле с учётом связки колёс: (режим, направление, ШИМ ‰, об/мин)."""
        s, L = self.st[w], self.st["a"]
        if self.pair == "free":
            return s["mode"], s["dir"], s["pwm"], s["rpm"]
        if (self.pair == "only1" and w == "b") or (self.pair == "only2" and w == "a"):
            return "off", 1, 0, 0.0
        d = -L["dir"] if self.pair == "opposite" and w == "b" else L["dir"]
        return L["mode"], d, L["pwm"], L["rpm"]

    # ---------- раз в секунду ----------
    def tick(self, dt=1.0):
        now = time.time()
        meas = {w: self.rpm_fn(WHEELS[w]) for w in self.st}
        rad = {w: self.radius_fn(WHEELS[w]) for w in self.st}
        rmm = float(self.rmm_fn() or 450.0)
        with self.lock:
            leaders = list(self.st) if self.pair == "free" else ["a"]
            for w in leaders:                                  # смена направления по таймеру
                s = self.st[w]
                if s["mode"] != "off" and s["flip"] and now - s["t_flip"] >= s["flip"] * 60:
                    s["dir"], s["t_flip"] = -s["dir"], now
                    for x in self.st.values():
                        x["i"] = 0.0
                    self.log(f"[mot] колесо {WHEELS[w]}{'' if self.pair == 'free' else ' (и связанное)'}: смена направления")
            out = {}
            for w, s in self.st.items():
                mode, d, pwm, rpm_set = self._eff(w)
                s["meas"], s["eff"], s["edir"] = meas[w], mode, d
                r_meas, k = rad[w]
                s["k"] = k
                if mode == "off":
                    duty, s["fb"], s["target"] = 0.0, False, None
                elif mode == "pwm":
                    duty, s["fb"], s["target"] = float(pwm), False, None
                else:
                    if mode == "zerog":
                        r_use = r_meas if r_meas and 100 <= r_meas <= 2000 else rmm
                        s["r"], s["r_src"] = round(r_use, 1), "обод" if r_use is r_meas else "настройки"
                        tgt = min(RPM_FULL, zero_g_rpm(r_use))
                    else:
                        tgt = rpm_set
                    s["target"] = round(tgt, 2)
                    ff = tgt / RPM_FULL * 1000.0
                    # Обратная связь — только когда колесо и вправду едет к цели (обод ≥ 60 % цели): на разгоне и после
                    # смены направления (агент тормозит до нуля, пауза 1 с, разгон) интегратор иначе накапливал бы
                    # ошибку «стоящего» колеса и давал перерегулирование 10–20 % на четверть минуты
                    if meas[w] is None or abs(s["duty"]) < 50 or abs(meas[w]) < 0.6 * tgt:
                        duty, s["fb"] = ff, False
                        if meas[w] is not None and abs(meas[w]) < 0.3 * tgt:
                            s["i"] = 0.0                          # колесо стоит или крутится не туда: прошлый интеграл не нужен
                    else:
                        err = tgt - meas[w]
                        raw = ff + KP * err + s["i"]
                        if 0.0 < raw < 1000.0 or (raw >= 1000.0 and err < 0) or (raw <= 0.0 and err > 0):
                            s["i"] = max(-I_MAX, min(I_MAX, s["i"] + KI * err * dt))   # не копить, упёршись в предел ШИМ
                        duty, s["fb"] = ff + KP * err + s["i"], True
                duty = max(0.0, min(1000.0, duty))
                s["duty"] = int(round(duty)) * d
                out[w] = s["duty"]
            line = f"MOT a={out['a']} b={out['b']} r={self.ramp} w={self.wd}"
        for mac, ip, alive in self.agents_fn():
            if alive and ip:
                self.send_fn(line, ip)
        self.sent = (round(now, 1), line)

    def run(self, stop=None):
        while not (stop and stop.is_set()):
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:                    # noqa: BLE001 — моторы важнее одной ошибки
                self.log(f"[mot] ошибка: {e!r}")
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    # ---------- наружу ----------
    def brief_locked(self):
        now = time.time()
        out = {}
        for w, s in self.st.items():
            lead = self.st[w] if self.pair == "free" else self.st["a"]
            out[w] = {**{k: v for k, v in s.items() if k != "t_flip"}, "wheel": WHEELS[w],
                      "flip_left": round(max(0.0, lead["flip"] * 60 - (now - lead["t_flip"])), 0) if lead["flip"] else None}
        return {"ramp": self.ramp, "wd": self.wd, "pair": self.pair, "rpm_full": RPM_FULL, "sent": self.sent,
                "zerog_set": round(zero_g_rpm(float(self.rmm_fn() or 450.0)), 2), "wheels": out}

    def brief(self):
        with self.lock:
            return self.brief_locked()
