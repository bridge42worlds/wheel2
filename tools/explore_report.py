"""Разбор свободного поиска (tools/explore.py): журнал data/series/explore_<дата>.json.

    python tools/explore_report.py [дата] [--since ЧЧ:ММ]

1. Таблица блоков по наибольшему |z| №8 против опор того же состояния колёс (крутятся / стоят).
2. Факторы по именам блоков «вращение · поле · счёт» (случайная развёртка): среднее каждого показателя по уровню
   фактора при прочих равных — МНК с фиктивными переменными, t по остатку.
"""
import argparse, json, math, os, statistics as S, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("day", nargs="?", default=time.strftime("%Y-%m-%d"))
ap.add_argument("--since", default=None)
A = ap.parse_args()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P  # noqa: E402

log = json.load(open(os.path.join(ROOT, "data", "series", f"explore_{A.day}.json"), encoding="utf-8"))
if A.since:
    h, m = map(int, A.since.split(":"))
    t0 = time.mktime(time.strptime(f"{A.day} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M"))
    log = [r for r in log if r["t0"] >= t0]
KEYS = ["sd_v", "sd_h", "p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "kurt_v", "kurt_h"]
spin = lambda r: (r.get("rpm") or {}).get("1", 0) > 20 or (r.get("rpm") or {}).get("2", 0) > 20
good = [r for r in log if (r.get("n8") or {}).get("n") and (r["n8"].get("drop") or 0) <= 3]
print(f"{A.day}: блоков {len(log)}, чистых {len(good)}")

# ---------- факторы (блоки вида «a · b · c») ----------
rows = [r for r in good if r["name"].count(" · ") == 2 and " · " in r["name"]]
if len(rows) >= 12:
    levels = [sorted({r["name"].split(" · ")[i] for r in rows}) for i in range(3)]
    print("\nфакторы:", " × ".join("/".join(l) for l in levels), f"· блоков {len(rows)}")
    cols = []                                    # фиктивные переменные: все уровни, кроме первого
    names = []
    for i, lv in enumerate(levels):
        for l in lv[1:]:
            names.append(l)
            cols.append([1.0 if r["name"].split(" · ")[i] == l else 0.0 for r in rows])
    def fit(y):
        X = [[1.0] + [c[k] for c in cols] for k in range(len(y))]
        n, p = len(y), len(X[0])
        if n <= p + 1:
            return None
        xtx = [[sum(X[k][i] * X[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
        xty = [sum(X[k][i] * y[k] for k in range(n)) for i in range(p)]
        m = [row[:] + [1.0 if i == j else 0.0 for j in range(p)] for i, row in enumerate(xtx)]
        for i in range(p):
            piv = max(range(i, p), key=lambda r: abs(m[r][i]))
            if abs(m[piv][i]) < 1e-12:
                return None
            m[i], m[piv] = m[piv], m[i]
            d = m[i][i]; m[i] = [v / d for v in m[i]]
            for r in range(p):
                if r != i:
                    f = m[r][i]; m[r] = [a - f * b for a, b in zip(m[r], m[i])]
        inv = [row[p:] for row in m]
        beta = [sum(inv[i][j] * xty[j] for j in range(p)) for i in range(p)]
        res = [y[k] - sum(b * x for b, x in zip(beta, X[k])) for k in range(n)]
        s2 = sum(e * e for e in res) / (n - p)
        return beta, [math.sqrt(max(0.0, s2 * inv[i][i])) for i in range(p)], n - p
    out = []
    for key in KEYS + ["pr_s", "zy", "zt"]:
        if key in KEYS:
            y = [r["n8"].get(key) for r in rows]
        else:
            y = [(r.get("gen") or {}).get(key) for r in rows]
        ok = [k for k, v in enumerate(y) if isinstance(v, (int, float))]
        if len(ok) < 12:
            continue
        yy = [y[k] for k in ok]
        if S.pstdev(yy) == 0:
            continue
        save = cols
        cols = [[c[k] for k in ok] for c in save]
        f = fit(yy)
        cols = save
        if not f:
            continue
        beta, se, df = f
        for j, nm in enumerate(names):
            t = beta[1 + j] / se[1 + j] if se[1 + j] > 0 else 0.0
            out.append((abs(t), key, nm, beta[1 + j], t, P.t_p(t, df)))
    out.sort(reverse=True)
    m = len(out)
    print(f"уровень фактора против первого (сравнений {m}); ✓ p·m ≤ 0,001, ◐ p·m ≤ 0,05:")
    for a, key, nm, b, t, p in out[:14]:
        pa = min(1.0, p * m)
        mark = "✓" if pa <= 0.001 else "◐" if pa <= 0.05 else "·"
        print(f"  {mark} {key:6s} {nm:20s} {b:+9.3f}  t {t:+5.1f}  p·m {pa:.2g}")

# ---------- все блоки по отклонению №8 от опор своего состояния ----------
def refs(r):
    return [q["n8"] for q in good if spin(q) == spin(r) and q is not r
            and (q["name"].startswith("опора") or "без поля · без счёта" in q["name"] or q["name"].startswith("вращение · ничего")
                 or q["name"] in ("выкл", "стоят · выкл", "стоп · ничего"))]
tab = []
for r in good:
    ref = refs(r)
    if len(ref) < 4:
        continue
    zs = []
    for k in KEYS:
        v = [x[k] for x in ref if isinstance(x.get(k), (int, float))]
        if len(v) >= 4 and isinstance(r["n8"].get(k), (int, float)) and S.stdev(v) > 0:
            zs.append((abs((r["n8"][k] - S.mean(v)) / S.stdev(v)), k, r["n8"][k] - S.mean(v)))
    if zs:
        z = max(zs)
        tab.append((z[0], time.strftime("%H:%M", time.localtime(r["t0"])), r["name"], z[1], z[2], len(ref)))
tab.sort(reverse=True)
print(f"\nблоки по наибольшему |z| №8 (всего {len(tab)}, медиана {S.median(t[0] for t in tab):.2f}):")
for z, hm, name, k, d, n in tab[:10]:
    print(f"  z {z:4.1f}  {hm} {name[:44]:44s} {k:7s} {d:+8.2f}  (опор {n})")
