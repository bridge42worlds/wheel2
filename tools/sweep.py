"""Пробежка по всему спектру (предложение пользователя 06.11): ячейки по 5 с с быстрой сменой поля, режим счёта
меняется раз в ~20 с, вращение — раз в несколько минут; каждая ячейка сравнивается со скользящей опорой «поле выкл»
в том же состоянии колёс и счёта. Дёрнулось (|z| > Z1 у статики №8 или у выхода/отсева/сырья счёта) — сразу повтор
той же ячейки трижды; подтвердилось (≥ 2 из 3 с |z| > Z2) — удержание 30 с × 4 (ABAB с выкл); подтвердилось и там —
кандидат: три пика, запись в журнал.

    python tools/sweep.py [--hours 3] [--cell 5] [--seed N] [--dry]

Журнал: data/series/sweep_<дата>.json (каждая ячейка: состояние, показатели, z, что решили).
Сторожа: ШИМ > 820 ‰ — стоп всего; плат на связи меньше 12 — пауза; аккумулятор катушки ≤ 25 % — два пика и стоп поля.
"""
import argparse, json, math, os, random, statistics as S, subprocess, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P        # noqa: E402  raw_stats, yield_z
import nullmodel           # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=float, default=3)
ap.add_argument("--cell", type=float, default=5)
ap.add_argument("--gen-every", type=float, default=75, help="смена режима счёта, с")
ap.add_argument("--rot-every", type=float, default=600, help="смена вращения, с")
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--z1", type=float, default=3.5)
ap.add_argument("--z2", type=float, default=2.5)
ap.add_argument("--info", default=None, help="файл «информации» (код, музыка): положить в платы перед пробежкой; режимы imix (код XOR регистр) и ipure (чистый код)")
ap.add_argument("--no-feed", action="store_true", help="файл уже лежит в платах — не подавать заново")
ap.add_argument("--rots", default=None, help="порядок вращений через запятую (например same,opposite,only1,stop); по умолчанию случайный")
ap.add_argument("--dry", action="store_true")
A = ap.parse_args()
Z1, Z2 = A.z1, A.z2


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        out = json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        out = json.load(e)
    if b is not None and isinstance(out, dict) and out.get("ok") is False:
        print(f"  ✗ {p}: {out.get('error')}", flush=True)
    return out


def beep(n):
    if A.dry:
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ";".join(["[console]::beep(880,300); Start-Sleep -m 200"] * n)],
                       timeout=15, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


fleet = req("/api/fleet")["agents"]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) == w] for w in ("1", "2")}
D3 = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
      and str(b.get("wheel")) == "3"]
ALL = W["1"] + W["2"] + D3
ID = {b["mac"]: b["id"] for b in fleet}
N8 = next((b["mac"] for b in fleet if b.get("id") == 8), None)
BASE = {"src": 128, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 1}
STATIC_KEYS = ("sd_v", "sd_h", "p4_v", "p4_h", "kurt_v", "kurt_h")


# ---------- слои ----------
def gen(kind):
    """Режим счёта: off · solo64 · solo256 · cascade · absorb (К1 считает, К2 поглощает) · absorb2 · circle (все рассеивают,
    шум ГСЧ+эфир) · dummy_gen (считают пустышки, колёса поглощают)."""
    own0 = {m: {"role": 0, "ss": 0} for m in ALL}
    if A.info:                                          # информация включена только в своих режимах, иначе эфир B как обычно
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": "INFO on" if kind in ("imix", "ipure") else "INFO off"})
    if kind == "off":
        req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": own0}); return
    bits = 256 if kind == "solo256" else 64
    cfg = {**BASE, "bits": bits, "run": 1}
    own = {m: {"role": 0, "ss": 0, "src": 128, "raw": 1, "bits": bits} for m in ALL}
    if kind in ("solo64", "solo256"):
        req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
    elif kind == "cascade":
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 1, "gv": 2, "rk": 1, "pw": 50}, "own": own})
    elif kind in ("absorb", "absorb2"):
        for m in (W["2"] if kind == "absorb" else W["1"]):
            own[m] = {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0}
        req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
    elif kind == "circle":
        own = {m: {"role": 0, "ss": 1, "src": 128 | 16, "raw": 1, "bits": 64} for m in ALL}
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "ss": 1, "src": 128 | 16}, "own": own})
    elif kind == "dummy_gen":
        for m in W["1"] + W["2"]:
            own[m] = {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0}
        req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
    elif kind in ("imix", "ipure"):                   # «информация» в шум: блок файла по кругу вместо эфира B (INFO on)
        src = 128 | 16 if kind == "imix" else 16
        own = {m: {"role": 0, "ss": 0, "src": src, "raw": 1, "bits": 64} for m in ALL}
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "src": src}, "own": own})
    elif kind.startswith("src"):                       # один источник шума; CSI — с пингом роутера; датчики — через SHA
        src = int(kind[3:]); raw = 1 if src in (1, 8) else 0
        c2 = {**cfg, "src": src, "raw": raw, "ping": 100 if src == 2 else 0, "cf": 1 if src == 2 else 0}
        own = {m: {"role": 0, "ss": 0, "src": src, "raw": raw, "bits": 64} for m in ALL}
        req("/api/control", {"targets": ALL, "cfg": c2, "own": own})


def mot(kind):
    """Вращение: opposite · same · only1 · stop (невесомость датчика, ~73 об/мин)."""
    if kind == "stop":
        req("/api/motors", {"stop": True}); return
    req("/api/motors", {"pair": {"opposite": "opposite", "same": "same", "only1": "only1"}[kind]})
    req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": 1, "flip": 0, "zg": 0})


def coil(c):
    """Поле: {"k1","k2": "выкл"|"+"|"-", "f": Гц|None (постоянное)|"rot", "shape": sin|sq, "level", "deg": фаза к вращению}."""
    if c["k1"] == "выкл" and c["k2"] == "выкл":
        req("/api/coils", {"off": True, "src": "пробежка"}); return
    for w in ("1", "2"):
        st = c[f"k{w}"]
        if st == "выкл":
            req("/api/coils", {"wheel": w, "state": "off", "src": "пробежка"}); continue
        ac = None
        if c.get("f") == "rot":
            ac = {"f": 1.2, "shape": c.get("shape", "sin"), "sync": {"k": 1, "deg": c.get("deg", 0)}}
        elif c.get("f"):
            ac = {"f": c["f"], "shape": c.get("shape", "sin")}
        req("/api/coils", {"wheel": w, "state": st, "level": c.get("level", 1000), "ac": ac, "src": "пробежка"})


OFF = {"k1": "выкл", "k2": "выкл"}
FIELDS = []
for who in (("+", "выкл"), ("выкл", "+"), ("+", "+"), ("+", "-")):
    k1, k2 = who
    FIELDS.append({"k1": k1, "k2": k2, "f": None, "level": 1000})                      # постоянное
    FIELDS.append({"k1": k1, "k2": k2, "f": None, "level": 300})
    for f in (0.3, 1.2, 5, 20, 50):
        FIELDS.append({"k1": k1, "k2": k2, "f": f, "shape": "sin", "level": 1000})
    for f in (1.2, 20):
        FIELDS.append({"k1": k1, "k2": k2, "f": f, "shape": "sq", "level": 1000})
    for deg in (0, 90, 180, 270):
        FIELDS.append({"k1": k1, "k2": k2, "f": "rot", "shape": "sin", "level": 1000, "deg": deg})
GENS = ["solo64", "solo256", "cascade", "absorb", "absorb2", "circle", "dummy_gen", "off",
        "src1", "src8", "src2", "src32", "src64",      # источники шума: ГСЧ, дрожание, CSI, акселерометр, магнитометр
        "imix", "ipure"]                               # «информация» (08.11): код XOR регистр; чистый код (только с --info)
ROTS = ["opposite", "same", "only1", "stop"]


def fname(c):
    if c["k1"] == "выкл" and c["k2"] == "выкл":
        return "выкл"
    w = "К1" if c["k2"] == "выкл" else "К2" if c["k1"] == "выкл" else ("оба" if c["k2"] == "+" else "оба ±")
    f = "=" if c["f"] is None else ("такт %d°" % c.get("deg", 0) if c["f"] == "rot" else f"{'∿' if c.get('shape') == 'sin' else '⊓'} {c['f']}")
    return f"{w} {f}" + (f" {c['level']}" if c.get("level", 1000) != 1000 else "")


# ---------- измерение ячейки ----------
def gens():
    out = {}
    for a in req("/api/dash")["agents"]:
        g = a.get("values") or {}
        if a.get("mac") in ID and g.get("run") and g.get("bits") and not g.get("role"):
            out[a["mac"]] = g
    return out


def gen_z(g0, g1):
    zy, zt, zq = [], [], []
    pr = cand = 0
    for m, g in g1.items():
        q = g0.get(m)
        if not q or q.get("cid") != g.get("cid") or (g.get("cand") or 0) <= (q.get("cand") or 0):
            continue
        dc, dp, dtd = g["cand"] - q["cand"], (g.get("pr") or 0) - (q.get("pr") or 0), (g.get("tdr") or 0) - (q.get("tdr") or 0)
        cand += dc; pr += dp
        try:
            y = nullmodel.yield_z(dc, dp, dtd, g["bits"], g.get("td") or 0)
            if isinstance(y.get("z_yield"), (int, float)): zy.append(y["z_yield"])
            if isinstance(y.get("z_td"), (int, float)): zt.append(y["z_td"])
        except Exception:  # noqa: BLE001
            pass
        for k, (qq, ss) in P.raw_stats(g, q).items():   # сырьё: доля единиц младшего бита — z по биномиальному
            e = g.get("e") or []; e0 = q.get("e") or []
            i = P.SRC_KEYS.index(k) if k in P.SRC_KEYS else -1
            if 0 <= i < len(e) and i < len(e0):
                n = e[i][4] - e0[i][4]
                if n > 0:
                    zq.append((qq - 0.5) / math.sqrt(0.25 / n))
    agg = lambda v: sum(v) / math.sqrt(len(v)) if v else None
    return {"pr": pr, "cand": cand, "zy": agg(zy), "zt": agg(zt), "zraw": agg(zq), "n": len(zy)}


def guard():
    wh = req("/api/motors").get("wheels") or {}
    duty = max(abs((wh.get(w) or {}).get("duty") or 0) for w in ("a", "b"))
    on = sum(1 for a in req("/api/dash")["agents"] if a.get("mac") in ID and a.get("status") in ("online", "quiet", "late"))
    bat = [b for b in (req("/api/battery").get("list") or []) if b.get("kind") == "coil" and b.get("alive")]
    # две подряд низкие точки: мгновенный провал под током (07.11, 7,74 В показалось < 7,0) — не повод
    low_now = {b["label"] for b in bat if b.get("soc", 100) <= 25 or (b.get("v") or 9) < 6.9}
    low = sorted(low_now & LOW_PREV["s"]); LOW_PREV["s"] = low_now
    return duty, on, low


BG = {"1": None, "2": None}                 # фон |B| магнитометра колеса при выключенной катушке (скользящий)
POSE = {"deg": None, "shifted": False}      # наклон №8: сглаженный; shifted — сдвиг замечен, следующую ячейку повторить
FAIL = {"1": 0, "2": 0}                     # подряд ячеек, где поле задано, а мост не выдаёт / плата молчит
HARD = {"1": False, "2": False}
LOW_PREV = {"s": set()}                     # аккумуляторы, низкие на прошлой проверке             # последняя проверка: мост не выдаёт (магнитометр тут не в счёт)


def field_check(c):
    """Поле по факту (07.11: №21 висела час, отвечая «жива», поле К2 не выдавалось): плата катушки жива, её мост
    выдаёт ШИМ, магнитометр колеса видит |B| выше фона. {"1": True/False/None, "2": …}; None — проверить нечем."""
    d = req("/api/coils")
    out = {}
    for w in ("1", "2"):
        want = c.get(f"k{w}", "выкл") != "выкл"
        ag = (d.get("agents") or {}).get(w) or {}
        sn = (d.get("sense") or {}).get(w) or {}
        babs = sn.get("babs") if sn.get("age") is not None and sn["age"] < 6 else None
        if not want:
            if babs is not None:
                BG[w] = babs if BG[w] is None else 0.8 * BG[w] + 0.2 * babs
            out[w] = None
            continue
        m = ag.get("mot") or {}
        drive = any(abs(x) > 0 for br in (m.get("a") or [], m.get("b") or []) for x in br[:2])
        # магнитометр меряет 20 Гц и усредняет: переменное поле выше ~5 Гц он не видит — там верим мосту
        f = c.get("f")
        slow = f is None or f == "rot" or (isinstance(f, (int, float)) and f <= 5)
        seen = None if (not slow or babs is None or BG[w] is None) else babs > BG[w] * 1.3 + 20
        out[w] = bool(ag.get("alive")) and drive and (seen is not False)
        HARD[w] = not (bool(ag.get("alive")) and drive)      # плата молчит или мост не выдаёт — повод для перезапуска
    return out


def measure(sec, rot, gk, c):
    """Одна ячейка: поле ставим, ждём sec, снимаем №8 по /api/static/seg и счёт по разности счётчиков; поле проверяем по факту."""
    coil(c)
    t0 = time.time() + 0.7                      # поле встаёт ~0,5 с
    g0 = gens()
    time.sleep(max(0.0, t0 + sec - time.time()))
    fv = field_check(c)
    t1 = time.time()
    g1 = gens()
    st = (req(f"/api/static/seg?t0={t0}&t1={t1}") or {}).get(N8) or {}
    rec = {"t0": round(t0, 3), "t1": round(t1, 3), "rot": rot, "gen": gk, "field": fname(c), "coil": c,
           "n8": {k: st.get(k) for k in STATIC_KEYS}, "drop": st.get("drop"), "g": gen_z(g0, g1), "fv": fv}
    # наклон №8 (07.11: датчик сдвинулся после касания — среднее и «Гаусс» уехали): изменился на 2° — журнал, два пика, повтор
    try:
        s8 = next(x for x in (req("/api/static").get("sensors") or []) if x.get("mac") == N8)
        deg = (s8.get("pose") or {}).get("deg")
        rec["pose_deg"] = deg
        if deg is not None and POSE["deg"] is not None and abs(deg - POSE["deg"]) > 2.0:
            rec["pose_shift"] = f"наклон №8 {POSE['deg']:.1f}° → {deg:.1f}°"
            print(f"  ⚠ {rec['pose_shift']} — датчик сдвинулся; ячейка повторится", flush=True)
            beep(2)
            POSE["deg"] = deg
            POSE["shifted"] = True
        elif deg is not None:
            POSE["deg"] = deg if POSE["deg"] is None else 0.9 * POSE["deg"] + 0.1 * deg
    except (StopIteration, Exception):  # noqa: BLE001
        pass
    bad = [w for w, ok in fv.items() if ok is False]
    if bad:
        rec["field_unverified"] = "К" + ", К".join(bad) + ": поле задано, по факту не видно"
        for w in bad:
            if not HARD[w]:                     # магнитометр не увидел, а мост выдаёт — помечаем, но плату не трогаем
                continue                        # (07.11: REBOOT по ложным «не видно» сам перезапускал катушки)
            FAIL[w] += 1
            if FAIL[w] == 5:                    # пять ячеек подряд без выдачи — перезапустить плату катушки командой
                ag = ((req("/api/coils").get("agents") or {}).get(w) or {})
                print(f"  ⚠ поле К{w} не видно 3 ячейки подряд — перезапуск платы №{ag.get('id')}", flush=True)
                if ag.get("mac"):
                    req("/api/reboot", {"mac": ag["mac"]}); time.sleep(25)
            elif FAIL[w] >= 10:
                beep(2); print(f"  ✗ поле К{w} не восстановилось после перезапуска — стоп пробежки"); raise SystemExit
    for w, ok in fv.items():
        if ok or not HARD[w]:
            FAIL[w] = 0
    return rec


def zs(rec, refs, pool=None):
    """z ячейки против опор (поле выкл, то же вращение и счёт): статика №8 и счёт. Среднее — по опорам того же
    состояния, разброс — по всем опорам того же вращения (pool, их больше: 3 опоры давали раздутые z, 06.11).
    Ячейка с толчком (drop > 0) по статике не судится; эксцесс — порог вдвое строже (удары)."""
    out = {}
    if not rec.get("drop"):
        for k in STATIC_KEYS:
            v = [r["n8"].get(k) for r in refs if isinstance(r["n8"].get(k), (int, float)) and not r.get("drop")]
            vp = [r["n8"].get(k) for r in (pool or refs) if isinstance(r["n8"].get(k), (int, float)) and not r.get("drop")]
            x = rec["n8"].get(k)
            if len(v) >= 3 and len(vp) >= 6 and isinstance(x, (int, float)) and S.stdev(vp) > 0:
                z = (x - S.mean(v)) / S.stdev(vp)
                out[k] = z / 2 if k.startswith("kurt") else z
    for k in ("zy", "zt", "zraw"):
        x = rec["g"].get(k)
        if rec.get("gen") == "ipure":            # чистая информация: кандидаты не случайны — теория выхода/отсева не про них
            continue                             # (08.11: ложный «кандидат» zy +14,8)
        if isinstance(x, (int, float)):
            out[k] = x                           # уже z против теории
    return out


# ---------- ход ----------
if not A.info:
    GENS = [g for g in GENS if g not in ("imix", "ipure")]
elif not A.dry and not A.no_feed:
    import subprocess
    print(f"информация: {A.info} → платы", flush=True)
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "info_feed.py"), A.info, "--kb", "256"], check=False)
seed = A.seed if A.seed is not None else int(time.time()) % 100000
R = random.Random(seed)
log_path = os.path.join(ROOT, "data", "series", f"sweep_{time.strftime('%Y-%m-%d')}.json")
log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
refs = {}                                    # (rot, gen) → [записи «выкл»]
cands = []
print(f"пробежка: {A.hours} ч · ячейка {A.cell} с · полей {len(FIELDS)} · счёт {len(GENS)} · вращение {len(ROTS)} · зерно {seed}", flush=True)
if A.dry:
    sys.exit(0)
t_end = time.time() + A.hours * 3600
try:
    rots = A.rots.split(",") if A.rots else ROTS[:]
    if not A.rots: R.shuffle(rots)
    while time.time() < t_end:
        for rot in rots:
            if time.time() >= t_end:
                break
            mot(rot)
            print(f"\n=== вращение: {rot} {time.strftime('%H:%M:%S')}", flush=True)
            time.sleep(45 if rot != "stop" else 20)          # разгон, обод просыпается ~40 с
            wh = req("/api/motors").get("wheels") or {}
            need = {"opposite": ("1", "2"), "same": ("1", "2"), "only1": ("1",), "stop": ()}[rot]
            dead = [w for w in need for v in wh.values() if v.get("wheel") == w and ((v.get("meas") or 0) < 30)]
            if dead:
                beep(2); print(f"  ✗ колесо {', '.join(dead)} не крутится (обод < 30 об/мин) — стоп пробежки"); raise SystemExit
            print("  обороты: " + "  ".join(f"к{v['wheel']} {round(v['meas'], 1) if v.get('meas') is not None else '—'}" for v in wh.values()), flush=True)
            t_rot = time.time()
            gs_ = GENS[:]; R.shuffle(gs_)
            while time.time() - t_rot < A.rot_every and time.time() < t_end:
                for gk in gs_:
                    if time.time() - t_rot >= A.rot_every or time.time() >= t_end:
                        break
                    gen(gk)
                    time.sleep(4)
                    key = (rot, gk)
                    refs.setdefault(key, [])
                    # «в такт» требует меток верха с обода: стоящему колесу не задать (стоят — всем; одно колесо — К2)
                    fs = [c for c in FIELDS if not (c.get("f") == "rot" and (rot == "stop" or (rot == "only1" and c["k2"] != "выкл")))]
                    R.shuffle(fs)
                    t_gen = time.time()
                    i = 0
                    while time.time() - t_gen < A.gen_every and i < len(fs):
                        duty, on, low = guard()
                        if duty > 820:
                            raise RuntimeError(f"ШИМ {duty} ‰ > 820")
                        if low:
                            beep(2); print(f"  ⚠ аккумулятор: {low} — поле выкл, стоп пробежки"); raise SystemExit
                        if on < 12:
                            lost_since = globals().get("lost_since") or time.time(); globals()["lost_since"] = lost_since
                            if time.time() - lost_since > 60:
                                print(f"  ✗ на связи {on} плат больше минуты — шина села? выходим, продолжим следующим куском"); raise SystemExit
                            print(f"  … на связи {on} плат — пауза 20 с"); time.sleep(20); continue
                        globals()["lost_since"] = None
                        # опора — каждая 3-я ячейка (и первые 4 для нового состояния)
                        c = OFF if (len(refs[key]) < 3 or i % 3 == 2) else fs[i]
                        rec = measure(A.cell, rot, gk, c)
                        if POSE["shifted"]:                      # датчик сдвинулся в этой ячейке — её же ещё раз
                            POSE["shifted"] = False
                            log.append(rec); rec = measure(A.cell, rot, gk, c); rec["after_shift"] = True
                            for kk in list(refs):                 # опоры прежнего положения датчика больше не годятся
                                refs[kk] = []
                        pool = [r for (ro, _), rr in refs.items() if ro == rot for r in rr]
                        z = zs(rec, refs[key], pool) if c is not OFF else {}
                        rec["z"] = z
                        if c is OFF:
                            refs[key].append(rec)
                        hit = [k for k, v in z.items() if abs(v) > Z1]
                        if rec.get("field_unverified"):
                            z = {}; hit = []                  # поля по факту нет — ячейка не судится
                        rec["z"], rec["hit"] = z, hit
                        line = f"  {time.strftime('%H:%M:%S')} {gk:9s} {rec['field']:16s} " + " ".join(f"{k} {v:+.1f}" for k, v in z.items()) + ("  ⚠ " + rec["field_unverified"] if rec.get("field_unverified") else "")
                        print(line + ("  ← ДЁРНУЛОСЬ" if hit else ""), flush=True)
                        log.append(rec)
                        if hit:                                   # повтор трижды
                            agree = 0
                            for _ in range(3):
                                r2 = measure(A.cell, rot, gk, c); z2 = zs(r2, refs[key], pool); r2["z"] = z2; r2["repeat"] = True
                                log.append(r2)
                                if any(abs(z2.get(k, 0)) > Z2 and (z2.get(k, 0) > 0) == (z[k] > 0) for k in hit):
                                    agree += 1
                                print(f"    повтор: " + " ".join(f"{k} {v:+.1f}" for k, v in z2.items() if k in hit), flush=True)
                            if agree >= 2:                        # удержание 30 с × 4 ABAB
                                print("    ◐ подтверждено повтором — удержание 30 с × 4", flush=True)
                                dz = {k: [] for k in hit}
                                for j in range(4):
                                    ro = measure(30, rot, gk, OFF); rc = measure(30, rot, gk, c)
                                    ro["dwell"] = rc["dwell"] = True; log += [ro, rc]
                                    for k in hit:
                                        a_, b_ = ro["n8"].get(k) if k in STATIC_KEYS else ro["g"].get(k), rc["n8"].get(k) if k in STATIC_KEYS else rc["g"].get(k)
                                        if isinstance(a_, (int, float)) and isinstance(b_, (int, float)):
                                            dz[k].append(b_ - a_)
                                res = {k: (S.mean(v) / (S.stdev(v) / math.sqrt(len(v))) if len(v) >= 3 and S.stdev(v) > 0 else None) for k, v in dz.items()}
                                ok = [k for k, t in res.items() if t is not None and abs(t) > 4]
                                print(f"    удержание: " + " ".join(f"{k} t {t:+.1f}" for k, t in res.items() if t is not None) + ("  ✓ КАНДИДАТ" if ok else "  · не подтвердилось"), flush=True)
                                if ok:
                                    cands.append({"rot": rot, "gen": gk, "field": rec["field"], "coil": c, "t": res, "time": time.strftime("%H:%M:%S")})
                                    beep(3)
                        json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False)
                        i += 1
finally:
    req("/api/coils", {"off": True, "src": "конец пробежки"})
    req("/api/motors", {"stop": True}); req("/api/motors", {"pair": "free"})
    gen("off")
    json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nстоп · ячеек {len(log)} · кандидатов {len(cands)}: {json.dumps(cands, ensure_ascii=False)[:600]}")
