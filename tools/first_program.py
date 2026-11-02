"""Первый эксперимент на двух колёсах, ~40 минут: невесомость навстречу и вместе, поле, сложные шумы, поглощение и
обратный шум, оба колеса одной группой, колесо-пустышка (подслушивающие, мост), у каждого свой шум.

    python tools/first_program.py              план словами и время — ничего не запускает
    python tools/first_program.py --start      эталон статики при вращении навстречу, затем программа

Состав берётся с сервера: шина колеса — агенты колеса (не катушки), у каждого колеса свой акселерометр
(датчик обода ICM-42688) и магнитометр (MLX90393) — по телеметрии (sens). Программа идёт без перемешивания:

  эталон   колёса навстречу на невесомости (73,2 об/мин, k = 1), эталон шума статики 2 мин — вибрация моторов
           входит в эталон (серия №17: стоя записанный эталон браковал каждый вращающийся опыт);
  0        только статика: агенты стоят, колёса навстречу — что вращение даёт датчикам без счёта;
  1        каскад, ГСЧ, каждое колесо своей группой: навстречу k = 1 · поле выкл, + +, + −; ступени −3 %
           (k ≈ 0,94) и +3 % (k ≈ 1,06); снова k = 1; вместе k = 1 · поле выкл, + + (колесо 2 — через
           остановку); потом колёса стоят: контроль, поле + +, контроль; отдых;
  2        сложные шумы каскадом: Σ все, ГСЧ + акселерометр + магнитометр, CSI с пульса, АЦП без хеша —
           каждый стоя (контроль) и навстречу k = 1 с полем + + (обратный порядок — дрейф поровну); отдых;
  4        навстречу k = 1, поле выкл: К1 генерирует — К2 поглощает (смешать целиком) против пустышки и
           наоборот; обратный шум — К2 поглощает простые К1, К1 считает только из его кадров (петля через
           эфир) против пустышки; оба колеса одной группой (12 каскадом) — поле выкл и + +;
  5        с колесом-пустышкой (если есть; выключаются --no-listen, --no-bridge), навстречу k = 1, поле выкл:
           подслушивающие — оба колеса одной группой считают только из кадров плат колеса-пустышки, те
           поглощают простые обоих колёс (смешать целиком) против пустышки-поглотителя; мост — первая плата
           звеном каскада колеса 1 (неподвижное среди вращающихся), вторая — одна, навстречу и контроль стоя; отдых;
  3       каждый сам (эгоистичный), у каждого агента свой шум: акселерометр, магнитометр, ГСЧ, CSI, АЦП, RC —
           навстречу k = 1 с полем + + и без, стоя с полем + +, контроль стоя; отдых;
  конец    третий контроль каскада — у опытов блока 1 три контроля (начало, середина, конец).

Колесо-пустышка — колесо 3 в составе (две платы без датчиков, без мотора и поля, лежат рядом): в каждом опыте своей
группой тем же режимом, что колёса — каскад, тот же шум, «каждый сам» (ГСЧ и CSI); в опытах с поглощением —
пара между собой: одна генерирует, другая поглощает (смешать или пустышка), в обратном шуме — поглощает и
возвращает кадры. Проверяется тем же критерием; её «аномалия» — ложная тревога детектора. Нет её — опыты те же.
В блоке 5 платы колеса-пустышки работают вместе с колёсами — там они участники опыта, не пустышка (сервер
считает пустышкой только группу из одних её плат).

Сравнение генерации — с контролем того же режима (control_for); статика — со своим эталоном. Проверочная
пара при аномалии — одна (confirm 1), испорченный опыт повторяется один раз. Для прогона на имитаторах —
короткие времена: --url http://127.0.0.1:8091 --sec 4 --own-sec 5 --rest 5 --settle 0.3 --spin-wait 10.
"""
import argparse, json, sys, time, urllib.error, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--start", action="store_true", help="записать эталон и запустить программу")
ap.add_argument("--rpm", type=float, default=73.2, help="невесомость, об/мин (колесо 1: тахометр и обод 30.10)")
ap.add_argument("--zg", type=float, default=3.0, help="ступень около невесомости, %% оборотов")
ap.add_argument("--sec", type=float, default=30, help="измерение опыта, с (статике — от 20)")
ap.add_argument("--own-sec", type=float, default=60, help="измерение в блоке «у каждого свой шум», с (медленные шумы)")
ap.add_argument("--rest", type=float, default=60, help="отдых между блоками, с")
ap.add_argument("--settle", type=float, default=1.0, help="множитель установления")
ap.add_argument("--base", choices=("spin", "still", "keep"), default="spin",
                help="эталон статики: при вращении навстречу (spin), стоя в начале программы (still), записанный (keep)")
ap.add_argument("--base-min", type=float, default=2, help="эталон, мин")
ap.add_argument("--spin-wait", type=float, default=90, help="ждать выхода колёс на обороты перед эталоном, с")
ap.add_argument("--partial", action="store_true", help="запускать, даже если кого-то из шин нет на связи")
ap.add_argument("--listen", action=argparse.BooleanOptionalAction, default=True,
                help="подслушивающие: колёса считают из кадров плат колеса-пустышки (--no-listen — без них)")
ap.add_argument("--bridge", action=argparse.BooleanOptionalAction, default=True,
                help="мост: плата колеса-пустышки звеном каскада колеса 1 (--no-bridge — без него)")
A = ap.parse_args()

CASCADE = {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}
SOLO = {"mode": 0, "dc": 1}
COMMON = {"pl": 1, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "raw": 0}   # пульс 10/с у всех: радио одинаково
RNG = {"src": 1}
MIXES = [("Σ все (ГСЧ + CSI + АЦП + RC)", {"src": 15}), ("ГСЧ + акселерометр + магнитометр", {"src": 97}),
         ("CSI с пульса", {"src": 2}), ("АЦП без хеша, фон Нейман", {"src": 4, "raw": 2})]
OWN_ORDER = [(1, "ГСЧ"), (2, "CSI с пульса"), (4, "АЦП D0"), (8, "дрожание RC")]
STEADY = 5          # установление, с: только поле
MODE = 8            # смена способа или шума
SPEED = 15          # смена оборотов около невесомости
SPIN = 20           # разгон или остановка колёс
OVER = 11           # на опыт сверх измерения и установления: проверка 10 с, настройка, итоги (замер на 8091)


def get(p):
    return json.load(urllib.request.urlopen(A.url + p, timeout=15))


def post(p, b):
    r = urllib.request.Request(A.url + p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


def say(t):
    print(f"{time.strftime('%H:%M:%S')} {t}", flush=True)


# ---------- состав ----------
fleet = [b for b in get("/api/fleet")["agents"] if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) in ("1", "2", "3")]
live = {a["mac"]: a for a in get("/api/dash")["agents"] if a.get("mac")}
W, OFF, SENS = {"1": [], "2": [], "3": []}, [], {}
for b in sorted(fleet, key=lambda b: b["id"]):
    a = live.get(b["mac"]) or {}
    if a.get("status") not in ("online", "late", "quiet"):
        OFF.append(b["id"])
        continue
    W[str(b["wheel"])].append(b["mac"])
    SENS[b["mac"]] = int((a.get("values") or {}).get("sens") or a.get("sens") or 0)
ID = {b["mac"]: b["id"] for b in fleet}
if not W["1"] or not W["2"]:
    sys.exit(f"нужны оба колеса: на связи колесо 1 — {len(W['1'])}, колесо 2 — {len(W['2'])}")
D = W.pop("3")                                  # колесо-пустышка: колесо 3 без мотора и поля
DL = "колесо-пустышка"


def own_noise(macs):
    """У каждого агента колеса свой шум: акселерометр — у агента с датчиком обода, магнитометр — у агента с
    MLX90393, остальным по очереди ГСЧ, CSI, АЦП, RC."""
    acc = next((m for m in macs if SENS[m] & 1), None)
    mag = next((m for m in macs if SENS[m] & 2 and m != acc), None)
    out, rest = {}, [m for m in macs if m not in (acc, mag)]
    if acc:
        out[acc] = (32, "акселерометр")
    if mag:
        out[mag] = (64, "магнитометр")
    for j, m in enumerate(rest):
        out[m] = OWN_ORDER[j % len(OWN_ORDER)]
    return out


OWN = {w: own_noise(W[w]) for w in W}
OWN["3"] = own_noise(D)                         # датчиков нет — ГСЧ и CSI


# ---------- шаги ----------
def groups(way, noise=None, own=False):
    out = []
    for w, macs in (("1", W["1"]), ("2", W["2"]), ("3", D)):
        if not macs:
            continue
        o = {m: {"role": 0, **({"src": OWN[w][m][0]} if own else {})} for m in macs}
        out.append({"targets": macs, "cfg": {**(SOLO if way == "solo" else CASCADE), **COMMON, **(noise or RNG)},
                    "own": o, "label": f"{DL if w == '3' else 'колесо ' + w} · {'каждый сам' if way == 'solo' else 'каскад'}"})
    return out


def dummy_pair(gw, sham, noise=None):
    """Колесо-пустышка в опыте с поглощением — пара между собой: та, что «на колесе gw», генерирует, другая поглощает
    (тот же способ: смешать или пустышка, тот же шум). Одна плата — просто генерирует (обратного шума ей не из чего)."""
    if not D:
        return []
    if len(D) == 1:
        return [{"targets": D, "cfg": {**CASCADE, **COMMON, **RNG}, "own": {D[0]: {"role": 0}}, "label": f"{DL} · каскад"}]
    g, a = (D[0], D[1]) if gw == "1" else (D[1], D[0])
    return [{"targets": [g, a], "cfg": {**CASCADE, **COMMON, **(noise or RNG), "sm": 1, "sh": 1 if sham else 0},
             "own": {g: {"role": 0}, a: {"role": 1, "ba": [], "src": 1}},
             "label": f"{DL} · №{ID[g]} генерирует, №{ID[a]} поглощает"}]


def absorb(gw, sham, noise=None):
    """Колесо gw генерирует каскадом, другое поглощает «все со всеми» (смешать целиком или пустышка) — одна группа,
    как в планшетке (makeStep, attachAbs). noise — шум генераторов (обратный — src 16); у поглотителей свой ГСЧ."""
    aw = "2" if gw == "1" else "1"
    own = {m: {"role": 0} for m in W[gw]}
    own.update({m: {"role": 1, "ba": [], "src": 1} for m in W[aw]})
    return [{"targets": W[gw] + W[aw], "cfg": {**CASCADE, **COMMON, **(noise or RNG), "sm": 1, "sh": 1 if sham else 0},
             "own": own, "label": f"колесо {gw} · генерация, колесо {aw} поглощает"}] + dummy_pair(gw, sham, noise)


def merged(noise=None):
    """Оба колеса одной группой: 12 агентов каскадом (предел прошивки — 16 в группе); колесо-пустышка — своей."""
    m = W["1"] + W["2"]
    return [{"targets": m, "cfg": {**CASCADE, **COMMON, **(noise or RNG)}, "own": {x: {"role": 0} for x in m},
             "label": "колёса 1+2 · генерация одной группой"}] + [g for g in groups("cascade", noise) if g["targets"] == D]


def listen(sham):
    """Подслушивающие: оба колеса одной группой считают только из кадров плат колеса-пустышки (обратный шум,
    src 16), а те поглощают простые обоих колёс (смешать целиком или пустышка) — петля через неподвижных."""
    m = W["1"] + W["2"]
    own = {x: {"role": 0} for x in m}
    own.update({x: {"role": 1, "ba": [], "src": 1} for x in D})
    return [{"targets": m + D, "cfg": {**CASCADE, **COMMON, **BC, "sm": 1, "sh": 1 if sham else 0}, "own": own,
             "label": f"колёса 1+2 · генерация из кадров плат {DL} (поглощают)"}]


def bridge():
    """Мост: первая плата колеса-пустышки — звено каскада колеса 1 (неподвижное среди вращающихся), колесо 2 —
    своим каскадом, вторая плата — одна, «каждый сам» (ГСЧ): она и остаётся пустышкой."""
    b, k1 = D[0], W["1"] + D[:1]
    out = [{"targets": k1, "cfg": {**CASCADE, **COMMON, **RNG}, "own": {x: {"role": 0} for x in k1},
            "label": f"колесо 1 + №{ID[b]} (мост) · каскад"},
           {"targets": W["2"], "cfg": {**CASCADE, **COMMON, **RNG}, "own": {x: {"role": 0} for x in W["2"]},
            "label": "колесо 2 · каскад"}]
    if D[1:]:
        out.append({"targets": D[1:], "cfg": {**SOLO, **COMMON, **RNG}, "own": {x: {"role": 0} for x in D[1:]},
                    "label": f"{DL} · каждый сам"})
    return out


def mot(k, pair="opposite"):
    """k — ступень около невесомости, % (None — колёса стоят); pair — навстречу (opposite) или вместе (same)."""
    if k is None:
        return {"pair": "opposite", "mode": "off", "dir": 1}
    return {"pair": pair, "mode": "rpm", "dir": 1, "rpm": round(A.rpm * (1 + k / 100), 1)}


def rot_word(k, pair="opposite"):
    if k is None:
        return "■■ стоят"
    kk = (1 + k / 100) ** 2
    way = "⟳⟲ навстречу" if pair == "opposite" else "⟳⟳ вместе"
    return f"{way} {A.rpm * (1 + k / 100):.1f} об/мин · k ≈ {kk:.2f}".replace(".", ",")


STEPS = []


def step(name, way, k, field, noise=None, nword="ГСЧ", own=False, control=False, control_for=None, sec=None,
         rest=False, idle=False, g=None, lay=None, pair="opposite"):
    """g, lay — свои группы и слои колёс (поглощение, одна группа на оба колеса); иначе каждое колесо — своей группой."""
    f = {"1": field[0], "2": field[1]}
    wl = {"role": "idle" if idle else "gen", "way": "" if idle else way, "noise": "" if idle else nword}
    s = {"name": name, "minutes": round((sec or A.sec) / 60, 3), "mot": mot(k, pair), "coil": f, "rest": rest,
         "layers": {"rot": rot_word(k, pair), "field": f, "w1": (lay or {}).get("1", wl), "w2": (lay or {}).get("2", wl)}}
    if idle:
        s["groups"] = [{"targets": W["1"] + W["2"] + D, "cfg": {"mode": 0, "dc": 0, "run": 0}, "own": {},
                        "label": "все стоят — только статика"}]
    else:
        s["groups"] = g or groups(way, noise, own)
    if control:
        s["control"] = True
    if control_for:
        s["control_for"] = control_for
    prev = STEPS[-1] if STEPS else {"mot": mot(0) if A.base == "spin" else mot(None), "groups": [{"cfg": {"mode": 0}}],
                                    "coil": {"1": "выкл", "2": "выкл"}, "name": ""}
    moving = lambda m: m["mode"] != "off"
    turn = moving(prev["mot"]) != moving(s["mot"]) or (moving(s["mot"]) and prev["mot"]["pair"] != s["mot"]["pair"])
    settle = (SPIN if turn else SPEED if prev["mot"] != s["mot"]
              else MODE if prev["groups"] != s["groups"] else STEADY)
    s["settle_s"] = round(settle * A.settle, 1)
    STEPS.append(s)


OFFF, PP, PM = ("выкл", "выкл"), ("+", "+"), ("+", "−")
C1, z = "контроль: каскад, стоят, поле выкл", A.zg
step("только статика: навстречу, k = 1", "", 0, OFFF, idle=True, control=True)
# блок 1 — невесомость навстречу, каскад
step("каскад · навстречу k = 1 · поле выкл", "cascade", 0, OFFF, control_for=C1)
step("каскад · навстречу k = 1 · поле + +", "cascade", 0, PP, control_for=C1)
step("каскад · навстречу k = 1 · поле + −", "cascade", 0, PM, control_for=C1)
step(f"каскад · навстречу −{z:g} % · поле выкл", "cascade", -z, OFFF, control_for=C1)
step(f"каскад · навстречу +{z:g} % · поле выкл", "cascade", z, OFFF, control_for=C1)
step("каскад · навстречу k = 1 · поле выкл · ещё раз", "cascade", 0, OFFF, control_for=C1)
step("каскад · вместе k = 1 · поле выкл", "cascade", 0, OFFF, control_for=C1, pair="same")   # колесо 2 — через остановку
step("каскад · вместе k = 1 · поле + +", "cascade", 0, PP, control_for=C1, pair="same")
step(C1, "cascade", None, OFFF, control=True)
step("каскад · стоят · поле + +", "cascade", None, PP, control_for=C1)
step(C1, "cascade", None, OFFF, control=True)
# блок 2 — сложные шумы: стоя (контроль), потом навстречу k = 1 с полем + + в обратном порядке
for i, (nw, nz) in enumerate(MIXES):
    step(f"контроль шума: {nw} · стоят", "cascade", None, OFFF, noise=nz, nword=nw, control=True, rest=i == 0)
for nw, nz in reversed(MIXES):
    step(f"{nw} · навстречу k = 1 · поле + +", "cascade", 0, PP, noise=nz, nword=nw, control_for=f"контроль шума: {nw} · стоят")
# блок 4 — излучение и поглощение навстречу k = 1, поле выкл: против пустышки (тот же трафик без числа); потом оба
# колеса одной группой. Колёса после блока 2 не останавливаются — разгона нет
GEN = {"role": "gen", "way": "cascade", "noise": "ГСЧ"}
ABS = lambda how: {"role": "abs", "way": f"all/{how}", "noise": ""}
SH = {"1": "контроль: К1 генерирует, К2 — пустышка · навстречу k = 1",
      "2": "контроль: К2 генерирует, К1 — пустышка · навстречу k = 1"}
step(SH["1"], "cascade", 0, OFFF, g=absorb("1", True), lay={"1": GEN, "2": ABS("sham")}, control=True, rest=True)
step("К1 генерирует, К2 поглощает (смешать целиком) · навстречу k = 1", "cascade", 0, OFFF, g=absorb("1", False),
     lay={"1": GEN, "2": ABS("mix")}, control_for=SH["1"])
step("К2 генерирует, К1 поглощает (смешать целиком) · навстречу k = 1", "cascade", 0, OFFF, g=absorb("2", False),
     lay={"1": ABS("mix"), "2": GEN}, control_for=SH["2"])
step(SH["2"], "cascade", 0, OFFF, g=absorb("2", True), lay={"1": ABS("sham"), "2": GEN}, control=True)
# обратный шум: К2 поглощает простые К1 и выпускает кадры, К1 строит числа только из этих кадров — петля через эфир
BC, BW = {"src": 16}, {"role": "gen", "way": "cascade", "noise": "обратный шум (только он)"}
SB = "контроль: обратный шум К1 ← К2, К2 — пустышка · навстречу k = 1"
step(SB, "cascade", 0, OFFF, g=absorb("1", True, BC), lay={"1": BW, "2": ABS("sham")}, control=True)
step("обратный шум К1 ← К2: К2 поглощает простые К1, К1 считает из его кадров · навстречу k = 1", "cascade", 0, OFFF,
     g=absorb("1", False, BC), lay={"1": BW, "2": ABS("mix")}, control_for=SB)
C12 ="контроль: оба колеса одной группой · навстречу k = 1 · поле выкл"
step(C12, "cascade", 0, OFFF, g=merged(), control=True)
step("оба колеса одной группой · навстречу k = 1 · поле + +", "cascade", 0, PP, g=merged(), control_for=C12)
# блок 5 — с колесом-пустышкой: подслушивающие и мост (выключаются --no-listen, --no-bridge). Здесь его платы —
# участники опыта: в группе с колёсами сервер их пустышкой не считает
if A.listen and D and len(W["1"] + W["2"] + D) <= 16:
    LW = {"role": "gen", "way": "cascade", "noise": f"обратный шум от {DL}"}
    SL = "контроль: подслушивающие — поглотитель-пустышка, колёса считают из кадров · навстречу k = 1"
    step(SL, "cascade", 0, OFFF, g=listen(True), lay={"1": LW, "2": LW}, control=True)
    step("подслушивающие: платы колеса-пустышки поглощают простые обоих колёс (смешать), колёса считают из их кадров"
         " · навстречу k = 1",
         "cascade", 0, OFFF, g=listen(False), lay={"1": LW, "2": LW}, control_for=SL)
elif A.listen and D:
    print(f"подслушивающих нет: колёса и {DL} вместе — {len(W['1'] + W['2'] + D)} плат, в группе не больше 16")
if A.bridge and D:
    CB = f"контроль: мост — №{ID[D[0]]} в каскаде колеса 1 · стоят · поле выкл"
    step(f"мост: №{ID[D[0]]} в каскаде колеса 1 · навстречу k = 1 · поле выкл", "cascade", 0, OFFF, g=bridge(),
         control_for=CB)
    step(CB, "cascade", None, OFFF, g=bridge(), control=True)
# блок 3 — каждый сам, у каждого свой шум
C3 = "контроль: у каждого свой шум, стоят, поле выкл"
step("у каждого свой шум · навстречу k = 1 · поле + +", "solo", 0, PP, nword="у каждого свой", own=True,
     control_for=C3, sec=A.own_sec, rest=True)
step("у каждого свой шум · навстречу k = 1 · поле выкл", "solo", 0, OFFF, nword="у каждого свой", own=True,
     control_for=C3, sec=A.own_sec)
step("у каждого свой шум · стоят · поле + +", "solo", None, PP, nword="у каждого свой", own=True, control_for=C3,
     sec=A.own_sec)
step(C3, "solo", None, OFFF, nword="у каждого свой", own=True, control=True, sec=A.own_sec)
step(C1, "cascade", None, OFFF, control=True, rest=True)

PROG = {"name": "первый эксперимент: невесомость навстречу, поле, шумы, у каждого свой шум",
        "baseline_min": A.base_min if A.base == "still" else 0, "repeat": 1, "shuffle": False, "settle_s": 8,
        "rest": A.rest, "z": 4, "confirm": 1, "redo": 1, "again": 0, "pause_lost": True,
        "metrics": ["pr_s", "cand_s"], "targets": W["1"] + W["2"] + D, "steps": STEPS,
        **({"dummy": D} if D else {})}

# ---------- план словами ----------
nm = lambda ms: " ".join(f"№{ID[m]}" for m in ms)
print(f"колесо 1: {nm(W['1'])} · колесо 2: {nm(W['2'])}" + (f" · колесо-пустышка: {nm(D)}" if D else " · колеса-пустышки нет")
      + (f" · нет на связи: {' '.join(f'№{i}' for i in OFF)}" if OFF else ""))
for w, macs in (("1", W["1"]), ("2", W["2"]), ("3", D)):
    if macs:
        print(f"  у каждого свой шум, {DL if w == '3' else 'колесо ' + w}: " + ", ".join(f"№{ID[m]} {OWN[w][m][1]}" for m in macs))
if D:
    print(f"  блок 5: подслушивающие {'да' if A.listen else 'выкл (--no-listen)'} · мост {'да' if A.bridge else 'выкл (--no-bridge)'}")
if len(D) >= 2:
    print(f"  поглощение у колеса-пустышки — между собой: К1 → К2 ~ №{ID[D[0]]} → №{ID[D[1]]}, К2 → К1 ~ №{ID[D[1]]} → №{ID[D[0]]}")
T = 0.0
if A.base == "spin":
    T += 40 + A.base_min * 60 + 10
    print(f"эталон: колёса навстречу {A.rpm:g} об/мин, статика {A.base_min:g} мин".replace(".", ","))
elif A.base == "still":
    T += A.base_min * 60 + 10
for i, s in enumerate(STEPS, 1):
    if s.get("rest"):
        T += A.rest
        print(f"   ⏸ отдых {A.rest:g} с")
    T += s["minutes"] * 60 + s["settle_s"] + OVER
    tag = "контроль" if s.get("control") else "опыт"
    print(f"{i:3d}. {s['name']}  ({s['minutes'] * 60:g} с + уст. {s['settle_s']:g} с, {tag})")
print(f"всего ≈ {T / 60:.0f} мин: опытов {len(STEPS)}, контрольных {sum(bool(s.get('control')) for s in STEPS)};"
      " найдётся аномалия — плюс пара «опыт — контроль», испорчен — повтор")
if not A.start:
    sys.exit(0)
if OFF and not A.partial:
    sys.exit(f"не все на связи ({' '.join(f'№{i}' for i in OFF)}) — включите или --partial")
d = get("/api/dash")
if d.get("program") or d.get("session"):
    sys.exit("уже идёт программа или сессия")

# ---------- эталон при вращении ----------
if A.base == "spin":
    post("/api/motors", {"pair": "opposite"})
    post("/api/motors", {"wheel": "a", "mode": "rpm", "dir": 1, "rpm": A.rpm, "flip": 0})
    say(f"колёса навстречу → {A.rpm:g} об/мин")
    t0 = time.time()
    while time.time() - t0 < A.spin_wait:
        time.sleep(2)
        wh = get("/api/motors")["wheels"]
        meas = [abs(wh[w].get("meas") or 0) for w in ("a", "b")]
        if all(abs(m - A.rpm) <= 0.02 * A.rpm for m in meas):
            break
    say(f"обороты: {' · '.join(f'колесо {wh[w].get('wheel', w)} {wh[w].get('meas')}' for w in ('a', 'b'))}"
        + ("" if time.time() - t0 < A.spin_wait else " — не вышли на заданные, эталон всё равно пишу"))
    time.sleep(10 * A.settle)                       # вибрация установилась
    r = post("/api/static/baseline", {"action": "start", "macs": "all", "minutes": A.base_min})
    if not r.get("ok"):
        post("/api/motors", {"stop": True})
        sys.exit(f"эталон не начался: {r.get('error')}")
    t0 = (r.get("rec") or {}).get("t0", time.time())
    say(f"эталон статики {A.base_min:g} мин…")
    while True:
        time.sleep(3)
        st = get("/api/static")
        if (st.get("last_rec") or {}).get("t0", 0) >= t0 - 1:
            break
    sens = (st.get("last_rec") or {}).get("sensors") or {}
    say("эталон: " + (", ".join(f"{m[-5:]} {'✓' if v.get('ok') else '✗'}" for m, v in sens.items()) or "датчиков нет"))
r = post("/api/program/start", {"program": PROG})
if not r.get("ok"):
    post("/api/motors", {"stop": True})
    sys.exit(f"программа не запустилась: {r.get('error')}")
say(f"программа №{r.get('id')} запущена — ход на планшетке")
