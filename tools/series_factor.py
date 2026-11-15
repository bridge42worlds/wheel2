"""Факторная серия, поле внутри опыта: колёса всегда навстречу в невесомости; в каждом опыте режим, шум и обработка
постоянны, а поле само перебирает состояния по расписанию. Генераторы весь опыт молчат: счётчики на границах
отрезков снимают сами и сдают всё после опыта. План записывается и запечатывается хешем до первого запуска.

    python tools/series_factor.py --plan             записать план (data/series/factor_plan.json) и его хеш;
                                                     до записи блоки идут черновиком (пилот)
    python tools/series_factor.py --block B --dry    блок B: опыты по порядку, расписание поля, проверка, время
    python tools/series_factor.py --block B          запустить блок B на 8090 (сторож ШИМ)
    python tools/series_factor.py --block B --rep 2  второй проход того же блока (свой порядок)
    разбор: python tools/factor_report.py <номера программ>

Опыт: отрезок 0 — установление (поле выкл, настройка подтверждается, в разбор не идёт), потом 2 круга по 5 состояний
поля — выкл, = + −, = + +, ∿ + −, ∿ + + — в случайном порядке, по SEG с. ∿ — переменное поле на частоте вращения
(замерена по ободу перед молчанием, без подстройки фазы: ни одного пакета с обода во время опыта); --sync — ⟳ в
такт вращению (тогда обод шлёт метку верха и во время опыта).
Опыты блока — шум (⚂ внутренний — регистр ГСЧ напрямую · ≋ внешний — CSI с пульса) × обработка (SHA-256 · Тёплиц ×4 ·
сырьё), 6 опытов в случайном порядке. Совместная работа — блоком: A оба колеса одной группой, B каждое своей,
C генерация → поглощение, S — как B, внутренний шум, статики тоже считают. Колесо-пустышка (№22, №23) — тем же
способом своей парой (опора). Эталон статики — в начале блока при вращении. Пауза на аккумуляторы — между блоками.
"""
import argparse, hashlib, json, os, random, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--plan", action="store_true", help="записать план и хеш (один раз, до первого блока)")
ap.add_argument("--set", type=int, default=1, choices=[1, 2], help="набор: 1 — первый (04.11), 2 — быстрый счёт, вдвое больше «выкл», D вместо A")
ap.add_argument("--block", choices=["A", "B", "C", "S", "D"])
ap.add_argument("--rep", type=int, default=1, help="проход (свой порядок опытов и поля)")
ap.add_argument("--no-dummy", action="store_true", help="без колеса-пустышки (не на связи): опоры «все/опора» не будет")
ap.add_argument("--dry", action="store_true", help="только план блока и проверка программы, без запуска")
ap.add_argument("--sync", action="store_true", help="⟳ в такт вращению вместо ∿ на частоте вращения (обод шлёт метку)")
ap.add_argument("--guard", type=int, default=820, help="ШИМ, ‰: выше дольше 10 с — стоп всего")
A = ap.parse_args()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P  # noqa: E402  проверка программы до запуска

PLAN_FILE = os.path.join(ROOT, "data", "series", "factor_plan.json")
SEG, ROUNDS, BASE_MIN = 12, 2, 2     # отрезок поля, с; кругов состояний в опыте; эталон при вращении, мин
AC = {"f": "rot", "shape": "sin"} if not A.sync else {"f": 1, "shape": "sin", "sync": {"k": 1, "deg": 0}}
FORMS = [("=", "постоянное", None), ("⟳" if A.sync else "∿", "в такт вращению" if A.sync else "на частоте вращения", AC)]
DIRS = [("+ −", "+", "−"), ("+ +", "+", "+")]
NOISES = [("⚂", "внутренний", {"src": 128}), ("≋", "внешний", {"src": 2})]
PROCS = [("SHA", "SHA-256", {"raw": 0}), ("T×4", "Тёплиц ×4", {"raw": 4, "xf": 4}), ("сырьё", "без хеша", {"raw": 1})]
BLOCKS = {"A": "оба колеса одной группой", "B": "каждое колесо своей группой", "C": "генерация → поглощение",
          "S": "каждое своей группой · статики считают"}
SEEDS = {(b, r): 1000 * (ord(b) - 64) + r for b in BLOCKS for r in (1, 2, 3)}
OFF_N, PW = 1, 20                     # «выкл» в круге; срок ожидания кусочков в каскаде, мс
if A.set == 2:                        # второй набор: генерация в 3–5 раз быстрее, «выкл» дважды в круге — разность
    PLAN_FILE = os.path.join(ROOT, "data", "series", "factor_plan_2.json")   # «поле вкл − выкл» точнее в 1,3 раза
    PROCS = [("SHA", "SHA-256", {"raw": 0, "rw": 1}), ("T×4", "Тёплиц ×4", {"raw": 4, "xf": 4}), ("сырьё", "без хеша", {"raw": 1})]
    BLOCKS = {"D": "каждая плата сама", "B": "каждое колесо своей группой", "C": "генерация → поглощение"}
    SEEDS = {(b, r): 20000 + 1000 * (ord(b) - 64) + r for b in BLOCKS for r in (1, 2, 3)}
    OFF_N, PW = 2, 50                 # pw 50: в каскаде из 6 при 20 мс треть шума уходила на замену кусочков
    # вместо шума — вращение: внешний шум (CSI) давал 0,1–0,4 простых/с на плату — по отрезкам почти ничего;
    # «навстречу наоборот» смысла не меняет, поэтому второе вращение — оба колеса в одну сторону
    ROTS = [("⟳⟲", "навстречу", {"pair": "opposite"}), ("⟳⟳", "в одну сторону", {"pair": "same"})]
if A.block and A.block not in BLOCKS:
    sys.exit(f"блока {A.block} в наборе {A.set} нет: {', '.join(BLOCKS)}")
PRIMARY = {
    "static": ["p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "sd_v", "sd_h", "kurt_v", "kurt_h"],
    "gen": ["все/опора pr_s", "z_pq", "z_ps", "q_ шума", "s_ шума"],
    "effects": ["поле вкл − выкл", "форма", "+ + − + −", "форма × направление"],
    "rule": "достоверно: p с поправкой Бонферрони на все главные проверки ≤ 0,001, знак тот же во всех блоках, "
            "у статики — и у второго датчика; намёк: p с поправкой ≤ 0,05; остальное — разведка",
}


def req(p, b=None):
    r = urllib.request.Request(A.url + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def cells(block):
    """Опыты блока: (шум, обработка, вращение); у набора 1 вращение одно (None) — навстречу."""
    if A.set == 2:
        return [(NOISES[0], p, r) for r in ROTS for p in PROCS]
    return [(n, p, None) for n in NOISES for p in PROCS] if block != "S" else [(NOISES[0], p, None) for p in PROCS]


def cell_name(c):
    n, p, r = c
    return f"{n[1]} · {p[1]}" + (f" · {r[1]}" if r else "")


def plan_doc():
    return {"what": "факторная серия «Колесо-2», поле внутри опыта", "seg_s": SEG, "rounds": ROUNDS,
            "baseline_min": BASE_MIN, "rotation": "навстречу · невесомость", "quiet": "генераторы молчат весь опыт",
            "field": {"форма": [f[1] for f in FORMS], "направление": [d[0] for d in DIRS], "sync": A.sync},
            "blocks": {b: {"cells": [cell_name(c) for c in cells(b)],
                           "seeds": {str(r): SEEDS[(b, r)] for r in (1, 2)}} for b in BLOCKS},
            "primary": PRIMARY,
            "no_field": "катушка не на связи или ниже 6,5 В — колесо без поля: в главном не участвует, «контроль без поля»; "
                        "в C генерирует колесо с полем, поглощает колесо без поля (при обеих катушках — К2 генерирует, К1 поглощает)",
            **({"set": 2, "off_per_round": OFF_N, "cascade_pw_ms": PW, "procs": {q[1]: q[2] for q in PROCS},
                "D": "каждая плата сама (mode 0, два ядра)",
                "rotation_order": "половины блока по вращению в случайном порядке, между ними разворот 30 с"}
               if A.set == 2 else {})}


if A.plan:
    os.makedirs(os.path.dirname(PLAN_FILE), exist_ok=True)
    if os.path.exists(PLAN_FILE):
        sys.exit(f"план уже записан: {PLAN_FILE} — менять его после первого блока нельзя (новый план — новым файлом)")
    text = json.dumps(plan_doc(), ensure_ascii=False, indent=1, sort_keys=True)
    open(PLAN_FILE, "w", encoding="utf-8").write(text)
    print(f"план записан: {PLAN_FILE}\nхеш sha256: {hashlib.sha256(text.encode()).hexdigest()}")
    sys.exit(0)
if not A.block:
    sys.exit("нужен --plan или --block A|B|C|S")
if os.path.exists(PLAN_FILE):                     # план запечатан: блоки идут только по нему
    text = open(PLAN_FILE, encoding="utf-8").read()
    SHA = hashlib.sha256(text.encode()).hexdigest()
    if json.loads(text) != json.loads(json.dumps(plan_doc(), ensure_ascii=False, sort_keys=True)):
        sys.exit("план в файле не совпадает с этим скриптом (или с --sync) — их меняли после записи плана")
    TAG = f"план {SHA[:8]}"
else:                                              # пилот до записи плана: в выводы «достоверно» не идёт
    SHA, TAG = "", "черновик"
    print("◐ план не записан — блок пойдёт черновиком (пилот); запечатать: python tools/series_factor.py"
          + (" --set 2" if A.set == 2 else "") + " --plan")

fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) in ("1", "2", "3")]
live = {a["mac"]: a for a in req("/api/dash")["agents"] if a.get("mac")}
W = {"1": [], "2": [], "3": []}
for b in sorted(fleet, key=lambda b: b["id"]):
    if (live.get(b["mac"]) or {}).get("status") == "online" or A.dry:
        W[str(b["wheel"])].append(b["mac"])
ID = {b["mac"]: b["id"] for b in fleet}
D = [] if A.no_dummy else W["3"]
SP = [a["mac"] for a in live.values() if a.get("kind") == "S" and a.get("dev") != "phone" and a.get("status") == "online"]
V_COIL = 6.5                                      # драйвер катушки (DRV8871) ниже этого не включается
cag = (req("/api/coils").get("agents") or {})
cbat = {b.get("wheel"): b for b in (req("/api/battery").get("list") or []) if b.get("kind") == "coil" and b.get("alive")}
LIVE = set()
for w in ("1", "2"):                               # катушка не на связи или разряжена — колесо идёт без поля, серия не встаёт;
    b = cbat.get(w)                                # разбор по записи аккумуляторов в опыте сам отделит его как контроль
    if not (cag.get(w) or {}).get("alive") or not b:
        print(f"◐ катушка К{w} не на связи — колесо {w} без поля (в разборе — контроль без поля)")
    elif (b.get("v") or 0) < V_COIL:
        print(f"◐ катушка К{w}: аккумулятор {b['v']:.1f} В < {V_COIL} — колесо {w} без поля (в разборе — контроль без поля)")
    else:
        LIVE.add(w)
        print(f"✓ катушка К{w}: {b['v']:.1f} В")
# C: поле только у одного колеса — оно генерирует, колесо без поля поглощает; иначе — К2 генерирует, К1 поглощает
GEN_C, ABS_C = ("1", "2") if LIVE == {"1"} else ("2", "1")
need1 = A.block in ("A", "C")
if not A.dry and ((need1 and len(W["1"]) < 6) or len(W["2"]) < 6 or (len(D) < 2 and not A.no_dummy)):
    sys.exit(f"не все на связи: К1 {len(W['1'])}, К2 {len(W['2'])}, пустышка {len(D)}"
             + (" (блоку нужно колесо 1)" if need1 else ""))

CASCADE = {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": PW}
COMMON = {"pl": 1, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0, "ss": 0}
ZG = {"pair": "opposite", "mode": "zerog", "dir": 1, "zg": 0}
OFF = {"1": "выкл", "2": "выкл"}


def gen_own(macs, noise):
    """Свои у генераторов — явно: запомненные свои панели (src, raw …) не должны перебить план."""
    return {m: {"role": 0, "det": 0, "ss": 0, "xf": 4, "rw": 4, "raw": 0, **noise} for m in macs}


def groups(block, noise):
    cfg = {**CASCADE, **COMMON, **noise}
    if block == "A":                                   # оба колеса — одна группа
        out = [{"targets": W["1"] + W["2"], "cfg": cfg, "own": gen_own(W["1"] + W["2"], noise),
                "label": "оба колеса одной группой · каскад"}]
        if D:
            out.append({"targets": D, "cfg": cfg, "own": gen_own(D, noise), "label": "колесо-пустышка · каскад"})
    elif block == "D":                                 # каждая плата сама — без группы, самый быстрый счёт
        solo = {**COMMON, **noise, "mode": 0, "dc": 1}
        out = [{"targets": W["1"] + W["2"], "cfg": solo, "own": gen_own(W["1"] + W["2"], noise),
                "label": "колёса · каждая плата сама"}]
        if D:
            out.append({"targets": D, "cfg": solo, "own": gen_own(D, noise), "label": "колесо-пустышка · каждая плата сама"})
    elif block in ("B", "S"):                          # каждое колесо своей группой
        out = [{"targets": W[w], "cfg": cfg, "own": gen_own(W[w], noise),
                "label": f"{'колесо ' + w if w != '3' else 'колесо-пустышка'} · каскад"} for w in ("1", "2", "3") if W[w]]
    else:                                              # генерирует колесо с полем, поглощает колесо без поля (C_RULE);
        g, a = GEN_C, ABS_C                            # пустышка — пара так же
        own = gen_own(W[g], noise)
        own.update({m: {"role": 1, "ba": [], "src": 1, "raw": 0, "det": 0, "ss": 0} for m in W[a]})
        out = [{"targets": W[g] + W[a], "cfg": {**cfg, "sm": 1, "sh": 0}, "own": own, "label": f"К{g} генерирует, К{a} поглощает"}]
        if len(D) >= 2:
            g, a = D[1], D[0]
            out.append({"targets": [g, a], "cfg": {**cfg, "sm": 1, "sh": 0},
                        "own": {**gen_own([g], noise), a: {"role": 1, "ba": [], "src": 1, "raw": 0, "det": 0, "ss": 0}},
                        "label": f"колесо-пустышка · №{ID[g]} генерирует, №{ID[a]} поглощает"})
    if SP:                                             # статики: в S считают («каждый сам»), иначе только меряют
        run = block == "S"
        out.append({"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 1 if run else 0, "sg": 1 if run else 0, "src": 1},
                    "own": {m: {"role": 2, "cpu": 80, "sg": 1 if run else 0} for m in SP},
                    "label": f"◆ статика {'считает (участник)' if run else 'меряет'}"})
    return out


STATES = [("выкл", "выкл", OFF)] * OFF_N + [(f[1], d[0], {"1": d[1], "2": d[2], **({"ac": f[2]} if f[2] else {})})
                                     for f in FORMS for d in DIRS]


def fseq(seed):
    rnd, out = random.Random(seed), []
    for r in range(ROUNDS):
        order = list(range(len(STATES)))
        rnd.shuffle(order)
        for i in order:
            form, d, coil = STATES[i]
            sym = "выкл" if form == "выкл" else f"{FORMS[[f[1] for f in FORMS].index(form)][0]} {d}"
            out.append({"name": sym, "coil": coil, "set": {"form": form, "dir": d, "round": str(r + 1)}})
    return {"seg": SEG, "states": out}


STEPS = []
CELLS = cells(A.block)
ORDER = list(range(len(CELLS)))
if A.set == 2:                                     # половины по вращению — в случайном порядке, внутри — обработка
    rnd = random.Random(SEEDS[(A.block, A.rep)])
    halves = [[i for i, c in enumerate(CELLS) if c[2] is r] for r in ROTS]
    if (list(BLOCKS).index(A.block) + A.rep) % 2:     # чередование по блокам: вращение не совпадает со временем блока
        halves.reverse()
    for h in halves:
        rnd.shuffle(h)
    ORDER = halves[0] + halves[1]
for j in ORDER:
    n, p, rot = CELLS[j]
    noise = {**n[2], **p[2]}
    mot = {**ZG, **(rot[2] if rot else {})}
    if A.set == 2 and STEPS and STEPS[-1]["mot"] != mot:   # смена вращения: колесо 2 разворачивается — 30 с без счёта
        STEPS.append({"name": f"{A.block}: разворот → {rot[1]}", "minutes": 0.5, "control": False, "settle_s": 20,
                      "mot": mot, "coil": OFF, "groups": [{"targets": W["1"] + W["2"] + D, "cfg": {"mode": 0, "dc": 0, "run": 0},
                                                           "own": {}, "label": "все стоят — колёса разворачиваются"}],
                      "layers": {"rot": rot[0] + " " + rot[1], "family": A.block, "field": OFF}})
    fs = fseq(SEEDS[(A.block, A.rep)] * 100 + j)
    STEPS.append({"name": f"{A.block}: {n[0]} {n[1]} · {p[1]}{' · ' + rot[0] + ' ' + rot[1] if rot else ''} · поле внутри",
                  "minutes": SEG * len(fs["states"]) / 60,
                  "control": False, "agent": True, "fseq": fs, "groups": groups(A.block, noise), "mot": mot, "coil": OFF,
                  "layers": {"rot": f"{rot[0]} {rot[1]}" if rot else "⟳⟲ навстречу · невесомость", "family": A.block,
                             "noise": n[1], "proc": p[1], "block": BLOCKS[A.block], "field": OFF},
                  # К1/К2 — номера плат колёс: разбор делит генерацию по колёсам (поле у каждого своё)
                  "set": {"noise": n[1], "proc": p[1], "block": A.block, **({"rot": rot[1]} if rot else {}),
                          "К1": " ".join(str(ID[m]) for m in W["1"]), "К2": " ".join(str(ID[m]) for m in W["2"])}})
PROG = {"name": f"факторная серия{' 2' if A.set == 2 else ''} · блок {A.block} · проход {A.rep}{' · без пустышки' if A.no_dummy else ''} · {TAG}", "baseline_min": BASE_MIN,
        "baseline_mot": STEPS[0]["mot"], "repeat": 1, "shuffle": A.set == 1, "seed": SEEDS[(A.block, A.rep)], "rest": 0, "z": 4,
        "confirm": 0, "redo": 1, "again": 0, "static_spoil": False, "metrics": [], "plan_sha": SHA,
        "targets": W["1"] + W["2"] + D + SP, "steps": STEPS, **({"dummy": D, "dummy_tmax": 75} if D else {})}
P.validate(json.loads(json.dumps(PROG)))                  # ошибка программы — до запуска
per_run = SEG * (1 + ROUNDS * len(STATES)) + 25
runs = [s for s in STEPS if s.get("fseq")]
est = BASE_MIN * 60 + 35 + per_run * len(runs) + 50 * (len(STEPS) - len(runs))   # разворот — 20 с установления + 30
print(f"блок {A.block} «{BLOCKS[A.block]}», проход {A.rep}: опытов {len(runs)} по {per_run / 60:.1f} мин "
      f"({ROUNDS} круга × {len(STATES)} состояний поля по {SEG} с)"
      f"{', разворотов ' + str(len(STEPS) - len(runs)) if len(STEPS) > len(runs) else ''} · ≈ {est / 60:.0f} мин · {TAG}")
if A.dry:
    order = list(range(len(STEPS)))
    if PROG["shuffle"]:                            # набор 1: порядок перемешивает программа; набор 2 — как в списке
        random.Random(SEEDS[(A.block, A.rep)]).shuffle(order)
    for k, i in enumerate(order, 1):
        s = STEPS[i]
        print(f"  {k}. {s['name']}" + (": " + " · ".join(x["name"] for x in s["fseq"]["states"]) if s.get("fseq") else ""))
    sys.exit(0)
r = req("/api/program/start", {"program": PROG})
if not r.get("ok"):
    sys.exit(f"✗ не запустилась: {r.get('error')}")
pid, t0, hi, last, x = r["id"], time.time(), 0, None, {}
print(f"программа №{pid} запущена; разбор потом: python tools/factor_report.py {pid}", flush=True)
why = None
hist = []                                          # (ШИМ ‰ по модулю, об/мин, цель) по колёсам — раз в секунду, 15 с
try:
    while True:
        time.sleep(1)
        wh = req("/api/motors").get("wheels") or {}
        hist = (hist + [{w: (abs((wh.get(w) or {}).get("duty") or 0), abs((wh.get(w) or {}).get("meas") or 0),
                             (wh.get(w) or {}).get("target")) for w in ("a", "b")}])[-15:]
        # сторож ШИМ: выше предела 10 с из 15 (ШИМ скачет между прямой подачей и ПИ — «подряд» его не ловит)
        over = [w for w in ("a", "b") if sum(1 for h in hist if h[w][0] > A.guard) >= 10]
        # моторы перепутаны мостами: одно колесо на полном ШИМ отстаёт, другое при этом перегоняет цель
        swap = len(hist) == 15 and all(h["a"][2] and h["b"][2] and (
            (h["a"][0] > 800 and h["a"][1] < 0.8 * h["a"][2] and h["b"][1] > 1.1 * h["b"][2]) or
            (h["b"][0] > 800 and h["b"][1] < 0.8 * h["b"][2] and h["a"][1] > 1.1 * h["a"][2])) for h in hist[-10:])
        if over or swap:
            why = "моторы перепутаны мостами (swap)?" if swap else f"ШИМ > {A.guard} ‰ 10 с из 15"
            print(f"✗ сторож: {why} — стоп · " + " · ".join(
                f"К{'1' if w == 'a' else '2'} ШИМ {hist[-1][w][0]} об/мин {hist[-1][w][1]:.0f}" for w in ("a", "b")), flush=True)
            req("/api/program/stop", {"now": True})
            break
        if len(hist) % 5:
            continue
        x = req(f"/api/program?id={pid}")
        n = x.get("now") or {}
        k = len(x.get("runs") or []) + (1 if n else 0)
        if k != last:
            print(f"{time.time() - t0:5.0f} с · опыт {k}/{len(STEPS)} «{(n.get('name') or '')[:70]}» · ШИМ "
                  f"{[(wh.get(w) or {}).get('duty') for w in ('a', 'b')]}", flush=True)
            last = k
        if x.get("state") not in ("running", "starting", "paused"):
            break
finally:
    req("/api/motors", {"stop": True})
    req("/api/coils", {"off": True, "src": "конец блока"})
print(f"программа №{pid}: {x.get('state')} за {(time.time() - t0) / 60:.1f} мин; разбор: python tools/factor_report.py {pid}")
import subprocess                                  # контроль хода часов (по пакетам до и после молчания)
subprocess.run([sys.executable, os.path.join(ROOT, "tools", "clock_drift.py"), str(pid)])
if why or x.get("state") != "done":                # остановлен сторожем или вручную — следующий блок цепочки не запускать
    sys.exit(1)
