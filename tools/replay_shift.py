"""Воспроизведение «сдвига» (07.11): повторить те же условия, при которых изменился наклон/среднее №8, и посмотреть,
повторится ли. Два вида:

    python tools/replay_shift.py seq 05:37:10 05:39:40 --rot same --n 3
        проиграть из журнала пробежки ту же последовательность ячеек (счёт + поле, те же длительности) при стоящих
        колёсах, затем запустить колёса (--rot) и 90 с смотреть №8; n раз с полем как было и n раз без поля (контроль)
    python tools/replay_shift.py cell 05:23:49 --n 10
        одну ячейку журнала (вращение, счёт, поле) повторить n раз по 10 с, между ними по 10 с «выкл»

Смотрим №8 каждые 2 с: наклон (°), среднее по осям (мкg), разброс; ступень — изменение среднего > 3 мg за ≤ 10 с.
Журнал: data/series/replay_<дата>.json.
"""
import argparse, json, math, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("kind", choices=["seq", "cell"])
ap.add_argument("times", nargs="+", help="ЧЧ:ММ:СС (seq: начало и конец окна; cell: время ячейки)")
ap.add_argument("--day", default=time.strftime("%Y-%m-%d"))
ap.add_argument("--rot", default="same", choices=["same", "opposite", "only1"])
ap.add_argument("--n", type=int, default=3)
ap.add_argument("--watch", type=float, default=90)
A = ap.parse_args()


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


fleet = req("/api/fleet")["agents"]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) == w] for w in ("1", "2")}
D3 = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if not b.get("retired") and b.get("kind") in ("A", "B")
      and str(b.get("wheel")) == "3"]
ALL = W["1"] + W["2"] + D3
N8 = next((b["mac"] for b in fleet if b.get("id") == 8), None)
BASE = {"src": 128, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 1}


def gen(kind):                                    # как в sweep.py
    own0 = {m: {"role": 0, "ss": 0} for m in ALL}
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
    elif kind.startswith("src"):
        src = int(kind[3:]); raw = 1 if src in (1, 8) else 0
        c2 = {**cfg, "src": src, "raw": raw, "ping": 100 if src == 2 else 0, "cf": 1 if src == 2 else 0}
        own = {m: {"role": 0, "ss": 0, "src": src, "raw": raw, "bits": 64} for m in ALL}
        req("/api/control", {"targets": ALL, "cfg": c2, "own": own})


def mot(kind):
    if kind == "stop":
        req("/api/motors", {"stop": True}); return
    req("/api/motors", {"pair": kind})
    req("/api/motors", {"wheel": "a", "mode": "zerog", "dir": 1, "flip": 0, "zg": 0})


def coil(c):
    if c["k1"] == "выкл" and c["k2"] == "выкл":
        req("/api/coils", {"off": True, "src": "повтор"}); return
    for w in ("1", "2"):
        st = c[f"k{w}"]
        if st == "выкл":
            req("/api/coils", {"wheel": w, "state": "off", "src": "повтор"}); continue
        ac = None
        if c.get("f") == "rot":
            ac = {"f": 1.2, "shape": c.get("shape", "sin"), "sync": {"k": 1, "deg": c.get("deg", 0)}}
        elif c.get("f"):
            ac = {"f": c["f"], "shape": c.get("shape", "sin")}
        req("/api/coils", {"wheel": w, "state": st, "level": c.get("level", 1000), "ac": ac, "src": "повтор"})


OFF = {"k1": "выкл", "k2": "выкл"}


def n8():
    s = next((x for x in (req("/api/static").get("sensors") or []) if x.get("mac") == N8), {}) or {}
    last = s.get("last") or {}
    return {"deg": (s.get("pose") or {}).get("deg"), "mean": last.get("mean"), "sd": last.get("sd"), "t": time.time()}


def watch(sec, tag, out):
    """Каждые 2 с — №8; ступень среднего > 3 мg (48 отсчётов при 16000/g) между соседними окнами — отметить."""
    t0 = time.time(); prev = None; steps = []
    while time.time() - t0 < sec:
        x = n8(); out.append({**x, "tag": tag})
        if prev and x["mean"] and prev["mean"]:
            d = [abs(a - b) for a, b in zip(x["mean"], prev["mean"])]
            if max(d) > 3000:                     # mean в мкg: 3 мg
                steps.append((round(time.time() - t0, 1), [round(v / 1000, 1) for v in d]))
        prev = x
        time.sleep(2)
    return steps


def hms(s):
    h, m, sec = map(int, s.split(":"))
    return time.mktime(time.strptime(f"{A.day} {h:02d}:{m:02d}:{sec:02d}", "%Y-%m-%d %H:%M:%S"))


L = json.load(open(os.path.join(ROOT, "data", "series", f"sweep_{A.day}.json"), encoding="utf-8"))
log_path = os.path.join(ROOT, "data", "series", f"replay_{time.strftime('%Y-%m-%d')}.json")
log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
trace = []
try:
    if A.kind == "seq":
        ta, tb = hms(A.times[0]), hms(A.times[1])
        cells = [r for r in L if ta <= r["t0"] <= tb]
        print(f"последовательность: {len(cells)} ячеек {A.times[0]}–{A.times[1]}, колёса стоят → {A.rot}; {A.n} с полем + {A.n} контроль")
        for trial in range(2 * A.n):
            with_field = trial % 2 == 0
            tag = f"{'поле' if with_field else 'контроль'} {trial // 2 + 1}"
            mot("stop"); coil(OFF); gen("off"); time.sleep(20)
            before = n8()
            steps_seq = []
            cur_gen = None
            for r in cells:
                if r["gen"] != cur_gen:
                    gen(r["gen"]); cur_gen = r["gen"]; time.sleep(3)
                coil(r["coil"] if with_field else OFF)
                steps_seq += watch(max(2.0, r["t1"] - r["t0"]), tag + " · стоят", trace)
            at_start = n8()
            mot(A.rot)
            steps_run = watch(A.watch, tag + " · старт", trace)
            after = n8()
            coil(OFF)
            rec = {"kind": "seq", "tag": tag, "with_field": with_field, "deg_before": before["deg"], "deg_at_start": at_start["deg"],
                   "deg_after": after["deg"], "mean_before": before["mean"], "mean_after": after["mean"],
                   "sd_before": before["sd"], "sd_after": after["sd"], "steps_standing": steps_seq, "steps_after_start": steps_run,
                   "t": time.time()}
            log.append(rec); json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False)
            dm = [round((a - b) / 1000, 2) for a, b in zip(after["mean"] or [0, 0], before["mean"] or [0, 0])]
            print(f"  {tag}: наклон {before['deg']}° → {at_start['deg']}° → {after['deg']}°; Δсреднего за опыт {dm} мg; "
                  f"ступени стоя {len(steps_seq)}, после старта {len(steps_run)} {steps_run[:3]}", flush=True)
    else:
        t = hms(A.times[0])
        r = min(L, key=lambda q: abs(q["t0"] - t))
        print(f"ячейка {time.strftime('%H:%M:%S', time.localtime(r['t0']))}: {r['rot']} · {r['gen']} · {r['field']} — {A.n} повторов по 10 с")
        mot(r["rot"] if r["rot"] != "stop" else "stop"); gen(r["gen"]); time.sleep(45 if r["rot"] != "stop" else 5)
        for i in range(A.n):
            coil(OFF); s0 = watch(10, f"повтор {i + 1} · выкл", trace)
            coil(r["coil"]); s1 = watch(10, f"повтор {i + 1} · поле", trace)
            x = n8()
            print(f"  повтор {i + 1}: наклон {x['deg']}°, ступени: выкл {len(s0)}, поле {len(s1)} {s1[:2]}", flush=True)
            log.append({"kind": "cell", "cell": r, "i": i, "steps_off": s0, "steps_on": s1, "deg": x["deg"], "t": time.time()})
        json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False)
finally:
    coil(OFF); mot("stop"); gen("off")
    json.dump({"trace": trace}, open(log_path.replace(".json", "_trace.json"), "w", encoding="utf-8"))
    print("стоп")
