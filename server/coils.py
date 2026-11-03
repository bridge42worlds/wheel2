# -*- coding: utf-8 -*-
"""Катушки колёс: агент роли M (та же прошивка, что у моторов: motor.h) на колесе — его мост «a» питает катушку.

Какой агент — катушка какого колеса, задаёт состав (compose: slot «coil», wheels — номер колеса). Сервер раз в
секунду шлёт ему MOT a=±уровень b=0 r=5000 w=5: + — ток в одну сторону, − — в другую, 0 — выкл. Смену знака
агент делает через остановку и паузу 1 с (так устроен motor.h); сторож — нет команд 5 с, мост выключается.
Поле задают: шаг программы (слой «поле катушек»: «+», «−», «выкл»), отдых и конец программы — выкл,
«Всё стоп» — выкл, и вручную с планшетки. Состояние — только в памяти: после перезапуска сервера поле выкл.

Переменное поле (ac): та же катушка, но ток — синус или мягкий меандр (прямоугольник с плавными фронтами)
частоты f (0,1–50 Гц). «+» и «−» — фаза 0 и 180°: обе катушки на «+» идут в одной фазе, «+» и «−» — в
противофазе. Агенту — fa (мГц), ka (форма), ea (фронт, мс) и qa — фаза по часам сервера на момент отправки
(доля периода от начала эпохи): до 5 Гц агент плавно подстраивает свою фазу под неё, поэтому две катушки на
разных агентах идут в одной фазе с точностью до задержки сети (единицы мс). Огибающая растёт и спадает
плавно — за 3 периода, от 1 до 5 с; смена рода поля (постоянное ↔ переменное, другая форма)
— через ноль. Магнитометр колеса меряет раз в секунду средним — переменное он видит размахом |B| внутри
секунды: фон и направление «+» на переменном не учатся.

С вращением (ac.sync = {"k": множитель, "deg": сдвиг}): частота поля — k × частота оборотов колеса, фаза — от
метки верха агента обода (прошивка шлёт её в imu, при молчании с rt — отдельным пакетом top): синус в максимуме,
когда агент с акселерометром проходит верх (сдвиг deg — позже на столько градусов периода поля). Поле в трубке
одно на весь обод, поэтому у других агентов колеса свой постоянный сдвиг: агент напротив проходит верх при
минимуме. Метки нет (колесо стоит, датчик молчит) — поле идёт прежней частотой без подстройки фазы.

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
SHAPES = {"sin": 0, "sq": 1}                  # синус, мягкий меандр
F_MIN, F_MAX = 0.1, 50.0                       # Гц


def ac_norm(ac):
    """Переменное поле: {"f": Гц, "shape": "sin"|"sq", "edge": мс, "sync": {"k", "deg"}} или None — постоянное."""
    if not ac:
        return None
    sync = None
    if ac.get("sync"):                        # с вращением: частота — k × обороты, f — пока меток нет
        k = float(ac["sync"].get("k") or 1)
        if not 0.25 <= k <= 4:
            raise ValueError("поле с вращением: множитель от 0,25 до 4")
        sync = {"k": round(k, 3), "deg": int(float(ac["sync"].get("deg") or 0)) % 360}
    f = float(ac.get("f") or (1.0 if sync else 0))
    if not F_MIN <= f <= F_MAX:
        raise ValueError(f"частота переменного поля от {F_MIN:g} до {F_MAX:g} Гц")
    shape = ac.get("shape") or "sin"
    if shape not in SHAPES:
        raise ValueError("форма переменного поля: sin — синус, sq — мягкий меандр")
    edge = int(min(2000, max(10, float(ac.get("edge") or 100))))
    return {"f": round(f, 3), "shape": shape, "edge": edge, **({"sync": sync} if sync else {})}


def ac_word(ac):
    """Переменное поле словами: «∿ 1 Гц», «⊓ 0,5 Гц (фронт 100 мс)»."""
    if not ac:
        return "постоянное"
    if ac.get("sync"):
        return f"⟳ ×{ac['sync']['k']:g} с вращением".replace(".", ",") + (f", сдвиг {ac['sync']['deg']}°" if ac["sync"]["deg"] else "")
    f = f"{ac['f']:g}".replace(".", ",")
    return f"∿ {f} Гц" if ac["shape"] == "sin" else f"⊓ {f} Гц (фронт {ac['edge']} мс)"


class Coils:
    def __init__(self, agents_fn, send_fn, log=print):
        self.agents_fn = agents_fn            # () -> {колесо "1"/"2": [(mac, ip, alive), …]} — агенты катушек
        self.send_fn = send_fn
        self.log = log
        self.lock = threading.Lock()
        self.st = {w: {"state": "off", "level": 1000, "ac": None, "src": "", "t": 0.0} for w in ("1", "2")}
        self.sent = {}
        self.applied = {w: [] for w in self.st}      # когда агенту ушло новое значение: [(время, a, f), …]
        self.learn = {}                               # колесо -> фон и направление «+» по магнитометру
        self.sense = {}                               # колесо -> последнее «поле по датчику»
        self.rot_fn = None                            # колесо -> (время верха, период оборота) или None — поле с вращением
        self.sync = {}                                # колесо -> как держится фаза: метка есть, её давность, период

    def set(self, wheel, state, level=None, src="вручную", ac=False):
        """ac: False — род поля как был, None — постоянное, {"f", "shape", "edge"} — переменное."""
        if wheel not in self.st:
            raise ValueError("колесо: 1 или 2")
        state = WORD.get(state, state)
        if state not in STATES:
            raise ValueError("поле: off, + или -")
        acn = ac_norm(ac) if ac is not False else False
        with self.lock:
            s = self.st[wheel]
            if level is not None:
                s["level"] = int(min(1000, max(50, float(level))))
            if acn is not False and acn != s["ac"]:
                self.log(f"[coil] колесо {wheel}: {ac_word(s['ac'])} → {ac_word(acn)} ({src})")
                s["ac"] = acn
            if s["state"] != state:
                self.log(f"[coil] колесо {wheel}: {s['state']} → {state} ({src})")
            s["state"], s["src"], s["t"] = state, src, round(time.time(), 1)

    def set_field(self, field, src):
        """Поле из шага программы: {"1": "+", "2": "выкл", "ac": {"f": 1, "shape": "sin"}, "level": 700}; пропущенное
        колесо — выкл; без "ac" — постоянное, без "level" — полный уровень (род и уровень у обеих катушек одни).
        Неполный уровень постоянного — контроль: мост переключается (ШИМ), а поле не меняется."""
        ac = (field or {}).get("ac") or None
        level = (field or {}).get("level") or 1000
        for w in self.st:
            self.set(w, (field or {}).get(w, "выкл"), level=level, src=src, ac=ac)

    def cmd(self, s, now=None, rot=None, w=None):
        """Команда агенту катушки по состоянию колеса s (под замком): (строка, a, f для истории). rot — (время верха,
        период оборота) колеса для поля с вращением."""
        a = STATES[s["state"]] * s["level"]
        ac = s["ac"]
        if not ac:
            return f"MOT a={a} b=0 r=5000 w=5 fa=0", a, 0
        now = now or time.time()
        f, q = ac["f"], None
        if ac.get("sync"):
            k, sh = ac["sync"]["k"], ac["sync"]["deg"] / 360.0
            if rot and rot[1] > 0:
                t_top, T = rot
                f = min(F_MAX, max(F_MIN, k / T))
                # максимум синуса (и середина «+» меандра) — четверть периода: у верха агента с акселерометром
                q = int(((k * (now - t_top) / T + 0.25 + sh) % 1.0) * 1000) % 1000
                ac["f"] = round(f, 3)
                if w:
                    self.sync[w] = {"ok": True, "T": round(T, 3), "age": round(now - t_top, 2), "f": round(f, 3)}
            elif w:
                self.sync[w] = {"ok": False, "f": f}  # метки нет — прежней частотой, фаза без подстройки
        else:
            q = int((now * f % 1.0) * 1000) % 1000   # фаза по часам сервера, тысячные периода
        r = int(min(5000, max(50, s["level"] / max(1.0, min(5.0, 3.0 / f)))))   # огибающая — за 3 периода, от 1 до 5 с
        line = f"MOT a={a} b=0 r={r} w=5 fa={int(round(f * 1000))} ka={SHAPES[ac['shape']]} ea={ac['edge']}"
        # в историю — род поля, а не точная частота: с вращением она меняется каждую секунду
        return line + (f" qa={q}" if q is not None else ""), a, (-1 if ac.get("sync") else int(round(f * 1000)))

    def all_off(self, src):
        for w in self.st:
            self.set(w, "off", src=src)

    def tick(self):
        ags = self.agents_fn()
        rot = {w: self.rot_fn(w) for w in self.st} if self.rot_fn else {}   # до замка: rot_fn берёт замок сервера
        with self.lock:
            now = time.time()                         # одно «сейчас» на оба колеса — фаза у обеих катушек общая
            cmds = {w: self.cmd(s, now, rot.get(w), w) for w, s in self.st.items()}
        for w, lst in ags.items():                    # всем агентам катушки колеса (их может быть несколько)
            for mac, ip, alive in (lst if isinstance(lst, list) else [lst]):
                if alive and ip and w in cmds:
                    line, a, f = cmds[w]
                    self.send_fn(line, ip)
                    self.sent[w] = (round(time.time(), 1), line)
                    with self.lock:
                        h = self.applied[w]
                        if not h or h[-1][1:] != (a, f):
                            h.append((time.time(), a, f))
                            del h[:-8]

    def _active(self, w, t0, t1):
        """(a, f), которые агент держал весь отрезок [t0, t1], или None — внутри было переключение.
        Смена знака у агента идёт через остановку на 1 с (motor.h) — первая секунда после неё не в счёт;
        переменное поле разгоняется за 3 периода (от 1 до 5 с) — до этого тоже не в счёт."""
        h, cur, prev = self.applied.get(w) or [], None, None
        for t, a, f in h:
            if t > t1:
                break
            if t > t0 - 0.1:
                return None
            prev, cur = cur, (t, a, f)
        if cur is None:
            return None
        if prev and prev[1] and cur[1] and (prev[1] > 0) != (cur[1] > 0) and t0 < cur[0] + 1.2:
            return None
        if cur[2] and t0 < cur[0] + max(1.0, min(5.0, 3.0 / (abs(cur[2]) / 1000.0))) + 0.2:   # −1 — с вращением
            return None
        return cur[1], cur[2]

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
            act = self._active(w, t - 1.15, t)
            if act and act[1] and act[0]:             # переменное: видно размахом |B| за секунду, фон и «+» не учатся
                self.sense[w] = {"mac": mac, "t": round(t, 2), "b": [round(x, 1) for x in b], "babs": round(babs, 1),
                                 "steady": steady, "nbg": L["nbg"], "nu": L["nu"], "cmd": "~",
                                 "spread": round(spread or 0, 1),
                                 "seen": "~" if (spread or 0) > max(25.0, 0.2 * (L["ref"] or 0)) else "off"}
                self.sense[w]["agree"] = self.sense[w]["seen"] == "~"
                return
            a = act[0] if steady and act and not act[1] else None
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
            return {"wheels": {w: dict(s) for w, s in self.st.items()}, "sent": dict(self.sent), "sync": dict(self.sync),
                    "sense": {w: {**x, "age": round(time.time() - x["t"], 1)} for w, x in self.sense.items()}}
