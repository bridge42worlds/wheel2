"""Сессия последнего цикла (план docs/plan_final_sessions.md, 08.11.2026): начало (проверки, опора «всё выкл», сборка
моторы → поле → счёт), четыре части пространства с нащупыванием по одному параметру (ячейки 5 с, через одну — «дом»
части), тишина 5 мин после второй части (генераторы молчат, статика не мерит — смотрит пользователь), конец.
Главный прибор — статика №8. Выделилось — два повтора; повторилось — центр туда (и удержание 4 × 30 с против «дома»
при том же вращении); тянет — центр туда; два обхода пусто — часть закрыта досрочно.

    python tools/session.py --session 1 [--dry] [--parts 1,2] [--no-start] [--no-silence]
"""
import argparse
import glob
import json
import math
import os
import random
import statistics as S
import subprocess
import sys
import time
import urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawidx  # noqa: E402

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--session", type=int, required=True)
ap.add_argument("--parts", default=None, help="только эти части (номера через запятую)")
ap.add_argument("--part-min", type=float, default=25.0)
ap.add_argument("--cell", type=float, default=5.0)
ap.add_argument("--settle", type=float, default=1.5)
ap.add_argument("--z1", type=float, default=4.0)
ap.add_argument("--z2", type=float, default=3.0)
ap.add_argument("--no-start", action="store_true", help="без начала (опора и сборка)")
ap.add_argument("--no-silence", action="store_true")
ap.add_argument("--dry", action="store_true")
ap.add_argument("--seed", type=int, default=None)
A = ap.parse_args()
rnd = random.Random(A.seed if A.seed is not None else int(time.time()))
MAC8 = "68ee8f5032f4"

# ---------- пространство ----------
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
GROUP = {"каскад": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}, "эстафета": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0},
         "одно число": {"mode": 1, "gv": 0, "dc": 0}}
GENS = ["эгоистичный", "каскад", "эстафета", "одно число", "поглощение 1→2", "поглощение 2→1", "своё рассеяние", "стоят"]
ABSORB = ["эгоистичный"] + [f"поглощение {d} · {h}" for d in ("1→2", "2→1") for h in ("целиком", "по кускам", "эстафетой", "пустышка", "только слушают")]
HOW = {"целиком": {"sm": 1, "sh": 0}, "по кускам": {"sm": 2, "sh": 0}, "эстафетой": {"sm": 3, "sh": 0},
       "пустышка": {"sm": 1, "sh": 1}, "только слушают": {"sm": 0, "sh": 0}}
SRCS = {"все пять": 157, "регистр": 128, "ГСЧ": 1, "код": 16, "дрожание": 8, "АЦП": 4, "все кроме: регистр": 29,
        "все кроме: ГСЧ": 156, "все кроме: код": 141, "все кроме: дрожание": 149, "все кроме: АЦП": 153}
F_DC = {"К1+ К2−": ("+", "-", 650, None), "К1− К2+": ("-", "+", 650, None), "К1+ К2+": ("+", "+", 650, None),
        "К1− К2−": ("-", "-", 650, None), "только К1": ("+", "off", 650, None), "только К2": ("off", "-", 650, None),
        "выкл": ("off", "off", 0, None), "К1+ К2− 300 ‰": ("+", "-", 300, None), "К1+ К2− 1000 ‰": ("+", "-", 1000, None)}
F_LOW = {"∿1,2 Гц в фазе": ("+", "+", 650, {"f": 1.2, "shape": "sin"}), "∿0,3 Гц": ("+", "+", 650, {"f": 0.3, "shape": "sin"}),
         "∿1,2 Гц противофаза": ("+", "-", 650, {"f": 1.2, "shape": "sin"}), "∿5 Гц": ("+", "+", 650, {"f": 5, "shape": "sin"}),
         "⊓1,2 Гц": ("+", "+", 650, {"f": 1.2, "shape": "sq"}), "выкл": ("off", "off", 0, None)}
F_HIGH = {"∿20 Гц": ("+", "+", 650, {"f": 20, "shape": "sin"}), "∿50 Гц": ("+", "+", 650, {"f": 50, "shape": "sin"}),
          "⊓20 Гц": ("+", "+", 650, {"f": 20, "shape": "sq"}), "∿20 Гц противофаза": ("+", "-", 650, {"f": 20, "shape": "sin"}),
          "выкл": ("off", "off", 0, None)}
F_SYNC = {f"в такт вращению {d}°": ("+", "+", 650, {"f": 1.2, "shape": "sin", "sync": {"k": 1, "deg": d}}) for d in (0, 90, 180, 270)}
F_SYNC["выкл"] = ("off", "off", 0, None)
FIELDS = {**F_DC, **F_LOW, **F_HIGH, **F_SYNC}
ROTS = {"стоят": None, "навстречу": (1, -1, 1.0, False), "в одну сторону": (1, 1, 1.0, False),
        "навстречу наоборот": (-1, 1, 1.0, False), "обе назад": (-1, -1, 1.0, False), "только колесо 1": (1, 0, 1.0, False),
        "только колесо 2": (0, -1, 1.0, False), "быстрее": (1, -1, 1.15, False), "медленнее": (1, -1, 0.75, False),
        "качание": (1, -1, 1.0, True)}
OBS = ["на связи", "молчат"]


def part(n, name, rot, fields, gens=GENS, srcs=None, extra=None, cell=None):
    return {"n": n, "name": name, "rot": rot, "fields": list(fields), "gens": list(gens), "srcs": list(srcs or SRCS),
            "obs": OBS, "extra": extra or {}, "cell": cell}


PARTS = [part(1, "стоят × постоянное поле", "стоят", F_DC),
         part(2, "стоят × переменное поле", "стоят", list(F_LOW) + [k for k in F_HIGH if k != "выкл"]),
         part(3, "навстречу × постоянное", "навстречу", F_DC),
         part(4, "навстречу × переменное низкое", "навстречу", F_LOW),
         part(5, "навстречу × переменное высокое", "навстречу", F_HIGH),
         part(6, "навстречу × в такт вращению", "навстречу", F_SYNC),
         part(7, "в одну сторону × постоянное", "в одну сторону", F_DC),
         part(8, "в одну сторону × в такт", "в одну сторону", F_SYNC),
         part(9, "навстречу наоборот × постоянное", "навстречу наоборот", F_DC),
         part(10, "обе назад × постоянное", "обе назад", F_DC),
         part(11, "только колесо 1 × постоянное и низкое", "только колесо 1", list(F_DC) + [k for k in F_LOW if k != "выкл"]),
         part(12, "только колесо 2 × постоянное и низкое", "только колесо 2", list(F_DC) + [k for k in F_LOW if k != "выкл"]),
         part(13, "качание × постоянное (ячейки 20 с)", "качание", F_DC, cell=20.0),
         part(14, "быстрее × постоянное", "быстрее", F_DC),
         part(15, "медленнее × постоянное", "медленнее", F_DC),
         part(16, "информация: код → прелюдия", "навстречу", F_DC, extra={"info": ["код проекта", "прелюдия Баха"]}),
         part(17, "как поглощают", "навстречу", F_DC, gens=ABSORB),
         part(18, "длина чисел 64 / 128 / 256", "навстречу", F_DC, extra={"bits": [64, 128, 256]}),
         part(19, "стоят × лучшее из 1–18", "стоят", F_DC),
         part(20, "повтор лучшего", None, [])]
SESSIONS = {1: [1, 2, 3, 4], 2: [5, 6, 7, 8], 3: [9, 10, 11, 12], 4: [13, 14, 15, 16], 5: [17, 18, 19, 20]}
INFO = {"код проекта": os.path.join(ROOT, "data", "info", "code.txt"),
        "прелюдия Баха": os.path.join(ROOT, "data", "info", "bach", "prelude1_8k_s16.pcm")}
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
NAMES = ([f"СКО {a}" for a in AX] + [f"эксцесс {a}" for a in AX] + [f"асимметрия {a}" for a in AX]
         + [f"≠ гаусс {a}" for a in AX] + [f"горб {a}" for a in AX]
         + [f"{lo}–{hi} Гц {a}" for a in AX for lo, hi in BANDS] + ["тяжесть |g|"])
FLOOR = np.array([5.0] * 3 + [0.15] * 3 + [0.05] * 3 + [1.0] * 3 + [0.04] * 3 + [0.4] * 24 + [10.0])


# ---------- связь ----------
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


fleet = [b for b in req("/api/fleet").get("agents", []) if not b.get("retired")]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
         and str(b.get("wheel")) == w] for w in ("1", "2")}
ALL = W["1"] + W["2"]
ID = {b["mac"]: b["id"] for b in fleet}
SP = [b["mac"] for b in fleet if b.get("id") == 8]
now_state = {}
base_pwm = {"a": 670, "b": 657}


# ---------- исполнение ----------
def set_gen(kind, bits=64, tl=0):
    r = _set_gen(kind, bits, tl)
    for m in ALL:                                        # информация в шуме — включена после каждой новой настройки
        req("/api/cmd", {"mac": m, "line": "INFO on"})
    return r


def _set_gen(kind, bits=64, tl=0):
    own = {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": bits, "tl": tl} for m in ALL}
    cfg = {**BASE, "run": 1, "bits": bits, "tc": 1 if bits <= 64 else 0, "tl": tl}
    if kind == "стоят":
        return req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
    if kind in GROUP:
        return req("/api/control", {"targets": ALL, "cfg": {**cfg, **GROUP[kind]}, "own": own})
    if kind.startswith("поглощение"):
        d = "1→2" if "1→2" in kind else "2→1"
        how = HOW[kind.split(" · ")[1]] if " · " in kind else HOW["целиком"]
        for m in (W["2"] if d == "1→2" else W["1"]):
            own[m] = {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0, "tl": tl, **how}
        return req("/api/control", {"targets": ALL, "cfg": {**cfg, **how}, "own": own})
    if kind == "своё рассеяние":
        own = {m: {"role": 0, "ss": 1, "src": 157, "raw": 1, "bits": bits, "tl": tl} for m in ALL}
        return req("/api/control", {"targets": ALL, "cfg": {**cfg, "ss": 1}, "own": own})
    return req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})


def set_field(name):
    k1, k2, lvl, ac = FIELDS[name]
    if k1 == "off" and k2 == "off":
        req("/api/coils", {"off": True, "src": "сессия"})
        return
    for w, st in (("1", k1), ("2", k2)):
        if st == "off":
            req("/api/coils", {"wheel": int(w), "state": "off", "src": "сессия"})
        else:
            req("/api/coils", {"wheel": int(w), "state": st, "level": lvl, "ac": ac, "src": "сессия"})


def wait_stopped(max_s=90):
    """Колёса выбегают долго: ждать, пока обод обоих покажет < 3 об/мин (или max_s), затем 10 с на успокоение."""
    t0 = time.time()
    while time.time() - t0 < max_s:
        w = req("/api/motors").get("wheels") or {}
        rpm = [abs((w.get(k) or {}).get("meas") or 0) for k in "ab"]
        if max(rpm) < 3 and time.time() - t0 > 8:
            break
        time.sleep(2)
    time.sleep(10)


def set_rotation(name):
    r = ROTS[name]
    if now_state.get("rot") == name:
        return
    req("/api/motors", {"mod": None})
    prev = ROTS.get(now_state.get("rot") or "стоят")
    if r is None:
        req("/api/motors", {"stop": True})
        wait_stopped()
    else:
        da, db, k, swing = r
        if prev is not None and (prev[0], prev[1]) != (da, db):
            req("/api/motors", {"stop": True})           # разворот — только после остановки
            wait_stopped()
        req("/api/motors", {"pair": "free"})
        for w, d in (("a", da), ("b", db)):
            if d == 0:
                req("/api/motors", {"wheel": w, "mode": "off"})
            else:
                req("/api/motors", {"wheel": w, "mode": "pwm", "dir": d, "pwm": min(780, int(base_pwm[w] * k))})
        if swing:
            req("/api/motors", {"mod": {"a": 0.05, "T": 20.0, "t0": time.time()}})
        time.sleep(25)
    now_state["rot"] = name
    w = req("/api/motors").get("wheels") or {}
    say(f"вращение: {name} — обод {[round((w.get(x) or {}).get('meas') or 0, 1) for x in 'ab']} об/мин")


def apply(st):
    """Состояние пробы: {"gen", "src", "field", "obs", "bits", "info"} — шлём только изменившееся."""
    bits = st.get("bits", 64)
    if (st["gen"], bits, st["obs"]) != (now_state.get("gen"), now_state.get("bits"), now_state.get("obs")):
        set_gen(st["gen"], bits, 2 if st["obs"] == "молчат" else 0)
        now_state.update(gen=st["gen"], bits=bits, obs=st["obs"], src=None)
    if st.get("info") and st["info"] != now_state.get("info"):
        say(f"информация: {st['info']} → платы (~2,5 мин)")
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "info_feed.py"), INFO[st["info"]], "--kb", "256",
                        "--ids", ",".join(str(ID[m]) for m in ALL)], capture_output=True, timeout=400)
        now_state["info"] = st["info"]
        now_state["src"] = None
    if st["src"] != now_state.get("src"):
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": f"NS {SRCS[st['src']]}"})
        now_state["src"] = st["src"]
    if st["field"] != now_state.get("field"):
        set_field(st["field"])
        now_state["field"] = st["field"]


def all_off():
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "NS off"})
    set_gen("стоят")
    req("/api/coils", {"off": True, "src": "сессия: всё выкл"})
    req("/api/motors", {"mod": None})
    req("/api/motors", {"stop": True})
    now_state.clear()
    now_state["rot"] = "стоят"


# ---------- статика ----------
class Static:
    """Ячейка статики по сырью. 09.11: место в .bin — сумма n строк индекса (tools/rawidx.py), не поле i0."""

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
        for ax in range(3):                               # ≠ гаусс и горб — как в планшетке
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
        vec = np.concatenate([sd, ku, sk, tv, hump, bp.ravel(), [gabs]])
        return {"vec": vec, "knock": knock, "peak": float(mag.max())}


# ---------- части ----------
def neighbors(center, P):
    out = []
    for dim, vals in (("gen", P["gens"]), ("src", P["srcs"]), ("field", P["fields"]), ("obs", P["obs"])):
        out += [{**center, dim: v} for v in vals if v != center[dim]]
    for dim, vals in P["extra"].items():
        if dim != "info":
            out += [{**center, dim: v} for v in vals if v != center.get(dim)]
    rnd.shuffle(out)
    return out


def name_of(st):
    return " · ".join(str(st[k]) for k in ("gen", "src", "field", "obs") if k in st) + \
        "".join(f" · {st[k]}" for k in ("bits", "info") if k in st and st[k] not in (64, None))


def run_part(P, st_obj, log, t_limit):
    cell = P["cell"] or A.cell
    home = {"gen": P["gens"][0], "src": "все пять", "field": P["fields"][0], "obs": "на связи"}
    if P["n"] == 19:                                     # тихий фон × лучшее из частей 1–18
        best = best_states(1)
        if best:
            b = best[0][1]
            home = {"gen": b.get("gen", home["gen"]), "src": b.get("src", "все пять"), "field": b.get("field", home["field"]),
                    "obs": b.get("obs", "на связи")}
            for dim, key in (("gens", "gen"), ("srcs", "src"), ("fields", "field")):
                if home[key] not in P[dim]:
                    P[dim].insert(0, home[key])
            say(f"часть 19: дом — лучшее прежних сессий «{name_of(home)}» ({best[0][3]})")
    if "bits" in P["extra"]:
        home["bits"] = 64
    blocks = P["extra"].get("info") or [None]
    rec_part = {"n": P["n"], "name": P["name"], "t0": time.time(), "cells": [], "hits": [], "moves": []}
    log["parts"].append(rec_part)
    say(f"═══ часть {P['n']}: {P['name']} — вращение «{P['rot']}», до {A.part_min:g} мин, ячейки {cell:g} с; дом: {name_of(home)}")
    set_rotation(P["rot"])
    t_end = min(time.time() + A.part_min * 60, t_limit)
    for bi, info in enumerate(blocks):
        if info:
            home["info"] = info
        t_block_end = t_end if len(blocks) == 1 else time.time() + (t_end - time.time()) / (len(blocks) - bi)
        center, cand, sweep, pending, empty_sweeps = dict(home), neighbors(home, P), [], [], 0
        home_ref, turn = [], "home"
        while time.time() < t_block_end:
            if not guard(log):
                return False
            if turn == "home":
                st = home
            elif pending:
                st = pending[0]["st"]
            else:
                if not cand:
                    if sweep:
                        zz, st2, j2, sg = max(sweep, key=lambda q: q[0])
                        pending.append({"st": st2, "j": j2, "sign": sg, "z": [zz * sg], "kind": "тянет"})
                        sweep = []
                        continue
                    empty_sweeps += 1
                    if empty_sweeps >= 2:
                        say(f"часть {P['n']}: два обхода без отклика — закрываю досрочно")
                        break
                    cand = neighbors(center, P)
                st = cand.pop()
            ta = time.time()
            apply(st)
            if P["rot"] == "качание":                       # ячейка — целый цикл качания
                time.sleep(max(0.0, cell - (time.time() - ta)))
            else:
                time.sleep(max(0.0, cell - (time.time() - ta) - 0.05))
            tb = time.time()
            c = st_obj.cell(ta + A.settle, tb)
            rec = {"t0": round(ta, 2), "st": st, "turn": turn}
            if c is None:
                rec["skip"] = "нет отсчётов"
                rec_part["cells"].append(rec)
                continue
            rec["vec"] = np.round(c["vec"], 3).tolist()
            if c["knock"]:
                rec["skip"] = f"толчок {c['peak'] / 1e6:.3f} g"
                rec_part["cells"].append(rec)
                continue
            if turn == "home":
                home_ref = (home_ref + [c["vec"]])[-20:]
            elif len(home_ref) >= 6:
                R = np.array(home_ref)
                med = np.median(R, axis=0)
                mad = np.maximum(1.4826 * np.median(np.abs(R - med), axis=0), FLOOR)
                z = (c["vec"] - med) / mad
                rec["z"] = np.round(z, 2).tolist()
                j = int(np.argmax(np.abs(z)))
                if pending:
                    pd = pending[0]
                    pd["z"].append(float(z[pd["j"]]))
                    if len(pd["z"]) == 3:
                        pending.pop(0)
                        need = A.z2 if pd["kind"] == "выделилось" else 2.5
                        ok = sum(1 for q in pd["z"][1:] if q * pd["sign"] > 0 and abs(q) >= need)
                        zs = ", ".join(f"{v:+.1f}" for v in pd["z"])
                        if ok == 2:
                            empty_sweeps = 0
                            if pd["kind"] == "выделилось":
                                say(f"★ ПОВТОРИЛОСЬ: «{name_of(pd['st'])}» — {NAMES[pd['j']]} z {zs}; удержание 4 × 30 с против «дома»")
                                beep(2)
                                held = hold(pd["st"], home, pd["j"], st_obj)
                                rec_part["hits"].append({"st": pd["st"], "metric": NAMES[pd["j"]], "z": pd["z"], "hold": held,
                                                         "rot": P["rot"], "part": P["n"]})
                                if held.get("ok"):
                                    say(f"★★ УДЕРЖАНИЕ ПОДТВЕРДИЛО: «{name_of(pd['st'])}» — {NAMES[pd['j']]} {held['mu']:+.2f} (t {held['t']:+.1f})")
                                    beep(3)
                            else:
                                say(f"→ тянет: «{name_of(pd['st'])}» — {NAMES[pd['j']]} z {zs}; центр сюда")
                            rec_part["moves"].append({"to": pd["st"], "kind": pd["kind"], "metric": NAMES[pd["j"]], "z": pd["z"]})
                            center, cand, sweep = dict(pd["st"]), neighbors(pd["st"], P), []
                        elif pd["kind"] == "выделилось":
                            say(f"   не повторилось: «{name_of(pd['st'])}» — {NAMES[pd['j']]} z {zs}")
                elif abs(z[j]) >= A.z1:
                    say(f"◐ выделилась «{name_of(st)}»: {NAMES[j]} z {z[j]:+.1f} — два повтора")
                    pending.append({"st": st, "j": j, "sign": 1 if z[j] > 0 else -1, "z": [float(z[j])], "kind": "выделилось"})
                else:
                    sweep.append((abs(float(z[j])), st, j, 1 if z[j] > 0 else -1))
            rec_part["cells"].append(rec)
            turn = "test" if turn == "home" else "home"
    rec_part["t1"] = time.time()
    tested = [c for c in rec_part["cells"] if c.get("z")]
    if tested:
        mx = max(tested, key=lambda c: max(abs(v) for v in c["z"]))
        jm = int(np.argmax(np.abs(mx["z"])))
        say(f"итог части {P['n']}: проб {len(tested)}, повторилось {len(rec_part['hits'])}, переездов {len(rec_part['moves'])}; "
            f"сильнее всех «{name_of(mx['st'])}» — {NAMES[jm]} z {mx['z'][jm]:+.1f}")
    return True


def hold(st, home, j, st_obj, pairs=4, sec=30.0):
    diffs = []
    for r in range(pairs):
        vals = {}
        for which in ((("пробa", st), ("дом", home)) if r % 2 == 0 else (("дом", home), ("пробa", st))):
            ta = time.time()
            apply(which[1])
            time.sleep(sec)
            c = st_obj.cell(ta + 2, time.time())
            vals[which[0]] = None if c is None or c["knock"] else float(c["vec"][j])
        if vals.get("пробa") is not None and vals.get("дом") is not None:
            diffs.append(vals["пробa"] - vals["дом"])
    if len(diffs) < 3:
        return {"ok": False, "diffs": diffs}
    mu = S.mean(diffs)
    se = S.stdev(diffs) / math.sqrt(len(diffs))
    t = mu / se if se > 0 else 0.0
    return {"ok": abs(t) >= 3 and (all(d > 0 for d in diffs) or all(d < 0 for d in diffs)), "diffs": diffs, "mu": mu, "t": t}


def guard(log, every=300):
    if time.time() - log.get("_g", 0) < every:
        return True
    log["_g"] = time.time()
    bat = {x.get("label"): x.get("soc") for x in (req("/api/battery").get("list") or []) if x.get("alive")}
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [ID[m] for m in ALL if m not in online]
    w = req("/api/motors").get("wheels") or {}
    say(f"сторожа: катушки {bat} %, генераторов {len(ALL) - len(lost)}/{len(ALL)}, обод {[round((w.get(k) or {}).get('meas') or 0, 1) for k in 'ab']}")
    if any(v is not None and v <= 10 for v in bat.values()):
        log["stop"] = "сели катушки"
    elif len(lost) > 2:
        log["_lost"] = log.get("_lost", 0) + 1
        if log["_lost"] >= 2:
            log["stop"] = f"агенты не отвечают: {lost}"
    else:
        log["_lost"] = 0
    if log.get("stop"):
        say(f"✗ ОСТАНОВКА: {log['stop']}")
        beep(2)
        return False
    return True


# ---------- начало, тишина, повтор лучшего ----------
def measure(st_obj, sec, label):
    ta = time.time()
    time.sleep(sec)
    c = st_obj.cell(ta, time.time())
    if c is None:
        say(f"{label}: нет отсчётов")
        return None
    v = c["vec"]
    say(f"{label}: СКО в/г1/г2 {v[0]:.0f}/{v[1]:.0f}/{v[2]:.0f} мкg · ≠ гаусс {v[9]:.1f}/{v[10]:.1f}/{v[11]:.1f} % · "
        f"горб {v[12]:.2f}/{v[13]:.2f}/{v[14]:.2f} · эксцесс {v[3]:+.2f}/{v[4]:+.2f}/{v[5]:+.2f}")
    return np.round(v, 3).tolist()


def calibrate_zero_g():
    req("/api/motors", {"mod": None})
    req("/api/motors", {"pair": "free"})
    req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": 1, "flip": 0, "zg": 0})
    req("/api/motors", {"wheel": "b", "mode": "zerog", "dir": -1, "flip": 0, "zg": 0})
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
    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": base_pwm["a"]})
    req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": base_pwm["b"]})
    now_state["rot"] = "навстречу"
    say(f"ШИМ невесомости {base_pwm['a']}/{base_pwm['b']} ‰")


def start(st_obj, log):
    say("начало: всё выкл, опора 60 с (после полной остановки колёс)")
    all_off()
    wait_stopped()
    log["start"] = {"опора": measure(st_obj, 60, "опора «всё выкл»")}
    say("сборка: моторы (невесомость по ободу, 100 с) …")
    calibrate_zero_g()
    log["start"]["моторы"] = measure(st_obj, 60, "моторы")
    set_field("К1+ К2−")
    now_state["field"] = "К1+ К2−"
    time.sleep(5)
    log["start"]["+ поле"] = measure(st_obj, 60, "моторы + поле")
    apply({"gen": "эгоистичный", "src": "все пять", "field": "К1+ К2−", "obs": "на связи"})
    time.sleep(5)
    log["start"]["+ счёт"] = measure(st_obj, 60, "моторы + поле + счёт")


def silence(st_obj, log, minutes=5.0):
    say(f"ТИШИНА {minutes:g} мин: генераторы молчат, статика не мерит — смотрите на бумажки")
    before = measure(st_obj, 30, "статика до тишины")
    beep(2)
    g_now, b_now, s_now = now_state.get("gen") or "эгоистичный", now_state.get("bits") or 64, now_state.get("src")
    set_gen(g_now, b_now, 2)
    if s_now:
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": f"NS {SRCS[s_now]}"})
    if SP:
        req("/api/control", {"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "bits": 64, "nb": 8, "td": 1000, "sx": 1, "tl": 2},
                             "own": {m: {"role": 2, "cpu": 80, "sg": 0, "sx": 1, "tl": 2} for m in SP}})
    time.sleep(minutes * 60)
    if SP:
        req("/api/control", {"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "bits": 64, "nb": 8, "td": 1000, "sx": 0, "tl": 0},
                             "own": {m: {"role": 2, "cpu": 80, "sg": 0, "sx": 0, "tl": 0} for m in SP}})
    set_gen(g_now, b_now, 0)
    now_state.update(obs="на связи", src=None)
    beep(1)
    time.sleep(10)
    after = measure(st_obj, 30, "статика после тишины")
    log["silence"] = {"before": before, "after": after, "t": time.time()}


def best_states(k=5):
    rows = []
    for p in sorted(glob.glob(os.path.join(ROOT, "data", "series", "session*_*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for pt in d.get("parts", []):
            for h in pt.get("hits", []):
                rows.append((min(abs(v) for v in h["z"]) + (5 if (h.get("hold") or {}).get("ok") else 0), h["st"], h.get("rot") or "навстречу", h["metric"]))
            for mv in pt.get("moves", []):
                rows.append((min(abs(v) for v in mv["z"]), mv["to"], next((q["rot"] for q in PARTS if q["n"] == pt["n"]), "навстречу"), mv["metric"]))
    rows.sort(key=lambda r: -r[0])
    seen, out = set(), []
    for r in rows:
        key = json.dumps([r[1], r[2]], sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:k]


def revisit(st_obj, log):
    best = best_states()
    say(f"═══ часть 20: повтор лучшего — {len(best)} состояний")
    for score, st, rot, metric in best:
        if not guard(log):
            return
        set_rotation(rot)
        home = {"gen": "эгоистичный", "src": "все пять", "field": "К1+ К2−", "obs": "на связи"}
        j = NAMES.index(metric) if metric in NAMES else 0
        h = hold(st, home, j, st_obj, pairs=6)
        say(f"  «{name_of(st)}» ({rot}): {metric} — удержание {'✓ ДЕРЖИТСЯ' if h.get('ok') else 'нет'} "
            f"{[round(d, 2) for d in h.get('diffs', [])]}")
        log.setdefault("revisit", []).append({"st": st, "rot": rot, "metric": metric, "hold": h})
        if h.get("ok"):
            beep(3)


def main():
    plan = [p for p in PARTS if p["n"] in (SESSIONS[A.session] if not A.parts else [int(v) for v in A.parts.split(",")])]
    if A.dry:
        print(f"сессия {A.session}: генераторов {len(ALL)}, статика {'есть' if SP else 'НЕТ'}; части:")
        for p in plan:
            nv = len(p["gens"]) * len(p["srcs"]) * max(1, len(p["fields"])) * len(p["obs"])
            for vals in p["extra"].values():
                nv *= len(vals)
            print(f"  {p['n']:2d}. {p['name']} — вращение «{p['rot']}», сочетаний {nv}, соседей у центра "
                  f"{len(p['gens']) + len(p['srcs']) + len(p['fields']) + len(p['obs']) - 4 + sum(len(v) - 1 for k, v in p['extra'].items() if k != 'info')}")
        print(f"  начало ~10 мин, части по {A.part_min:g} мин, тишина 5 мин после второй части; всего ≈ "
              f"{10 + A.part_min * len(plan) + (0 if A.no_silence else 5):.0f} мин")
        return
    if len([m for m in ALL if any(b['mac'] == m and b.get('online') for b in req('/api/fleet').get('agents', []))]) < 12:
        sys.exit("✗ на связи меньше 12 генераторов")
    path = os.path.join(ROOT, "data", "series", time.strftime(f"session{A.session}_%Y-%m-%d_%H%M.json"))
    log = {"session": A.session, "t0": time.time(), "parts": [], "args": vars(A)}
    st_obj = Static()
    t_limit = time.time() + 118 * 60
    try:
        if not A.no_start:
            start(st_obj, log)
        for i, P in enumerate(plan):
            if P["n"] == 20:
                revisit(st_obj, log)
            else:
                if not run_part(P, st_obj, log, t_limit):
                    break
            json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
            if i == 1 and not A.no_silence:
                silence(st_obj, log)
            if time.time() > t_limit:
                say("2 часа истекли — конец сессии")
                break
    finally:
        json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
        all_off()
        say(f"сессия {A.session} закончена: всё выключено; журнал {path}")
        hits = [h for p in log["parts"] for h in p["hits"]]
        for h in hits:
            say(f"  повторилось: часть {h['part']} «{name_of(h['st'])}» — {h['metric']} z {', '.join(f'{v:+.1f}' for v in h['z'])}"
                f"{' · удержание ✓' if (h.get('hold') or {}).get('ok') else ''}")
        if not hits:
            say("  повторений нет")


if __name__ == "__main__":
    main()
