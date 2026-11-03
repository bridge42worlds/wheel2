"""Разбор серии (tools/series_search.py) или любой программы по семьям: опыт против контролей своей семьи (тот же
control_for) — генерация (темп по часам агентов) и статика (показатели №… и телефона), Стьюдент с интервалом
предсказания и поправкой Бонферрони на число сравнений опыта; плюс вывод статики против эталона и толчки.

    python tools/series_report.py 23              программа №23 с сервера 8090
    python tools/series_report.py 23 --top 10     сколько сильнейших отклонений серии показать
"""
import argparse, json, math, os, statistics as S, sys, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("pid", type=int)
ap.add_argument("--url", default="http://127.0.0.1:8090")
ap.add_argument("--top", type=int, default=12)
A = ap.parse_args()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import program as P  # noqa: E402

x = json.load(urllib.request.urlopen(f"{A.url}/api/program?id={A.pid}", timeout=30))
runs = x.get("runs") or []
names = {}
for r in runs:
    names.update(r.get("snames") or {})


def gen_ch(r):
    g = r.get("gen") or {}
    out = {}
    for key, lab in (("все", "все"), ("колесо-пустышка", "пустышка"), ("все/опора", "к опоре")):
        for k in ("pr_s", "cand_s"):
            if (g.get(key) or {}).get(k) is not None:
                out[f"{lab} {'простых' if k == 'pr_s' else 'кандидатов'}"] = g[key][k]
    stx = sum((v or {}).get("stx_s") or 0 for k, v in g.items() if k.startswith("№"))
    if stx:
        out["кадров рассеяния"] = stx
    return out


def stat_ch(r):
    out = {}
    for mac, st in (r.get("static") or {}).items():
        for k, v in (st or {}).items():
            if k != "n" and isinstance(v, (int, float)):
                out[f"{names.get(mac, mac[-5:])} {k}"] = v
    return out


def change(q):
    """Генерация — в процентах; статика (полосы в дБ, эксцесс, асимметрия, среднее) — разностью."""
    if q["k"].split(" ")[-1].startswith(("p", "kurt", "skew", "mean", "sd")) and not q["k"].startswith(("все", "пустышка", "к опоре", "кадров")):
        return f"Δ {q['v'] - q['mu']:+.3g}"
    return f"{100 * (q['v'] / q['mu'] - 1):+.1f} %" if q["mu"] else f"Δ {q['v'] - q['mu']:+.3g}"


def tests(r, ctrl, fn):
    out = []
    v = fn(r)
    for k, val in v.items():
        base = [fn(c).get(k) for c in ctrl]
        base = [q for q in base if isinstance(q, (int, float))]
        if len(base) < 3:
            continue
        mu, sd = S.mean(base), S.stdev(base)
        if sd <= 0:
            continue
        t = (val - mu) / (sd * math.sqrt(1 + 1 / len(base)))
        out.append({"k": k, "t": t, "p": P.t_p(t, len(base) - 1), "v": val, "mu": mu, "n": len(base)})
    m = len(out)
    for q in out:
        q["p_adj"] = min(1.0, q["p"] * m)
    return sorted(out, key=lambda q: q["p"])


print(f"программа №{x.get('id')} «{(x.get('program') or {}).get('name', '')}» · {x.get('state')} · опытов {len(runs)} · "
      f"испорчено {sum(1 for r in runs if r.get('spoiled'))} · аномалий по программе {len(x.get('anomalies') or {})}")
allq = []
fam_of = lambda r: r.get("control_for") or (r["name"] if r.get("control") else None)
for i, r in enumerate(runs, 1):
    w = r.get("watch") or {}
    sv = " | ".join(f"{names.get(m, m[-5:])} {s.get('verdict')}" + (f": {(s.get('reasons') or [''])[0][:60]}" if s.get("reasons") else "")
                    for m, s in (w.get("sensors") or {}).items())
    head = f"{i:2d}. {'К' if r.get('control') else 'О'} {r['name'][:70]}"
    fam = fam_of(r)
    if r.get("control") or not fam:
        print(f"{head}\n      статика: {sv}")
        continue
    ctrl = [c for c in runs if c.get("control") and c["name"] == fam and not c.get("spoiled")]
    tg, ts = tests(r, ctrl, gen_ch), tests(r, ctrl, stat_ch)
    for q in tg + ts:
        allq.append((q["p_adj"], i, r["name"], q))
    fmt = lambda L: ", ".join(f"{q['k']} {change(q)} (t {q['t']:+.1f}, p·m {q['p_adj']:.2g})" for q in L[:2]) or "—"
    print(f"{head}\n      генерация: {fmt(tg)}\n      статика: {fmt(ts)}\n      статика к эталону: {sv}")
print(f"\n— сильнейшие отклонения серии (поправка на число сравнений опыта; порог z 4 ≈ p 6e-5) —")
for p_adj, i, name, q in sorted(allq, key=lambda z: z[0])[:A.top]:
    print(f"  {i:2d}. {q['k']}: {change(q)}, t {q['t']:+.1f}, p с поправкой {p_adj:.2g} · {name[:60]}")
