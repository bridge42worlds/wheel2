"""Этапный опыт (08.11): одна неизменная сцена, меняются только наблюдение (этапы) и источник шума (блоки).

Сцена весь опыт: «Г», зеркально; колёса навстречу (к1 вперёд, к2 назад) на постоянном ШИМ невесомости, обороты
качаются синусом ±5 % с периодом 20 с у обоих колёс в фазе (каждая плата обода дважды за период проходит свою
невесомость); поле постоянное К1 «+» / К2 «−»; все платы колёс и пустышки «каждая сама», 64 бит, два проверяльщика,
без поглощения. Источник шума меняется у всех плат одновременно командой NS (без нового номера, без ответа в эфир):
пятёрка регистр · ГСЧ · код · дрожание · АЦП — по одному, «все пять» и «все, кроме одного» (11 блоков по 160 с).

    python tools/stage_run.py --dry                     план и проверка программы
    python tools/stage_run.py                           подготовка (код в платы, обороты, качание) и опыт
    python tools/stage_run.py --test --ids 22,23        сжатая проверка на пустышках: без колёс и поля, блоки 10 с

Этапы: 0 эталон (поле выкл, на связи) · A набор (поле, на связи) · B тишина (молчат, статика меряет) ·
C без наблюдения (молчат, статика не читает и не передаёт) · D возврат (молчат, статика меряет) · E закрытие
(поле выкл, на связи). Шаг 1 = 0+A+B, шаг 2 = C+D+E: таблицы первой половины сдаются до возможной замены
аккумуляторов (перед C). Порядок блоков: A — по порядку, B — в обратном (у статики каждое сочетание в среднем в
середине этапа), C — вперемешку (зерно пишется).
Разбор: python tools/stage_report.py <номер программы>
"""
import argparse, json, math, os, random, statistics as S, subprocess, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--level", type=int, default=650, help="уровень постоянного поля, ‰ (650 — хватает на ~2 ч)")
ap.add_argument("--amp", type=float, default=0.05, help="размах качания оборотов (доля): 0,05 — ±5 %")
ap.add_argument("--period", type=float, default=20.0, help="период качания, с")
ap.add_argument("--block", type=int, default=160, help="блок одного источника, с (кратно периоду)")
ap.add_argument("--seg", type=int, default=80, help="отрезок таблицы платы, с (делит блок)")
ap.add_argument("--seed", type=int, default=None, help="зерно порядка в этапе C")
ap.add_argument("--info", default=os.path.join(ROOT, "data", "info", "code.txt"))
ap.add_argument("--no-feed", action="store_true", help="код уже в платах — не подавать заново")
ap.add_argument("--ids", default=None, help="только эти генераторы (номера через запятую) — для проверки")
ap.add_argument("--test", action="store_true", help="сжатая проверка: без колёс и поля, блоки 10 с, отрезки 5 с")
ap.add_argument("--dry", action="store_true")
ap.add_argument("--dummy", action="store_true", help="брать и пустышки (колесо 3); по умолчанию без них")
A = ap.parse_args()
if A.test:
    A.block, A.seg = 10, 5
if A.block % A.seg or (not A.test and A.block % int(A.period)):
    sys.exit("блок должен делиться на отрезок и на период качания")
PER = A.block // A.seg                                   # отрезков в блоке


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=30))
    except urllib.error.HTTPError as e:
        return json.load(e)


def beep(n):
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ";".join(["[console]::beep(880,300); Start-Sleep -m 200"] * n)],
                       timeout=15, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired")]
ID = {b["mac"]: b["id"] for b in fleet}
pick = {int(x) for x in A.ids.split(",")} if A.ids else None
gen_ok = lambda b: b.get("kind") in ("A", "B") and b.get("slot") != "coil" and (pick is None or b["id"] in pick)
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if gen_ok(b) and str(b.get("wheel")) == w] for w in ("1", "2", "3")}
GENS, DUMMY = W["1"] + W["2"], W["3"]
if not (A.dummy or pick):
    DUMMY = []                                           # 08.11: пустышки исключены (решение пользователя)
SP = [b["mac"] for b in fleet if b.get("kind") == "S" or b.get("role") == 2 or b.get("id") == 8]
SP = [m for m in SP if any(b["mac"] == m and b.get("online") for b in fleet)]
online = {b["mac"] for b in fleet if b.get("online")}
lost = [ID[m] for m in GENS + DUMMY if m not in online]
print(f"генераторы: колесо 1 — {len(W['1'])}, колесо 2 — {len(W['2'])}, пустышки — {len(DUMMY)}; статика: "
      f"{' '.join('№' + str(ID[m]) for m in SP) or 'нет'}" + (f"; не на связи: {lost}" if lost else ""))
if lost and not A.dry:
    sys.exit("не все генераторы на связи — опыт не начинаю")

# ---------- источники и порядок ----------
FIVE = [("регистр", 128), ("ГСЧ", 1), ("код", 16), ("дрожание", 8), ("АЦП", 4)]
ALL = sum(v for _, v in FIVE)
COMBOS = FIVE + [("все пять", ALL)] + [(f"все кроме: {k}", ALL ^ v) for k, v in FIVE]
seed = A.seed if A.seed is not None else int(time.time()) % 100000
shuffled = COMBOS[:]
random.Random(seed).shuffle(shuffled)
FIELD = {"1": "+", "2": "-", "level": A.level} if not A.test else {"1": "выкл", "2": "выкл"}
OFF = {"1": "выкл", "2": "выкл"}
#               этап  блоки                                 поле   tl  sx  stl
STAGES1 = [("0", [("все пять", ALL)] * 2, OFF, 0, 0, 0),
           ("A", COMBOS, FIELD, 0, 0, 0),
           ("B", COMBOS[::-1], FIELD, 2, 0, 0)]
STAGES2 = [("C", shuffled, FIELD, 2, 1, 2),
           ("D", [("все пять", ALL)] * 4, FIELD, 2, 0, 0),
           ("E", [("все пять", ALL)] * 2, OFF, 0, 0, 0)]
WORD = {"0": "эталон", "A": "набор", "B": "тишина", "C": "без наблюдения", "D": "возврат", "E": "закрытие"}


def states(stages):
    out = []
    for name, blocks, coil, tl, sx, stl in stages:
        first = True
        for bi, (cname, mask) in enumerate(blocks):
            for j in range(PER):
                st = {"name": f"{name} · {cname}"[:40], "coil": coil,
                      "set": {"stage": name, "combo": cname, "ns": str(mask), "block": str(bi + 1),
                              "form": "постоянное" if coil.get("1") != "выкл" else "выкл",
                              "k1": "1" if coil.get("1") != "выкл" else "0", "k2": "1" if coil.get("2") != "выкл" else "0",
                              "obs": {"0": "на связи", "A": "на связи", "B": "молчат", "C": "никто", "D": "молчат", "E": "на связи"}[name]}}
                if j == 0:
                    st["ns"] = mask                       # источник — в начале каждого блока
                if first:                                 # телеметрия и статика — в начале этапа
                    st.update(tl=tl, sx=sx, stl=stl)
                    first = False
                out.append(st)
    return out


gen_cfg = {"mode": 0, "dc": 1, "pl": 0, "ro": 0, "bits": 64, "td": 1000, "sq": 0, "tc": 1, "tmax": 73, "cpu": 240, "hb": 10,
           "ping": 0, "cf": 0, "det": 0, "ss": 0, "src": ALL, "raw": 1, "xf": 4, "rw": 4, "rt": 0}
gen_own = lambda macs: {m: {"role": 0, "det": 0, "ss": 0, "xf": 4, "rw": 4, "src": ALL, "raw": 1, "bits": 64} for m in macs}


def groups(sx, stl):
    g = []
    if GENS:
        g.append({"targets": GENS, "cfg": gen_cfg, "own": gen_own(GENS), "label": "колёса: каждая сама, 64 бит"})
    if DUMMY:
        g.append({"targets": DUMMY, "cfg": gen_cfg, "own": gen_own(DUMMY), "label": "пустышки: каждая сама, 64 бит"})
    if SP:
        g.append({"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "sx": sx, "tl": stl},
                  "own": {m: {"role": 2, "cpu": 80, "sg": 0, "sx": sx, "tl": stl} for m in SP},
                  "label": "◆ статика не мерит" if sx else "◆ статика меряет"})
    return g


scene = (f"«Г» зеркально · навстречу, качание ±{A.amp * 100:g} % / {A.period:g} с · поле постоянное К1+ К2− {A.level} ‰"
         if not A.test else "проверка на пустышках: без колёс и поля")
s1, s2 = states(STAGES1), states(STAGES2)
step1 = {"name": "этапы 0 · A · B: эталон, набор, тишина", "minutes": A.seg * len(s1) / 60, "control": False, "agent": True,
         "fseq": {"seg": A.seg, "tab": True, "states": s1}, "groups": groups(0, 0), "mot": None, "coil": OFF,
         "keep_field": True, "quiet": True, "quiet_at": "never", "rest": False,
         "layers": {"rot": "⟳⟲ навстречу · качание", "family": "этапы", "noise": "11 сочетаний пятёрки", "block": "0 · A · B"},
         "set": {"block": "этапы", "scene": scene[:60], "seed": str(seed)}}
step2 = {"name": "этапы C · D · E: без наблюдения, возврат, закрытие", "minutes": A.seg * len(s2) / 60, "control": False,
         "agent": True, "fseq": {"seg": A.seg, "tab": True, "states": s2}, "groups": groups(1, 2), "mot": None,
         "coil": FIELD, "keep_field": False, "quiet": True, "quiet_at": "start", "rest": False,
         "layers": {"rot": "⟳⟲ навстречу · качание", "family": "этапы", "noise": "11 сочетаний пятёрки", "block": "C · D · E"},
         "set": {"block": "этапы", "scene": scene[:60], "seed": str(seed)}}
prog = {"name": f"этапы наблюдения · {scene}", "baseline_min": 0, "repeat": 1, "shuffle": False, "seed": 1, "rest": 0,
        "z": 4, "confirm": 0, "redo": 0, "again": 0, "static_spoil": False, "pause_lost": True, "metrics": [],
        "dummy": DUMMY, "targets": GENS + DUMMY + SP, "steps": [step1, step2]}
total = A.seg * (len(s1) + len(s2) + 2) / 60
print(f"{prog['name']}\nшаг 1: {len(s1)} отрезков по {A.seg} с · шаг 2: {len(s2)} · всего ≈ {total:.0f} мин + сдача итогов · "
      f"порядок C (зерно {seed}): {', '.join(c for c, _ in shuffled)}")
if A.dry:
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import program as P  # noqa: E402
    P.validate(json.loads(json.dumps(prog)))
    print("✓ программа проверена")
    sys.exit(0)

# ---------- подготовка ----------
if not A.no_feed:
    print("код → платы …", flush=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "info_feed.py"), A.info, "--kb", "256"]
                   + ["--ids", ",".join(str(ID[m]) for m in GENS + DUMMY)], check=False)
else:
    for m in GENS + DUMMY:
        req("/api/cmd", {"mac": m, "line": "INFO on"})
mot = {}
if not A.test:
    print("обороты: невесомость по ободу …", flush=True)
    req("/api/coils", {"off": True, "src": "этапы: подготовка"})
    req("/api/motors", {"mod": None})
    req("/api/motors", {"pair": "free"})
    req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": 1, "flip": 0, "zg": 0})
    req("/api/motors", {"wheel": "b", "mode": "zerog", "dir": -1, "flip": 0, "zg": 0})
    t0, duty = time.time(), {"a": [], "b": []}
    while time.time() - t0 < 120:
        time.sleep(1)
        w = req("/api/motors")["wheels"]
        if max(abs(w[k]["duty"]) for k in "ab") > 800:
            req("/api/motors", {"stop": True})
            sys.exit("✗ ШИМ > 800 ‰ при разгоне — стоп")
        if time.time() - t0 > 60:
            for k in "ab":
                if w[k].get("fb"):
                    duty[k].append(abs(w[k]["duty"]))
    if min(len(duty["a"]), len(duty["b"])) < 10:
        req("/api/motors", {"stop": True})
        sys.exit("✗ регулятор не вышел на невесомость (обод молчит?) — стоп")
    base = {k: int(S.median(duty[k])) for k in "ab"}
    print(f"  ШИМ невесомости: колесо 1 — {base['a']} ‰, колесо 2 — {base['b']} ‰", flush=True)
    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": base["a"]})
    req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": base["b"]})
    amp, t_mod = A.amp, time.time()
    for it in range(2):                                  # размах ШИМ под размах оборотов ±amp — по ободу, 3 периода
        req("/api/motors", {"mod": {"a": amp, "T": A.period, "t0": t_mod}})
        time.sleep(A.period)                             # период на установление
        pts = {"a": [], "b": []}
        t1 = time.time()
        while time.time() - t1 < 3 * A.period:
            time.sleep(0.5)
            w = req("/api/motors")["wheels"]
            for k in "ab":
                if w[k].get("meas"):
                    pts[k].append((time.time(), abs(w[k]["meas"])))
        rel = []
        for k in "ab":
            p = pts[k]
            if len(p) < 20:
                continue
            m = S.mean(v for _, v in p)
            ph = [2 * math.pi * (t - t_mod) / A.period for t, _ in p]
            c = 2 * S.mean(v * math.sin(f) for (_, v), f in zip(p, ph))
            d = 2 * S.mean(v * math.cos(f) for (_, v), f in zip(p, ph))
            rel.append(math.hypot(c, d) / m)
            print(f"  колесо {'1' if k == 'a' else '2'}: {m:.1f} об/мин, качание ±{100 * math.hypot(c, d) / m:.1f} % "
                  f"(отставание {math.degrees(math.atan2(-d, c)):.0f}°)", flush=True)
        if not rel:
            print("  ◐ обод молчит — размах оставляю как задан", flush=True)
            break
        got = S.mean(rel)
        if abs(got - A.amp) < 0.005:
            break
        amp = min(0.09, max(0.02, amp * A.amp / max(got, 1e-3)))
        print(f"  размах ШИМ → ±{amp * 100:.1f} %", flush=True)
    mot = {"base": base, "amp": amp, "t0": t_mod}
    print(f"  качание: ШИМ ±{amp * 100:.1f} %, период {A.period:g} с, обе в фазе", flush=True)

# ---------- опыт ----------
prog["set_mot"] = mot
r = req("/api/program/start", {"program": prog})
if not r.get("ok"):
    if not A.test:
        req("/api/motors", {"mod": None}); req("/api/motors", {"stop": True})
    sys.exit(f"✗ программа не запустилась: {r.get('error')}")
pid = r["id"]
print(f"программа №{pid} запущена · разбор: python tools/stage_report.py {pid}", flush=True)
log_path = os.path.join(ROOT, "data", "series", f"stage_{time.strftime('%Y-%m-%d')}.json")
log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
log.append({"pid": pid, "t": time.time(), "seed": seed, "mot": mot, "level": A.level, "amp": A.amp, "period": A.period,
            "block": A.block, "seg": A.seg, "combos": COMBOS, "order_C": shuffled, "test": A.test})
json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
paused, warned, last = False, False, ""
try:
    while True:
        time.sleep(5)
        x = req(f"/api/program?id={pid}")
        state, now = x.get("state"), x.get("now") or {}
        if state not in ("running", None):
            print(f"программа №{pid}: {state}", flush=True)
            break
        line = f"шаг {now.get('n')}/{now.get('of')} · {now.get('phase')}" + (" · ПАУЗА" if now.get("pause") else "")
        if line != last:
            print(time.strftime("%H:%M:%S"), line, flush=True)
            last = line
        if now.get("pause") and not paused:
            paused = True
            beep(2)
            print("  ⏸ пауза: замена аккумуляторов? После неё — «продолжить» (POST /api/program/continue)", flush=True)
        if paused and not now.get("pause") and mot:      # после паузы — колёса снова (программа их остановила)
            paused = False
            req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": mot["base"]["a"]})
            req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": mot["base"]["b"]})
            req("/api/motors", {"mod": {"a": mot["amp"], "T": A.period, "t0": mot["t0"]}})
            print("  ▶ колёса снова: ШИМ и качание как были", flush=True)
        # за 2 мин до конца шага 1 — аккумуляторы катушек: мало — пауза перед этапом C (замена)
        if now.get("n") == 1 and now.get("phase") == "run" and not warned and now.get("ph_t") and now.get("ph_dur"):
            left = now["ph_t"] + now["ph_dur"] - time.time()
            if left < 120:
                warned = True
                bat = [b for b in (req("/api/battery").get("list") or []) if b.get("kind") == "coil" and b.get("alive")]
                low = [b["label"] for b in bat if (b.get("soc") or 0) < 40]
                if low and not A.test:
                    req("/api/program/pause", {"why": f"перед этапом C: {', '.join(low)} — заменить"})
                    print(f"  ◐ {', '.join(low)} < 40 % — пауза перед этапом C", flush=True)
finally:
    if mot:
        req("/api/motors", {"mod": None})
        req("/api/motors", {"stop": True})
        req("/api/motors", {"pair": "free"})
    req("/api/coils", {"off": True, "src": "этапы: конец"})
    print("колёса и поле выключены", flush=True)
