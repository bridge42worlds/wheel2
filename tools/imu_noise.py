# -*- coding: utf-8 -*-
"""Шум и точность акселерометра агента (ICM-42688 или ADXL355): по осям X, Y, Z и по модулю |a| (в сумме).

    python tools/imu_noise.py <номер агента> [минут, по умолчанию 10] [--from <unix-время>] [--date ГГГГ-ММ-ДД]

Берёт сырые отсчёты из data/raw/<дата>.jsonl: пакеты "imr" (обод, 20 Гц — каждый 10-й отсчёт из 200 Гц, предел
±16 g, ступень 0,49 мg) или "acc" (статический, 500 Гц, ±2 g; ADXL355 — 16000 отсчётов на g, ICM-42688 —
16384). Датчик должен лежать неподвижно. Считает:
среднее и разброс (СКО) по каждой оси и по модулю, гистограмму отклонений с наложенной гауссовой кривой,
спектр шума (плотность, мкg/√Гц, рядом — паспорт TDK: 65 по X/Y, 70 по Z), девиацию Аллана (как точно
усреднение находит постоянную величину за время τ — где кончается белый шум и начинается дрейф), гироскоп —
смещение нуля и шум. Картинка и сводка — data/sensor/agent<N>_<время>.png / .txt (data/ — не в git).
"""
import base64, glob, json, os, sys, time
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_UG = {"x": 65.0, "y": 65.0, "z": 70.0}            # мкg/√Гц, TDK ICM-42688-P (сводка на сайте TDK, DS-000347)
SPEC_ADXL_UG = {"x": 25.0, "y": 25.0, "z": 25.0}         # мкg/√Гц, ADXL355 при ±2 g (паспорт ADI, Rev. A)
SPEC_GYRO_MDPS = 2.8                                     # мград/с/√Гц
sys.stdout.reconfigure(encoding="utf-8")


def load(agent, minutes, t_from=None, date=None):
    path = os.path.join(ROOT, "data", "raw", (date or time.strftime("%Y-%m-%d")) + ".jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"нет {path}")
    acc, gyr, t_pk, kind, lsb, glsb = [], [], [], None, None, None
    t_end = time.time()
    t0 = t_from or (t_end - minutes * 60)
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if '\\"imr\\"' not in ln and '\\"acc\\"' not in ln:
                continue
            o = json.loads(ln)
            if o["rx"] < t0 or (t_from and o["rx"] > t0 + minutes * 60):
                continue
            p = json.loads(o["raw"])
            if p.get("id") != agent:
                continue
            if p["type"] == "imr":
                kind, lsb, glsb = "imr", p["alsb"], p["glsb"]
                for r in p["raw"]:
                    acc.append(r[:3]); gyr.append(r[3:6]); t_pk.append(o["rx"])
            elif p["type"] == "acc":                     # статический: base64 int16 x y z
                kind = "acc"
                lsb = float(p.get("lsb") or 16384.0)
                raw = np.frombuffer(base64.b64decode(p["b64"]), dtype="<i2").reshape(-1, 3)
                acc.extend(raw.tolist()); t_pk.extend([o["rx"]] * len(raw))
    if not acc:
        raise SystemExit(f"№{agent}: за {minutes} мин отсчётов нет")
    return kind, np.array(acc, float), (np.array(gyr, float) if gyr else None), lsb, glsb, np.array(t_pk)


def allan(x, fs, taus):
    """Перекрывающаяся девиация Аллана."""
    out = []
    c = np.concatenate([[0.0], np.cumsum(x)])
    for tau in taus:
        m = int(round(tau * fs))
        if m < 1 or 2 * m >= len(x):
            out.append(np.nan); continue
        avg = (c[m:] - c[:-m]) / m
        d = avg[m:] - avg[:-m]
        out.append(np.sqrt(0.5 * np.mean(d * d)))
    return np.array(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    agent = int(args[0]) if args else 6
    minutes = float(args[1]) if len(args) > 1 else 10
    t_from = float(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else None
    date = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv else None
    kind, a, g, lsb, glsb, tp = load(agent, minutes, t_from, date)
    global SPEC_UG
    adxl = kind == "acc" and round(lsb) in (16000, 8000, 4000)
    if adxl:
        SPEC_UG = SPEC_ADXL_UG
    chip = "ADXL355" if adxl else "ICM-42688"
    fs = 20.0 if kind == "imr" else 500.0
    mg = a / lsb * 1000.0                                # мg
    mag = np.linalg.norm(mg, axis=1)
    cols = {"x": mg[:, 0], "y": mg[:, 1], "z": mg[:, 2], "|a|": mag}
    dur = len(mg) / fs
    step_mg = 1000.0 / lsb
    lines = [f"Агент №{agent}, {chip}, {'обод: 20 Гц из 200, ±16 g' if kind == 'imr' else 'статический: 500 Гц, ±2 g'}, "
             f"ступень {step_mg:.3f} мg; {len(mg)} отсчётов, {dur / 60:.1f} мин "
             f"({time.strftime('%H:%M', time.localtime(tp[0]))}–{time.strftime('%H:%M', time.localtime(tp[-1]))})", ""]
    lines.append(f"{'ось':>4} {'среднее, мg':>12} {'СКО, мg':>8} {'СКО, ступеней':>14} {'мин…макс, мg':>18} {'плотность, мкg/√Гц':>20} {'паспорт':>8}")
    import scipy.signal as ss
    psd = {}
    for k, v in cols.items():
        d = v - v.mean()
        f, P = ss.welch(d, fs=fs, nperseg=min(1024, len(d) // 4), detrend="linear")
        psd[k] = (f, np.sqrt(P) * 1000.0)                 # мкg/√Гц
        band = (f > 0.5) & (f < fs / 2 * 0.9)
        dens = np.median(psd[k][1][band])
        lines.append(f"{k:>4} {v.mean():12.2f} {v.std():8.3f} {v.std() / step_mg:14.2f} {v.min() - v.mean():8.2f}…{v.max() - v.mean():+.2f}"
                     f" {dens:20.0f} {('%.0f' % SPEC_UG[k]) if k in SPEC_UG else '—':>8}")
    lines += ["", f"|a| = {mag.mean():.2f} мg — земное притяжение, должно быть ≈ 1000 (наклон не влияет; отличие — масштаб и смещение нуля датчика)."]
    if kind == "imr":
        lines += ["Внимание: отсчёты взяты через один из десяти — шум полосы 0–40 Гц сложился в 0–10 Гц, поэтому плотность",
                  "в таблице завышена примерно в √(40/10) = 2 раза; честная плотность — СКО / √(полоса 40 Гц × 1,2)."]
        for k in ("x", "y", "z"):
            lines.append(f"  {k}: СКО {cols[k].std():.3f} мg → плотность ≈ {cols[k].std() / np.sqrt(48.0) * 1000:.0f} мкg/√Гц (паспорт {SPEC_UG[k]:.0f})")
    if g is not None:
        dps = g / glsb
        lines += ["", "Гироскоп (неподвижно — должен показывать 0):"]
        for i, k in enumerate("xyz"):
            lines.append(f"  {k}: смещение нуля {dps[:, i].mean():+.3f} °/с, СКО {dps[:, i].std():.4f} °/с "
                         f"(ступень {1 / glsb:.3f} °/с; паспорт: шум {SPEC_GYRO_MDPS} мград/с/√Гц → в полосе 40 Гц ≈ {SPEC_GYRO_MDPS * np.sqrt(48) / 1000:.3f} °/с)")
    taus = np.logspace(np.log10(2 / fs), np.log10(dur / 4), 30)
    ad = {k: allan(v, fs, taus) for k, v in cols.items()}
    lines += ["", "Девиация Аллана (точность среднего за время τ), мg:"]
    for tt in (0.1, 1, 10, 60):
        if tt < dur / 4:
            vals = {k: allan(v, fs, [tt])[0] for k, v in cols.items()}
            lines.append(f"  τ = {tt:>4g} с: " + "  ".join(f"{k} {vals[k]:.3f}" for k in cols))
    best = {k: (np.nanmin(ad[k]), taus[np.nanargmin(ad[k])]) for k in cols}
    lines.append("  наилучшая: " + "  ".join(f"{k} {best[k][0]:.3f} мg при τ≈{best[k][1]:.0f} с" for k in cols))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    COL = {"x": "#0072B2", "y": "#E69F00", "z": "#009E73", "|a|": "#000000"}     # Okabe-Ito; в легенде — подписи
    LS = {"x": "-", "y": "--", "z": "-.", "|a|": ":"}
    fig, ax = plt.subplots(2, 2, figsize=(14, 9))
    t = np.arange(len(mg)) / fs / 60
    for k, v in cols.items():
        ax[0, 0].plot(t, v - v.mean(), LS[k], color=COL[k], lw=0.6, label=f"{k}: СКО {v.std():.2f} мg")
    ax[0, 0].set(title="Отклонение от среднего во времени", xlabel="минуты", ylabel="мg")
    ax[0, 0].legend(fontsize=8)
    for k, v in cols.items():
        d = v - v.mean()
        bins = np.arange(np.floor(d.min() / step_mg) - 0.5, np.ceil(d.max() / step_mg) + 1.5) * step_mg if k != "|a|" else 40
        ax[0, 1].hist(d, bins=bins, histtype="step", color=COL[k], linestyle=LS[k], lw=1.4, density=True, label=f"{k} (σ {v.std():.2f} мg)")
    xs = np.linspace(-4, 4, 200) * max(c.std() for c in cols.values())
    sig = np.mean([cols[k].std() for k in "xyz"])
    ax[0, 1].plot(xs, np.exp(-xs ** 2 / (2 * sig ** 2)) / (sig * np.sqrt(2 * np.pi)), color="#CC79A7", lw=1, label=f"гаусс σ {sig:.2f} мg")
    ax[0, 1].set(title=f"Распределение отклонений (ступень датчика {step_mg:.2f} мg)", xlabel="мg", ylabel="плотность")
    ax[0, 1].legend(fontsize=8)
    for k in cols:
        f, P = psd[k]
        ax[1, 0].loglog(f[1:], P[1:], LS[k], color=COL[k], lw=1, label=k)
    for k in ("x", "z"):
        ax[1, 0].axhline(SPEC_UG[k] * (2 if kind == "imr" else 1), color="#999999", lw=0.8, ls=":")
    ax[1, 0].set(title="Спектр шума" + (" (20 Гц: сложенный, паспорт ×2 — серые линии)" if kind == "imr" else ", серое — паспорт"),
                 xlabel="Гц", ylabel="мкg/√Гц")
    ax[1, 0].legend(fontsize=8)
    for k in cols:
        ax[1, 1].loglog(taus, ad[k], LS[k], color=COL[k], lw=1.2, marker="o", ms=3, label=k)
    ax[1, 1].set(title="Девиация Аллана: точность среднего за время τ", xlabel="τ, с", ylabel="мg")
    ax[1, 1].legend(fontsize=8)
    for a_ in ax.flat:
        a_.grid(True, which="both", alpha=0.3)
    fig.suptitle(lines[0], fontsize=11)
    fig.tight_layout()
    out = os.path.join(ROOT, "data", "sensor")
    os.makedirs(out, exist_ok=True)
    stem = os.path.join(out, f"agent{agent}_{time.strftime('%Y%m%d_%H%M', time.localtime(tp[-1]))}")
    fig.savefig(stem + ".png", dpi=110)
    open(stem + ".txt", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nкартинка: {stem}.png")


if __name__ == "__main__":
    main()
