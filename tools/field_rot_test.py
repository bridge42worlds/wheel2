"""Проверка находки 09.11 03:59 (сессия 1, часть 4): при вращении навстречу «поле выкл» против «∿1,2 Гц в фазе» —
полоса 4–8 Гц г1 выше (+0,69 дБ, t +6,7, 4 пары по 30 с). Три опыта:
  1) вращение: ∿1,2 Гц / выкл по 60 с × 3;
  2) колёса стоят: то же — если эффект пропадает, он идёт через вращение;
  3) вращение: частота поля 0,6 / 1,2 / 2,4 / 5 Гц против выкл (по 45 с) — сильнее около частоты вращения (1,2 Гц) —
     взаимодействие поля с вращающимся колесом.
Счёт — эгоистичный, «все кроме регистра» (как у подтверждённой пробы). Статика — через rawidx (сумма n, не i0).

    python tools/field_rot_test.py [--block 60] [--cycles 3]
"""
import argparse, json, math, os, statistics as S, sys, time, urllib.request
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawidx  # noqa: E402

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--block", type=float, default=60.0)
ap.add_argument("--cycles", type=int, default=3)
ap.add_argument("--scan-block", type=float, default=45.0)
ap.add_argument("--pwm", default="687,658")
ap.add_argument("--only", default=None, help="только эти опыты: 1,2,3")
A = ap.parse_args()
PA, PB = (int(v) for v in A.pwm.split(","))
MAC8 = "68:ee:8f:50:32:f4"
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def say(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


fleet = [b for b in req("/api/fleet").get("agents", []) if not b.get("retired")]
ALL = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
       and str(b.get("wheel")) in ("1", "2")]


def counting():
    req("/api/control", {"targets": ALL, "cfg": {**BASE, "run": 1},
                         "own": {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": 64} for m in ALL}})
    for m in ALL:
        req("/api/cmd", {"mac": m, "line": "INFO on"})
        req("/api/cmd", {"mac": m, "line": "NS 29"})               # все кроме регистра


def field(f):
    if f is None:
        req("/api/coils", {"off": True, "src": "проверка находки"})
        return
    for w in (1, 2):
        req("/api/coils", {"wheel": w, "state": "+", "level": 650, "ac": {"f": f, "shape": "sin"}, "src": "проверка находки"})


def wheels(on):
    if on:
        req("/api/motors", {"mod": None})
        req("/api/motors", {"pair": "free"})
        req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": 1, "pwm": PA})
        req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": -1, "pwm": PB})
        time.sleep(30)
    else:
        req("/api/motors", {"stop": True})
        t0 = time.time()
        while time.time() - t0 < 90:
            w = req("/api/motors").get("wheels") or {}
            if max(abs((w.get(k) or {}).get("meas") or 0) for k in "ab") < 3 and time.time() - t0 > 8:
                break
            time.sleep(2)
        time.sleep(10)


def block(label, sec, log):
    t0, rpm = time.time(), []
    while time.time() - t0 < sec:
        time.sleep(2)
        w = req("/api/motors").get("wheels") or {}
        rpm.append([(w.get(k) or {}).get("meas") for k in "ab"])
    log.append({"label": label, "t0": t0, "t1": time.time(), "rpm": rpm})


def metrics(a, b, frame):
    t, x = rawidx.load(MAC8, a, b)
    if t is None or len(x) < 1000:
        return None
    g0, basis = frame
    d = (x - g0) @ basis.T * 62.5
    c = d - d.mean(axis=0)
    sd = c.std(axis=0)
    ku = (c ** 4).mean(axis=0) / sd ** 4 - 3
    fs = len(x) / max(t[-1] - t[0], 1e-6)
    nper = int(2 * fs)
    acc, k = 0, 0
    for s in range(0, len(c) - nper + 1, nper // 2):
        acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * np.hanning(nper)[:, None], axis=0)) ** 2
        k += 1
    fr = np.fft.rfftfreq(nper, 1 / fs)
    bp = np.array([[10 * np.log10((acc / k)[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
    return {"sd": sd, "ku": ku, "bp": bp, "g": (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) * 62.5}


def compare(title, blocks, frame, on_label, off_label):
    rows = {}
    for bl in blocks:
        m = metrics(bl["t0"] + 3, bl["t1"], frame)
        if m:
            rows.setdefault(bl["label"], []).append(m)
    on, off = rows.get(on_label, []), rows.get(off_label, [])
    n = min(len(on), len(off))
    if not n:
        print(f"{title}: мало блоков")
        return
    print(f"\n{title}: «{on_label}» − «{off_label}», пар {n}")
    out = []
    for ax in range(3):
        for j, (lo, hi) in enumerate(BANDS):
            d = [on[i]["bp"][ax][j] - off[i]["bp"][ax][j] for i in range(n)]
            mu = S.mean(d)
            se = S.stdev(d) / math.sqrt(n) if n > 1 else float("inf")
            out.append((abs(mu / se) if se > 0 else 0, f"{lo}–{hi} Гц {AX[ax]}", mu, mu / se if se > 0 else 0, d))
    for _, nm, mu, t, d in sorted(out, reverse=True)[:6]:
        print(f"   {nm:14s} Δ {mu:+.2f} дБ (t {t:+.1f}) по парам {[round(v, 2) for v in d]}")
    g = [on[i]["g"] - off[i]["g"] for i in range(n)]
    print(f"   тяжесть |g|: Δ {S.mean(g):+.1f} мкg по парам {[round(v, 1) for v in g]}")
    r_on = [S.mean(v[0] for v in bl["rpm"] if v[0]) for bl in blocks if bl["label"] == on_label and any(v[0] for v in bl["rpm"])]
    r_off = [S.mean(v[0] for v in bl["rpm"] if v[0]) for bl in blocks if bl["label"] == off_label and any(v[0] for v in bl["rpm"])]
    if r_on and r_off:
        print(f"   обороты колеса 1: с полем {S.mean(r_on):.2f}, без {S.mean(r_off):.2f} об/мин")


def main():
    only = {int(v) for v in A.only.split(",")} if A.only else {1, 2, 3}
    log = {"exp1": [], "exp2": [], "exp3": []}
    try:
        counting()
        if 1 in only:
            say(f"1) вращение навстречу: ∿1,2 Гц / выкл по {A.block:g} с × {A.cycles}")
            wheels(True)
            for c in range(A.cycles):
                for f, lab in ((1.2, "∿1,2 Гц"), (None, "выкл")):
                    field(f)
                    block(lab, A.block, log["exp1"])
        if 2 in only:
            say(f"2) колёса стоят: ∿1,2 Гц / выкл по {A.block:g} с × {A.cycles}")
            wheels(False)
            for c in range(A.cycles):
                for f, lab in ((1.2, "∿1,2 Гц"), (None, "выкл")):
                    field(f)
                    block(lab, A.block, log["exp2"])
        if 3 in only:
            say(f"3) вращение: частота поля 0,6 / 1,2 / 2,4 / 5 Гц против выкл, по {A.scan_block:g} с")
            wheels(True)
            for f in (0.6, 1.2, 2.4, 5.0):
                field(f)
                block(f"∿{f:g} Гц", A.scan_block, log["exp3"])
                field(None)
                block("выкл", A.scan_block, log["exp3"])
    finally:
        field(None)
        req("/api/motors", {"stop": True})
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": "NS off"})
        req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
        json.dump(log, open(os.path.join(ROOT, "data", "series", time.strftime("field_rot_%Y-%m-%d_%H%M.json")), "w",
                            encoding="utf-8"), ensure_ascii=False, default=float)
        say("проверка закончена: колёса, поле и счёт выключены")
    allb = log["exp1"] + log["exp2"] + log["exp3"]
    t, x = rawidx.load(MAC8, allb[0]["t0"], allb[-1]["t1"])
    g = x.mean(axis=0)
    gu = g / np.linalg.norm(g)
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))
    if log["exp1"]:
        compare("1) вращение", log["exp1"], frame, "∿1,2 Гц", "выкл")
    if log["exp2"]:
        compare("2) колёса стоят", log["exp2"], frame, "∿1,2 Гц", "выкл")
    if log["exp3"]:
        print("\n3) частота поля при вращении: каждая против соседнего «выкл» — полоса 4–8 Гц г1 и сильнейшая")
        for i in range(0, len(log["exp3"]) - 1, 2):
            on, off = log["exp3"][i], log["exp3"][i + 1]
            m1, m0 = metrics(on["t0"] + 3, on["t1"], frame), metrics(off["t0"] + 3, off["t1"], frame)
            if m1 and m0:
                d = m1["bp"] - m0["bp"]
                ax, j = np.unravel_index(np.argmax(np.abs(d)), d.shape)
                print(f"   {on['label']:9s}: 4–8 Гц г1 Δ {d[1][2]:+.2f} дБ · сильнее всего {BANDS[j][0]}–{BANDS[j][1]} Гц {AX[ax]} Δ {d[ax][j]:+.2f} дБ")


if __name__ == "__main__":
    main()
