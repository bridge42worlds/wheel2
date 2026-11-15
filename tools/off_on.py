"""Опора «всё выключено» и режим (пользователь 08.11 19:43: «всё отключи и померяй, потом опять включай этот режим —
тогда можешь сказать, что это опора»). Циклы: ВСЁ ВЫКЛ (колёса стоят, поле выкл, счёт стоит) — минута замера, затем
РЕЖИМ (колёса 670/−657 ‰ ровно, поле К1+ К2− 650 ‰, счёт эгоистичный, все пять) — минута замера. Разбор: форма
(СКО, асимметрия, эксцесс, высота горба против гаусса), линии 8, 50, 100 Гц и полосы по осям в/г1/г2 — режим против опоры.

    python tools/off_on.py [--cycles 3] [--measure 60] [--pwm 670,657]
"""
import argparse, json, math, os, time, urllib.request
import numpy as np

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--cycles", type=int, default=3)
ap.add_argument("--measure", type=float, default=60.0)
ap.add_argument("--spin", type=float, default=25.0, help="разгон/выбег колёс до замера, с")
ap.add_argument("--pwm", default="670,657")
ap.add_argument("--level", type=int, default=650)
A = ap.parse_args()
PA, PB = (int(v) for v in A.pwm.split(","))
MAC8 = "68ee8f5032f4"
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
AX = ("в", "г1", "г2")
BANDS = [(1, 4), (4, 16), (16, 64), (64, 128), (128, 250)]


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired")]
ALL = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
       and str(b.get("wheel")) in ("1", "2")]


def say(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


def all_off():
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "NS off"})
    req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
    req("/api/coils", {"off": True, "src": "опора: всё выкл"})
    req("/api/motors", {"mod": None})
    req("/api/motors", {"stop": True})


def mode_on():
    req("/api/coils", {"wheel": 1, "state": "+", "level": A.level, "ac": None, "src": "режим"})
    req("/api/coils", {"wheel": 2, "state": "-", "level": A.level, "ac": None, "src": "режим"})
    req("/api/motors", {"pair": "free"})
    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": PA})
    req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": PB})
    req("/api/control", {"targets": ALL, "cfg": {**BASE, "run": 1},
                         "own": {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": 64} for m in ALL}})
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "INFO on"})


def load(a, b):
    base = os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(a)), MAC8)
    sz = os.path.getsize(base + ".idx.jsonl")
    recs = []
    with open(base + ".idx.jsonl", "rb") as f:
        f.seek(max(0, sz - 25_000_000))
        f.readline()
        for ln in f:
            try:
                p = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if a - 2 <= p["rx"] <= b + 2:
                recs.append(p)
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
    return np.concatenate(ts), np.concatenate(xs)


def shape(t, x, frame, a, b):
    """Форма и спектр за [a, b): по окнам 5 с (среднее по окнам) — СКО, асимметрия, эксцесс, горб/гаусс; линии и полосы."""
    g0, basis = frame
    out = {"sd": [], "sk": [], "ku": [], "hump": [], "line8": [], "line50": [], "line100": [], "bands": []}
    for w0 in np.arange(a, b - 4.99, 5):
        m = (t >= w0) & (t < w0 + 5)
        if m.sum() < 1500:
            continue
        d = (x[m] - g0) @ basis.T * 62.5
        c = d - d.mean(axis=0)
        sd = c.std(axis=0)
        out["sd"].append(sd)
        out["sk"].append((c ** 3).mean(axis=0) / sd ** 3)
        out["ku"].append((c ** 4).mean(axis=0) / sd ** 4 - 3)
        hm = []
        for ax in range(3):
            h, e = np.histogram(c[:, ax], bins=31, range=(-4 * sd[ax], 4 * sd[ax]))
            gpk = len(c) * (e[1] - e[0]) / (sd[ax] * math.sqrt(2 * math.pi))
            hm.append(h[14:17].mean() / gpk)
        out["hump"].append(hm)
        fs = m.sum() / 5.0
        sp = np.abs(np.fft.rfft(c * np.hanning(len(c))[:, None], axis=0)) ** 2
        fr = np.fft.rfftfreq(len(c), 1 / fs)
        tot = sp[fr > 1].sum(axis=0)
        line = lambda f0, bw=1.5: sp[(fr > f0 - bw) & (fr < f0 + bw)].sum(axis=0) / tot
        out["line8"].append(line(8, 1.0))
        out["line50"].append(line(50))
        out["line100"].append(line(100))
        out["bands"].append([10 * np.log10(sp[(fr >= lo) & (fr < hi)].sum(axis=0) + 1e-9) for lo, hi in BANDS])
    return {k: np.array(v) for k, v in out.items()}


def main():
    phases = []
    try:
        for cyc in range(1, A.cycles + 1):
            say(f"цикл {cyc}: ВСЁ ВЫКЛ — колёса стоп, поле выкл, счёт стоит; выбег {A.spin:g} с, замер {A.measure:g} с")
            all_off()
            time.sleep(A.spin)
            t0 = time.time()
            time.sleep(A.measure)
            phases.append(("опора", cyc, t0, time.time()))
            w = req("/api/motors").get("wheels") or {}
            say(f"   обод {[round((w.get(k) or {}).get('meas') or 0, 1) for k in 'ab']} об/мин")
            say(f"цикл {cyc}: РЕЖИМ — колёса {PA}/−{PB} ‰ ровно, поле К1+ К2− {A.level} ‰, счёт эгоистичный · все пять; разгон {A.spin:g} с")
            mode_on()
            time.sleep(A.spin)
            t0 = time.time()
            time.sleep(A.measure)
            phases.append(("режим", cyc, t0, time.time()))
            w = req("/api/motors").get("wheels") or {}
            c = req("/api/coils")
            say(f"   обод {[round((w.get(k) or {}).get('meas') or 0, 1) for k in 'ab']} об/мин · поле "
                f"{[round(((c.get('sense') or {}).get(k) or {}).get('babs') or 0) for k in ('1', '2')]} мкТл")
    finally:
        json.dump({"phases": phases, "args": vars(A)}, open(os.path.join(ROOT, "data", "series",
                  time.strftime("off_on_%Y-%m-%d_%H%M.json")), "w", encoding="utf-8"), ensure_ascii=False)
        say("режим оставлен включённым (колёса, поле, счёт)")
    t, x = load(phases[0][2] - 5, phases[-1][3] + 5)
    sel = np.zeros(len(t), bool)
    for kind, cyc, a, b in phases:
        if kind == "опора":
            sel |= (t >= a) & (t < b)
    g = x[sel].mean(axis=0)
    gu = g / np.linalg.norm(g)
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))
    print("\nфаза      цикл | СКО в/г1/г2 мкg   | эксцесс в/г1/г2   | горб/гаусс в/г1/г2 | доля 100 Гц в/г1/г2 | 50 Гц | 8 Гц")
    res = []
    for kind, cyc, a, b in phases:
        s = shape(t, x, frame, a, b)
        if not len(s["sd"]):
            print(kind, cyc, "нет отсчётов")
            continue
        mv = {k: v.mean(axis=0) for k, v in s.items()}
        res.append((kind, cyc, mv))
        print(f"{kind:8s} {cyc:3d}  | {mv['sd'][0]:5.0f} {mv['sd'][1]:5.0f} {mv['sd'][2]:5.0f} | {mv['ku'][0]:+5.2f} {mv['ku'][1]:+5.2f} {mv['ku'][2]:+5.2f} | "
              f"{mv['hump'][0]:4.2f} {mv['hump'][1]:4.2f} {mv['hump'][2]:4.2f}     | {100 * mv['line100'][0]:3.0f} {100 * mv['line100'][1]:3.0f} {100 * mv['line100'][2]:3.0f} %      | "
              f"{100 * mv['line50'][0]:3.0f} % | {100 * mv['line8'][1]:3.0f} %")
    print("\nрежим − опора по циклам (разность, среднее и разброс по трём циклам):")
    for key, nm in (("sd", "СКО"), ("ku", "эксцесс"), ("hump", "горб/гаусс"), ("line100", "доля 100 Гц")):
        ds = np.array([r1[2][key] - r0[2][key] for r0, r1 in zip([r for r in res if r[0] == "опора"], [r for r in res if r[0] == "режим"])])
        if len(ds):
            mu = ds.mean(axis=0)
            se = ds.std(axis=0, ddof=1) / math.sqrt(len(ds)) if len(ds) > 1 else np.full(3, np.nan)
            print(f"   {nm:12s} " + " · ".join(f"{AX[i]} {mu[i]:+.3f} (t {mu[i] / se[i] if se[i] > 0 else float('nan'):+.1f})" for i in range(3)))


if __name__ == "__main__":
    main()
