"""Сводка прогонов аномалий (tools/anomaly_run.py) — таблица для документации.

    python tools/anomaly_summary.py data/tests/anomaly_A.json [anomaly_B.json …]

По каждому шагу показатели пересчитываются из журнала простых (data/primes) по номеру
настройки: так сводка одинакова для старых и новых отчётов. Колонки: простых (и в
секунду), отсев и выход против теории (z), доля единиц в разрядах и общий перекос (σ),
равенство соседних разрядов сверх ожидаемого по долям единиц (z), разброс единиц по
агентам, сходство соседних по времени простых одного агента (память шума, z), худший p остатков по 3, 5, 7, 11, промежутки (коэффициент вариации и худший z
связи соседних промежутков). Внизу — повторы по всем журналам за все дни.
"""
import datetime, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prime_stats                                     # noqa: E402

IDS = {}


def ids():
    if not IDS:
        import csv
        with open(os.path.join(prime_stats.ROOT, "registry", "devices.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                IDS[r["mac"].strip().lower()] = r["id"]
    return IDS


def fmt(x, d=1, sign=True):
    if x is None:
        return "—"
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s.replace(".", ",").replace("-", "−")


def flag(z, warn=3, bad=5):
    return "" if z is None or abs(z) < warn else " ◐" if abs(z) < bad else " ✗"


def step_rows(t, cid):
    day0 = datetime.date.fromtimestamp(t)
    rows = []
    for k in range(3):                                 # прогон может перейти через полночь
        rows += prime_stats.load(str(day0 + datetime.timedelta(days=k)), cid=cid)
    return rows


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    files = sys.argv[1:]
    if not files:
        raise SystemExit(__doc__)
    print("| шаг | простых | в с | отсев, z | выход, z | единиц, % (σ) | соседние разряды, z | единиц по агентам, % (σ) "
          "| соседние простые агента, z | остатки, худший p | промежутки: CV, худший z |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for fn in files:
        rep = json.load(open(fn, encoding="utf-8"))
        for s in rep["steps"]:
            rows = step_rows(rep["t"], s["cid"])
            m = {}
            if rows:
                prime_stats.report(rows, m=m)
            ag = m.get("agents") or {}
            ag_txt = ", ".join(f"№{ids().get(mac, '?')} {fmt(v[0], 2, False)} ({fmt(v[1])})" for mac, v in sorted(
                ag.items(), key=lambda kv: int(ids().get(kv[0], 0))))
            worst_ag = max((abs(v[1]) for v in ag.values()), default=0)
            gaps = m.get("gaps") or []
            cv = [g[3] for g in gaps]
            zr = max((abs(g[5]) for g in gaps), default=None)
            modp = min((m.get("mod") or {}).values(), default=None)
            print(f"| {s['name']} | {len(rows)} | {fmt(len(rows) / s['dur'], 2, False)} "
                  f"| {fmt(s['z_sieve'])}{flag(s['z_sieve'])} | {fmt(s['z_yield'])}{flag(s['z_yield'])} "
                  f"| {fmt(m.get('ones'), 2, False)} ({fmt(m.get('zm'))}){flag(m.get('zm'))} "
                  f"| {fmt(m.get('z_eq'))}{flag(m.get('z_eq'))} "
                  f"| {ag_txt or '—'}{flag(worst_ag)} "
                  f"| {fmt(m.get('z_ham'))}{flag(m.get('z_ham'))} "
                  f"| {fmt(modp, 3, False)}{' ✗' if modp is not None and modp < 1e-3 else ''} "
                  f"| {fmt(min(cv), 2, False) + '–' + fmt(max(cv), 2, False) if cv else '—'}, {fmt(zr, 1, False)}{flag(zr, 4, 6)} |")
    total, dups = prime_stats.global_dups()
    print(f"\nповторы по всем журналам за все дни: {len(dups)} из {total} простых")


if __name__ == "__main__":
    main()
