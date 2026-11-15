"""Промежуточный и итоговый разбор опыта «шум колеса» (tools/wheel_noise.py) — можно в любой момент, по ходу опыта.

Пробежка — из журнала опыта (data/series/wheel_noise_*.json, пишется после пробежки). Вдумчивая часть идёт в памяти
инструмента до конца — её блоки восстанавливаются по журналу команд сервера (data/commands.jsonl: каждая смена режима —
настройка со временем), пары — по строкам «→ «режим»» журнала хода (data/series/wheel_noise_run.log); статика — по
сырью (rawidx), теми же ячейками 10 с. Первая строка — вердикт: «ничего нет» или «что-то есть».

    python tools/wheel_noise_report.py [--out docs/experiments_2026-11-09_wheel_noise.md]
"""
import argparse, glob, io, json, math, os, re, statistics as S, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_argv, sys.argv = sys.argv, ["wheel_noise.py"]
import wheel_noise as wn  # noqa: E402  (ячейки статики, имена мер, состав плат)
sys.argv = _argv

ROOT = wn.ROOT
ap = argparse.ArgumentParser()
ap.add_argument("--out", default=os.path.join(ROOT, "docs", "experiments_2026-11-09_wheel_noise.md"))
ap.add_argument("--quiet", action="store_true", help="печатать только вердикт")
A = ap.parse_args()
J = {n: i for i, n in enumerate(wn.NAMES)}
KEY = ["тяжесть |g|", "горб в", "горб г1", "горб г2", "≠ гаусс в", "≠ гаусс г1", "≠ гаусс г2"]


def tstat(d):
    if len(d) < 3:
        return 0.0
    se = S.stdev(d) / math.sqrt(len(d))
    return S.mean(d) / se if se > 0 else 0.0


def main():
    jp = sorted(glob.glob(os.path.join(ROOT, "data", "series", "wheel_noise_2026-*.json")), key=os.path.getmtime)
    logs = [json.load(open(p, encoding="utf-8")) for p in jp]
    log = next((g for g in reversed(logs) if g.get("run")), logs[-1] if logs else {})   # с пробежкой (вдумчивый запуск — без неё)
    t0 = log.get("t0") or 0
    run = log.get("run") or []
    lines, found, margin = [], [], []
    lines.append(f"# Опыт «шум колеса» — {time.strftime('%d.%m.%Y', time.localtime(t0 or time.time()))}\n")
    lines.append(f"Разбор {time.strftime('%H:%M')} (обновляется по ходу опыта; `python tools/wheel_noise_report.py`).\n")

    # ---------- пробежка ----------
    if run:
        Z = np.array([r["z"] for r in run])
        lines.append(f"## Пробежка: {len(run)} блоков по 75 с против соседних «счёт стоит»\n")
        lines.append("| мера | среднее z | плюс | \\|z\\| ≥ 3 |\n|---|---|---|---|")
        for nm in KEY:
            z = Z[:, J[nm]]
            lines.append(f"| {nm} | {z.mean():+.2f} (± {1 / math.sqrt(len(z)):.2f}) | {int((z > 0).sum())}/{len(z)} | {int((abs(z) >= 3).sum())} |")
        big = [(r, j) for r in run for j in range(len(wn.NAMES)) if abs(r["z"][j]) >= 4]
        lines.append("\n|z| ≥ 4: " + ("; ".join(f"«{wn.name_of(r['st'])}» — {wn.NAMES[j]} z {r['z'][j]:+.1f}" for r, j in big) or "нет") + "\n")
        for sec in "АБВГ":
            zz = [r["z"][J["тяжесть |g|"]] for r in run if r.get("sec") == sec]
            if zz:
                lines.append(f"* часть {sec}: тяжесть z среднее {S.mean(zz):+.2f}, плюс {sum(1 for v in zz if v > 0)}/{len(zz)}")
        lines.append("")

    # ---------- вдумчиво: блоки по журналу команд ----------
    rl = os.path.join(ROOT, "data", "series", "wheel_noise_run.log")
    text = io.open(rl, encoding="utf-8").read() if os.path.exists(rl) else ""
    day = time.strftime("%Y-%m-%d", time.localtime(t0 or time.time()))
    picks = []
    for m in re.finditer(r"^(\d\d:\d\d:\d\d) → «(.+?)» — (.+?) \(пробежка", text, re.M):
        picks.append({"t": time.mktime(time.strptime(f"{day} {m.group(1)}", "%Y-%m-%d %H:%M:%S")), "name": m.group(2),
                      "metric": m.group(3)})
    holds = re.findall(r"^\d\d:\d\d:\d\d +(.+?): Δ ([+-][\d.]+) \(t ([+-][\d.]+), один знак (\d+)/(\d+)\).*— (✓ ДЕРЖИТСЯ|нет)", text, re.M)
    if picks:
        cnt, alln = set(wn.CNT), set(wn.ALL)
        ev = []
        with open(os.path.join(ROOT, "data", "commands.jsonl"), encoding="utf-8") as f:
            for ln in f:                                   # весь журнал: за опыт он вырастает на десятки МБ
                try:
                    d = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                t, tg, cfg = d.get("t") or 0, set(d.get("targets") or []), d.get("cfg") or {}
                if t < picks[0]["t"] - 5:
                    continue
                if tg == alln and not cfg.get("run"):
                    ev.append((t, "v"))                    # «счёт стоит»
                elif tg == cnt and cfg.get("run"):
                    ev.append((t, "x"))                    # режим
        st = wn.Static()
        st.rt = wn.rawidx.RawTail(wn.MAC8, picks[0]["t"])   # весь день: у инструмента хвост сырья только 30 мин
        st.rt_day = time.strftime("%Y-%m-%d", time.localtime(picks[0]["t"]))
        lines.append("## Вдумчиво: пары 60 с — режим против «счёт стоит» (порядок в парах чередуется)\n")
        for k, p in enumerate(picks):
            t_end = picks[k + 1]["t"] if k + 1 < len(picks) else time.time()
            seq = [(t, kind) for t, kind in ev if p["t"] - 5 <= t < t_end]
            blocks = []
            for i, (t, kind) in enumerate(seq):
                tb = seq[i + 1][0] if i + 1 < len(seq) else min(t_end, t + 62)
                if tb - t < 30:
                    continue
                cells = st.cells(t, tb)
                blocks.append((kind, np.mean(cells, axis=0) if len(cells) >= 3 else None))
            d = []
            for i in range(0, len(blocks) - 1, 2):
                a, b = blocks[i], blocks[i + 1]
                if a[1] is None or b[1] is None or a[0] == b[0]:
                    continue
                x, v = (a[1], b[1]) if a[0] == "x" else (b[1], a[1])
                d.append(x - v)
            if not d:
                lines.append(f"### «{p['name']}» — пар пока нет\n")
                continue
            D = np.array(d)
            tt = np.array([tstat(list(D[:, j])) for j in range(D.shape[1])])
            same = np.array([max((D[:, j] > 0).sum(), (D[:, j] < 0).sum()) for j in range(D.shape[1])])
            jm = J.get(p["metric"], 0)
            going = k + 1 == len(picks) and "опыт закончен" not in text[text.rfind(f"→ «{p['name']}»"):]
            lines.append(f"### «{p['name']}» — пар {len(D)}{' (идёт)' if going else ''}\n")
            lines.append(f"* мера пробежки — {p['metric']}: Δ {D[:, jm].mean():+.3f}, t {tt[jm]:+.1f}, один знак {same[jm]}/{len(D)}")
            lines.append(f"* тяжесть |g|: Δ {D[:, J['тяжесть |g|']].mean():+.1f} мкg, t {tt[J['тяжесть |g|']]:+.1f}, один знак {same[J['тяжесть |g|']]}/{len(D)}")
            hump = ", ".join(f"{a} {D[:, J['горб ' + a]].mean():+.3f} (t {tt[J['горб ' + a]]:+.1f})" for a in wn.AX)
            lines.append(f"* горб: {hump}")
            # мер 40: |t| 3–4 у одной из них выпадает и случайно — «на грани»; держится — |t| ≥ 4 и один знак ≥ 80 % пар
            ok80 = lambda j: same[j] >= math.ceil(0.8 * len(D)) and len(D) >= 6
            strong = [j for j in range(len(wn.NAMES)) if abs(tt[j]) >= 4 and ok80(j)]
            edge = [j for j in range(len(wn.NAMES)) if 3 <= abs(tt[j]) < 4 and ok80(j)]
            if strong:
                lines.append("* **держится (|t| ≥ 4, один знак ≥ 80 %)**: " + "; ".join(f"{wn.NAMES[j]} Δ {D[:, j].mean():+.3f} t {tt[j]:+.1f}" for j in strong))
                found += [f"«{p['name']}» — {wn.NAMES[j]} t {tt[j]:+.1f}" for j in strong]
            if edge:
                lines.append("* на грани (|t| 3–4 — среди 40 мер бывает случайно): " + "; ".join(f"{wn.NAMES[j]} t {tt[j]:+.1f}" for j in edge))
                margin += [f"{wn.NAMES[j]} t {tt[j]:+.1f}" for j in edge]
            lines.append("")
    if "★★ НАХОДКА" in text:
        found.append("НАХОДКА в журнале хода (см. ★★)")
    verdict = ("**что-то есть:** " + "; ".join(found)) if found else (
        "**ничего нет** — ни одна мера не держится в парах" + (f" (на грани: {'; '.join(margin)})" if margin else ""))
    lines.insert(1, f"\n{verdict}\n")
    io.open(A.out, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    print(verdict.replace("**", ""))
    if not A.quiet:
        print("\n".join(lines[2:]))


if __name__ == "__main__":
    main()
