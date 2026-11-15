"""Долгий опыт: один режим надолго. Колёса навстречу в невесомости; на колесе с катушкой (К1) — переменное поле ∿
(на частоте вращения или с заданной частотой); генераторы «каждая плата сама» на одном чистом шуме — регистр ГСЧ
напрямую, без хеша (самый быстрый счёт без подмесов). Генераторы весь опыт молчат; биты простых, тепло и счётчики
плата сама кладёт в таблицу отрезков и сдаёт после опыта.

    python tools/phase_run.py                          К1 считает, К2 поглощает; поле 1 мин выкл · 10 мин ∿ · 1 мин выкл
    python tools/phase_run.py --min 20 --blk 2.5       поле блоками ABBA по 2,5 мин (выкл вкл вкл выкл …) — 20 мин
    python tools/phase_run.py --freqs rot,20           по опыту на каждую частоту ∿ (rot — частота вращения, или Гц);
                                                       эталон статики — только перед первым
    python tools/phase_run.py --all                    оба колеса считают «каждая сама» (без поглощения)
    python tools/phase_run.py --ss                     каждая плата ещё и рассеивает свои простые в эфир (ESP-NOW)
    python tools/phase_run.py --ro                     эгоизм: оба колеса «каждая сама», Wi-Fi и ESP-NOW плат колёс выкл
                                                       весь шаг — на связь только после опыта; статика меряет
    python tools/phase_run.py --blind --ro             то же без наблюдения: статика ESP не читает датчик (sx), эталона
                                                       нет (берётся записанный); телефон — закрыть приложение руками
    python tools/phase_run.py --journal                и журнал простых — сверить подсчёт бит платой с сервером
    python tools/phase_run.py --nofix                  без фиксации: ни журнала, ни таблиц — только счёт и итог шага
    python tools/phase_run.py --bits 64 --td 100 --sq 1   длина числа, отсев делителями до td, квадрат умножением
    python tools/phase_run.py ... --dry                только проверка программы и время
    разбор: python tools/phase_report.py <номера программ>; ход часов — сам после каждого опыта

Отрезки по 30 с. Поле блоками ABBA отделяет от поля и линейный, и изогнутый дрейф (аккумуляторы, тепло).
"""
import argparse, json, os, subprocess, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--min", type=float, default=10, help="без --blk: поле ∿, минут; с --blk — весь опыт, минут")
ap.add_argument("--off", type=float, default=1, help="без --blk: поле выкл в начале и в конце, минут")
ap.add_argument("--blk", type=float, default=0, help="поле блоками ABBA по столько минут (0 — один блок поля)")
ap.add_argument("--seg", type=int, default=30, help="отрезок, с")
ap.add_argument("--freqs", default="rot", help="частоты ∿ через запятую: rot — частота вращения, иначе Гц (0,1…50)")
ap.add_argument("--base", type=float, default=2, help="эталон статики при вращении перед первым опытом, мин (0 — записанный)")
ap.add_argument("--all", action="store_true", help="оба колеса считают «каждая сама» (без поглощения)")
ap.add_argument("--ss", action="store_true", help="каждая плата рассеивает свои простые в эфир (не с --ro)")
ap.add_argument("--ro", action="store_true", help="радио плат колёс выкл весь шаг, оба колеса «каждая сама»")
ap.add_argument("--blind", action="store_true", help="без наблюдения: статика не мерит, эталона нет")
ap.add_argument("--journal", action="store_true", help="и журнал простых (сверка подсчёта бит платой)")
ap.add_argument("--nofix", action="store_true", help="без фиксации: без таблиц отрезков и журнала")
ap.add_argument("--both", action="store_true", help="каждую частоту дважды: с таблицами, потом без фиксации")
ap.add_argument("--bits", type=int, default=256, choices=[32, 64, 128, 256, 512, 1024, 2048])
ap.add_argument("--td", type=int, default=1000, choices=[0, 100, 1000, 10000, 20000], help="отсев делителями до td")
ap.add_argument("--tc", type=int, default=1, choices=[0, 1], help="два проверяльщика: 1 — ядро связи тоже проверяет (прошивка 07.11), 0 — только ядро счёта")
ap.add_argument("--sq", type=int, default=0, choices=[0, 1], help="квадрат в проверке: 0 ускорителем, 1 умножением")
ap.add_argument("--dry", action="store_true")
ap.add_argument("--guard", type=int, default=820, help="ШИМ, ‰: выше 10 с из 15 — стоп всего")
ap.add_argument("--src", type=int, default=128, help="источник шума (битовая маска config.h): 128 регистр ГСЧ, 1 ГСЧ, 8 дрожание, 2 CSI, 4 АЦП, 16 эфир B, 32 акселерометр, 64 магнитометр")
ap.add_argument("--raw", type=int, default=1, choices=[0, 1], help="1 — сырьё в число как есть (без хеша), 0 — через SHA-256")
ap.add_argument("--cascade", action="store_true", help="каждое колесо считает каскадом (группа A: mode 1), а не «каждая сама»")
ap.add_argument("--nofield", action="store_true", help="поле выключено весь опыт (катушки не нужны): сравнение режимов счёта/вращения без поля")
ap.add_argument("--dir", type=int, default=1, choices=[1, -1], help="направление колеса 1 (+1 вперёд, −1 назад); колесо 2 — по --pair")
ap.add_argument("--still", action="store_true", help="колёса стоят весь опыт (статика без механики вращения)")
ap.add_argument("--pair", default="opposite", choices=["opposite", "same"], help="колёса навстречу (opposite) или в одну сторону (same)")
A = ap.parse_args()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P  # noqa: E402


def req(p, b=None):
    r = urllib.request.Request(A.url + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


FREQS = []
for f in A.freqs.split(","):
    f = f.strip()
    if f != "rot" and not 0.1 <= float(f) <= 50:
        sys.exit(f"частота {f}: rot или 0,1…50 Гц")
    FREQS.append(f if f == "rot" else float(f))
if A.ss and A.ro:
    sys.exit("--ss с --ro не бывает: при выключенном радио рассеивать некуда")
fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) in ("1", "2")]
live = {a["mac"]: a for a in req("/api/dash")["agents"] if a.get("mac")}
W = {"1": [], "2": []}
for b in sorted(fleet, key=lambda b: b["id"]):
    if (live.get(b["mac"]) or {}).get("status") == "online" or A.dry:
        W[str(b["wheel"])].append(b["mac"])
ID = {b["mac"]: b["id"] for b in fleet}
SP = [a["mac"] for a in live.values() if a.get("kind") == "S" and a.get("status") == "online"]
if A.blind and any(live[m].get("dev") == "phone" for m in SP):
    print("◐ без наблюдения: телефон командой не выключить — закройте приложение на время опыта")
if not A.dry and (len(W["1"]) < 6 or len(W["2"]) < 6):
    sys.exit(f"не все на связи: К1 {len(W['1'])}, К2 {len(W['2'])}")
cbat = {b.get("wheel"): b for b in (req("/api/battery").get("list") or []) if b.get("kind") == "coil" and b.get("alive")}
LIVE = [w for w in ("1", "2") if cbat.get(w) and (cbat[w].get("v") or 0) >= 6.5]
if A.dry and not LIVE:
    LIVE = ["1", "2"]                               # проверка без установки — как с обеими катушками
if A.nofield:
    LIVE = []                                       # поле выключено весь опыт — катушки не нужны
    print("поле выключено весь опыт")
elif not LIVE:
    sys.exit("ни одной живой катушки (на связи и ≥ 6,5 В) — опыт без поля не имеет смысла")
else:
    for w in ("1", "2"):
        print(f"✓ катушка К{w}: {cbat[w]['v']:.1f} В" if w in LIVE and cbat.get(w) else
              f"◐ катушка К{w} не на связи или разряжена — поле только на {', '.join('К' + x for x in LIVE)}")

ZG = {"pair": "free", "mode": "off", "dir": 1, "zg": 0} if A.still else {"pair": A.pair, "mode": "zerog", "dir": A.dir, "zg": 0}
OFF = {"1": "выкл", "2": "выкл"}
SRC_NAMES = {1: "ГСЧ", 8: "дрожание", 128: "регистр ГСЧ", 2: "CSI", 4: "АЦП", 16: "эфир B", 32: "акселерометр", 64: "магнитометр"}
SRC_NAME = "+".join(v for k, v in SRC_NAMES.items() if A.src & k) or str(A.src)
NOISE = {"src": A.src, "raw": A.raw}              # по умолчанию регистр ГСЧ напрямую, слова как есть — без хеша и подмесов
BOTH = A.all or A.ro                               # оба колеса считают сами (при радио выкл поглощать некому)
SS = 1 if A.ss else 0


def schedule(freq):
    """Состояния поля по отрезкам. Обе катушки и --blk: блоки выкл · К1 · К2 · оба · оба · К2 · К1 · выкл (зеркально —
    линейный дрейф уходит из всех разностей; поле каждого колеса горит половину времени, действие каждого — отдельно).
    Одна катушка и --blk: ABBA (выкл вкл вкл выкл …). Без --blk: выкл · поле · выкл."""
    acs = {"f": freq, "shape": "sin"}
    fname = "на частоте вращения" if freq == "rot" else f"{freq:g} Гц"
    sym = "∿" if freq == "rot" else f"∿ {freq:g} Гц"

    def st(k1, k2):
        on = k1 or k2
        coil = {"1": "+" if k1 else "выкл", "2": "+" if k2 else "выкл", **({"ac": acs} if on else {})}
        name = "выкл" if not on else f"{sym} " + ("оба" if k1 and k2 else "К1" if k1 else "К2")
        return {"name": name, "coil": coil, "set": {"form": fname if on else "выкл", "dir": "+" if on else "выкл",
                                                    "k1": "1" if k1 else "0", "k2": "1" if k2 else "0"}}
    two = LIVE == ["1", "2"]
    off, on = st(False, False), st("1" in LIVE, "2" in LIVE)
    if A.blk > 0:
        k = max(1, round(A.blk * 60 / A.seg))
        nb = round(A.min / A.blk)
        pat = [off, st(True, False), st(False, True), st(True, True)] if two else [off, on]
        order = [0, 1, 2, 3, 3, 2, 1, 0] if two else [0, 1, 1, 0]
        if nb % len(order):
            print(f"◐ блоков {nb} — не кратно {len(order)}: зеркальный порядок неполный, дрейф отделится хуже")
        out = []
        for b in range(nb):
            x = pat[order[b % len(order)]]
            out += [{**x, "set": {**x["set"], "round": str(b + 1)}} for _ in range(k)]
        return out
    n_off, n_on = round(A.off * 60 / A.seg), round(A.min * 60 / A.seg)
    return ([{**off, "set": {**off["set"], "round": "начало"}}] * n_off + [{**on, "set": {**on["set"], "round": "поле"}}] * n_on
            + [{**off, "set": {**off["set"], "round": "конец"}}] * n_off)


def program(freq, fix, first):
    gen = {"mode": 0, "dc": 1, "pl": 1 if fix and A.journal else 0, "ro": 1 if A.ro else 0, "bits": A.bits, "td": A.td,
           "sq": A.sq, "tc": A.tc, "tmax": 73, "cpu": 240, "hb": 10, "ping": 100 if A.src & 2 else 0, "cf": 1 if A.src & 2 else 0, "det": 0, "ss": SS, **NOISE,
           **({"mode": 1, "gv": 2, "rk": 1, "pw": 50} if A.cascade else {})}
    own = {m: {"role": 0, "det": 0, "ss": SS, "xf": 4, "rw": 4, **NOISE} for m in W["1"]}
    IMU = [b["mac"] for b in fleet if b.get("id") in (6, 14)]         # 07.11: платы с акселерометром не молчат — по ним смотрим
    for m in IMU:
        if m in own: own[m]["tl"] = 0
    if BOTH:
        own.update({m: {"role": 0, "det": 0, "ss": SS, "xf": 4, "rw": 4, **NOISE, **({"tl": 0} if m in IMU else {})} for m in W["2"]})
    else:                                          # К2 поглощает простые К1
        gen.update(sm=1, sh=0)
        own.update({m: {"role": 1, "ba": [], "src": 1, "raw": 0, "det": 0, "ss": 0} for m in W["2"]})
    how = (("оба колеса каскадом" if A.cascade else "оба колеса каждая сама") if BOTH else "К1 сама · К2 поглощает") + (" · круг (рассеяние, эфир в шум)" if A.ss and A.src & 16 else "") + (" · без поля" if A.nofield else "") + (" · колёса стоят" if A.still else ("" if A.pair == "opposite" else " · в одну сторону") + ("" if A.dir > 0 else " · к1 назад")) + (" · радио выкл" if A.ro else "") \
        + (" · рассеивают своё" if SS else "")
    groups = [{"targets": W["1"] + W["2"], "cfg": gen, "own": own, "label": how}]
    sx = 1 if A.blind else 0                       # без наблюдения: статика (ESP) не читает датчик весь шаг
    if SP:
        groups.append({"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "sx": sx},
                       "own": {m: {"role": 2, "cpu": 80, "sg": 0} for m in SP},
                       "label": "◆ статика не мерит" if sx else "◆ статика меряет"})
    states = schedule(freq)
    fword = ("∿ на частоте вращения" if freq == "rot" else f"∿ {freq:g} Гц") + (
        " · К1, К2, обе по очереди" if LIVE == ["1", "2"] and A.blk > 0 else " · " + " и ".join("К" + w for w in LIVE))
    obs = "без наблюдения" if A.blind else "статика меряет"
    word = f"{A.bits} бит · {how} · {fword} · {obs}" + ("" if fix else " · без фиксации") + (" · журнал" if fix and A.journal else "")
    step = {"name": f"долгий: {word}", "minutes": A.seg * len(states) / 60, "control": False, "agent": True,
            "fseq": {"seg": A.seg, "tab": fix, "states": states}, "groups": groups, "mot": ZG, "coil": OFF,
            "layers": {"rot": "■ стоят" if A.still else ("⟳⟲ навстречу" if A.pair == "opposite" else "⟳⟳ в одну сторону") + (" · к1 вперёд" if A.dir > 0 else " · к1 назад") + " · невесомость", "family": "долгий", "noise": SRC_NAME, "proc": "без хеша" if A.raw else "SHA-256",
                       "block": how, "field": fword},
            "set": {"noise": SRC_NAME, "proc": "без хеша" if A.raw else "SHA-256", "block": "долгий", "freq": str(freq), "obs": obs,
                    "radio": "выкл" if A.ro else "вкл", "fix": "таблицы" if fix else "нет", "bits": str(A.bits),
                    "К1": " ".join(str(ID[m]) for m in W["1"]), "К2": " ".join(str(ID[m]) for m in W["2"])}}
    base = A.base if first and not A.blind else 0
    prog = {"name": f"долгий опыт · {word}", "baseline_min": base, "baseline_mot": ZG, "repeat": 1, "shuffle": False,
            "seed": 1, "rest": 0, "z": 4, "confirm": 0, "redo": 0, "again": 0, "static_spoil": False, "metrics": [],
            "targets": W["1"] + W["2"] + SP, "steps": [step]}
    if not base:                                   # без эталона колёса стоят: разгон до невесомости с включённым радио —
        prog["steps"].insert(0, {                  # с выключенным регулятор остался бы без оборотов и держал ШИМ разгона
            "name": "разгон: колёса " + ("навстречу" if A.pair == "opposite" else "в одну сторону") + " до невесомости", "minutes": 1.0, "control": False, "mot": ZG, "coil": OFF,
            "groups": [{"targets": W["1"] + W["2"], "cfg": {"mode": 0, "dc": 0, "run": 0}, "own": {}, "label": "все стоят"}]})
    if A.blind and SP:                             # после опыта — статика снова меряет (короткий шаг, все стоят)
        prog["steps"].append({"name": "наблюдение снова: статика меряет", "minutes": 0.1, "control": False,
                              "mot": ZG, "coil": OFF,
                              "groups": [{"targets": W["1"] + W["2"], "cfg": {"mode": 0, "dc": 0, "run": 0}, "own": {},
                                          "label": "все стоят"},
                                         {"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "sx": 0},
                                          "own": {m: {"role": 2, "cpu": 80, "sg": 0} for m in SP}, "label": "◆ статика меряет"}]})
    P.validate(json.loads(json.dumps(prog)))
    return prog, len(states), base


def run(freq, fix, first):
    prog, n, base = program(freq, fix, first)
    est = (base or 1.2) * 60 + 35 + A.seg * (1 + n) + 60
    print(f"{prog['name']}: {n} отрезков по {A.seg} с · ≈ {est / 60:.0f} мин", flush=True)
    if A.dry:
        return True, est
    r = req("/api/program/start", {"program": prog})
    if not r.get("ok"):
        sys.exit(f"✗ не запустилась: {r.get('error')}")
    pid, t0, x, why, hist, tick = r["id"], time.time(), {}, None, [], 0
    print(f"программа №{pid} запущена; разбор: python tools/phase_report.py {pid}", flush=True)
    try:
        while True:
            time.sleep(1)
            wh = req("/api/motors").get("wheels") or {}
            hist = (hist + [{w: (abs((wh.get(w) or {}).get("duty") or 0), abs((wh.get(w) or {}).get("meas") or 0),
                                 (wh.get(w) or {}).get("target")) for w in ("a", "b")}])[-15:]
            over = [w for w in ("a", "b") if sum(1 for h in hist if h[w][0] > A.guard) >= 10]
            swap = len(hist) == 15 and all(h["a"][2] and h["b"][2] and (
                (h["a"][0] > 800 and h["a"][1] < 0.8 * h["a"][2] and h["b"][1] > 1.1 * h["b"][2]) or
                (h["b"][0] > 800 and h["b"][1] < 0.8 * h["b"][2] and h["a"][1] > 1.1 * h["a"][2])) for h in hist[-10:])
            if over or swap:
                why = "моторы перепутаны мостами (swap)?" if swap else f"ШИМ > {A.guard} ‰ 10 с из 15"
                print(f"✗ сторож: {why} — стоп", flush=True)
                req("/api/program/stop", {"now": True})
                break
            tick += 1                                  # состояние программы — раз в 10 с (hist короче 15 не растёт)
            if tick % 10:
                continue
            x = req(f"/api/program?id={pid}")
            if x.get("state") not in ("running", "starting", "paused"):
                break
    finally:
        req("/api/motors", {"stop": True})
        req("/api/coils", {"off": True, "src": "конец опыта"})
    print(f"программа №{pid}: {x.get('state')} за {(time.time() - t0) / 60:.1f} мин; разбор: python tools/phase_report.py {pid}",
          flush=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "clock_drift.py"), str(pid)])   # ход часов
    return not why and x.get("state") == "done", 0


total, first = 0, True
for freq in FREQS:
    for fix in ([True, False] if A.both else [not A.nofix]):
        ok, est = run(freq, fix, first)
        total += est
        first = False
        if not ok:
            sys.exit(1)
if A.dry:
    print(f"всего ≈ {total / 60:.0f} мин")
