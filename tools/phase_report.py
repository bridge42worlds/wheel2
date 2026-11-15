"""Разбор опыта с накоплением (tools/phase_run.py): один режим надолго, поле ∿ в середине, «выкл» на обоих концах.

    python tools/phase_report.py 35 36

По каждому показателю отрезка (статика каждого датчика; биты простых и сырьё у колеса с полем, если вели журнал):
y = a + b·t + c·поле — линейный дрейф (аккумуляторы, тепло) отделяется от поля по двум концам «выкл»; c — действие
поля, t по Стьюденту, поправка Бонферрони на все показатели. Накопление: то же по четвертям поля — растёт ли c от
начала поля к концу. Генерация за весь опыт — предсказанный ноль (выход, отсев, биты) и скорость.
"""
import argparse, json, math, os, statistics as S, sys

ap = argparse.ArgumentParser()
ap.add_argument("pids", type=int, nargs="+")
ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "programs"))
A = ap.parse_args()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import program as P  # noqa: E402

STATIC = ["p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "sd_v", "sd_h", "kurt_v", "kurt_h"]
GEN = ["z_pq", "z_ps", "q_rrg", "s_rrg", "temp", "work"]   # temp, work — тепло и торможение платы (ковариаты)


def ols(y, cols):
    """МНК: y = X·β; β, их стандартные ошибки и число степеней свободы (или None)."""
    n, k = len(y), len(cols[0])
    if n <= k + 1:
        return None
    xtx = [[sum(r[i] * r[j] for r in cols) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * v for r, v in zip(cols, y)) for i in range(k)]
    m = [row[:] + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(xtx)]   # обращение Гауссом
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(m[r][i]))
        if abs(m[p][i]) < 1e-12:
            return None
        m[i], m[p] = m[p], m[i]
        d = m[i][i]
        m[i] = [v / d for v in m[i]]
        for r in range(k):
            if r != i:
                f = m[r][i]
                m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    inv = [row[k:] for row in m]
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    res = [v - sum(b * x for b, x in zip(beta, r)) for r, v in zip(cols, y)]
    df = n - k
    s2 = sum(e * e for e in res) / df
    return beta, [math.sqrt(max(0.0, s2 * inv[i][i])) for i in range(k)], df


for pid in A.pids:
    x = json.load(open(os.path.join(A.dir, f"{pid}.json"), encoding="utf-8"))
    names = {}
    for r in x.get("runs") or []:
        names.update(r.get("snames") or {})
    for r in x.get("runs") or []:
        segs = r.get("segs") or []
        if not segs:
            continue
        st = r.get("set") or {}
        wheel = {w: {f"№{i}" for i in (st.get(f"К{w}") or "").split()} for w in ("1", "2")}
        k1 = wheel["1"]
        t_mid = S.mean(sg["t0"] for sg in segs)
        on_i = [i for i, sg in enumerate(segs) if (sg.get("set") or {}).get("form") != "выкл"]
        # две катушки: поле каждого колеса — своим столбцом (k1, k2 в наборе отрезка); иначе одно «поле»
        two = all("k1" in (sg.get("set") or {}) for sg in segs) and             {sg["set"]["k1"] for sg in segs} == {"0", "1"} and {sg["set"]["k2"] for sg in segs} == {"0", "1"}
        fx = [[1.0 if (sg.get("set") or {}).get("k1") == "1" else 0.0, 1.0 if (sg.get("set") or {}).get("k2") == "1" else 0.0]
              if two else [1.0 if i in on_i else 0.0] for i, sg in enumerate(segs)]
        eff = ["поле К1", "поле К2"] if two else ["поле"]
        series = {}                                   # показатель → [(отрезок, значение)]
        for i, sg in enumerate(segs):
            for mac, m in (sg.get("static") or {}).items():
                for key in STATIC:
                    if isinstance((m or {}).get(key), (int, float)):
                        series.setdefault(f"{names.get(mac, mac[-5:])} {key}", []).append((i, m[key]))
            g = sg.get("gen") or {}
            for w in ("1", "2"):                       # генерация — по платам каждого колеса
                for key in GEN:
                    v = [m[key] for c, m in g.items() if c in wheel[w] and isinstance((m or {}).get(key), (int, float))]
                    if v:
                        series.setdefault(f"К{w} {key}", []).append((i, sum(v) / len(v)))
        tests = []
        for name, pts in series.items():
            if len(pts) < 3 or S.pstdev(v for _, v in pts) < 1e-9:   # постоянный (доля работы 100 %) — проверять нечего
                continue
            # дрейф — кривой 3-й степени по времени (остывание и нагрев плат, аккумуляторы — не прямые): иначе поле,
            # включённое в середине опыта, принимает на себя изгиб дрейфа (05.11, №40: остывание после выкл радио)
            tt = lambda i: (segs[i]["t0"] - t_mid) / 600.0
            drift = lambda i: [1.0, tt(i), tt(i) ** 2, tt(i) ** 3]
            ND = 4
            cols = [drift(i) + fx[i] for i, _ in pts]
            fit = ols([v for _, v in pts], cols)
            if not fit:
                continue
            beta, se, df = fit
            for j, e in enumerate(eff):
                t = beta[ND + j] / se[ND + j] if se[ND + j] > 0 else 0.0
                # накопление: поле по четвертям (по времени его включения) — у каждой четверти свой столбец
                onj = [i for i in range(len(segs)) if fx[i][j]]
                q = len(onj) // 4 or 1
                quart = [onj[u * q:(u + 1) * q] for u in range(4)] if len(onj) >= 8 else []
                cq = []
                if quart:
                    rest = [fx[i][:j] + fx[i][j + 1:] for i in range(len(segs))]
                    cols4 = [drift(i) + rest[i] + [1.0 if i in qq else 0.0 for qq in quart] for i, _ in pts]
                    f4 = ols([v for _, v in pts], cols4)
                    if f4:
                        cq = f4[0][ND + len(rest[0]):]
                tests.append({"k": name, "e": e, "c": beta[ND + j], "t": t, "df": df, "p": P.t_p(t, df), "drift": beta[1] / 10.0,
                              "q": cq})
        m = len(tests)
        print(f"\n№{pid} «{r['name']}» · отрезков {len(segs)} (поле {len(on_i)}) · проверок {m}"
              + (" · поле К1 и К2 — отдельно" if two else ""))
        for q in sorted(tests, key=lambda q: q["p"])[:12]:
            pa = min(1.0, q["p"] * m)
            mark = "✓ достоверно" if pa <= 0.001 else "◐ намёк" if pa <= 0.05 else "·"
            unit = "дБ" if q["k"].split(" ", 1)[-1].startswith("p") and not q["k"].startswith("К") else                 "мкg" if q["k"].split(" ", 1)[-1].startswith("sd") else ""
            qs = " / ".join(f"{v:+.2g}" for v in q["q"]) if q["q"] else "—"
            print(f"  {mark:13s} {q['k']:16s} {q['e']:8s} {q['c']:+8.3f} {unit:3s} (t {q['t']:+5.1f}, p·m {pa:.2g}) · дрейф "
                  f"{q['drift']:+.3g}/мин · по четвертям {qs}")
        hits = [q for q in tests if q["p"] * m <= 0.05]
        print(f"  вывод: {'◐/✓ находок ' + str(len(hits)) if hits else '✓ достоверных аномалий нет, намёков нет'}")
        for w in ("1", "2"):
            g = r.get("gen") or {}
            kk = wheel[w]
            zs = [(abs(v), c, key) for c, mm in g.items() if c in kk for key, v in (mm or {}).items()
                  if key.startswith("z_") and isinstance(v, (int, float))]
            rate = [mm.get("pr_s") for c, mm in g.items() if c in kk and isinstance((mm or {}).get("pr_s"), (int, float))]
            cand = [mm.get("cand_s") for c, mm in g.items() if c in kk and isinstance((mm or {}).get("cand_s"), (int, float))]
            if rate:
                print(f"  генерация К{w}: {sum(rate) / len(rate):.1f} простых/с и {sum(cand) / max(1, len(cand)):.0f} канд/с на плату"
                      + (f" · предсказанный ноль: {len(zs)} проверок, наибольшее |z| {max(zs)[0]:.1f} ({max(zs)[1]} {max(zs)[2]})"
                         if zs else ""))
        lost = [(((r.get("agents") or {}).get(mac) or {}).get("gen") or {}).get("pll") for mac in (r.get("agents") or {})]
        if any(lost):
            print(f"  потеряно в журнале {sum(v for v in lost if v)}")
