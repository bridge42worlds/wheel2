"""Пробежка по режимам счёта при постоянном вращении и поле (план пользователя 08.11 на следующий сеанс):
колёса всё время крутятся навстречу на ШИМ невесомости, поле К1+ К2− всё время включено, режим счёта меняется каждые
10 с: эгоистичный, каскад, эстафета, одно число на всех, поглощение (в обе стороны), своё рассеяние, стоят.
Главный прибор — статика №8: каждая ячейка (с 2-й по 10-ю секунду) против скользящей опоры последних ячеек —
СКО, эксцесс, мощность в 8 полосах 1–250 Гц по осям в/г1/г2, тяжесть |g| и средние. Дёрнулось (|z| ≥ Z1, без толчка) —
сразу повтор того же режима трижды вперемешку с другими; повторилось (≥ 2 из 3 с тем же знаком, |z| ≥ Z2) — удержание:
4 × (30 с этот режим, 30 с соседний); держится у всех четырёх пар — кандидат: три пика, журнал. Нет — дальше.

    python tools/mode_sweep.py [--hours 2] [--cell 10] [--level 650] [--swing] [--seed N] [--dry]

Журнал ячеек: data/series/mode_sweep_<дата_время>.json. Сторожа: ШИМ > 820 ‰ — стоп; плат меньше 12 — пауза до
возврата; аккумулятор катушки ≤ 25 % — два пика; поле не видно магнитометром — два пика; толчок или сдвиг №8 — ячейка
не в счёт и переигрывается.
"""
import argparse
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

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=float, default=2.0)
ap.add_argument("--cell", type=float, default=10.0)
ap.add_argument("--settle", type=float, default=2.0, help="секунд после смены режима не в счёт")
ap.add_argument("--level", type=int, default=650)
ap.add_argument("--swing", action="store_true", help="качание оборотов ±5 % / 20 с (по умолчанию ровные обороты)")
ap.add_argument("--ref", type=int, default=40, help="ячеек в скользящей опоре")
ap.add_argument("--z1", type=float, default=5.0)
ap.add_argument("--z2", type=float, default=3.0)
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--dry", action="store_true")
A = ap.parse_args()
rnd = random.Random(A.seed if A.seed is not None else int(time.time()))

BASE = {"src": 128, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 1, "tc": 1}
GROUP = {"cascade": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}, "relay": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0},
         "one": {"mode": 1, "gv": 0, "dc": 0}}
MODES = ["эгоистичный", "каскад", "эстафета", "одно число", "поглощение 1→2", "поглощение 2→1", "своё рассеяние", "стоят"]
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
NAMES = ([f"СКО {a}" for a in AX] + [f"эксцесс {a}" for a in AX] + [f"{lo}–{hi} Гц {a}" for a in AX for lo, hi in BANDS]
         + ["тяжесть |g|", "среднее в", "среднее г1", "среднее г2"])
FLOOR = np.array([5.0] * 3 + [0.15] * 3 + [0.4] * 24 + [10.0] * 4)
UNIT = ["мкg"] * 3 + [""] * 3 + ["дБ"] * 24 + ["мкg"] * 4
MAC8 = "68ee8f5032f4"


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
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ";".join(["[console]::beep(880,300); Start-Sleep -m 200"] * n)],
                       timeout=15, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def say(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


# ---------- установка ----------
fleet = [b for b in req("/api/fleet").get("agents", []) if not b.get("retired")]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
         and str(b.get("wheel")) == w] for w in ("1", "2")}
ALL = W["1"] + W["2"]
ID = {b["mac"]: b["id"] for b in fleet}


def set_mode(kind):
    """Режим счёта всех 12 генераторов колёс."""
    own = {m: {"role": 0, "ss": 0, "src": 128, "raw": 1, "bits": 64} for m in ALL}
    cfg = {**BASE, "run": 1}
    if kind == "стоят":
        return req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
    if kind in ("каскад", "эстафета", "одно число"):
        g = GROUP[{"каскад": "cascade", "эстафета": "relay", "одно число": "one"}[kind]]
        return req("/api/control", {"targets": ALL, "cfg": {**cfg, **g}, "own": own})
    if kind.startswith("поглощение"):
        absorb = W["2"] if kind.endswith("1→2") else W["1"]
        for m in absorb:
            own[m] = {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0}
        return req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
    if kind == "своё рассеяние":
        own = {m: {"role": 0, "ss": 1, "src": 128 | 16, "raw": 1, "bits": 64} for m in ALL}
        return req("/api/control", {"targets": ALL, "cfg": {**cfg, "ss": 1, "src": 128 | 16}, "own": own})
    return req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})          # эгоистичный


def field_on():
    req("/api/coils", {"wheel": 1, "state": "+", "level": A.level, "ac": None, "src": "пробежка режимов"})
    req("/api/coils", {"wheel": 2, "state": "-", "level": A.level, "ac": None, "src": "пробежка режимов"})


def field_seen():
    c = req("/api/coils")
    out = {}
    for w in ("1", "2"):
        sn = (c.get("sense") or {}).get(w) or {}
        out[w] = sn.get("babs") if (sn.get("age") or 99) < 10 else None
    return out


def wheels_on():
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
    if min(len(duty["a"]), len(duty["b"])) < 10:
        req("/api/motors", {"stop": True})
        sys.exit("✗ регулятор не вышел на невесомость (обод молчит?) — стоп")
    base = {k: int(S.median(duty[k])) for k in "ab"}
    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": base["a"]})
    req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": base["b"]})
    if A.swing:
        req("/api/motors", {"mod": {"a": 0.05, "T": 20.0, "t0": time.time()}})
    return base


# ---------- статика: ячейка по сырью ----------
class Static:
    def __init__(self):
        self.frame = None

    def files(self, t):
        return os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(t)), MAC8)

    def cell(self, a, b):
        base = self.files(b)
        if not os.path.exists(base + ".idx.jsonl"):
            return None
        sz = os.path.getsize(base + ".idx.jsonl")
        with open(base + ".idx.jsonl", "rb") as f:          # хвост индекса: последние ~3 МБ
            f.seek(max(0, sz - 3_000_000))
            if f.tell():
                f.readline()
            recs = []
            for ln in f:
                try:
                    p = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if a - 1 <= p["rx"] <= b + 1:
                    recs.append(p)
        if not recs:
            return None
        ts, xs = [], []
        with open(base + ".bin", "rb") as fb:
            for p in recs:
                fb.seek(p["i0"] * 6)
                buf = fb.read(p["n"] * 6)
                if len(buf) < p["n"] * 6:
                    continue
                k = np.arange(p["n"])
                ts.append(p["rx"] - (p["n"] - 1 - k) / (p.get("hz") or 500.0))
                xs.append(np.frombuffer(buf, dtype="<i2").reshape(-1, 3).astype(np.float64))
        t = np.concatenate(ts)
        x = np.concatenate(xs)
        m = (t >= a) & (t < b)
        t, x = t[m], x[m]
        if len(x) < 0.6 * (b - a) * 400:
            return None
        if self.frame is None:
            self.reframe(x)
        g0, basis = self.frame
        d = (x - g0) @ basis.T * 62.5
        mean = d.mean(axis=0)
        c = d - mean
        sd = c.std(axis=0)
        kurt = (c ** 4).mean(axis=0) / np.maximum(sd ** 4, 1e-12) - 3
        fs = len(x) / max(t[-1] - t[0], 1e-6)
        nper = int(2 * fs)
        acc, n = 0, 0
        win = np.hanning(nper)
        for s in range(0, len(c) - nper + 1, nper // 2):
            acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * win[:, None], axis=0)) ** 2
            n += 1
        fr = np.fft.rfftfreq(nper, 1 / fs)
        bp = np.array([[10 * np.log10((acc / n)[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
        gabs = (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) * 62.5
        return {"vec": np.concatenate([sd, kurt, bp.ravel(), [gabs], mean]), "peak": np.abs(c).max(axis=0), "sd": sd,
                "mean": mean, "x": x}

    def reframe(self, x):
        g = x.mean(axis=0)
        gu = g / np.linalg.norm(g)
        e1 = np.array([1.0, 0, 0]) - gu[0] * gu
        e1 /= np.linalg.norm(e1)
        self.frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))


# ---------- пробежка ----------
def main():
    seq_note = f"режимы: {', '.join(MODES)}; ячейка {A.cell:g} с (первые {A.settle:g} с не в счёт)"
    if A.dry:
        print(f"генераторы: колесо 1 — {len(W['1'])}, колесо 2 — {len(W['2'])}; {seq_note}; "
              f"на {A.hours:g} ч ≈ {int(A.hours * 3600 / A.cell)} ячеек ({int(A.hours * 3600 / A.cell / len(MODES))} кругов)")
        return
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [ID[m] for m in ALL if m not in online]
    if lost or len(ALL) < 12:
        sys.exit(f"✗ не все генераторы на связи: {lost}")
    path = os.path.join(ROOT, "data", "series", time.strftime("mode_sweep_%Y-%m-%d_%H%M.json"))
    log = {"t0": time.time(), "args": vars(A), "modes": MODES, "cells": [], "events": []}
    st = Static()
    ref, hist = [], {k: [] for k in MODES}
    queue, last_mode = [], None
    t_end = time.time() + A.hours * 3600
    try:
        say(f"поле: К1+ К2− {A.level} ‰")
        field_on()
        say("колёса: невесомость по ободу (100 с) …")
        base = wheels_on()
        log["base"] = base
        say(f"ШИМ невесомости {base['a']}/{base['b']} ‰" + (", качание ±5 % / 20 с" if A.swing else ", ровно") + f"; {seq_note}")
        fs0 = field_seen()
        say(f"поле по магнитометрам: К1 {fs0['1']} мкТл, К2 {fs0['2']} мкТл")
        n_cell, t_stat = 0, time.time()
        while time.time() < t_end:
            if not queue:
                order = MODES[:]
                rnd.shuffle(order)
                if last_mode and order[0] == last_mode:
                    order.append(order.pop(0))
                queue = [("пробежка", k) for k in order]
            why, kind = queue.pop(0)
            t_a = time.time()
            set_mode(kind)
            time.sleep(max(0.0, A.cell - (time.time() - t_a)))
            t_b = time.time()
            cell = st.cell(t_a + A.settle, t_b)
            n_cell += 1
            rec = {"t0": round(t_a, 2), "t1": round(t_b, 2), "mode": kind, "why": why}
            if cell is None:
                rec["skip"] = "нет отсчётов статики"
                log["cells"].append(rec)
                say(f"◐ ячейка «{kind}»: нет отсчётов статики №8 — не в счёт")
                continue
            v = cell["vec"]
            rec["vec"] = np.round(v, 3).tolist()
            ev = None
            if len(ref) >= 16:
                R = np.array(ref)
                med = np.median(R, axis=0)
                mad = np.maximum(1.4826 * np.median(np.abs(R - med), axis=0), FLOOR)
                z = (v - med) / mad
                rec["z"] = np.round(z, 2).tolist()
                sd_ref = np.median(R[:, :3], axis=0)
                mean_ref = np.median(R[:, -3:], axis=0)
                knock = cell["peak"].max() > max(8 * sd_ref.max(), 3000)
                shift = np.abs(cell["mean"] - mean_ref).max() > 2000
                if knock or shift:
                    rec["skip"] = "толчок" if knock else "сдвиг положения"
                    say(f"◐ №8 {rec['skip']} в ячейке «{kind}» — не в счёт, переиграю" +
                        (f"; пик {cell['peak'].max() / 1e6:.3f} g" if knock else ""))
                    if shift:
                        st.reframe(cell["x"])
                        ref.clear()
                    queue.insert(0, (why, kind))
                    log["cells"].append(rec)
                    continue
                j = int(np.argmax(np.abs(z)))
                if why == "пробежка" and abs(z[j]) >= A.z1:
                    ev = (j, z[j])
                    others = [k for k in MODES if k != kind]
                    rnd.shuffle(others)
                    queue[:0] = [("повтор", kind), ("между", others[0]), ("повтор", kind), ("между", others[1]),
                                 ("повтор", kind)]
                    log["events"].append({"t": t_a, "mode": kind, "metric": NAMES[j], "z": float(z[j]), "repeats": []})
                    say(f"⚠ ДЁРНУЛОСЬ: «{kind}» — {NAMES[j]} {v[j] - med[j]:+.1f} {UNIT[j]} (z {z[j]:+.1f}); повтор трижды")
                    beep(2)
                if why == "повтор" and log["events"]:
                    e = log["events"][-1]
                    jj = NAMES.index(e["metric"])
                    e["repeats"].append(float(z[jj]))
                    say(f"   повтор «{kind}»: {e['metric']} z {z[jj]:+.1f}")
                    if len(e["repeats"]) == 3:
                        same = sum(1 for q in e["repeats"] if q * e["z"] > 0 and abs(q) >= A.z2)
                        if same >= 2:
                            say(f"◐ ПОВТОРИЛОСЬ ({same} из 3): «{kind}» — {e['metric']}; удержание 4 × 30 с против «{last_mode or MODES[0]}»")
                            e["held"] = hold(st, kind, last_mode if last_mode and last_mode != kind else MODES[0], jj, e)
                        else:
                            say(f"✓ не повторилось ({same} из 3) — дальше")
            if not ev and why == "пробежка" and "skip" not in rec:
                ref.append(v)
                if len(ref) > A.ref:
                    ref.pop(0)
                hist[kind].append(v)
            log["cells"].append(rec)
            last_mode = kind
            if time.time() - t_stat > 300:                 # раз в 5 мин: сторожа и сводка по режимам
                t_stat = time.time()
                guard()
                summary(hist)
                json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
    finally:
        json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
        set_mode("стоят")
        req("/api/motors", {"mod": None})
        req("/api/motors", {"stop": True})
        req("/api/coils", {"off": True, "src": "пробежка режимов"})
        say(f"колёса, поле и счёт выключены; журнал: {path}")
        summary(hist, final=True)


def hold(st, kind, other, j, e):
    """Удержание: 4 пары по 30 с (этот режим, соседний); разность показателя j в каждой паре."""
    diffs = []
    for r in range(4):
        vals = {}
        for k in ((kind, other) if r % 2 == 0 else (other, kind)):
            t_a = time.time()
            set_mode(k)
            time.sleep(30)
            c = st.cell(t_a + A.settle, time.time())
            vals[k] = None if c is None else float(c["vec"][j])
        if vals.get(kind) is not None and vals.get(other) is not None:
            diffs.append(vals[kind] - vals[other])
        say(f"   удержание {r + 1}/4: «{kind}» − «{other}» по {NAMES[j]}: {diffs[-1]:+.2f} {UNIT[j]}" if len(diffs) == r + 1
            else f"   удержание {r + 1}/4: нет отсчётов")
    if len(diffs) >= 3:
        mu = S.mean(diffs)
        se = S.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else float("inf")
        t = mu / se if se > 0 else 0.0
        same = all(d * e["z"] > 0 for d in diffs)
        if same and abs(t) >= 3:
            say(f"★ КАНДИДАТ: «{kind}» — {NAMES[j]} {mu:+.2f} {UNIT[j]} (t {t:+.1f}, все {len(diffs)} пар одного знака) — запись в журнал")
            beep(3)
            return {"diffs": diffs, "t": t, "candidate": True}
        say(f"✓ удержание не подтвердило (t {t:+.1f}, знаки {'одинаковы' if same else 'разные'}) — дальше")
        return {"diffs": diffs, "t": t, "candidate": False}
    return {"diffs": diffs, "candidate": False}


def guard():
    w = req("/api/motors").get("wheels") or {}
    if max(abs((w.get(k) or {}).get("duty") or 0) for k in "ab") > 820:
        req("/api/motors", {"stop": True})
        say("✗ ШИМ > 820 ‰ — моторы стоп")
        beep(2)
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [ID[m] for m in ALL if m not in online]
    fs = field_seen()
    bat = {x.get("label"): x.get("soc") for x in (req("/api/battery").get("list") or []) if x.get("alive")}
    low = [k for k, v in bat.items() if v is not None and v <= 25]
    say(f"сторожа: обороты {[(w.get(k) or {}).get('meas') for k in 'ab']}, поле К1 {fs['1']} К2 {fs['2']} мкТл, "
        f"аккумуляторы {bat}" + (f", нет плат: {lost}" if lost else ""))
    if lost or low or any(v is not None and v < 200 for v in fs.values()):
        beep(2)


def summary(hist, final=False):
    """Режим против остальных по каждому показателю: t Уэлча; верх списка с поправкой на все сравнения."""
    rows = []
    allv = {k: np.array(v) for k, v in hist.items() if len(v) >= 3}
    for k, X in allv.items():
        Y = np.concatenate([v for kk, v in allv.items() if kk != k]) if len(allv) > 1 else None
        if Y is None or len(Y) < 6:
            continue
        se = np.sqrt(X.var(axis=0, ddof=1) / len(X) + Y.var(axis=0, ddof=1) / len(Y))
        t = (X.mean(axis=0) - Y.mean(axis=0)) / np.maximum(se, 1e-9)
        for j in np.argsort(-np.abs(t))[:2]:
            rows.append((abs(t[j]), k, NAMES[j], X.mean(axis=0)[j] - Y.mean(axis=0)[j], t[j], len(X)))
    if not rows:
        return
    m = len(NAMES) * max(1, len(allv))
    top = sorted(rows, reverse=True)[:3]
    say(("ИТОГ по режимам" if final else "сводка по режимам") + f" (сравнений {m}; поправка: |t| ≥ {abs(np.sqrt(2) * 3.9):.1f} грубо): "
        + "; ".join(f"«{k}» {nm} {d:+.2f} (t {t:+.1f}, ячеек {n})" for _, k, nm, d, t, n in top))


if __name__ == "__main__":
    main()
