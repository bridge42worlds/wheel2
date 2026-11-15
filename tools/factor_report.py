"""Разбор факторной серии с полем внутри опыта (tools/series_factor.py): разности состояний поля внутри опыта,
проверка по всем опытам сразу.

    python tools/factor_report.py 31 32 33        программы блоков (data/programs/<номер>.json)
    python tools/factor_report.py 31 --all        и разведка: все показатели, по шуму и обработке

В опыте режим, шум и обработка постоянны, а поле перебирает состояния (выкл, = + −, = + +, ∿/⟳ + −, ∿/⟳ + +) по
отрезкам, два круга. Внутри опыта считаются разности: поле вкл − выкл, форма (∿/⟳ − =), + + − + −, взаимодействие
формы и направления — дрейф (температура, аккумуляторы, время) за секунды отрезков в них не входит. Разности всех
опытов — выборка: Стьюдент по числу опытов, поправка Бонферрони на все главные проверки (показатели × эффекты из
плана). Вывод — по правилу плана: достоверно (p с поправкой ≤ 0,001, знак тот же во всех блоках, у статики — и у
второго датчика в ту же сторону), намёк (p с поправкой ≤ 0,05), остальное — разведка.
"""
import argparse, json, math, os, statistics as S, sys

ap = argparse.ArgumentParser()
ap.add_argument("pids", type=int, nargs="+")
ap.add_argument("--all", action="store_true", help="разведка: все показатели, по шуму и обработке")
ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "programs"))
A = ap.parse_args()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import program as P  # noqa: E402

STATIC = ["p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "sd_v", "sd_h", "kurt_v", "kurt_h"]
GEN = ["все/опора pr_s", "z_pq", "z_ps", "q_ шума", "s_ шума"]
EFFECTS = ["поле вкл − выкл", "форма", "+ + − + −", "форма × направление"]
SRC_OF = {"внутренний": "rrg", "внешний": "csi"}
V_COIL = 6.5                                    # драйвер катушки ниже этого не включается


def gen_vals(g, noise, fake, keep=None, ctrl=()):
    """Генерация отрезка: опора, биты простых и сырьё шума — среднее по платам колёс (не пустышки). keep — платы колёс
    с живой катушкой (None — все); ctrl — платы колеса без поля: те же показатели отдельно, «контроль без поля»."""
    out = {}
    if (g.get("все/опора") or {}).get("pr_s") is not None:
        out["все/опора pr_s"] = g["все/опора"]["pr_s"]
    k = SRC_OF.get(noise)
    for name, key in (("z_pq", "z_pq"), ("z_ps", "z_ps"), ("q_ шума", f"q_{k}"), ("s_ шума", f"s_{k}")):
        for pre, ok in (("", lambda c: keep is None or c in keep), ("без поля ", lambda c: c in ctrl)):
            v = [m[key] for c, m in g.items() if c.startswith("№") and c not in fake and ok(c)
                 and isinstance((m or {}).get(key), (int, float))]
            if v:
                out[pre + name] = sum(v) / len(v)
    if A.all:
        for c in ("все", "колесо-пустышка"):
            for key, v in (g.get(c) or {}).items():
                if isinstance(v, (int, float)):
                    out[f"{c} {key}"] = v
    return out


def static_vals(st, names):
    out = {}
    for mac, m in (st or {}).items():
        for key, v in (m or {}).items():
            if (key in STATIC or A.all and key not in ("n", "drop")) and isinstance(v, (int, float)):
                out[f"{names.get(mac, mac[-5:])} {key}"] = v
    return out


# ---------- опыты ----------
runs, names, nulls, progs, dead = [], {}, [], [], []
for pid in A.pids:
    x = json.load(open(os.path.join(A.dir, f"{pid}.json"), encoding="utf-8"))
    progs.append(x)
    if x.get("null"):
        nulls.append((pid, x["null"]))
    for r in x.get("runs") or []:
        names.update(r.get("snames") or {})
    for r in x.get("runs") or []:
        if r.get("spoiled") or not r.get("segs"):
            continue
        st = r.get("set") or {}
        fake = {c[4:] for c in r.get("dummy_ch") or ()}
        # где было поле: катушка на связи в конце опыта и аккумулятор не ниже V_COIL (иначе драйвер не включается)
        wheel = {f"№{i}": w for w in ("1", "2") for i in (st.get(f"К{w}") or "").split()}
        bat = r.get("bat")
        live = None if bat is None or not wheel else \
            {b.get("wheel") for b in bat.values() if b.get("kind") == "coil" and (b.get("v") or 0) >= V_COIL}
        keep, ctrl = None, set()
        if live is not None and live != {"1", "2"}:
            on = {c for c, w in wheel.items() if w in live}
            gens = {c for sg in r["segs"] for c, m in (sg.get("gen") or {}).items() if c in wheel
                    and ((m or {}).get("pr_s") or (m or {}).get("cand_s") or (m or {}).get("z_pq") is not None)}
            if gens & on:                              # генераторы есть на колесе с полем — главное по ним, остальные — контроль
                keep, ctrl = on, set(wheel) - on
            dead.append(f"№{pid} «{r['name']}»: поле только на {', '.join('К' + w for w in sorted(live)) or 'нигде'}")
        states = {}
        for sg in r["segs"]:
            ss = sg.get("set") or {}
            vals = {**static_vals(sg.get("static"), names), **gen_vals(sg.get("gen") or {}, st.get("noise"), fake, keep, ctrl)}
            states.setdefault((ss.get("form"), ss.get("dir")), []).append(vals)
        runs.append({"pid": pid, "block": st.get("block") or str(pid), "noise": st.get("noise"), "proc": st.get("proc"), "rot": st.get("rot"),
                     "name": r["name"], "states": states})
if not runs:
    sys.exit("нет опытов с полем внутри (у опытов нет segs)")
forms = sorted({f for r in runs for f, _ in r["states"] if f != "выкл"}, key=lambda f: f != "постоянное")
F0, F1 = (forms + [None, None])[:2]
D0, D1 = "+ −", "+ +"


def mean_of(r, f, d, k):
    v = [q[k] for q in r["states"].get((f, d), []) if k in q]
    return sum(v) / len(v) if v else None


def contrasts(r, k):
    """Разности опыта по показателю k: {эффект: значение} — что посчиталось."""
    m = {(f, d): mean_of(r, f, d, k) for f in (F0, F1) for d in (D0, D1)}
    off = mean_of(r, "выкл", "выкл", k)
    out = {}
    if all(v is not None for v in m.values()):
        if off is not None:
            out["поле вкл − выкл"] = sum(m.values()) / 4 - off
        out["форма"] = (m[(F1, D0)] + m[(F1, D1)] - m[(F0, D0)] - m[(F0, D1)]) / 2
        out["+ + − + −"] = (m[(F0, D1)] + m[(F1, D1)] - m[(F0, D0)] - m[(F1, D0)]) / 2
        out["форма × направление"] = (m[(F1, D1)] - m[(F1, D0)]) - (m[(F0, D1)] - m[(F0, D0)])
    return out


keys = sorted({k for r in runs for vs in r["states"].values() for q in vs for k in q})
primary = [k for k in keys if k.split(" ", 1)[-1] in STATIC or k in GEN]
tests = []
for k in keys:
    for e in EFFECTS:
        per = [(r, contrasts(r, k).get(e)) for r in runs]
        per = [(r, v) for r, v in per if v is not None]
        if len(per) < 3:
            continue
        d = [v for _, v in per]
        mu, sd = S.mean(d), S.stdev(d)
        if sd <= 0:
            continue
        t = mu / (sd / math.sqrt(len(d)))
        by = {}
        for r, v in per:
            by.setdefault(r["block"], []).append(v)
        tests.append({"k": k, "e": e, "n": len(d), "mu": mu, "t": t, "p": P.t_p(t, len(d) - 1), "primary": k in primary,
                      "per": per, "blocks": {b: S.mean(v) for b, v in by.items()}})
m_primary = sum(1 for q in tests if q["primary"])
for q in tests:
    q["p_adj"] = min(1.0, q["p"] * max(1, m_primary)) if q["primary"] else None


def sensor_of(k):
    return k.split(" ", 1)[0] if k.split(" ", 1)[-1] in STATIC else None


def verdict(q):
    """✓ достоверно · ◐ намёк · · разведка — по правилу плана."""
    if not q["primary"]:
        return "·"
    same = all(math.copysign(1, v) == math.copysign(1, q["mu"]) for v in q["blocks"].values())
    other_ok = True
    s = sensor_of(q["k"])
    if s:                                       # статика: второй датчик — в ту же сторону и хоть сколько-то заметно
        rest = q["k"].split(" ", 1)[1]
        others = [o for o in tests if o["e"] == q["e"] and sensor_of(o["k"]) not in (None, s) and o["k"].split(" ", 1)[1] == rest]
        other_ok = any(math.copysign(1, o["mu"]) == math.copysign(1, q["mu"]) and o["p"] <= 0.05 for o in others)
    if q["p_adj"] <= 0.001 and same and other_ok:
        return "✓ достоверно"
    return "◐ намёк" if q["p_adj"] <= 0.05 else "·"


def unit(k):
    rest = k.split(" ", 1)[-1]
    if not sensor_of(k):
        return ""
    return "дБ" if rest.startswith("p") else "мкg" if rest.startswith(("sd", "mean")) else ""


fl = {"форма": f"{'∿' if F1 == 'на частоте вращения' else '⟳'} − =" if F1 else "форма"}
print(f"программы {', '.join(str(p) for p in A.pids)} · опытов {len(runs)} · главных проверок {m_primary} · план: "
      f"{', '.join(sorted({(x['program'].get('plan_sha') or 'черновик')[:8] for x in progs}))}")
for e in EFFECTS:
    rows = sorted([q for q in tests if q["e"] == e and q["primary"]], key=lambda q: q["p"])
    print(f"\n— {fl.get(e, e)} —")
    for q in rows[:6 if not A.all else 12]:
        bl = " / ".join(f"{b} {v:+.3g}" for b, v in sorted(q["blocks"].items())) if len(q["blocks"]) > 1 else ""
        print(f"  {verdict(q):13s} {q['k']:24s} {q['mu']:+9.3f} {unit(q['k']):3s} (опытов {q['n']}, t {q['t']:+5.1f}, "
              f"p·m {q['p_adj']:.2g}{', блоки ' + bl if bl else ''})")
hits = [q for q in tests if q["primary"] and verdict(q).startswith("✓")]
hint = [q for q in tests if q["primary"] and verdict(q).startswith("◐")]
print(f"\nвывод: {'✓ достоверных ' + str(len(hits)) if hits else '✓ достоверных аномалий нет'}"
      f"{' · ◐ намёков ' + str(len(hint)) if hint else ''}")
ctl = sorted([q for q in tests if q["k"].startswith("без поля ") and q["e"] == "поле вкл − выкл"], key=lambda q: q["p"])
if dead:
    print(f"\n◐ катушка без поля в опытах: {len(dead)} — " + "; ".join(dead[:3]) + (" …" if len(dead) > 3 else ""))
if ctl:                                         # колесо без катушки — контроль: у него разности поля должны быть нулём
    print("  контроль без поля (колесо без катушки), поле вкл − выкл: "
          + " · ".join(f"{q['k'][9:]} {q['mu']:+.3f} (t {q['t']:+.1f}, p {q['p']:.2g})" for q in ctl))
if A.all:                                       # разведка: зависит ли действие поля от шума и обработки
    print("\n— разведка: поле вкл − выкл по шуму, обработке и вращению (среднее разностей опытов) —")
    for q in sorted([q for q in tests if q["e"] == "поле вкл − выкл"], key=lambda q: q["p"])[:8]:
        by = {}
        for r, v in q["per"]:
            by.setdefault(r["noise"], []).append(v)
            by.setdefault(r["proc"], []).append(v)
            if r["rot"]:                        # набор 2: навстречу / в одну сторону
                by.setdefault(r["rot"], []).append(v)
        print(f"  {q['k']:24s} " + " · ".join(f"{g} {S.mean(v):+.3g}" for g, v in by.items()))
for pid, nl in nulls:
    sm = ", ".join(f"{k} {v['mean']:+.2f}/{v['sd']:.2f}" for k, v in (nl.get("summary") or {}).items())
    print(f"предсказанный ноль №{pid}: z {nl.get('tests')}, находок {len(nl.get('hits') or [])}"
          f"{', ошибок счёта ' + str(len(nl['det'])) if nl.get('det') else ''}{' · ' + sm if sm else ''}")
