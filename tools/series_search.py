"""Серия поиска аномалий по полю вариантов: шесть семей, у каждой свои контроли (то же вращение и тот же способ, поле
выкл), чтобы отклонение статики или генерации сравнивалось с тем, что меняется только полем.

    python tools/series_search.py --dry          план, проверка программы, время — без запуска
    python tools/series_search.py                запустить на 8090 (все платы на связи), сторож ШИМ
    python tools/series_search.py --only F4,F1   только эти семьи;  --sec 30 — измерение опыта, с

Семьи (вращение — глазами наблюдателя: «навстречу» — колёса в разные стороны, нужна настройка установки mirror):
  F4  стоят · никто не считает (только статика): поле + −, + +, ∿ 1, ∿ 5, ∿ 17 Гц, ⊓ 1 Гц — поле на статику без радио
  F1  стоят · генерация двумя группами (ГСЧ + акс. + магн.): + −, + +, − −, ∿ 0,5 / 2 / 7 Гц, ⊓ 1 Гц, = 70 % ШИМ
  F6  стоят · своё рассеяние ◈ (каждый сам) против пустышки рассеяния — разведка, без контролей
  F2  навстречу k = 1 · К2 генерирует, К1 поглощает (смешать): + −, − +, + +, − −, ∿ 1, ⟳ ×1, ⟳ ×2, ∿ 5 Гц
  F3  навстречу k = 1 · петля ↺+: К2 — ГСЧ + кадры поглотителей К1 (фон 50/с; главный показатель — кандидаты): + −, ⟳ ×1, ∿ 1 Гц + +, − −
  F5  обороты: навстречу 30 об/мин, k = 1 −3 %, +3 % — поле выкл против + − (разведка, без контролей)
Колесо-пустышка (№22, №23) в каждом опыте работает тем же способом своей группой — опора. Статика меряет всё время
и опыт не портит (static_spoil false); эталон статики — записанный заранее (baseline_min 0).
"""
import argparse, json, os, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--sec", type=float, default=30, help="измерение опыта, с (статике нужно ≥ 20: два окна по 10 с)")
ap.add_argument("--only", default="", help="семьи через запятую: F4,F1,F6,F2,F3,F5")
ap.add_argument("--dry", action="store_true", help="только план и проверка программы, без запуска")
ap.add_argument("--guard", type=int, default=820, help="ШИМ, ‰: выше дольше 10 с — стоп всего")
A = ap.parse_args()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import program as P  # noqa: E402  проверка программы до запуска


def req(p, b=None):
    r = urllib.request.Request(A.url + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) in ("1", "2", "3")]
live = {a["mac"]: a for a in req("/api/dash")["agents"] if a.get("mac")}
W = {"1": [], "2": [], "3": []}
for b in sorted(fleet, key=lambda b: b["id"]):
    if (live.get(b["mac"]) or {}).get("status") == "online" or A.dry:
        W[str(b["wheel"])].append(b["mac"])
ID = {b["mac"]: b["id"] for b in fleet}
D = W["3"]
if not A.dry and (len(W["1"]) < 6 or len(W["2"]) < 6 or len(D) < 2):
    sys.exit(f"не все на связи: К1 {len(W['1'])}, К2 {len(W['2'])}, пустышка {len(D)}")
if not req("/api/rig").get("mirror"):
    print("◐ настройка установки mirror выкл: «навстречу» пойдёт по моторам, а не глазами наблюдателя")

CASCADE = {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}
COMMON = {"pl": 1, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "raw": 0}
FAST = {"src": 97}                                          # ГСЧ + акселерометр + магнитометр
ABSORB = {"sm": 1, "sh": 0}
LOOPP = {"src": 17, "sm": 1, "sh": 0, "bgr": 50}            # ↺+: без фона поглотителей петля не запустится; шум петли — кадры (каждый блок ждёт и их), фон 50/с даёт их больше


def g_cascade(noise=FAST):
    return [{"targets": W[w], "cfg": {**CASCADE, **COMMON, **noise}, "own": {m: {"role": 0} for m in W[w]},
             "label": f"{'колесо ' + w if w != '3' else 'колесо-пустышка'} · каскад"} for w in ("1", "2", "3") if W[w]]


def g_absorb(cfg):
    """К2 генерирует каскадом, К1 поглощает «все со всеми»; пустышка — пара: №23 генерирует, №22 поглощает."""
    own = {m: {"role": 0} for m in W["2"]}
    own.update({m: {"role": 1, "ba": [], "src": 1} for m in W["1"]})
    out = [{"targets": W["2"] + W["1"], "cfg": {**CASCADE, **COMMON, **cfg}, "own": own,
            "label": "К2 генерирует, К1 поглощает"}]
    if len(D) >= 2:
        g, a = D[1], D[0]
        out.append({"targets": [g, a], "cfg": {**CASCADE, **COMMON, **cfg},
                    "own": {g: {"role": 0}, a: {"role": 1, "ba": [], "src": 1}},
                    "label": f"колесо-пустышка · №{ID[g]} генерирует, №{ID[a]} поглощает"})
    return out


def g_idle():
    m = W["1"] + W["2"] + D
    return [{"targets": m, "cfg": {"mode": 0, "dc": 0, "run": 0}, "own": {}, "label": "никто не считает — только статика"}]


def g_self(sham):
    m = W["1"] + W["2"] + D
    return [{"targets": m, "cfg": {"mode": 0, "dc": 0, **COMMON, "src": 1, "ss": 1, "sh": 1 if sham else 0},
             "own": {x: {"role": 0} for x in m}, "label": f"каждый сам · ◈ {'пустышка' if sham else 'сам рассеивает'}"}]


OFF = {"pair": "opposite", "mode": "off", "dir": 1}
ZG = lambda zg=0: {"pair": "opposite", "mode": "zerog", "dir": 1, "zg": zg}
RPM = lambda r: {"pair": "opposite", "mode": "rpm", "dir": 1, "rpm": r}
F = lambda a, b, ac=None, lvl=None: {"1": a, "2": b, **({"ac": ac} if ac else {}), **({"level": lvl} if lvl else {})}
AC = lambda f, sh="sin": {"f": f, "shape": sh, **({"edge": 100} if sh == "sq" else {})}
SYNC = lambda k: {"f": 1, "shape": "sin", "sync": {"k": k, "deg": 0}}
FOFF = F("выкл", "выкл")
STEPS, SETTLE = [], {"field": 6, "mode": 8, "spin": 25, "speed": 15}


def step(fam, name, groups, mot, coil, settle, ctrl=None, control=False, rot="■■ стоят"):
    STEPS.append({"name": name[:100], "minutes": round(A.sec / 60, 3), "control": control,
                  **({"control_for": ctrl} if ctrl else {}), "groups": groups, "settle_s": settle, "mot": mot,
                  "coil": coil, "layers": {"rot": rot, "field": {"1": coil["1"], "2": coil["2"]}, "family": fam}})


def family(fam, ctrl_name, groups, mot, variants, first_settle, rot):
    """Контроль ×4 вперемешку с опытами: К, о, о, К, о, о, К, о, о, К (сколько опытов есть)."""
    n_c = 4
    per = [len(variants) // (n_c - 1) + (1 if i < len(variants) % (n_c - 1) else 0) for i in range(n_c - 1)] + [0]
    vs = list(variants)
    for k in range(n_c):
        step(fam, ctrl_name, groups(), mot, FOFF, first_settle if k == 0 else SETTLE["field"], control=True, rot=rot)
        for _ in range(per[k]):
            if vs:
                nm, coil = vs.pop(0)
                step(fam, f"{ctrl_name.split(':', 1)[1].split('· поле')[0].strip()} · поле {nm}", groups(), mot, coil,
                     SETTLE["field"], ctrl=ctrl_name, rot=rot)


NA = "⟳⟲ навстречу · невесомость"
want = [f.strip() for f in A.only.split(",") if f.strip()] or ["F4", "F1", "F6", "F2", "F3", "F5"]
for fam in want:
    if fam == "F4":
        family("F4", "контроль F4: стоят · никто не считает · поле выкл", g_idle, OFF,
               [("+ −", F("+", "−")), ("+ +", F("+", "+")), ("∿ 1 Гц + −", F("+", "−", AC(1))),
                ("∿ 5 Гц + −", F("+", "−", AC(5))), ("∿ 17 Гц + −", F("+", "−", AC(17))),
                ("⊓ 1 Гц + −", F("+", "−", AC(1, "sq")))], SETTLE["mode"], "■■ стоят")
    elif fam == "F1":
        family("F1", "контроль F1: стоят · генерация двумя группами · поле выкл", g_cascade, OFF,
               [("+ −", F("+", "−")), ("+ +", F("+", "+")), ("− −", F("−", "−")), ("∿ 0,5 Гц + −", F("+", "−", AC(0.5))),
                ("∿ 2 Гц + −", F("+", "−", AC(2))), ("∿ 7 Гц + −", F("+", "−", AC(7))),
                ("⊓ 1 Гц + −", F("+", "−", AC(1, "sq"))), ("= 70 % ШИМ + −", F("+", "−", lvl=700))],
               SETTLE["mode"], "■■ стоят")
    elif fam == "F6":                                       # разведка: без контролей
        step("F6", "стоят · каждый сам · ◈ сам рассеивает · поле выкл", g_self(False), OFF, FOFF, SETTLE["mode"])
        step("F6", "стоят · каждый сам · ◈ пустышка рассеяния · поле выкл", g_self(True), OFF, FOFF, SETTLE["mode"])
    elif fam == "F2":
        family("F2", f"контроль F2: {NA} · К2 → К1 поглощение · поле выкл", lambda: g_absorb(FAST | ABSORB), ZG(),
               [("+ −", F("+", "−")), ("− +", F("−", "+")), ("+ +", F("+", "+")), ("− −", F("−", "−")),
                ("∿ 1 Гц + −", F("+", "−", AC(1))), ("⟳ ×1 + −", F("+", "−", SYNC(1))),
                ("⟳ ×2 + −", F("+", "−", SYNC(2))), ("∿ 5 Гц + −", F("+", "−", AC(5)))], SETTLE["spin"], NA)
    elif fam == "F3":
        family("F3", f"контроль F3: {NA} · петля ↺+ К2 ← К1 · поле выкл", lambda: g_absorb(LOOPP), ZG(),
               [("+ −", F("+", "−")), ("⟳ ×1 + −", F("+", "−", SYNC(1))), ("∿ 1 Гц + +", F("+", "+", AC(1))),
                ("− −", F("−", "−"))], SETTLE["mode"], NA)
    elif fam == "F5":                                       # разведка оборотов: пары «поле выкл / + −»
        for nm, mot in (("навстречу 30 об/мин", RPM(30)), ("навстречу k = 1 −3 %", ZG(-3)), ("навстречу k = 1 +3 %", ZG(3))):
            step("F5", f"{nm} · генерация двумя группами · поле выкл", g_cascade(), mot, FOFF, SETTLE["speed"], rot=nm)
            step("F5", f"{nm} · генерация двумя группами · поле + −", g_cascade(), mot, F("+", "−"), SETTLE["field"], rot=nm)
# имена опытов — разные (контроль семьи — одно имя на все её контроли)
seen = {}
for s in STEPS:
    if not s["control"]:
        seen[s["name"]] = seen.get(s["name"], 0) + 1
        if seen[s["name"]] > 1:
            s["name"] = f"{s['name'][:94]} · №{seen[s['name']]}"
PROG = {"name": f"серия поиска: {', '.join(want)} · {len(STEPS)} опытов по {A.sec:g} с", "baseline_min": 0, "repeat": 1,
        "shuffle": False, "rest": 0, "z": 4, "confirm": 0, "redo": 0, "again": 0, "static_spoil": False, "metrics": [],
        "targets": W["1"] + W["2"] + D, "steps": STEPS, **({"dummy": D, "dummy_tmax": 75} if D else {})}
P.validate(json.loads(json.dumps(PROG)))                  # ошибка программы — до запуска
est = sum(s["settle_s"] + A.sec + (0 if s["groups"] and s["groups"][0]["cfg"].get("run") == 0 else 10) + 6 for s in STEPS)
fams = {}
for s in STEPS:
    f = s["layers"]["family"]
    fams.setdefault(f, [0, 0])[0 if s["control"] else 1] += 1
print(f"опытов {len(STEPS)}: " + " · ".join(f"{f} — контролей {c}, опытов {e}" for f, (c, e) in fams.items())
      + f" · ≈ {est / 60:.0f} мин")
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "series"), exist_ok=True)
plan_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "series", "series_search.json")
json.dump(PROG, open(plan_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
if A.dry:
    for i, s in enumerate(STEPS, 1):
        print(f"  {i:2d}. [{s['layers']['family']}] {'К ' if s['control'] else '  '}{s['name']}")
    sys.exit(0)
r = req("/api/program/start", {"program": PROG})
if not r.get("ok"):
    sys.exit(f"✗ не запустилась: {r.get('error')}")
pid, t0, hi, last = r["id"], time.time(), 0, None
print(f"программа №{pid} запущена; разбор потом: python tools/series_report.py {pid}", flush=True)
try:
    while True:
        time.sleep(5)
        x = req(f"/api/program?id={pid}")
        wh = req("/api/motors").get("wheels") or {}
        duty = max(abs((wh.get(w) or {}).get("duty") or 0) for w in ("a", "b"))
        hi = hi + 1 if duty > A.guard else 0
        if hi >= 2:
            print(f"✗ сторож: ШИМ {duty} ‰ > {A.guard} дольше 10 с — стоп", flush=True)
            req("/api/program/stop", {"now": True})
            break
        n = x.get("now") or {}
        k = len(x.get("runs") or []) + (1 if n else 0)
        if k != last:
            print(f"{time.time() - t0:5.0f} с · опыт {k}/{len(STEPS)} «{(n.get('name') or '')[:70]}» · ШИМ "
                  f"{[(wh.get(w) or {}).get('duty') for w in ('a', 'b')]}", flush=True)
            last = k
        if x.get("state") not in ("running", "starting"):
            break
finally:
    req("/api/motors", {"stop": True})
    req("/api/coils", {"off": True, "src": "конец серии"})
print(f"программа №{pid}: {x.get('state')} за {(time.time() - t0) / 60:.1f} мин; разбор: python tools/series_report.py {pid}")
