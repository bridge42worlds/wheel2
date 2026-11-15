"""Свободный поиск: короткие блоки с быстрой сменой режимов (вращение, поле, генерация) и статика по каждому блоку.
Без программ — прямые команды; журнал data/series/explore_<дата>.json. Отклонение блока — против опорных блоков
(те же колёса без поля и счёта) по полосам 8–64 Гц, разбросу и эксцессу №8; печатается сразу.

    python tools/explore.py <план>     план — имя из PLANS или файл .json со списком блоков
    python tools/explore.py --list
"""
import argparse, json, os, statistics as S, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("plan", nargs="?")
ap.add_argument("--list", action="store_true")
ap.add_argument("--sec", type=float, default=0, help="длина блока по умолчанию, с (0 — из плана)")
A = ap.parse_args()


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        out = json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        out = json.load(e)
    if b is not None and isinstance(out, dict) and out.get("ok") is False:   # команда не принята — сразу видно
        print(f"  ✗ {p}: {out.get('error')}", flush=True)
    return out


fleet = req("/api/fleet")["agents"]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) == w] for w in ("1", "2")}
D3 = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
      and str(b.get("wheel")) == "3"]                 # пустышка — в счёт «сами» тоже
ALL = W["1"] + W["2"] + D3
ID = {b["mac"]: b["id"] for b in fleet}
N8 = next((b["mac"] for b in fleet if b.get("id") == 8), None)
N9 = next((b["mac"] for b in fleet if b.get("id") == 9), None)
BASE = {"src": 128, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0}
OFF = {"1": "выкл", "2": "выкл"}


# ---------- режимы ----------
def gen(kind, bits=64, src=128, raw=1, ss=0):
    """Генерация: off · solo (оба сами) · absorb (К1 сама, К2 поглощает) · cascade (каждое колесо каскадом) · one (одна группа)."""
    if kind == "quench":                         # тушение: шум в шум — все рассеивают пустышкой, простых нет
        req("/api/compose", {"action": "quench", "force": True})
        return
    if kind == "off":
        req("/api/control", {"targets": ALL, "cfg": {**BASE, "mode": 0, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
        return
    noise = {"src": src, "raw": raw, "bits": bits}
    cfg = {**BASE, **noise, "run": 1, "dc": 1, "ss": ss}
    own = {m: {"role": 0, "ss": ss, **noise} for m in ALL}
    if kind == "solo":
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 0}, "own": own})
    elif kind in ("absorb", "absorb2"):          # absorb: К1 считает, К2 поглощает; absorb2 — наоборот
        own.update({m: {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0} for m in (W["2"] if kind == "absorb" else W["1"])})
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 0, "sm": 1, "sh": 0}, "own": own})
    elif kind == "half":                         # считает половина плат каждого колеса (чёт/нечет по номеру), остальные стоят
        on = [m for m in ALL if ID[m] % 2 == (bits % 2)]
        off = [m for m in ALL if m not in on]
        req("/api/control", {"targets": off, "cfg": {**BASE, "mode": 0, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in off}})
        req("/api/control", {"targets": on, "cfg": {**cfg, "mode": 0, "bits": 64}, "own": {m: {**own[m], "bits": 64} for m in on}})
    elif kind == "dummy_gen":                    # пустышка считает, оба колеса поглощают
        own.update({m: {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0} for m in W["1"] + W["2"]})
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 0, "sm": 1, "sh": 0}, "own": own})
    elif kind == "dummy_abs":                    # оба колеса считают, пустышка поглощает
        own.update({m: {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0} for m in D3})
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 0, "sm": 1, "sh": 0}, "own": own})
    elif kind == "solo2":                        # только колесо 2 считает, колесо 1 стоит без счёта
        req("/api/control", {"targets": W["1"], "cfg": {**BASE, "mode": 0, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in W["1"]}})
        req("/api/control", {"targets": W["2"], "cfg": {**cfg, "mode": 0}, "own": {m: own[m] for m in W["2"]}})
    elif kind == "cascade":
        req("/api/control", {"targets": W["1"], "cfg": {**BASE, "mode": 0, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in W["1"]}})
        req("/api/control", {"targets": W["2"], "cfg": {**cfg, "mode": 1, "gv": 2, "rk": 1, "pw": 50}, "own": {m: own[m] for m in W["2"]}})
    elif kind == "one":
        req("/api/control", {"targets": ALL, "cfg": {**cfg, "mode": 1, "gv": 2, "rk": 1, "pw": 50}, "own": own})


def mot(pair="opposite", mode="zerog", rpm=None, direction=1):
    if pair == "off":
        req("/api/motors", {"stop": True})
        return
    req("/api/motors", {"pair": pair})
    body = {"wheel": "a", "mode": mode, "dir": direction, "flip": 0, "zg": 0}
    if rpm is not None:
        body["rpm"] = rpm
    req("/api/motors", body)


def coil(k1="выкл", k2="выкл", f=None, level=1000, shape="sin", sync=None, k=1, k2sync=None):
    """Поле: постоянное, ∿ f Гц, или в такт вращению (sync — фаза, °; k — раз за оборот; k2sync — своя фаза у К2).
    В такт — нужны метки верха с обода: rt=1 всем платам колёс (мягкая настройка, счёт не сбивает)."""
    if sync is not None:                              # метка верха с обода — мягкая настройка, но серверу нужна полная cfg
        cur = {m: (a.get("values") or {}) for a in req("/api/dash")["agents"] for m in [a.get("mac")] if m in ID}
        for m in ALL:
            v = cur.get(m) or {}
            cfg = {**BASE, **{k: v[k] for k in ("src", "bits", "raw", "mode", "dc", "run", "sm", "sh", "td") if v.get(k) is not None}, "rt": 1}
            req("/api/control", {"targets": [m], "cfg": cfg, "own": {m: {"role": v.get("role", 0)}}})
    if k1 == "выкл" and k2 == "выкл":
        req("/api/coils", {"off": True, "src": "поиск"})
        return
    for w, st in (("1", k1), ("2", k2)):
        if st == "выкл":
            req("/api/coils", {"wheel": w, "state": "off", "src": "поиск"})
            continue
        ac = None
        if sync is not None:
            ac = {"f": 1.2, "shape": shape, "sync": {"k": k, "deg": (k2sync if (w == "2" and k2sync is not None) else sync)}}
        elif f is not None:
            ac = {"f": f, "shape": shape}
        req("/api/coils", {"wheel": w, "state": st, "level": level, "ac": ac, "src": "поиск"})


# ---------- планы: (имя, секунд, {mot:…, coil:…, gen:…}) ----------
Z = {"pair": "opposite", "mode": "zerog"}
G = ("absorb2", 64)                              # колесо 2 считает, колесо 1 поглощает
F = lambda f=None, **k: {"k1": "+", "k2": "+", **({"f": f} if f else {}), **k}
PLANS = {
    # D: одно колесо крутится — механика, поле, счёт с поглощением
    "D": [("стоп · ничего", 30, {"mot": {"pair": "off"}, "coil": {}, "gen": "off"}),
          ("вращение · ничего", 40, {"mot": Z}),
          ("К2 считает · К1 поглощает", 30, {"gen": G}), ("опора", 20, {"gen": "off"}),
          ("счёт + оба ∿ 1,2", 25, {"gen": G, "coil": F(1.2)}), ("счёт + оба ∿ 20", 25, {"coil": F(20)}),
          ("счёт + оба =", 25, {"coil": F()}), ("счёт + К2 ∿ 1,2", 25, {"coil": {"k2": "+", "f": 1.2}}),
          ("счёт + К1 ∿ 1,2", 25, {"coil": {"k1": "+", "f": 1.2}}), ("опора", 20, {"gen": "off", "coil": {}}),
          ("наоборот: К1 считает · К2 поглощает", 30, {"gen": ("absorb", 64)}), ("+ оба ∿ 1,2", 25, {"coil": F(1.2)}),
          ("оба сами 64", 25, {"gen": ("solo", 64), "coil": {}}), ("К2 сама · К1 молчит", 25, {"gen": ("solo2", 64)}),
          ("К2 считает · рассеивает своё", 25, {"gen": ("absorb2", 64, 128, 1, 1)}), ("опора", 20, {"gen": "off"}),
          ("без поля · ∿ 0,5", 20, {"coil": F(0.5)}), ("∿ 5", 20, {"coil": F(5)}), ("∿ 12", 20, {"coil": F(12)}),
          ("∿ 35", 20, {"coil": F(35)}), ("∿ 50", 20, {"coil": F(50)}), ("⊓ 1,2 меандр", 20, {"coil": F(1.2, shape="sq")}),
          ("опора", 20, {"coil": {}}),
          ("К2 назад · счёт", 30, {"mot": {**Z, "direction": -1}, "gen": G}), ("+ оба ∿ 1,2", 25, {"coil": F(1.2)}),
          ("К2 40 об/мин · счёт", 30, {"mot": {"pair": "only2", "mode": "rpm", "rpm": 40}, "coil": {}}), ("+ оба ∿ 1,2", 25, {"coil": F(1.2)}),
          ("К2 90 об/мин · счёт", 30, {"mot": {"pair": "only2", "mode": "rpm", "rpm": 90}, "coil": {}}), ("+ оба ∿ 1,2", 25, {"coil": F(1.2)}),
          ("К2 невесомость · счёт 256 SHA", 30, {"mot": Z, "gen": ("absorb2", 256, 128, 0), "coil": {}}),
          ("каскад К2 64 · К1 стоит", 25, {"gen": ("cascade", 64)}),
          ("стоп · счёт · оба ∿ 1,2", 30, {"mot": {"pair": "off"}, "gen": G, "coil": F(1.2)}),
          ("стоп · ничего", 30, {"gen": "off", "coil": {}})],
    # A: механика и поле без счёта
    "A": [("стоп · ничего", 40, {"mot": {"pair": "off"}, "coil": {}, "gen": "off"}),
          ("вращение · ничего", 40, {"mot": Z}),
          ("К1 = +", 25, {"coil": {"k1": "+"}}), ("К2 = +", 25, {"coil": {"k2": "+"}}), ("оба = +", 25, {"coil": {"k1": "+", "k2": "+"}}),
          ("оба = −", 25, {"coil": {"k1": "-", "k2": "-"}}), ("опора", 25, {"coil": {}}),
          ("оба ∿ 0,5 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 0.5}}), ("оба ∿ 1,2 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("оба ∿ 5 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 5}}), ("оба ∿ 12 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 12}}),
          ("оба ∿ 20 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 20}}), ("оба ∿ 35 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 35}}),
          ("оба ∿ 50 Гц", 25, {"coil": {"k1": "+", "k2": "+", "f": 50}}), ("опора", 25, {"coil": {}}),
          ("оба ⊓ 1,2 Гц меандр", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2, "shape": "sq"}}),
          ("оба ∿ 1,2 Гц половина", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2, "level": 500}}),
          ("стоп · оба ∿ 1,2 Гц", 30, {"mot": {"pair": "off"}, "coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("стоп · ничего", 30, {"coil": {}})],
    # B: счёт при вращении, без поля
    "B": [("вращение · ничего", 30, {"mot": Z, "coil": {}, "gen": "off"}),
          ("сами 64", 30, {"gen": ("solo", 64)}), ("опора", 20, {"gen": "off"}),
          ("сами 256 SHA", 30, {"gen": ("solo", 256, 128, 0)}), ("К1 сама · К2 поглощает", 30, {"gen": ("absorb", 64)}),
          ("сами 64 · рассеивают своё", 30, {"gen": ("solo", 64, 128, 1, 1)}), ("опора", 20, {"gen": "off"}),
          ("каскады 64", 30, {"gen": ("cascade", 64)}), ("одна группа 64", 30, {"gen": ("one", 64)}),
          ("сами 64 ГСЧ esp", 30, {"gen": ("solo", 64, 1, 1)}), ("сами 64 CSI", 30, {"gen": ("solo", 64, 2, 1)}), ("опора", 20, {"gen": "off"})],
    # C: счёт + поле + скорость и направление
    "C": [("вращение · сами 64", 25, {"mot": Z, "coil": {}, "gen": ("solo", 64)}),
          ("+ оба ∿ 1,2", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}), ("+ оба ∿ 20", 25, {"coil": {"k1": "+", "k2": "+", "f": 20}}),
          ("+ оба =", 25, {"coil": {"k1": "+", "k2": "+"}}), ("опора (счёт)", 25, {"coil": {}}),
          ("в одну сторону · сами 64", 30, {"mot": {"pair": "same", "mode": "zerog"}}), ("+ оба ∿ 1,2", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("навстречу назад · сами 64", 30, {"mot": {**Z, "direction": -1}, "coil": {}}), ("+ оба ∿ 1,2", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("40 об/мин · сами 64", 30, {"mot": {"pair": "opposite", "mode": "rpm", "rpm": 40}, "coil": {}}), ("+ оба ∿ 1,2", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("85 об/мин · сами 64", 30, {"mot": {"pair": "opposite", "mode": "rpm", "rpm": 85}, "coil": {}}), ("+ оба ∿ 1,2", 25, {"coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("только К1 крутится", 30, {"mot": {"pair": "only1", "mode": "zerog"}, "coil": {}}), ("только К2 крутится", 30, {"mot": {"pair": "only2", "mode": "zerog"}}),
          ("стоп · сами 64 · оба ∿ 1,2", 30, {"mot": {"pair": "off"}, "coil": {"k1": "+", "k2": "+", "f": 1.2}}),
          ("стоп · ничего", 30, {"coil": {}, "gen": "off"})],
}
if A.list:
    for k, v in PLANS.items():
        print(k, len(v), "блоков,", sum(x[1] for x in v) // 60, "мин")
    sys.exit(0)
plan = PLANS.get(A.plan) or json.load(open(A.plan, encoding="utf-8"))
LOG = os.path.join(ROOT, "data", "series", f"explore_{time.strftime('%Y-%m-%d')}.json")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
log = json.load(open(LOG, encoding="utf-8")) if os.path.exists(LOG) else []

KEYS = ["sd_v", "sd_h", "p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "kurt_v", "kurt_h"]


def snapshot():
    """Платы колёс: температура, магнитометр и акселерометр обода, если есть."""
    out = {}
    for a in req("/api/dash")["agents"]:
        if a.get("mac") in ID and a.get("status") == "online":
            v = a.get("values") or {}
            out[str(ID[a["mac"]])] = {k: v.get(k) for k in ("temp", "imu", "mag", "rpm", "work") if v.get(k) is not None}
    return out


def gens():
    out = {}
    for a in req("/api/dash")["agents"]:
        g = a.get("gen") or a.get("values") or {}      # на /api/dash счётчики лежат в values
        if a.get("mac") in ID and g.get("run") and g.get("bits") and not g.get("role"):
            out[a["mac"]] = (g.get("cand", 0), g.get("tdr", 0), g.get("pr", 0), g.get("bits"), g.get("td", 1000), g.get("cid"))
    return out


def genz(g0, g1, dt):
    """Темп и выход простых против теории за блок (сумма по платам с той же настройкой)."""
    import math
    sys.path.insert(0, os.path.join(ROOT, "server"))
    import nullmodel
    cand = tdr = pr = 0; zy = []; zt = []
    for m, (c1, t1_, p1, bits, td, cid) in g1.items():
        q = g0.get(m)
        if not q or q[5] != cid or c1 <= q[0]:
            continue
        dc, dtd, dp = c1 - q[0], t1_ - q[1], p1 - q[2]
        cand += dc; tdr += dtd; pr += dp
        try:
            y = nullmodel.yield_z(dc, dp, dtd, bits, td)
            zy.append(y.get("z_yield")); zt.append(y.get("z_td"))
        except Exception:                             # noqa: BLE001
            pass
    zy = [v for v in zy if isinstance(v, (int, float))]; zt = [v for v in zt if isinstance(v, (int, float))]
    return {"cand_s": cand / dt, "pr_s": pr / dt, "n": len(zy),
            "zy": sum(zy) / math.sqrt(len(zy)) if zy else 0.0, "zt": sum(zt) / math.sqrt(len(zt)) if zt else 0.0}


def guard():
    wh = req("/api/motors").get("wheels") or {}
    return max(abs((wh.get(w) or {}).get("duty") or 0) for w in ("a", "b"))


ref = []                                            # опорные блоки: показатели №8
hi = 0
try:
    for name, sec, act in plan:
        sec = A.sec or sec
        if "mot" in act:
            mot(**act["mot"])
        if "coil" in act:
            coil(**act["coil"])
        if "gen" in act:
            g = act["gen"]
            gen(g) if isinstance(g, str) else gen(*g)
        t0 = time.time()
        settle = 8 if "mot" in act else 3
        g0 = None
        strobe = act.get("strobe")                       # {"period": с, "k1":…, "k2":…} — поле вкл/выкл; "hop": поле К1↔К2
        ph = 0
        while time.time() - t0 < sec:
            time.sleep(1)
            el = time.time() - t0
            if g0 is None and el >= settle:
                g0 = gens()
            ms = act.get("mstep")                        # {"period": с, "pwm": ‰} — колёса шагами: толчок / стоп
            if ms and int(el // ms["period"]) != ph and not strobe:
                ph = int(el // ms["period"])
                req("/api/motors", {"pair": ms.get("pair", "opposite")})
                if ms.get("swing"):                          # маятник: вперёд / назад
                    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1 if ph % 2 == 0 else -1, "pwm": ms.get("pwm", 500), "flip": 0})
                else:
                    req("/api/motors", {"wheel": "a", "mode": "pwm" if ph % 2 == 0 else "off", "dir": ms.get("dir", 1),
                                        "pwm": ms.get("pwm", 500), "flip": 0})
            hs = act.get("halfswap")                         # чередование половин плат каждые hs с
            if hs and int(el // hs) != ph and not strobe and not ms:
                ph = int(el // hs)
                gen("half", ph % 2)
            if strobe and int(el // strobe["period"]) != ph:
                ph = int(el // strobe["period"])
                if strobe.get("hop"):
                    coil(k1="+" if ph % 2 == 0 else "выкл", k2="+" if ph % 2 else "выкл", f=strobe.get("f"))
                else:
                    coil(**({"k1": strobe.get("k1", "+"), "k2": strobe.get("k2", "+"), "f": strobe.get("f")} if ph % 2 == 0 else {}))
            hi = hi + 1 if guard() > 820 else 0
            if hi >= 8:
                print("✗ сторож ШИМ — стоп", flush=True)
                raise SystemExit(1)
            wh = req("/api/motors").get("wheels") or {}       # колесо под командой без оборотов 20 с — мост завис (06.11)
            stuck = [w["wheel"] for w in wh.values() if w.get("eff") not in (None, "off") and abs(w.get("duty") or 0) > 300
                     and not (w.get("meas") or 0) and time.time() - t0 > 20]
            if stuck:
                print(f"✗ колесо {stuck} не крутится под ШИМ — плата моторов зависла? стоп", flush=True)
                raise SystemExit(1)
        t1 = time.time()
        st = req(f"/api/static/seg?t0={t0 + settle}&t1={t1}")
        m8 = (st.get(N8) or {}) if N8 else {}
        m9 = (st.get(N9) or {}) if N9 else {}
        wh = req("/api/motors").get("wheels") or {}
        rpm = {w["wheel"]: round(w.get("meas") or 0) for w in wh.values()}
        g1 = gens()
        gz = genz(g0, g1, t1 - (t0 + settle)) if g0 else {}
        rec = {"t0": t0, "t1": t1, "name": name, "act": act, "n8": m8, "n9": m9, "rpm": rpm, "boards": snapshot(), "gen": gz}
        log.append(rec)
        json.dump(log, open(LOG, "w", encoding="utf-8"), ensure_ascii=False)
        base = name.startswith("опора") or name.startswith("вращение · ничего")
        if base and m8.get("n"):
            ref.append(m8)
        dev = ""
        if ref and m8.get("n"):
            parts = []
            for k in KEYS:
                r = [x[k] for x in ref if isinstance(x.get(k), (int, float))]
                if len(r) >= 1 and isinstance(m8.get(k), (int, float)):
                    sd = S.stdev(r) if len(r) > 2 else (abs(S.mean(r)) * 0.05 + (0.3 if k.startswith("p") else 10 if k.startswith("sd") else 0.3))
                    z = (m8[k] - S.mean(r)) / (sd or 1e-9)
                    if abs(z) >= 2.5:
                        parts.append(f"{k} {m8[k] - S.mean(r):+.2f} (z {z:+.1f})")
            dev = " · ⚑ " + ", ".join(parts) if parts else " · тихо"
        temps = [b.get("temp") for b in rec["boards"].values() if isinstance(b.get("temp"), (int, float))]
        bat = {q["wheel"]: q for q in (req("/api/battery").get("list") or []) if q.get("kind") == "coil" and q.get("alive")}
        rec["bat"] = {w: (q.get("v"), q.get("soc")) for w, q in bat.items()}
        low = [f"К{w} {q.get('soc')} % ({q.get('v'):.2f} В)" for w, q in bat.items() if (q.get("soc") or 100) <= 25 or (q.get("v") or 9) < 7.0]
        dev += (" · 🔋 МЕНЯТЬ АККУМУЛЯТОР: " + ", ".join(low)) if low else " · 🔋 " + "/".join(f"{q.get('soc')}" for q in bat.values()) + " %"
        if gz:
            dev += f" · счёт {gz['pr_s']:.0f} пр/с, выход z {gz['zy']:+.1f}, отсев z {gz['zt']:+.1f}"
        print(f"{time.strftime('%H:%M:%S')} {name:30s} об/мин {rpm.get('1', 0)}/{rpm.get('2', 0)} · №8 sd {m8.get('sd_v', 0):.0f}/{m8.get('sd_h', 0):.0f} мкg "
              f"p4 {m8.get('p4_v', 0):.1f}/{m8.get('p4_h', 0):.1f} kurt {m8.get('kurt_v', 0):.2f} толчков {m8.get('drop', 0)}"
              f" · t° до {max(temps) if temps else 0:.0f}{dev}", flush=True)
finally:
    gen("off")
    req("/api/motors", {"stop": True})
    req("/api/coils", {"off": True, "src": "конец поиска"})
print("журнал:", LOG)
