"""Разбор этапного опыта (tools/stage_run.py): статика — главное, счёт — контроль.

    python tools/stage_report.py <номер программы> [--no-raw] [--mac 68:ee:8f:50:32:f4]

1. Статика по этапам (0 · A · B · D · E; в C датчик не мерит): средние показателей отрезков, разности B−A и D−B,
   поле (A, B, D) против 0 и E: y = a + дрейф (куб по времени) + c·поле.
2. Статика по источникам шума (этапы A и B): y = a + дрейф (куб) + этап B + сочетание (опора «все пять»), t по каждому,
   поправка Бонферрони на все проверки.
3. Статика по фазе качания (сырые отсчёты): цикл делится на 16 долей; в каждой — среднее по осям (вертикаль по тяжести
   и две горизонтали) и мощность в полосах 2–8, 8–32, 32–128 Гц; по этапам. Плавная часть (1-я и 2-я гармоники —
   механика от скорости) и остаток сверх неё: острый отклик в момент невесомости отдельных плат был бы в остатке.
4. Счёт: биты простых и выход по этапам и сочетаниям, колёса против пустышек.
"""
import argparse, glob, json, math, os, statistics as S, sys, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("pid", type=int)
ap.add_argument("--no-raw", action="store_true", help="без сырых отсчётов (без разбора по фазе качания)")
ap.add_argument("--mac", default="68:ee:8f:50:32:f4", help="статический датчик (№8)")
ap.add_argument("--bins", type=int, default=16)
ap.add_argument("--test-phase", action="store_true", help="проверка разбора по фазе на старом опыте: этап A у всех, качание 20 с")
A = ap.parse_args()

KEYS = ["sd_v", "sd_h", "p3_v", "p4_v", "p5_v", "p3_h", "p4_h", "p5_h", "kurt_v", "kurt_h"]
x = json.load(open(os.path.join(ROOT, "data", "programs", f"{A.pid}.json"), encoding="utf-8"))
segs = [sg for r in x.get("runs") or [] for sg in (r.get("segs") or [])]
gens_meta = {}
for r in x.get("runs") or []:
    for g in r.get("groups") or []:
        gens_meta[g.get("label") or ""] = g.get("targets") or []
log = []
for f in glob.glob(os.path.join(ROOT, "data", "series", "stage_*.json")):
    log += json.load(open(f, encoding="utf-8"))
meta = next((m for m in log if m.get("pid") == A.pid), {})
if A.test_phase:
    meta = {"period": 20.0, "mot": {"t0": segs[0]["t0"], "amp": 0.05}}
    for sg in segs:
        sg.setdefault("set", {})["stage"] = sg.get("set", {}).get("stage") or "A"
print(f"№{A.pid} «{x.get('name') or (x.get('program') or {}).get('name')}» · {x.get('state')} · отрезков {len(segs)}"
      + (f" · качание ±{meta['mot']['amp'] * 100:.1f} % / {meta['period']:g} с" if meta.get("mot") else ""))
if not segs:
    sys.exit("отрезков нет")
STAGE_ORDER = ["0", "A", "B", "C", "D", "E"]
t_mid = S.mean(sg["t0"] for sg in segs)


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
        d = m[i][i]
        m[i] = [v / d for v in m[i]]
        for r in range(k):
            if r != i:
                f = m[r][i]
                m[r] = [a - f * b for a, b in zip(m[r], m[i])]
    inv = [row[k:] for row in m]
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
    res = [v - sum(b * c for b, c in zip(beta, r)) for r, v in zip(cols, y)]
    df = n - k
    s2 = sum(e * e for e in res) / df
    return beta, [math.sqrt(max(0.0, s2 * inv[i][i])) for i in range(k)], df


def drift(sg):
    t = (sg["t0"] - t_mid) / 600.0
    return [1.0, t, t * t, t ** 3]


def mark(pa):
    return "✓ достоверно" if pa <= 0.001 else "◐ намёк" if pa <= 0.05 else "·"


# ---------- 1. статика по этапам ----------
st_segs = [sg for sg in segs if isinstance((sg.get("static") or {}).get(A.mac), dict) and (sg["static"][A.mac] or {}).get("sd_v") is not None]
print(f"\n1. Статика №8 по этапам (отрезков со статикой {len(st_segs)}):")
by = {}
for sg in st_segs:
    by.setdefault((sg.get("set") or {}).get("stage", "?"), []).append(sg)
print("   этап  отр.  " + "  ".join(f"{k:>8s}" for k in ("sd_v", "sd_h", "p4_v", "p4_h", "kurt_v")))
for stg in STAGE_ORDER:
    if stg in by:
        row = []
        for k in ("sd_v", "sd_h", "p4_v", "p4_h", "kurt_v"):
            v = [sg["static"][A.mac].get(k) for sg in by[stg] if isinstance(sg["static"][A.mac].get(k), (int, float))]
            row.append(f"{S.mean(v):8.2f}" if v else "       —")
        print(f"   {stg:4s}  {len(by[stg]):4d}  " + "  ".join(row))
tests = []
for key in KEYS:
    pts = [(sg, sg["static"][A.mac].get(key)) for sg in st_segs if isinstance(sg["static"][A.mac].get(key), (int, float))]
    if len(pts) < 12:
        continue
    # поле вкл/выкл (этапы 0 и E — выкл) и этапы B, D против A — при кубическом дрейфе
    cols = [drift(sg) + [1.0 if (sg.get("set") or {}).get("k1") == "1" else 0.0,
                         1.0 if (sg.get("set") or {}).get("stage") == "B" else 0.0,
                         1.0 if (sg.get("set") or {}).get("stage") == "D" else 0.0] for sg, _ in pts]
    f = ols([v for _, v in pts], cols)
    if not f:
        continue
    beta, se, df = f
    for j, name in ((4, "поле вкл"), (5, "B − A"), (6, "D − A")):
        if se[j] > 0:
            t = beta[j] / se[j]
            tests.append((key, name, beta[j], t, P.t_p(t, df)))
m = len(tests)
print(f"   разности при кубическом дрейфе (проверок {m}):")
for key, name, b, t, p in sorted(tests, key=lambda q: q[4])[:10]:
    print(f"   {mark(min(1, p * m)):13s} {key:7s} {name:9s} {b:+9.3f}  t {t:+5.1f}  p·m {min(1, p * m):.2g}")

# ---------- 2. статика по источникам (A и B) ----------
ab = [sg for sg in st_segs if (sg.get("set") or {}).get("stage") in ("A", "B")]
combos = sorted({(sg.get("set") or {}).get("combo") for sg in ab} - {None, "все пять"})
print(f"\n2. Статика по источникам шума (A и B, отрезков {len(ab)}, опора «все пять»):")
tests2 = []
for key in KEYS:
    pts = [(sg, sg["static"][A.mac].get(key)) for sg in ab if isinstance(sg["static"][A.mac].get(key), (int, float))]
    if len(pts) < len(combos) + 8:
        continue
    cols = [drift(sg) + [1.0 if (sg.get("set") or {}).get("stage") == "B" else 0.0]
            + [1.0 if (sg.get("set") or {}).get("combo") == c else 0.0 for c in combos] for sg, _ in pts]
    f = ols([v for _, v in pts], cols)
    if not f:
        continue
    beta, se, df = f
    for j, c in enumerate(combos):
        if se[5 + j] > 0:
            t = beta[5 + j] / se[5 + j]
            tests2.append((key, c, beta[5 + j], t, P.t_p(t, df)))
m2 = len(tests2)
for key, c, b, t, p in sorted(tests2, key=lambda q: q[4])[:10]:
    print(f"   {mark(min(1, p * m2)):13s} {key:7s} {c:22s} {b:+9.3f}  t {t:+5.1f}  p·m {min(1, p * m2):.2g}")
if not tests2:
    print("   мало отрезков")

# ---------- 3. статика по фазе качания (сырые отсчёты) ----------
if not A.no_raw and meta.get("mot"):
    import numpy as np
    T, t0m, NB = meta["period"], meta["mot"]["t0"], A.bins
    day = time.strftime("%Y-%m-%d", time.localtime(segs[0]["t0"]))
    base = os.path.join(ROOT, "data", "accel", day, A.mac.replace(":", ""))
    idx = [json.loads(l) for l in open(base + ".idx.jsonl", encoding="utf-8")]
    raw = np.fromfile(base + ".bin", dtype="<i2").reshape(-1, 3).astype(np.float64)
    fs = 500.0
    t_all = np.empty(len(raw))
    for p in idx:                                     # время отсчёта: приход пакета минус хвост пакета
        k = np.arange(p["n"])
        t_all[p["i0"]:p["i0"] + p["n"]] = p["rx"] - (p["n"] - 1 - k) / fs
    stage_of = np.full(len(raw), "", dtype=object)
    for sg in segs:
        sel = (t_all >= sg["t0"]) & (t_all < sg["t1"])
        stage_of[sel] = (sg.get("set") or {}).get("stage", "")
    g = raw[stage_of != ""].mean(axis=0)
    gu = g / np.linalg.norm(g)                        # вертикаль — по тяжести; горизонтали — ортонормированно
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(gu, e1)
    lsb = 16000.0 / 1e6                               # отсчётов на мкg… (16000 на 1 g) → мкg = отсч / 0,016
    proj = np.stack([(raw - g) @ gu, (raw - g) @ e1, (raw - g) @ e2], axis=1) / lsb   # мкg
    phase = ((t_all - t0m) / T) % 1.0
    win = int(fs * T / NB)
    bands = [(2, 8), (8, 32), (32, 128)]
    print(f"\n3. Статика по фазе качания: цикл {T:g} с на {NB} долей, отсчётов {len(raw)}")
    for stg in [s for s in STAGE_ORDER if s != "C"]:
        sel_idx = np.where(stage_of == stg)[0]
        if len(sel_idx) < fs * T * 4:
            continue
        # куски по доле цикла: непрерывные отрезки в пределах одной доли
        cyc = np.floor((t_all[sel_idx] - t0m) / T * NB).astype(np.int64)
        prof = {q: [] for q in range(NB)}
        starts = np.r_[0, np.where(np.diff(cyc) != 0)[0] + 1]
        ends = np.r_[starts[1:], len(cyc)]
        for a, b in zip(starts, ends):
            if b - a < win * 0.8:
                continue
            ii = sel_idx[a:b]
            q = int(cyc[a] % NB)
            seg = proj[ii]
            spec = np.abs(np.fft.rfft(seg - seg.mean(axis=0), axis=0)) ** 2
            fr = np.fft.rfftfreq(len(seg), 1 / fs)
            bp = [10 * np.log10(spec[(fr >= lo) & (fr < hi)].sum(axis=0) + 1e-9) for lo, hi in bands]
            prof[q].append(np.concatenate([seg.mean(axis=0), seg.std(axis=0), np.ravel(bp)]))
        n_cyc = min(len(v) for v in prof.values())
        if n_cyc < 4:
            continue
        M = np.array([np.mean(prof[q], axis=0) for q in range(NB)])
        E = np.array([np.std(prof[q], axis=0) / math.sqrt(len(prof[q])) for q in range(NB)])
        names = ["ср.верт", "ср.гор1", "ср.гор2", "sd верт", "sd гор1", "sd гор2"] + \
                [f"{lo}–{hi} Гц {ax}" for lo, hi in bands for ax in ("в", "г1", "г2")]
        ph = (np.arange(NB) + 0.5) / NB * 2 * math.pi
        X = np.stack([np.ones(NB), np.cos(ph), np.sin(ph), np.cos(2 * ph), np.sin(2 * ph)], axis=1)
        out = []
        for j, nm in enumerate(names):
            y, e = M[:, j], np.maximum(E[:, j], 1e-9)
            w = 1 / e ** 2
            beta = np.linalg.lstsq(X * np.sqrt(w)[:, None], y * np.sqrt(w), rcond=None)[0]
            res = (y - X @ beta) / e
            chi = float((res ** 2).sum())
            df = NB - 5
            amp1 = math.hypot(beta[1], beta[2])
            zmax = float(np.abs(res).max())
            # χ² остатка сверх двух гармоник: p по Уилсону — Хилферти
            zc = ((chi / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
            out.append((zc, nm, amp1, chi, df, zmax, int(np.abs(res).argmax())))
        print(f"   этап {stg} ({n_cyc} циклов): остаток сверх плавной части, худшие — "
              + "; ".join(f"{nm}: χ²/df {chi / df:.2f} (z {zc:+.1f}), 1-я гарм. {amp1:.2f}, пик z {zm:.1f} в доле {q}"
                          for zc, nm, amp1, chi, df, zm, q in sorted(out, reverse=True)[:3]))

# ---------- 4. счёт ----------
print("\n4. Счёт (контроль): по этапам, колёса и пустышки")
dummy_names = {f"№{int(i)}" for i in []}
for r in x.get("runs") or []:
    for g in r.get("groups") or []:
        if "пустыш" in (g.get("label") or ""):
            dummy_names |= {(r.get("snames") or {}).get(m) or m for m in g.get("targets") or []}
for stg in STAGE_ORDER:
    ss = [sg for sg in segs if (sg.get("set") or {}).get("stage") == stg]
    if not ss:
        continue
    row = {}
    for part in ("колёса", "пустышки"):
        z = {k: [] for k in ("z_pq", "z_ps", "z_yield", "z_td")}
        pr = []
        for sg in ss:
            for name, mm in (sg.get("gen") or {}).items():
                if name in ("все", "колесо-пустышка") or not isinstance(mm, dict):
                    continue
                dmy = name in dummy_names or name in ("№22", "№23")
                if (part == "пустышки") != dmy:
                    continue
                for k in z:
                    if isinstance(mm.get(k), (int, float)):
                        z[k].append(mm[k])
                if isinstance(mm.get("pr_s"), (int, float)):
                    pr.append(mm["pr_s"])
        row[part] = (S.mean(pr) if pr else 0, {k: (sum(v) / math.sqrt(len(v)) if v else None) for k, v in z.items()})
    print(f"   {stg}: " + " · ".join(f"{part} {row[part][0]:.0f} пр/с, z: " + ", ".join(
        f"{k[2:]} {v:+.1f}" for k, v in row[part][1].items() if v is not None) for part in row))
combo_names = sorted({(sg.get("set") or {}).get("combo") for sg in segs} - {None})
print("   по сочетаниям (колёса, все этапы):")
for c in combo_names:
    pr, zp = [], []
    for sg in segs:
        if (sg.get("set") or {}).get("combo") != c:
            continue
        for name, mm in (sg.get("gen") or {}).items():
            if isinstance(mm, dict) and name.startswith("№") and name not in ("№22", "№23"):
                if isinstance(mm.get("pr_s"), (int, float)):
                    pr.append(mm["pr_s"])
                if isinstance(mm.get("z_pq"), (int, float)):
                    zp.append(mm["z_pq"])
    if pr:
        print(f"   {c:22s} {S.mean(pr):6.0f} пр/с" + (f" · биты z {sum(zp) / math.sqrt(len(zp)):+.1f} (n {len(zp)})" if zp else ""))
