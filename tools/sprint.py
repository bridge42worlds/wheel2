"""Спринт: полная производительность каждого режима (docs/thermal.md, § 4.4).

    python tools/sprint.py [секунд на спринт] [режимы через запятую]

Для каждого режима: отдых, пока температура не установится (генераторы «Стоп», 80 МГц),
потом режим на полной мощности (240 МГц) — из холодного состояния регулятор не успевает
урезать работу — и замер на окне от 15-й секунды до конца. Потом снова отдых и следующий.
Режимы — наборы панели: solo, solo_dc, one, relay, cascade (по умолчанию все пять).
Итог — таблица: простых/с на всех и на агента, температура в начале и в конце,
наименьшая доля работы (если меньше 100 % — регулятор уже резал, спринт надо короче).
Отчёт — data/tests/sprint_<время>.json. В конце все генераторы — «Стоп».
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anomaly_run import get, post, rest_until_stable, ROOT   # noqa: E402

MODES = {
    "solo":    ("каждый сам", {"mode": 0, "dc": 0}),
    "solo_dc": ("каждый сам, два ядра", {"mode": 0, "dc": 1}),
    "one":     ("все на одном числе", {"mode": 1, "gv": 0, "dc": 0}),
    "relay":   ("эстафета, два ядра", {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0}),
    "cascade": ("каскад, два ядра, срок 20 мс", {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}),
}
SKIP_S = 15                                  # первые секунды — разгон: группа собирается, очереди наполняются


def primes(d, macs, group):
    tot = 0
    for m in macs:
        g = d["gen"].get(m) or {}
        tot += ((g.get("gstat") or {}).get("primes_m") or 0) if group else (g.get("pr") or 0)
    return tot


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 75
    keys = (sys.argv[2] if len(sys.argv) > 2 else ",".join(MODES)).split(",")
    d = get("/api/dash?window=10")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and d["gen"].get(a["mac"]) and a.get("kind") != "S"]
    ids = {a["mac"]: a["id"] for a in d["agents"]}
    if not macs:
        raise SystemExit("нет агентов-генераторов на связи")
    base = {**d["default"], "cpu": 240, "pl": 0, "tl": 0, "src": 1, "raw": 0}
    print(f"агенты: {', '.join('№%d' % ids[m] for m in macs)}; спринт {secs:g} с, замер с {SKIP_S}-й секунды", flush=True)
    rep = {"t": time.time(), "secs": secs, "modes": []}
    path = os.path.join(ROOT, "data", "tests", time.strftime("sprint_%Y%m%d_%H%M%S.json"))
    for key in keys:
        name, over = MODES[key]
        group = over.get("mode") == 1 and not over.get("gv")      # «все на одном числе»: простые — у мастеров
        print(f"\n{name}", flush=True)
        rest = rest_until_stable(macs, ids)
        r = post("/api/control", {"targets": macs, "cfg": {**base, **over, "run": 1}})
        cid = r.get("cid")
        t0 = time.time()
        while time.time() - t0 < 30:
            d = get("/api/dash?window=10")
            if all((d["gen"].get(m) or {}).get("cid") == cid for m in macs):
                break
            time.sleep(0.5)
        t_go = time.time()
        p0 = None
        works, temps = {}, {}
        while time.time() - t_go < secs:
            time.sleep(1)
            d = get("/api/dash?window=10")
            el = time.time() - t_go
            if p0 is None and el >= SKIP_S:
                p0, tp0 = primes(d, macs, group), time.time()
            for a in d["agents"]:
                if a["mac"] in macs:
                    v = a.get("values") or {}
                    if v.get("work") is not None:
                        works[a["mac"]] = min(works.get(a["mac"], 100.0), v["work"])
                    if v.get("temp") is not None:
                        temps[a["mac"]] = v["temp"]
        d = get("/api/dash?window=10")
        p1, tp1 = primes(d, macs, group), time.time()
        rate = (p1 - (p0 or 0)) / max(tp1 - tp0, 1e-9) if p0 is not None else 0.0
        wmin = min(works.values()) if works else None
        res = {"key": key, "name": name, "cid": cid, "rate": round(rate, 2), "per_agent": round(rate / len(macs), 2),
               "temp_start": rest["temps"], "temp_end": {ids[m]: t for m, t in temps.items()},
               "work_min": wmin, "rest_s": rest["s"]}
        rep["modes"].append(res)
        print(f"   {rate:.1f} простых/с ({rate / len(macs):.1f} на агента); в конце "
              + ", ".join(f"№{ids[m]} {t:.1f} °C" for m, t in temps.items())
              + (f"; ◐ регулятор резал до {wmin:.0f} % — спринт короче" if wmin is not None and wmin < 99.5 else "; работа 100 %"),
              flush=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    post("/api/compose", {"action": "stop"})
    print("\nвсе генераторы — «Стоп»")
    print("\nрежим | простых/с | на агента | работа мин.")
    for m in rep["modes"]:
        print(f"{m['name']} | {m['rate']:.1f} | {m['per_agent']:.1f} | {m['work_min'] if m['work_min'] is not None else '—'}")
    print(f"отчёт: {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
