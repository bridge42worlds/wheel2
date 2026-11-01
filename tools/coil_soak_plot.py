"""График прогона катушки на выносливость (tools/coil_soak.py): напряжение пары и ток INA219, поле катушки по
магнитометру колеса (показания минус фон, фон — секунды без поля рядом по времени).

    python tools/coil_soak_plot.py <выход.svg> <coil_soak_….csv> [ещё csv того же прогона …]
"""
import csv, math, statistics as S, sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

out, files = sys.argv[1], sys.argv[2:]
rows = []
for fn in files:
    with open(fn, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            def num(k):
                try:
                    return float(r[k])
                except (ValueError, TypeError):
                    return None
            rows.append({"t": num("t"), "st": r["state"], "age": num("state_age_s"), "v": num("v"), "i": num("i_ma"),
                         "b": [num("bx_ut"), num("by_ut"), num("bz_ut")], "mage": num("mag_age_s")})
rows.sort(key=lambda r: r["t"])
t0 = rows[0]["t"]
good = lambda r: r["mage"] is not None and r["mage"] < 3 and None not in r["b"]
bg = [r for r in rows if r["st"] == "off" and (r["age"] or 0) >= 1.5 and (r["i"] or 999) < 200 and good(r)]


def bg_at(t):
    near = sorted(bg, key=lambda r: abs(r["t"] - t))[:6]
    return [S.mean(r["b"][k] for r in near) for k in range(3)]


on = [r for r in rows if (r["i"] or 0) > 400]
rest = [r for r in rows if (r["i"] or 999) < 200 and r["v"]]
fld = {"+": [], "-": []}
for r in rows:
    if r["st"] in fld and (r["age"] or 0) >= 4 and (r["i"] or 0) > 400 and good(r):
        g = bg_at(r["t"])
        fld[r["st"]].append(((r["t"] - t0) / 60, math.sqrt(sum((x - y) ** 2 for x, y in zip(r["b"], g)))))

INK, MUT, OR, BL, GR, VM = "#1d2228", "#68707c", "#E69F00", "#0072B2", "#009E73", "#D55E00"
plt.rcParams.update({"font.family": ["Segoe UI", "DejaVu Sans"], "font.size": 10, "axes.edgecolor": MUT,
                     "axes.labelcolor": INK, "xtick.color": MUT, "ytick.color": MUT, "svg.fonttype": "none"})
fig, ax = plt.subplots(3, 1, figsize=(9, 7.6), sharex=True, gridspec_kw={"hspace": 0.12})
m = lambda L: [(r["t"] - t0) / 60 for r in L]
ax[0].plot(m(on), [r["v"] for r in on], ".", ms=2, color=VM, label="под полем (≈ 0,9 А)")
ax[0].plot(m(rest), [r["v"] for r in rest], "x", ms=3, color=BL, label="без поля (≈ 0,1 А)")
ax[0].axhline(6.5, color=MUT, ls="--", lw=1)
ax[0].text(0.3, 6.53, "стоп: 6,5 В под полем (3,25 В на банку)", color=MUT, fontsize=9)
ax[0].set_ylabel("пара 2×18650, В")
ax[0].legend(loc="center left", bbox_to_anchor=(0.0, 0.42), frameon=False, fontsize=9)
ax[1].plot(m(on), [r["i"] for r in on], ".", ms=2, color=VM)
ax[1].set_ylabel("ток от пары\nпод полем, мА")
for st, col, mk, lab in (("+", BL, "o", "«+»"), ("-", GR, "s", "«−»")):
    L = fld[st]
    ax[2].plot([x for x, _ in L], [y for _, y in L], mk, ms=1.8, color=col, label=f"поле {lab}")
ax[2].set_ylabel("поле катушки\nпо датчику, мкТл")
ys = sorted(y for L in fld.values() for _, y in L)
if ys:                                                  # крайние 0,5 % — переходы на краях фаз, не в масштаб
    ax[2].set_ylim(ys[len(ys) // 200] - 30, ys[-len(ys) // 200 - 1] + 30)
ax[2].legend(loc="lower left", frameon=False, fontsize=9, markerscale=3)
ax[2].set_xlabel("минуты от начала прогона")
for a in ax:
    a.grid(True, color="#dcd8cf", lw=0.6)
    a.spines[["top", "right"]].set_visible(False)
fig.savefig(out, bbox_inches="tight")
print("ok", out, len(rows), "строк")
