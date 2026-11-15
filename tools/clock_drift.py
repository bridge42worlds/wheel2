"""Ход часов агентов по уже записанным данным: смещение «время прихода на сервер − миллисекунды платы» до молчания
опыта и после него. Ход за опыт = −Δсмещения / Δвремени сервера (ppm, + — часы платы спешат). Задержка Wi-Fi только
положительная — смещение окна берётся по нижней огибающей (самые быстрые пакеты).

    python tools/clock_drift.py 28 29 30 32 33 34 36

Часы сервера общие для всех плат: смотрится и ход относительно медианы плат того же опыта (разность — без сервера).
"""
import argparse, bisect, json, os, statistics as S, sys
from datetime import datetime

ap = argparse.ArgumentParser()
ap.add_argument("pids", type=int, nargs="+")
ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
ap.add_argument("--q", type=int, default=3, help="смещение окна — среднее k наименьших")
A = ap.parse_args()

runs, wins = [], []                                # окна: (t0, t1, номер опыта, сторона)
for pid in A.pids:
    x = json.load(open(os.path.join(A.dir, "programs", f"{pid}.json"), encoding="utf-8"))
    for r in x.get("runs") or []:
        if not r.get("segs"):
            continue
        seg = r["segs"][0]["t1"] - r["segs"][0]["t0"]
        ta, t1 = r["t_apply"], r["t1"]
        ids = {m: a.get("id") for m, a in (r.get("agents") or {}).items()}
        k1 = {int(i) for i in ((r.get("set") or {}).get("К1") or "").split()}
        k2 = {int(i) for i in ((r.get("set") or {}).get("К2") or "").split()}
        i = len(runs)
        runs.append({"pid": pid, "name": r["name"], "ta": ta, "t1": t1, "ids": ids, "k1": k1, "k2": k2,
                     "set": r.get("set") or {}, "agents": r.get("agents") or {}, "off": {}})
        wins.append((ta - 30, ta + seg - 1, i, "до"))
        wins.append((t1 + 1, t1 + 120, i, "после"))       # после молчания все сдают журналы — эфир забит, выброс бывает
if not runs:
    sys.exit("нет опытов с отрезками")
wins.sort()
starts = [w[0] for w in wins]
lo, hi = wins[0][0], max(w[1] for w in wins)
days = sorted({datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t in (lo, hi)})
pts = {}                                           # (опыт, сторона, mac) → [(смещение, rx, ms)]
for d in days:
    f = os.path.join(A.dir, "raw", f"{d}.jsonl")
    if not os.path.exists(f):
        continue
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith('{"rx": '):
                continue
            try:
                rx = float(line[7:line.index(",", 7)])
            except ValueError:
                continue
            if rx < lo or rx > hi:
                continue
            j = bisect.bisect_right(starts, rx) - 1
            hit = [w for w in wins[max(0, j - 3):j + 1] if w[0] <= rx <= w[1]]
            if not hit or '\\"ms\\":' not in line:
                continue
            try:
                p = json.loads(json.loads(line)["raw"])
            except Exception:                      # noqa: BLE001
                continue
            if p.get("dev") == "phone" or "ms" not in p or "mac" not in p:
                continue
            for _, _, i, side in hit:
                pts.setdefault((i, side, p["mac"]), []).append((rx - p["ms"] / 1000.0, rx, p["ms"]))

res = []
for i, r in enumerate(runs):
    rates = {}
    for mac, iid in r["ids"].items():
        b, a = pts.get((i, "до", mac)), pts.get((i, "после", mac))
        if not b or not a or iid is None:
            continue
        if min(q[2] for q in a) < max(q[2] for q in b):   # перезапуск (миллисекунды пошли с нуля) — ход не мерить
            continue
        kb = sorted(b)[:A.q]
        ka = sorted(a)[:A.q]
        ob, tb = S.mean(q[0] for q in kb), S.mean(q[1] for q in kb)
        oa, ta = S.mean(q[0] for q in ka), S.mean(q[1] for q in ka)
        if ta - tb < 60:
            continue
        rates[iid] = {"ppm": -(oa - ob) / (ta - tb) * 1e6, "dt": ta - tb, "nb": len(b), "na": len(a),
                      "temp": ((r["agents"].get(mac) or {}).get("temp"))}
    if len(rates) < 3:
        continue
    med = S.median(v["ppm"] for v in rates.values())
    for iid, v in rates.items():
        v["rel"] = v["ppm"] - med
    r["rates"] = rates
    res.append(r)

# ход каждой платы: постоянный уход кварца (среднее по опытам) и отклонения опыта от него
by = {}
for r in res:
    for iid, v in r["rates"].items():
        by.setdefault(iid, []).append(v["rel"])
print(f"опытов {len(res)} · ход часов платы относительно медианы плат того же опыта, ppm (+ — спешат)")
print("плата  опытов  средний уход   разброс между опытами")
base = {}
for iid in sorted(by):
    v = by[iid]
    base[iid] = S.mean(v)
    print(f"  №{iid:<3} {len(v):5d}   {S.mean(v):+8.2f}       {S.stdev(v) if len(v) > 1 else 0:6.2f}")
print("\nсерверные часы против плат (медиана хода плат, ppm) и выбросы опытов: |отклонение от своего ухода| > 4·разброс")
sd = {iid: (S.stdev(v) if len(v) > 2 else None) for iid, v in by.items()}
for r in res:
    med = S.median(v["ppm"] for v in r["rates"].values())
    odd = [f"№{iid} {v['rel'] - base[iid]:+.1f}" for iid, v in r["rates"].items()
           if sd[iid] and abs(v["rel"] - base[iid]) > 4 * sd[iid]]
    print(f"  №{r['pid']} {r['name'][:52]:52s} медиана {med:+7.2f} · плат {len(r['rates'])}"
          + (f" · ◐ {', '.join(odd)}" if odd else ""))
# колесо с полем против колеса без поля: разность средних отклонений (от своего ухода) по опытам
d = []
for r in res:
    a = [v["rel"] - base[iid] for iid, v in r["rates"].items() if iid in r["k1"]]
    b = [v["rel"] - base[iid] for iid, v in r["rates"].items() if iid in r["k2"]]
    if a and b:
        d.append((S.mean(a) - S.mean(b), r))
if len(d) > 2:
    v = [q[0] for q in d]
    t = S.mean(v) / (S.stdev(v) / len(v) ** 0.5) if S.stdev(v) > 0 else 0
    print(f"\nК1 (поле) − К2 (без поля), отклонение хода от своего ухода: {S.mean(v):+.2f} ppm · разброс {S.stdev(v):.2f} · "
          f"опытов {len(v)} · t {t:+.1f}")
    for blk in sorted({q[1]["set"].get("block") for q in d}):
        vb = [q[0] for q in d if q[1]["set"].get("block") == blk]
        print(f"   блок {blk}: {S.mean(vb):+.2f} ppm ({len(vb)} опытов)")
