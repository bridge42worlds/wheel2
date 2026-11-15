"""Повтор события статики (08.11): тот же режим, что в опыте №67 (колёса навстречу на ШИМ невесомости с качанием ±5 % /
20 с, поле К1+ К2− 650 ‰, 12 генераторов «каждая сама» 64 бит, код в шуме), и та же смена источника шума — туда и
обратно по нескольку раз. После опыта — «до/после» каждой смены по сырью статики №8: мощность в полосах, СКО, эксцесс,
средние (тяжесть) по осям в/г1/г2; сравнение парное по повторам.

    python tools/repeat_switch.py --pair 29:156 --pair 149:153 --reps 6 --hold 40
"""
import argparse
import json
import math
import os
import statistics as S
import subprocess
import sys
import time
import urllib.request

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
URL = "http://127.0.0.1:8090"
ap = argparse.ArgumentParser()
ap.add_argument("--pair", action="append", default=[], help="смена источника «из:в» (маски), повторяется туда-обратно")
ap.add_argument("--reps", type=int, default=6)
ap.add_argument("--hold", type=float, default=40.0, help="сколько держать каждый источник, с")
ap.add_argument("--level", type=int, default=650)
ap.add_argument("--amp", type=float, default=0.05)
ap.add_argument("--period", type=float, default=20.0)
ap.add_argument("--analyze", default=None, help="только разбор: путь к журналу смен (json)")
A = ap.parse_args()
MAC8 = "68ee8f5032f4"
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")


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


# ---------- разбор ----------
def load_raw(t0, t1):
    base = os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(t0)), MAC8)
    idx = [json.loads(l) for l in open(base + ".idx.jsonl", encoding="utf-8")]
    idx = [p for p in idx if t0 - 2 <= p["rx"] <= t1 + 2]
    i0 = idx[0]["i0"]
    n = idx[-1]["i0"] + idx[-1]["n"] - i0
    raw = np.fromfile(base + ".bin", dtype="<i2", count=n * 3, offset=i0 * 6).reshape(-1, 3).astype(np.float64)
    t = np.empty(len(raw))
    for p in idx:
        k = np.arange(p["n"])
        a = p["i0"] - i0
        t[a:a + p["n"]] = p["rx"] - (p["n"] - 1 - k) / (p.get("hz") or 500.0)
    return t, raw


def win_metrics(t, raw, frame, a, b):
    g0, basis = frame
    m = (t >= a) & (t < b)
    x = raw[m]
    if len(x) < 1000:
        return None
    d = (x - g0) @ basis.T * 62.5                       # мкg (16000 отсчётов на g)
    mean = d.mean(axis=0)
    c = d - mean
    sd = c.std(axis=0)
    kurt = (c ** 4).mean(axis=0) / np.maximum(sd ** 4, 1e-12) - 3
    fs = len(x) / max(t[m][-1] - t[m][0], 1e-6)
    nper = int(2 * fs)
    acc, k = 0, 0
    win = np.hanning(nper)
    for s in range(0, len(c) - nper + 1, nper // 2):
        acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * win[:, None], axis=0)) ** 2
        k += 1
    fr = np.fft.rfftfreq(nper, 1 / fs)
    bp = np.array([[10 * np.log10((acc / k)[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
    gabs = (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) * 62.5
    return {"mean": mean, "sd": sd, "kurt": kurt, "bp": bp, "g": gabs}


def analyze(log):
    sw = log["switches"]
    t, raw = load_raw(sw[0]["t"] - A.hold - 5, sw[-1]["t"] + A.hold + 5)
    g = raw.mean(axis=0)
    gu = g / np.linalg.norm(g)
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(gu, e1)
    frame = (g, np.stack([gu, e1, e2]))
    names = [f"{lo}–{hi} Гц {AX[ax]}" for ax in range(3) for lo, hi in BANDS] + [f"СКО {a}" for a in AX] + \
            [f"эксцесс {a}" for a in AX] + [f"среднее {a}" for a in AX] + ["тяжесть |g|"]
    print(f"\nРазбор: смен {len(sw)}; окно «до» — {A.hold - 3:.0f} с перед сменой, «после» — с 3-й по {A.hold:.0f}-ю секунду после")
    by = {}
    for s in sw:
        b0 = win_metrics(t, raw, frame, s["t"] - A.hold + 3, s["t"])
        b1 = win_metrics(t, raw, frame, s["t"] + 3, s["t"] + A.hold)
        if not b0 or not b1:
            continue
        v0 = np.concatenate([b0["bp"].ravel(), b0["sd"], b0["kurt"], b0["mean"], [b0["g"]]])
        v1 = np.concatenate([b1["bp"].ravel(), b1["sd"], b1["kurt"], b1["mean"], [b1["g"]]])
        by.setdefault(f"{s['from']}→{s['to']}", []).append(v1 - v0)
    m_all = len(names) * len(by)
    best = []
    for key, ds in by.items():
        D = np.array(ds)
        n = len(D)
        mu = D.mean(axis=0)
        se = D.std(axis=0, ddof=1) / math.sqrt(n) if n > 1 else np.full(len(names), np.inf)
        tt = mu / np.maximum(se, 1e-9)
        order = np.argsort(-np.abs(tt))
        print(f"\n  смена {key} ({n} раз): сильнее всего —")
        for j in order[:5]:
            # p по Стьюденту (n−1 степеней) — грубо через нормальное при n ≥ 6, иначе осторожно
            p = math.erfc(abs(tt[j]) / math.sqrt(2))
            print(f"     {names[j]:16s} Δ {mu[j]:+9.2f} ± {se[j]:7.2f}  t {tt[j]:+5.1f}  знак одинаков у {int(max((D[:, j] > 0).sum(), (D[:, j] < 0).sum()))}/{n}"
                  f"  p·m ≈ {min(1, p * m_all):.2g}")
            best.append((abs(tt[j]), key, names[j], mu[j], tt[j], int(max((D[:, j] > 0).sum(), (D[:, j] < 0).sum())), n))
    return best


def run():
    fleet = req("/api/fleet")["agents"]
    ids = {3, 5, 6, 11, 12, 20, 14, 15, 16, 17, 18, 19}
    gens = [b["mac"] for b in fleet if b["id"] in ids and b.get("online")]
    if len(gens) < 12:
        sys.exit(f"✗ на связи генераторов {len(gens)} из 12")
    pairs = [tuple(int(v) for v in p.split(":")) for p in (A.pair or ["29:156", "149:153"])]
    log = {"t_start": time.time(), "pairs": pairs, "hold": A.hold, "reps": A.reps, "switches": []}
    path = os.path.join(ROOT, "data", "series", time.strftime("repeat_%Y-%m-%d_%H%M.json"))
    try:
        print("генераторы: каждая сама, 64 бит, код в шуме …", flush=True)
        out = req("/api/control", {"targets": gens, "cfg": BASE,
                                   "own": {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": 64} for m in gens}})
        if not out.get("ok"):
            sys.exit(f"✗ {out.get('error')}")
        for m in gens:
            req("/api/cmd", {"mac": m, "line": "INFO on"})
        print(f"поле: К1+ К2− {A.level} ‰ …", flush=True)
        req("/api/coils", {"wheel": 1, "state": "+", "level": A.level, "ac": None, "src": "повтор события"})
        req("/api/coils", {"wheel": 2, "state": "-", "level": A.level, "ac": None, "src": "повтор события"})
        print("колёса: невесомость по ободу …", flush=True)
        req("/api/motors", {"mod": None})
        req("/api/motors", {"pair": "free"})
        req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": 1, "flip": 0, "zg": 0})
        req("/api/motors", {"wheel": "b", "mode": "zerog", "dir": -1, "flip": 0, "zg": 0})
        t0, duty = time.time(), {"a": [], "b": []}
        while time.time() - t0 < 100:
            time.sleep(1)
            w = req("/api/motors")["wheels"]
            if max(abs(w[k]["duty"]) for k in "ab") > 800:
                sys.exit("✗ ШИМ > 800 ‰ при разгоне — стоп")
            if time.time() - t0 > 50:
                for k in "ab":
                    if w[k].get("fb"):
                        duty[k].append(abs(w[k]["duty"]))
        if min(len(duty["a"]), len(duty["b"])) < 10:
            sys.exit("✗ регулятор не вышел на невесомость — стоп")
        base = {k: int(S.median(duty[k])) for k in "ab"}
        req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": base["a"]})
        req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": base["b"]})
        req("/api/motors", {"mod": {"a": A.amp, "T": A.period, "t0": time.time()}})
        log["base"] = base
        print(f"  ШИМ невесомости {base['a']}/{base['b']} ‰, качание ±{A.amp * 100:.0f} % / {A.period:g} с; установление 40 с", flush=True)
        time.sleep(40)
        for a_, b_ in pairs:
            req_ns(gens, a_)
            time.sleep(A.hold)
            for r in range(A.reps):
                for frm, to in ((a_, b_), (b_, a_)):
                    req_ns(gens, to)
                    log["switches"].append({"t": time.time(), "from": frm, "to": to, "rep": r + 1})
                    print(f"  {time.strftime('%H:%M:%S')} смена {frm} → {to} (повтор {r + 1})", flush=True)
                    time.sleep(A.hold)
        json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    finally:
        for m in gens:
            req("/api/cmd", {"mac": m, "line": "NS off"})
        req("/api/control", {"targets": gens, "cfg": {**BASE, "run": 0, "dc": 0}, "own": {m: {"role": 0, "ss": 0} for m in gens}})
        req("/api/motors", {"mod": None})
        req("/api/motors", {"stop": True})
        req("/api/coils", {"off": True, "src": "повтор события"})
        print("колёса, поле и счёт выключены", flush=True)
    return log, path


def req_ns(gens, mask):
    for m in gens:
        req("/api/cmd", {"mac": m, "line": f"NS {mask}"})


if __name__ == "__main__":
    if A.analyze:
        log = json.load(open(A.analyze, encoding="utf-8"))
    else:
        log, path = run()
        print(f"журнал смен: {path}", flush=True)
    best = analyze(log)
    strong = [b for b in best if b[0] >= 4 and b[5] == b[6]]
    print("\nИТОГ: " + ("◐ повторилось у: " + "; ".join(f"{k} {nm} Δ {mu:+.2f} (t {t:+.1f}, знак {s}/{n})" for _, k, nm, mu, t, s, n in strong[:4])
                        if strong else "✓ не повторилось — ни одна смена не даёт устойчивого отклонения статики (|t| ≥ 4 и один знак у всех повторов)"))
