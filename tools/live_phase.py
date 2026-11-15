"""Промежуточный разбор идущего опыта phase_run по статике — не дожидаясь конца шага.

    python tools/live_phase.py [номер программы]      по умолчанию — идущая

Берёт расписание поля шага (fseq) и начало шага (now.t0), режет статику №8 (и №9, если есть) на отрезки через
/api/static/seg и считает ту же регрессию, что phase_report: y = a + дрейф (куб) + c1·поле К1 + c2·поле К2;
печатает верх списка по |t| с поправкой Бонферрони на все показатели. Счёт плат по ходу не виден (таблицы сдаются
после шага) — здесь только статика.
"""
import json, math, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import program as P  # noqa: E402

KEYS = ["sd_v", "sd_h", "p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "kurt_v", "kurt_h"]


def get(p):
    return json.load(urllib.request.urlopen(URL + p, timeout=20))


def ols(y, cols):
    n, k = len(y), len(cols[0])
    if n <= k + 1:
        return None
    xtx = [[sum(r[i] * r[j] for r in cols) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * v for r, v in zip(cols, y)) for i in range(k)]
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(xtx)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(m[r][i]))
        if abs(m[p][i]) < 1e-12:
            return None
        m[i], m[p] = m[p], m[i]
        d = m[i][i]; m[i] = [v / d for v in m[i]]
        for r in range(k):
            if r != i:
                f = m[r][i]; m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    inv = [row[k:] for row in m]
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    res = [v - sum(b * x for b, x in zip(beta, r)) for r, v in zip(cols, y)]
    df = n - k
    s2 = sum(e * e for e in res) / df
    return beta, [math.sqrt(max(0.0, s2 * inv[i][i])) for i in range(k)], df


pid = int(sys.argv[1]) if len(sys.argv) > 1 else None
if pid is None:
    progs = get("/api/programs")
    progs = progs if isinstance(progs, list) else progs.get("list", progs)
    run = [p for p in progs if p.get("state") == "running"]
    if not run:
        sys.exit("идущей программы нет")
    pid = run[0]["id"]
d = get(f"/api/program?id={pid}")
steps = (d.get("program") or {}).get("steps") or []
now = d.get("now") or {}
fs = (steps[-1].get("fseq") if steps else None) or {}
states, seg = fs.get("states") or [], fs.get("seg") or 60
t0 = now.get("t0")
if not states or not t0:
    sys.exit(f"№{pid}: расписания поля или начала шага нет (now={now})")
t_apply = t0 + 3.0                                  # настройка уходит через ~3 с после начала шага
tnow = time.time()
segs = []
for k, sx in enumerate(states):
    a, b = t_apply + k * seg, t_apply + (k + 1) * seg
    if b > tnow - 5:
        break
    segs.append({"k": k, "t0": a, "t1": b, "set": sx.get("set") or {}, "name": sx.get("name")})
if len(segs) < 8:
    sys.exit(f"№{pid}: прошло отрезков {len(segs)} — мало для разбора (нужно ≥ 8)")
names = {}
for a in get("/api/fleet")["agents"]:
    names[a["mac"]] = f"№{a['id']}"
two = {s["set"].get("k1") for s in segs} >= {"0", "1"} and {s["set"].get("k2") for s in segs} >= {"0", "1"}
for s in segs:
    s["m"] = get(f"/api/static/seg?t0={s['t0']}&t1={s['t1']}") or {}
t_mid = sum(s["t0"] for s in segs) / len(segs)
tests = []
macs = sorted({m for s in segs for m in s["m"]})
for mac in macs:
    for key in KEYS:
        pts = [(i, s["m"][mac].get(key)) for i, s in enumerate(segs) if isinstance(s["m"].get(mac), dict) and isinstance(s["m"][mac].get(key), (int, float))
               and not (s["m"][mac].get("drop") or 0) > 20]
        if len(pts) < 8:
            continue
        tt = lambda i: (segs[i]["t0"] - t_mid) / 600.0
        cols = []
        for i, _ in pts:
            fx = [1.0 if segs[i]["set"].get("k1") == "1" else 0.0, 1.0 if segs[i]["set"].get("k2") == "1" else 0.0] if two \
                else [1.0 if segs[i]["set"].get("form", "выкл") != "выкл" else 0.0]
            cols.append([1.0, tt(i), tt(i) ** 2, tt(i) ** 3] + fx)
        fit = ols([v for _, v in pts], cols)
        if not fit:
            continue
        beta, se, df = fit
        for j, e in enumerate(["поле К1", "поле К2"] if two else ["поле"]):
            t = beta[4 + j] / se[4 + j] if se[4 + j] > 0 else 0.0
            tests.append((abs(t), f"{names.get(mac, mac[-5:])} {key}", e, beta[4 + j], t, P.t_p(t, df)))
m = len(tests)
on = sum(1 for s in segs if s["set"].get("form", "выкл") != "выкл")
print(f"№{pid} по ходу: отрезков {len(segs)} (поле {on}) из {len(states)} · проверок {m} · {time.strftime('%H:%M')}")
for a, k, e, c, t, p in sorted(tests, reverse=True)[:8]:
    pa = min(1.0, p * m)
    mark = "✓" if pa <= 0.001 else "◐" if pa <= 0.05 else "·"
    unit = "дБ" if k.split()[-1].startswith("p") else "мкg" if k.split()[-1].startswith("sd") else ""
    print(f"  {mark} {k:14s} {e:8s} {c:+9.3f} {unit:3s} t {t:+5.1f}  p·m {pa:.2g}")
hits = [x for x in tests if x[5] * m <= 0.05]
print("  пока: " + ("◐/✓ " + str(len(hits)) if hits else "аномалий и намёков нет"))
