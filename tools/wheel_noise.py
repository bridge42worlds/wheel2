"""Опыт «шум колеса» (09.11.2026): каждое колесо считает только на шуме своего места.

Шум колеса — записи его датчиков обода целиком: акселерометр-гироскоп ICM-42688 (колесо 1 — №6, колесо 2 — №14; шесть
осей по 16 бит — положение и вращение) и магнитометр MLX90393 (№20, №18; три оси — поле), к каждой записи — время
отсчёта и температура датчика. Плата с датчиком раздаёт записи всем платам своего колеса по ESP-NOW (прошивка: команда
WS <колесо> <что>), считающая плата берёт в шум оси и по выбору время (1) и температуру (2); шум идёт через SHA-256, ГСЧ
и внутренние источники выключены — проверка по счётчикам источников (e). Платы с датчиками в вариантах «колесо» не
считают (под полным счётом замолкают — 07.11), считают по четыре на колесе; в вариантах «датчики» — наоборот: считают
только №6, №14 (своим акселерометром-гироскопом) и №20, №18 (своим магнитометром), остальные стоят и молчат.

Вращение (навстречу, невесомость по ободу) и поле (К1+ К2− 650 ‰) — всё время. Мера — статика №8 по сырью (rawidx).

  начало    — опора «всё выкл» (колёса стоят), вращение, поле, проверка шума;
  пробежка  — блоки по 75 с между «счёт стоит» по 40 с (z по ячейкам 10 с против соседних «стоит»):
              А лестница шума: АГ → +М → +время → +температура → всё + нога, туда и обратно (эгоистичный);
              Б режимы на полном шуме: оба генерят (эгоистичный, каскад, эстафета, одно число), одно генерит — другое
                поглощает (1→2, 2→1 × целиком, по кускам, эстафетой, пустышка, только слушают), своё рассеяние;
              В молчим: шесть режимов с молчащей телеметрией;
              Г только платы с датчиками своим набором: свои датчики → + время → + температура, дважды;
  вдумчиво  — три лучших: 60 с против «счёт стоит» × 10 пар; держится — повтор в одну сторону и контроль
              «то же на ГСЧ» против «на шуме колеса».

    python -u tools/wheel_noise.py [--phase all|check|run|deep] [--deep "режим @ шум [@ молчим]|мера;…"] [--dry]
"""
import argparse, json, math, os, statistics as S, subprocess, sys, time, urllib.request
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawidx  # noqa: E402

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--phase", default="all", choices=("all", "check", "run", "deep"))
ap.add_argument("--deep", default=None, help="вдумчиво — эти «режим @ шум [@ молчим]|мера» через «;» (иначе — лучшие пробежки)")
ap.add_argument("--sections", default="АБВГ", help="какие части пробежки")
ap.add_argument("--rot", default="навстречу")
ap.add_argument("--rot2", default="в одну сторону", help="направление повтора удержавшегося")
ap.add_argument("--field", default="К1+ К2−")
ap.add_argument("--mode-s", type=float, default=75.0)
ap.add_argument("--ref-s", type=float, default=40.0)
ap.add_argument("--pair-s", type=float, default=60.0)
ap.add_argument("--pairs", type=int, default=10)
ap.add_argument("--cell", type=float, default=10.0)
ap.add_argument("--settle", type=float, default=3.0)
ap.add_argument("--top", type=int, default=3)
ap.add_argument("--minutes", type=float, default=175.0, help="предел опыта")
ap.add_argument("--dry", action="store_true")
A = ap.parse_args()
MAC8 = "68ee8f5032f4"

SENSOR_IDS = {"1": (6, 20), "2": (14, 18)}            # акселерометр-гироскоп, магнитометр
ACC_IDS = (6, 14)
# шум: (источники, что из записи — 1 время, 2 температура, кто считает); источники None — у каждой платы свой датчик
NOISE = {"АГ": (32, 0, "колесо"), "АГ+М": (96, 0, "колесо"), "АГ+М+время": (96, 1, "колесо"),
         "АГ+М+время+темп.": (96, 3, "колесо"), "всё + нога": (100, 3, "колесо"),
         "свои датчики": (None, 0, "датчики"), "свои + время": (None, 1, "датчики"), "свои + время + темп.": (None, 3, "датчики"),
         "ГСЧ · колесо": (1, 0, "колесо"), "ГСЧ · датчики": (1, 0, "датчики")}
LADDER = ["АГ", "АГ+М", "АГ+М+время", "АГ+М+время+темп.", "всё + нога"]
SENS_LADDER = ["свои датчики", "свои + время", "свои + время + темп."]
MAIN = "АГ+М+время+темп."
INTERNAL = {"rng": 0, "csi": 1, "jit": 3, "bc": 4, "rrg": 7}   # места в e — у шума колеса должны стоять
BASE = {"src": 96, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 0, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
GROUP = {"каскад": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}, "эстафета": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0},
         "одно число": {"mode": 1, "gv": 0, "dc": 0}}
HOW = {"целиком": {"sm": 1, "sh": 0}, "по кускам": {"sm": 2, "sh": 0}, "эстафетой": {"sm": 3, "sh": 0},
       "пустышка": {"sm": 1, "sh": 1}, "только слушают": {"sm": 0, "sh": 0}}
MODES = (["эгоистичный", "каскад", "эстафета", "одно число"]
         + [f"поглощение {d} · {h}" for d in ("1→2", "2→1") for h in HOW] + ["своё рассеяние"])
QUIET_MODES = ["эгоистичный", "каскад", "эстафета", "одно число", "поглощение 1→2 · целиком", "поглощение 2→1 · целиком"]
SECTIONS = {"А": ("лестница шума", [("эгоистичный", n, False) for n in LADDER + LADDER[::-1]]),
            "Б": ("режимы на полном шуме", [(m, MAIN, False) for m in MODES]),
            "В": ("молчим", [(m, MAIN, True) for m in QUIET_MODES]),
            "Г": ("только платы с датчиками", [("эгоистичный", n, False) for n in SENS_LADDER * 2])}
FIELDS = {"К1+ К2−": ("+", "-", 650, None), "К1+ К2+": ("+", "+", 650, None), "∿1,2 Гц в фазе": ("+", "+", 650, {"f": 1.2, "shape": "sin"})}
ROTS = {"навстречу": (1, -1), "в одну сторону": (1, 1), "навстречу наоборот": (-1, 1), "обе назад": (-1, -1)}
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
NAMES = ([f"СКО {a}" for a in AX] + [f"эксцесс {a}" for a in AX] + [f"асимметрия {a}" for a in AX]
         + [f"≠ гаусс {a}" for a in AX] + [f"горб {a}" for a in AX]
         + [f"{lo}–{hi} Гц {a}" for a in AX for lo, hi in BANDS] + ["тяжесть |g|"])
FLOOR = np.array([5.0] * 3 + [0.15] * 3 + [0.05] * 3 + [1.0] * 3 + [0.04] * 3 + [0.4] * 24 + [10.0])


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def beep(n):
    if A.dry:
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ";".join(["[console]::beep(880,300); Start-Sleep -m 250"] * n)],
                       timeout=15, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def say(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


def name_of(st):
    return f"{st[0]} · {st[1]}" + (" · молчим" if st[2] else "")


fleet = [b for b in req("/api/fleet").get("agents", []) if not b.get("retired")]
ID = {b["mac"]: b["id"] for b in fleet}
MAC = {b["id"]: b["mac"] for b in fleet}
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
         and str(b.get("wheel")) == w] for w in ("1", "2")}
ALL = W["1"] + W["2"]
WHEEL = {m: w for w in W for m in W[w]}
SENS = {w: [MAC[i] for i in SENSOR_IDS[w] if i in MAC] for w in W}            # платы с датчиками
COUNT = {w: [m for m in W[w] if m not in SENS[w]] for w in W}                  # считают на шуме колеса
CNT = COUNT["1"] + COUNT["2"]
SRCB = SENS["1"] + SENS["2"]
SP = [b["mac"] for b in fleet if b.get("id") == 8]
now_state = {}
base_pwm = {"a": 670, "b": 657}


# ---------- исполнение ----------
def control(targets, cfg, own):
    r = req("/api/control", {"targets": targets, "cfg": cfg, "own": own})
    if not r.get("ok", True):
        say(f"✗ настройка: {r.get('error')}")
    return r


def ws_send(scope, sel):
    """Команда WS: «колесо» — все двенадцать раздают и принимают свой поток; «датчики» — без раздачи, своим датчиком."""
    for m in ALL:
        if scope == "колесо":
            line = f"WS {WHEEL[m]} {sel}"
        else:
            line = f"WS 0 {sel}" if m in SRCB else "WS off"
        req("/api/cmd", {"mac": m, "line": line})


def stop_all():
    control(ALL, {**BASE, "dc": 0, "run": 0, "tl": 0}, {m: {"role": 0, "ss": 0, "tl": 0} for m in ALL})


def set_mode(mode, noise, quiet=False):
    src, sel, scope = NOISE[noise]
    tl = 2 if quiet else 0
    ws_send(scope, sel)                            # раньше настройки: иначе до команды WS плата без датчика берёт ГСЧ
    if mode == "счёт стоит":
        stop_all()
    elif scope == "датчики":                       # считают только платы с датчиками — каждая своим; остальные стоят и молчат
        control(CNT, {**BASE, "dc": 0, "run": 0, "tl": 2}, {m: {"role": 0, "ss": 0, "tl": 2} for m in CNT})
        own = {m: {"role": 0, "ss": 0, "src": src or (32 if ID[m] in ACC_IDS else 64), "raw": 0, "bits": 64, "tl": tl} for m in SRCB}
        control(SRCB, {**BASE, "src": 32, "run": 1, "tl": tl}, own)
    else:
        if now_state.get("src_tl") != tl or now_state.get("scope") != "колесо":   # платы с датчиками раздают и стоят
            control(SRCB, {**BASE, "dc": 0, "run": 0, "tl": tl}, {m: {"role": 0, "ss": 0, "src": 96, "raw": 0, "tl": tl} for m in SRCB})
            now_state["src_tl"] = tl
        own = {m: {"role": 0, "ss": 0, "src": src, "raw": 0, "bits": 64, "tl": tl} for m in CNT}
        cfg = {**BASE, "src": src, "run": 1, "tl": tl}
        if mode in GROUP:
            control(CNT, {**cfg, **GROUP[mode]}, own)
        elif mode.startswith("поглощение"):
            d = "1→2" if "1→2" in mode else "2→1"
            how = HOW[mode.split(" · ")[1]]
            for m in (COUNT["2"] if d == "1→2" else COUNT["1"]):        # поглотители — на шуме своего колеса
                own[m] = {"role": 1, "ba": [], "src": src, "raw": 0, "ss": 0, "tl": tl, **how}
            control(CNT, {**cfg, **how}, own)
        elif mode == "своё рассеяние":
            for m in CNT:
                own[m]["ss"] = 1
            control(CNT, {**cfg, "ss": 1}, own)
        else:
            control(CNT, cfg, own)
    if mode == "счёт стоит" or scope == "датчики":
        now_state["src_tl"] = None
    now_state.update(mode=mode, noise=noise, quiet=quiet, scope=scope)


def set_field(name):
    k1, k2, lvl, ac = FIELDS[name]
    for w, st in (("1", k1), ("2", k2)):
        req("/api/coils", {"wheel": int(w), "state": st, "level": lvl, "ac": ac, "src": "шум колеса"})
    now_state["field"] = name


def wait_stopped(max_s=120):
    t0 = time.time()
    while time.time() - t0 < max_s:
        w = req("/api/motors").get("wheels") or {}
        if max(abs((w.get(k) or {}).get("meas") or 0) for k in "ab") < 3 and time.time() - t0 > 8:
            break
        time.sleep(2)
    time.sleep(10)


def calibrate_zero_g(da=1, db=-1):
    """ШИМ невесомости по ободу (как в сессиях): разгон с обратной связью 100 с, медиана ШИМ последних 50 с."""
    req("/api/motors", {"mod": None})
    req("/api/motors", {"pair": "free"})
    req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": da, "flip": 0, "zg": 0})
    req("/api/motors", {"wheel": "b", "mode": "zerog", "dir": db, "flip": 0, "zg": 0})
    t0, duty = time.time(), {"a": [], "b": []}
    while time.time() - t0 < 100:
        time.sleep(1)
        w = req("/api/motors").get("wheels") or {}
        if max(abs((w.get(k) or {}).get("duty") or 0) for k in "ab") > 800:
            req("/api/motors", {"stop": True})
            sys.exit("✗ ШИМ > 800 ‰ при разгоне — стоп")
        if time.time() - t0 > 50:
            for k in "ab":
                if (w.get(k) or {}).get("fb"):
                    duty[k].append(abs(w[k]["duty"]))
    if min(len(duty["a"]), len(duty["b"])) >= 10:
        base_pwm.update({k: int(S.median(duty[k])) for k in "ab"})
    say(f"ШИМ невесомости {base_pwm['a']}/{base_pwm['b']} ‰")


def set_rotation(name):
    if now_state.get("rot") == name:
        return
    da, db = ROTS[name]
    if now_state.get("rot"):
        say(f"вращение: {now_state['rot']} → {name}: остановка, выбег …")
        req("/api/motors", {"stop": True})
        wait_stopped()
    req("/api/motors", {"mod": None})
    req("/api/motors", {"pair": "free"})
    for w, d in (("a", da), ("b", db)):
        req("/api/motors", {"wheel": w, "mode": "pwm", "dir": d, "pwm": min(780, base_pwm[w])})
    time.sleep(30)
    now_state["rot"] = name
    w = req("/api/motors").get("wheels") or {}
    say(f"вращение: {name} — обод {[round((w.get(x) or {}).get('meas') or 0, 1) for x in 'ab']} об/мин")


def all_off():
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "WS off"})
    stop_all()
    req("/api/coils", {"off": True, "src": "шум колеса: всё выкл"})
    req("/api/motors", {"mod": None})
    req("/api/motors", {"stop": True})
    now_state.clear()


# ---------- проверка шума ----------
def noise_check(label="", verbose=True):
    """Считающие берут только то, что задано: внутренних бит 0, датчики идут, нога — только в «всё + нога»;
    поток колеса доходит (ws: кадров принято), что в шуме — как задано (ws[8])."""
    if now_state.get("quiet") or now_state.get("mode") == "счёт стоит":
        return []
    src, sel, scope = NOISE[now_state["noise"]]
    st = {a["mac"]: a for a in req("/api/state").get("agents", [])}
    gen = req("/api/dash?window=60").get("gen") or {}
    work = SRCB if scope == "датчики" else CNT
    bad, rows = [], []
    for m in work:
        ws = (st.get(m) or {}).get("ws")
        e = (gen.get(m) or {}).get("e") or []
        bits = lambda i: (e[i][1] if i < len(e) else 0)
        inner = sum(bits(i) for i in INTERNAL.values())
        my = src or (32 if ID[m] in ACC_IDS else 64)
        if my != 1:
            if inner:
                bad.append(f"№{ID[m]}: внутренних бит {inner}")
            if my & 32 and not bits(5):
                bad.append(f"№{ID[m]}: акселерометр не идёт")
            if my & 64 and not bits(6):
                bad.append(f"№{ID[m]}: магнитометр не идёт")
            if bool(my & 4) != bool(bits(2)):
                bad.append(f"№{ID[m]}: нога {'не идёт' if my & 4 else 'идёт, а не должна'}")
            if scope == "датчики" and not sel and not ws:   # без раздачи и без добавок поля ws в телеметрии нет
                pass
            elif not ws or len(ws) < 9 or ws[8] != sel:
                bad.append(f"№{ID[m]}: WS {ws[8] if ws and len(ws) >= 9 else 'нет'} вместо {sel}")
            elif scope == "колесо" and ws[2] == 0:
                bad.append(f"№{ID[m]}: поток колеса не приходит")
        rows.append(f"№{ID[m]} ws {ws[1:6] if ws else '—'} · акс {bits(5)} магн {bits(6)} нога {bits(2)} внутр {inner}")
    if verbose or bad:
        say(f"шум {label} «{now_state['noise']}»: " + "; ".join(rows))
    if bad:
        say(f"✗ шум: {', '.join(bad)}")
    return bad


# ---------- статика ----------
class Static:
    """Ячейка статики по сырью (как в tools/session.py): место в .bin — сумма n строк индекса, не i0."""

    def __init__(self):
        self.frame, self.rt, self.rt_day = None, None, None

    def cell(self, a, b):
        day = time.strftime("%Y-%m-%d", time.localtime(b))
        if self.rt is None or self.rt_day != day:
            self.rt, self.rt_day = rawidx.RawTail(MAC8, b, keep_s=1800), day
        self.rt.update()
        t, x = self.rt.window(a, b)
        if t is None or len(x) < 0.6 * (b - a) * 400:
            return None
        if self.frame is None:
            g = x.mean(axis=0)
            gu = g / np.linalg.norm(g)
            e1 = np.array([1.0, 0, 0]) - gu[0] * gu
            e1 /= np.linalg.norm(e1)
            self.frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))
        g0, basis = self.frame
        d = (x - g0) @ basis.T * 62.5
        c = d - d.mean(axis=0)
        sd = c.std(axis=0)
        ku = (c ** 4).mean(axis=0) / np.maximum(sd ** 4, 1e-12) - 3
        sk = (c ** 3).mean(axis=0) / np.maximum(sd ** 3, 1e-12)
        tv, hump = [], []
        for ax in range(3):
            h, e = np.histogram(c[:, ax], bins=31, range=(-4 * sd[ax], 4 * sd[ax]))
            P = lambda v: 0.5 * (1 + np.vectorize(math.erf)(v / (sd[ax] * math.sqrt(2))))
            E = len(c) * (P(e[1:]) - P(e[:-1]))
            tv.append(100 * (np.abs(h - E).sum() + max(0.0, len(c) - E.sum())) / (2 * len(c)))
            ctr = (e[:-1] + e[1:]) / 2
            sel = np.abs(ctr) <= 0.35 * sd[ax]
            hump.append(h[sel].sum() / max(E[sel].sum(), 1e-9))
        fs = len(x) / max(t[-1] - t[0], 1e-6)
        nper = int(2 * fs)
        acc, n = 0, 0
        win = np.hanning(nper)
        for s in range(0, len(c) - nper + 1, nper // 2):
            acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * win[:, None], axis=0)) ** 2
            n += 1
        fr = np.fft.rfftfreq(nper, 1 / fs)
        bp = np.array([[10 * np.log10((acc / max(n, 1))[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
        gabs = (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) * 62.5
        mag = np.linalg.norm(c, axis=1)
        knock = float(mag.max()) > max(12 * float(np.median(mag)), 4000.0)
        return {"vec": np.concatenate([sd, ku, sk, tv, hump, bp.ravel(), [gabs]]), "knock": knock}

    def cells(self, t0, t1):
        out = []
        a = t0 + A.settle
        while a + A.cell <= t1 + 0.01:
            c = self.cell(a, a + A.cell)
            if c is not None and not c["knock"]:
                out.append(c["vec"])
            a += A.cell
        return out


def zcomp(X, R):
    """z каждой меры: среднее ячеек режима против ячеек опоры, разброс ячеек не меньше FLOOR."""
    X, R = np.array(X), np.array(R)
    sx = np.maximum(X.std(axis=0, ddof=1), FLOOR) / math.sqrt(len(X))
    sr = np.maximum(R.std(axis=0, ddof=1), FLOOR) / math.sqrt(len(R))
    return (X.mean(axis=0) - R.mean(axis=0)) / np.sqrt(sx ** 2 + sr ** 2)


def block(st_obj, st, sec, log, tag):
    t0 = time.time()
    set_mode(*st)
    time.sleep(max(0.0, sec - (time.time() - t0)))
    t1 = time.time()
    cells = st_obj.cells(t0, t1)
    log["blocks"].append({"tag": tag, "mode": st[0], "noise": st[1], "quiet": st[2], "rot": now_state.get("rot"),
                          "field": now_state.get("field"), "t0": round(t0, 2), "t1": round(t1, 2),
                          "cells": [np.round(v, 3).tolist() for v in cells]})
    return cells


def guard(log, every=300):
    if time.time() - log.get("_g", 0) < every:
        return True
    if now_state.get("quiet"):                     # после «молчим» платы и обод молчат по плану — проверим после следующего блока
        return True
    log["_g"] = time.time()
    bat = {x.get("label"): x.get("soc") for x in (req("/api/battery").get("list") or []) if x.get("alive")}
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [ID[m] for m in ALL if m not in online]
    w = req("/api/motors").get("wheels") or {}
    rpm = [round((w.get(k) or {}).get("meas") or 0, 1) for k in "ab"]
    say(f"сторожа: аккумуляторы {bat} %, плат на связи {12 - len(lost)}/12{' (нет ' + str(lost) + ')' if lost else ''}, обод {rpm}")
    if any(v is not None and v <= 10 for v in bat.values()):
        log["stop"] = "сели аккумуляторы"
    elif any(abs(v) < 2 for v in rpm):
        log["_still"] = log.get("_still", 0) + 1
        if log["_still"] >= 2:
            log["stop"] = f"колесо стоит: обод {rpm}"
    elif len(lost) > 2:                             # сторож — после блока «счёт стоит»: все говорят
        log["_lost"] = log.get("_lost", 0) + 1
        if log["_lost"] >= 2:
            log["stop"] = f"платы не отвечают: {lost}"
    else:
        log["_lost"] = log["_still"] = 0
    if log.get("stop"):
        say(f"✗ ОСТАНОВКА: {log['stop']}")
        beep(2)
        return False
    return True


def summary(v):
    return (f"СКО в/г1/г2 {v[0]:.0f}/{v[1]:.0f}/{v[2]:.0f} мкg · ≠ гаусс {v[9]:.1f}/{v[10]:.1f}/{v[11]:.1f} % · "
            f"горб {v[12]:.2f}/{v[13]:.2f}/{v[14]:.2f}")


# ---------- части опыта ----------
def start(st_obj, log):
    say("начало: всё выкл, опора 60 с (колёса стоят)")
    all_off()
    wait_stopped()
    ta = time.time()
    time.sleep(60)
    ref = st_obj.cells(ta, time.time())
    log["start"] = {"опора": [np.round(v, 3).tolist() for v in ref]}
    if ref:
        say(f"опора «всё выкл»: {summary(np.mean(ref, axis=0))}")
    set_mode("счёт стоит", MAIN)
    say(f"вращение: {A.rot}, невесомость по ободу (100 с) …")
    calibrate_zero_g(*ROTS[A.rot])
    now_state["rot"] = A.rot
    set_field(A.field)
    say(f"поле: {A.field} — всё время")
    time.sleep(20)
    set_mode("эгоистичный", MAIN)
    time.sleep(40)
    bad = noise_check("· начало")
    if bad:
        say("✗ шум колеса не работает как надо — смотрите строки выше")
        beep(2)
    return not bad


def run_through(st_obj, log, t_end):
    res = log.setdefault("run", [])
    for key in A.sections:
        title, items = SECTIONS[key]
        if time.time() > t_end or log.get("stop"):
            break
        say(f"═══ {key}: {title} — {len(items)} блоков по {A.mode_s:g} с между «счёт стоит» {A.ref_s:g} с "
            f"(вращение «{now_state.get('rot')}», поле «{now_state.get('field')}»)")
        ref_prev = block(st_obj, ("счёт стоит", items[0][1], False), A.ref_s, log, key)
        for st in items:
            if time.time() > t_end or not guard(log):
                break
            X = block(st_obj, st, A.mode_s, log, key)
            noise_check(f"· {st[0]}", verbose=False)
            ref_next = block(st_obj, ("счёт стоит", st[1], False), A.ref_s, log, key)
            R = ref_prev + ref_next
            ref_prev = ref_next
            if len(X) < 4 or len(R) < 4:
                say(f"  {name_of(st)}: мало ячеек ({len(X)}/{len(R)})")
                continue
            z = zcomp(X, R)
            order = np.argsort(-np.abs(z))[:3]
            say(f"  {name_of(st)}: " + " · ".join(f"{NAMES[j]} z {z[j]:+.1f}" for j in order))
            res.append({"sec": key, "st": list(st), "rot": now_state.get("rot"), "z": np.round(z, 2).tolist()})
            if abs(z[order[0]]) >= 4:
                say(f"◐ выделился «{name_of(st)}»: {NAMES[order[0]]} z {z[order[0]]:+.1f} — в вдумчивую часть")
    return res


def pick(res, k):
    rows = [(abs(zz), tuple(r["st"]), j) for r in res for j, zz in enumerate(r["z"])]
    rows.sort(key=lambda q: -q[0])
    out, seen = [], set()
    for zz, st, j in rows:
        if st in seen:
            continue
        seen.add(st)
        out.append({"st": st, "j": j, "z": zz})
        if len(out) >= k:
            break
    return out


def pairs(st_obj, log, st, j, n, vs=None, tag="вдумчиво"):
    """n пар по A.pair_s: состояние и «против» по очереди (порядок чередуется); разность меры j в каждой паре."""
    vs = vs or ("счёт стоит", st[1], False)
    d = []
    for r in range(n):
        if not guard(log):
            break
        seq = [("x", st), ("v", vs)]
        if r % 2:
            seq.reverse()
        m = {}
        for key, s in seq:
            cells = block(st_obj, s, A.pair_s, log, tag)
            m[key] = float(np.mean([c[j] for c in cells])) if len(cells) >= 3 else None
        if m.get("x") is not None and m.get("v") is not None:
            d.append(m["x"] - m["v"])
    if len(d) < 4:
        return {"n": len(d), "d": d, "ok": False}
    mu, se = S.mean(d), S.stdev(d) / math.sqrt(len(d))
    t = mu / se if se > 0 else 0.0
    same = max(sum(1 for v in d if v > 0), sum(1 for v in d if v < 0))
    return {"n": len(d), "d": [round(v, 3) for v in d], "mu": mu, "t": t, "same": same,
            "ok": abs(t) >= 3 and same >= math.ceil(0.8 * len(d))}


def deep(st_obj, log, picks, t_end):
    say(f"═══ вдумчиво: {len(picks)} — по {A.pairs} пар × {A.pair_s:g} с против «счёт стоит»")
    log["deep"] = []
    for p in picks:
        if time.time() > t_end or log.get("stop"):
            break
        st, j = tuple(p["st"]), p["j"]
        set_rotation(A.rot)
        say(f"→ «{name_of(st)}» — {NAMES[j]} (пробежка |z| {p.get('z', 0):.1f})")
        h = pairs(st_obj, log, st, j, A.pairs)
        rec = {"pick": p, "hold": h, "rot": now_state.get("rot")}
        log["deep"].append(rec)
        if "mu" not in h:
            say(f"   мало пар ({h['n']})")
            continue
        say(f"   {NAMES[j]}: Δ {h['mu']:+.3f} (t {h['t']:+.1f}, один знак {h['same']}/{h['n']}) по парам {h['d']}"
            f" — {'✓ ДЕРЖИТСЯ' if h['ok'] else 'нет'}")
        if not h["ok"] or time.time() > t_end:
            continue
        beep(2)
        set_rotation(A.rot2)
        h2 = pairs(st_obj, log, st, j, max(6, A.pairs // 2))
        rec["rot2"] = {"rot": A.rot2, "hold": h2}
        say(f"   при «{A.rot2}»: Δ {h2.get('mu', 0):+.3f} (t {h2.get('t', 0):+.1f}) — {'✓' if h2.get('ok') else 'нет'}")
        set_rotation(A.rot)
        ctrl = (st[0], "ГСЧ · датчики" if NOISE[st[1]][2] == "датчики" else "ГСЧ · колесо", st[2])
        hc = pairs(st_obj, log, st, j, 6, vs=ctrl, tag="контроль")
        rec["control"] = hc
        say(f"   свой шум против ГСЧ в том же режиме: Δ {hc.get('mu', 0):+.3f} (t {hc.get('t', 0):+.1f}) — "
            f"{'✓ дело в шуме места' if hc.get('ok') else 'нет'}")
        if h2.get("ok") and hc.get("ok"):
            say(f"★★ НАХОДКА: «{name_of(st)}» — {NAMES[j]} держится, при другом вращении и против ГСЧ")
            beep(3)


def main():
    if A.dry:
        print(f"считают на шуме колеса: колесо 1 {[ID[m] for m in COUNT['1']]}, колесо 2 {[ID[m] for m in COUNT['2']]}; "
              f"платы с датчиками: {[ID[m] for m in SRCB]}; статика {'есть' if SP else 'НЕТ'}")
        tot = 6.0
        for key in A.sections:
            title, items = SECTIONS[key]
            mins = (len(items) * (A.mode_s + A.ref_s) + A.ref_s) / 60
            tot += mins
            print(f"{key} {title}: {len(items)} блоков ≈ {mins:.0f} мин")
            for st in items:
                print(f"    {name_of(st)}")
        deep_m = A.top * A.pairs * 2 * A.pair_s / 60
        print(f"вдумчиво: {A.top} × {A.pairs} пар ≈ {deep_m:.0f} мин (+ на каждое удержавшееся ≈ {(5 + 6) * 2 * A.pair_s / 60 + 6:.0f} мин)")
        print(f"всего ≈ {tot + deep_m:.0f} мин без повторов; предел {A.minutes:g} мин")
        return
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    miss = [ID[m] for m in ALL if m not in online]
    if miss:
        sys.exit(f"✗ не на связи: {miss}")
    path = os.path.join(ROOT, "data", "series", time.strftime("wheel_noise_%Y-%m-%d_%H%M.json"))
    log = {"t0": time.time(), "args": vars(A), "blocks": []}
    st_obj = Static()
    t_end = time.time() + A.minutes * 60
    try:
        if A.phase == "check":                     # без моторов: каждый вариант шума 30 с — счётчики и поток
            for noise in LADDER + SENS_LADDER:
                set_mode("эгоистичный", noise)
                time.sleep(30)
                noise_check("· проверка")
            return
        if A.phase in ("all", "run"):
            if not start(st_obj, log):
                return
            run_through(st_obj, log, t_end)
            json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
        if A.phase in ("all", "deep") and not log.get("stop"):
            if A.deep:
                picks = []
                for item in A.deep.split(";"):
                    sn, metric = item.split("|", 1)       # мера «тяжесть |g|» сама с чертой
                    f = [v.strip() for v in sn.split(" @ ")]
                    picks.append({"st": (f[0], f[1] if len(f) > 1 else MAIN, len(f) > 2 and f[2] == "молчим"),
                                  "j": NAMES.index(metric.strip())})
            else:
                picks = pick(log.get("run") or [], A.top)
            if A.phase == "deep":
                set_mode("счёт стоит", MAIN)
                calibrate_zero_g(*ROTS[A.rot])
                now_state["rot"] = A.rot
                set_field(A.field)
            deep(st_obj, log, picks, t_end)
    finally:
        json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
        all_off()
        say(f"опыт закончен: всё выключено; журнал {path}")


if __name__ == "__main__":
    main()
