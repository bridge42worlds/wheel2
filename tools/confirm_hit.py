"""Проверка находок нащупывания (08.11 19:37): 1) «одно число на всех» против «эгоистичного» — 4 пары по 30 с, обороты
по ободу каждые 2 с; 2) катушка К1 выкл/вкл по 30 с × 6 при включённой К2 — ступенька положения №8 у включения
(сдвиг 19:35:10 через 41 с после «К1 off → +»). Колёса — как есть (ровно), поле К2 постоянное, счёт — «все пять»."""
import json, math, os, statistics as S, sys, time, urllib.request
import numpy as np

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MAC8 = "68ee8f5032f4"
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
ONE = {"mode": 1, "gv": 0, "dc": 0}
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
HOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


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
OWN = {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": 64} for m in ALL}


def mode(kind):
    cfg = {**BASE, "run": 1, **(ONE if kind == "одно число" else {})}
    req("/api/control", {"targets": ALL, "cfg": cfg, "own": OWN})
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "NS 157"})


def hold(sec, log, tag):
    t0 = time.time()
    rpm = []
    while time.time() - t0 < sec:
        time.sleep(2)
        w = req("/api/motors").get("wheels") or {}
        rpm.append([(w.get(k) or {}).get("meas") for k in "ab"])
    log.append({"tag": tag, "t0": t0, "t1": time.time(), "rpm": rpm})


def load(a, b):
    base = os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(a)), MAC8)
    idx = [json.loads(l) for l in open(base + ".idx.jsonl", encoding="utf-8")]
    idx = [p for p in idx if a - 2 <= p["rx"] <= b + 2]
    i0 = idx[0]["i0"]
    n = idx[-1]["i0"] + idx[-1]["n"] - i0
    raw = np.fromfile(base + ".bin", dtype="<i2", count=n * 3, offset=i0 * 6).reshape(-1, 3).astype(np.float64)
    t = np.empty(len(raw))
    for p in idx:
        k = np.arange(p["n"])
        q = p["i0"] - i0
        t[q:q + p["n"]] = p["rx"] - (p["n"] - 1 - k) / (p.get("hz") or 500.0)
    return t, raw


def metrics(t, raw, frame, a, b):
    g0, basis = frame
    m = (t >= a) & (t < b)
    x = raw[m]
    d = (x - g0) @ basis.T * 62.5
    mean = d.mean(axis=0)
    c = d - mean
    sd = c.std(axis=0)
    ku = (c ** 4).mean(axis=0) / sd ** 4 - 3
    fs = len(x) / max(t[m][-1] - t[m][0], 1e-6)
    nper = int(2 * fs)
    acc, k = 0, 0
    for s in range(0, len(c) - nper + 1, nper // 2):
        acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * np.hanning(nper)[:, None], axis=0)) ** 2
        k += 1
    fr = np.fft.rfftfreq(nper, 1 / fs)
    bp = np.array([[10 * np.log10((acc / k)[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
    return mean, sd, ku, bp


def main():
    log = {"one": [], "coil": []}
    try:
        print(f"{time.strftime('%H:%M:%S')} 1) «одно число» против «эгоистичного»: 4 пары по {HOLD:g} с", flush=True)
        req("/api/coils", {"wheel": 1, "state": "+", "level": 650, "ac": None, "src": "проверка находки"})
        req("/api/coils", {"wheel": 2, "state": "-", "level": 650, "ac": None, "src": "проверка находки"})
        for r in range(4):
            for kind in (("одно число", "эгоистичный") if r % 2 == 0 else ("эгоистичный", "одно число")):
                mode(kind)
                hold(HOLD, log["one"], kind)
        mode("эгоистичный")
        print(f"{time.strftime('%H:%M:%S')} 2) катушка К1: выкл/вкл по {HOLD:g} с × 6 (К2 включена)", flush=True)
        for r in range(6):
            for st in ("off", "+"):
                if st == "off":
                    req("/api/coils", {"wheel": 1, "state": "off", "src": "проверка находки"})
                else:
                    req("/api/coils", {"wheel": 1, "state": "+", "level": 650, "ac": None, "src": "проверка находки"})
                hold(HOLD, log["coil"], "К1 " + ("выкл" if st == "off" else "вкл"))
    finally:
        req("/api/coils", {"wheel": 1, "state": "+", "level": 650, "ac": None, "src": "проверка находки"})
        mode("эгоистичный")
        json.dump(log, open(os.path.join(ROOT, "data", "series", time.strftime("confirm_%Y-%m-%d_%H%M.json")), "w",
                            encoding="utf-8"), ensure_ascii=False)
    # ---------- разбор ----------
    allb = log["one"] + log["coil"]
    t, raw = load(allb[0]["t0"], allb[-1]["t1"])
    g = raw.mean(axis=0)
    gu = g / np.linalg.norm(g)
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))
    print("\n1) «одно число» − «эгоистичный» по парам (блоки по 30 с, первые 3 с не в счёт):")
    rows = []
    for blk in log["one"]:
        mean, sd, ku, bp = metrics(t, raw, frame, blk["t0"] + 3, blk["t1"])
        rp = [r for r in blk["rpm"] if None not in r]
        rows.append((blk["tag"], sd, ku, bp, mean, np.mean(rp, axis=0) if rp else [np.nan, np.nan]))
        print(f"   {blk['tag']:12s} СКО {np.round(sd, 0)} эксц {np.round(ku, 2)} обод {np.round(rows[-1][5], 1)} об/мин")
    one = [r for r in rows if r[0] == "одно число"]
    ego = [r for r in rows if r[0] == "эгоистичный"]
    for nm, f in (("СКО г1", lambda r: r[1][1]), ("СКО в", lambda r: r[1][0]), ("СКО г2", lambda r: r[1][2]),
                  ("эксцесс г1", lambda r: r[2][1]), ("обод к1", lambda r: r[5][0]), ("обод к2", lambda r: r[5][1])):
        d = [f(a) - f(b) for a, b in zip(one, ego)]
        mu = S.mean(d)
        se = S.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else float("inf")
        print(f"   {nm:10s} разности {[round(x, 2) for x in d]} → {mu:+.2f} (t {mu / se if se > 0 else 0:+.1f})")
    print("\n2) катушка К1 выкл/вкл: ступенька положения №8 (среднее по осям, мкg) и формы")
    steps = []
    for i in range(1, len(log["coil"])):
        prev, cur = log["coil"][i - 1], log["coil"][i]
        m0 = metrics(t, raw, frame, prev["t1"] - 10, prev["t1"])
        m1 = metrics(t, raw, frame, cur["t0"] + 1, cur["t0"] + 11)
        m2 = metrics(t, raw, frame, cur["t1"] - 10, cur["t1"])
        steps.append((cur["tag"], m1[0] - m0[0], m2[0] - m0[0], m1[1] - m0[1]))
        print(f"   → {cur['tag']}: сразу Δсреднее {np.round(m1[0] - m0[0], 0)}, к концу {np.round(m2[0] - m0[0], 0)}, ΔСКО {np.round(m1[1] - m0[1], 0)}")
    for tag in ("К1 вкл", "К1 выкл"):
        ds = np.array([s[1] for s in steps if s[0] == tag])
        if len(ds) > 1:
            mu, se = ds.mean(axis=0), ds.std(axis=0, ddof=1) / math.sqrt(len(ds))
            print(f"   {tag}: среднее Δ {np.round(mu, 1)} мкg ± {np.round(se, 1)} (t {np.round(mu / np.maximum(se, 1e-9), 1)})")


if __name__ == "__main__":
    main()
