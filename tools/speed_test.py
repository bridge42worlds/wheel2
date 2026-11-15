"""Замер скорости счёта на одном колесе (без вращения и поля): «каждая плата сама», регистр ГСЧ без хеша —
длина числа (64 бита — свой код без ускорителя), журнал, таблицы отрезков, радио, способ возведения в квадрат. Заодно сверка: биты простых, посчитанные
платой (таблица отрезков), против журнала — должны совпасть до бита.

    python tools/speed_test.py              колесо 2, по 20 с на режим (~8 мин)
    python tools/speed_test.py --wheel 1 --sec 30
"""
import argparse, json, os, sys, time, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--wheel", default="2")
ap.add_argument("--sec", type=int, default=20)
A = ap.parse_args()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import nullmodel  # noqa: E402
import program as P  # noqa: E402


def req(p, b=None):
    r = urllib.request.Request(A.url + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


fleet = [b for b in req("/api/fleet")["agents"] if not b.get("retired") and b.get("kind") in ("A", "B")
         and b.get("slot") != "coil" and str(b.get("wheel")) == A.wheel]
live = {a["mac"]: a for a in req("/api/dash")["agents"] if a.get("mac")}
W = [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if (live.get(b["mac"]) or {}).get("status") == "online"]
if not W:
    sys.exit(f"колесо {A.wheel}: никого на связи")
# (имя, длина, журнал, таблицы, радио выкл, квадрат умножением[, отсев делителями до td — по умолчанию 1000])
CASES = [("256 · журнал + таблицы", 256, 1, 1, 0, 0), ("256 · таблицы", 256, 0, 1, 0, 0), ("256 · ничего", 256, 0, 0, 0, 0),
         ("256 · таблицы · радио выкл", 256, 0, 1, 1, 0), ("256 · таблицы · квадрат ×", 256, 0, 1, 0, 1),
         ("128 · таблицы", 128, 0, 1, 0, 0), ("128 · таблицы · квадрат ×", 128, 0, 1, 0, 1),
         ("128 · таблицы · радио выкл", 128, 0, 1, 1, 0),
         ("64 · таблицы (свой код)", 64, 0, 1, 0, 0), ("64 · ничего", 64, 0, 0, 0, 0),
         ("64 · таблицы · радио выкл", 64, 0, 1, 1, 0),
         ("64 · таблицы · отсев < 100", 64, 0, 1, 0, 0, 100), ("64 · таблицы · без отсева", 64, 0, 1, 0, 0, 0)]
OFF = {"1": "выкл", "2": "выкл"}
NOISE = {"src": 128, "raw": 1}
steps = []
seg = 10
for name, bits, pl, tab, ro, sq, *td in CASES:
    cfg = {"mode": 0, "dc": 1, "pl": pl, "bits": bits, "sq": sq, "ro": ro, "td": td[0] if td else 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0,
           "cf": 0, "det": 0, "ss": 0, **NOISE}
    own = {m: {"role": 0, "det": 0, "ss": 0, "xf": 4, "rw": 4, **NOISE} for m in W}
    n = max(1, A.sec // seg)
    st = [{"name": "выкл", "coil": OFF, "set": {"form": "выкл", "dir": "выкл", "round": "1"}} for _ in range(n)]
    steps.append({"name": name, "minutes": seg * n / 60, "control": False, "agent": True, "coil": OFF,
                  "fseq": {"seg": seg, "tab": bool(tab), "states": st},
                  "groups": [{"targets": W, "cfg": cfg, "own": own, "label": f"колесо {A.wheel} · {name}"}]})
PROG = {"name": f"замер скорости · колесо {A.wheel}", "baseline_min": 0, "repeat": 1, "shuffle": False, "seed": 1, "rest": 0,
        "z": 4, "confirm": 0, "redo": 0, "again": 0, "static_spoil": False, "metrics": [], "targets": W, "steps": steps}
P.validate(json.loads(json.dumps(PROG)))
r = req("/api/program/start", {"program": PROG})
if not r.get("ok"):
    sys.exit(f"✗ {r.get('error')}")
pid = r["id"]
print(f"программа №{pid}: {len(CASES)} режимов по {A.sec} с, плат {len(W)}", flush=True)
while True:
    time.sleep(5)
    x = req(f"/api/program?id={pid}")
    if x.get("state") not in ("running", "starting", "paused"):
        break
base = None
print(f"\n{'режим':30s} {'канд/с':>7s} {'пр/с':>6s} {'×':>5s} {'МР %':>5s}  на плату · таблиц · проверка бит")
for run in x.get("runs") or []:
    rows, qbs = [], {}
    for mac, a in (run.get("agents") or {}).items():
        g = a.get("gen") or {}
        if g.get("age") and g.get("role") == 0:
            rows.append((g["cand"] / g["age"], g["pr"] / g["age"], g.get("mrus", 0) / 1e4 / g["age"]))
            if g.get("qb"):
                qbs[mac] = (g["qb"], g.get("cid"), g.get("bits"))
    if not rows:
        print(f"{run['name']:30s} нет данных · не сдали {len(run.get('not_done') or [])}")
        continue
    n = len(rows)
    c, p, m = (sum(q[i] for q in rows) / n for i in range(3))
    base = base or p
    chk = ""
    if "журнал" in run["name"] and qbs:                # сверка с журналом: data/primes/<день>/<mac>.txt
        day = time.strftime("%Y-%m-%d", time.localtime(run["t0"]))
        ok = bad = 0
        for mac, (qb, cid, bits) in qbs.items():
            f = os.path.join(ROOT, "data", "primes", day, mac.replace(":", "") + ".txt")
            tot = [0, 0, 0, 0]
            if os.path.exists(f):                      # таблица кончается за доли секунды до остановки платы —
                rows = [q for q in (line.split() for line in open(f, encoding="utf-8", errors="replace"))
                        if len(q) >= 6 and q[2] == str(cid)]
                rows.sort(key=lambda q: int(q[1]))     # сверка с первыми N простыми журнала (по времени платы)
                for q in rows[:qb[0] // max(1, bits - 2)]:
                    for i, v in enumerate(nullmodel.prime_bits(q[5])):
                        tot[i] += v
            ok, bad = (ok + 1, bad) if tot == list(qb) else (ok, bad + 1)
        chk = f" · биты платы = журнал: ✓ {ok}" + (f", ✗ {bad}" if bad else "")
    th = [(mm.get("temp"), mm.get("work")) for sg in run.get("segs") or [] for k, mm in (sg.get("gen") or {}).items()
          if k.startswith("№") and isinstance(mm, dict)]
    tmax = max((t for t, _ in th if t is not None), default=None)
    wmin = min((w for _, w in th if w is not None), default=None)
    heat = (f" · кристалл до {tmax:.0f} °C" if tmax is not None else "") + (f", работа от {wmin:.0f} %" if wmin is not None else "")
    print(f"{run['name']:30s} {c:7.0f} {p:6.1f} {p / base:5.2f} {m:5.0f}  · таблиц {run.get('seg_tabs')}{heat}{chk}")
