"""Длительный прогон одной настройки у всех генераторов на связи с записью температуры и темпа.

    python tools/soak.py <минут> [ключ=значение …]      например: python tools/soak.py 10 dc=1 tmax=70

Применяет общие параметры (поверх настроек по умолчанию) всем агентам-генераторам на связи,
каждые 15 с записывает по каждому агенту: температуру кристалла, долю работы регулятора,
состояние предела, загрузку ядер, простых и кандидатов в секунду, переподключения Wi-Fi
сторожем, неушедшие пакеты. Печатает строку на каждый замер и итог: нагрев за прогон, темп
по минутам, упирался ли кто-то в предел. Файл — data/tests/soak_<дата-время>.csv.
"""
import csv, json, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=15))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    over = {}
    for kv in sys.argv[2:]:
        k, v = kv.split("=", 1)
        over[k] = int(v)
    d = get("/api/dash?window=10")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and d["gen"].get(a["mac"]) is not None
            and (d["gen"][a["mac"]].get("role") or 0) == 0]
    names = {a["mac"]: f"№{a['id']}" for a in d["agents"]}
    if not macs:
        raise SystemExit("нет генераторов на связи")
    cfg = {**d["default"], **over, "run": 1}
    r = post("/api/control", {"targets": macs, "cfg": cfg, "own": {}, "reset": True})
    if not r.get("ok"):
        raise SystemExit(f"настройка не принята: {r.get('error')}")
    print(f"прогон {minutes:g} мин: {', '.join(names[m] for m in macs)}; {over or 'настройки по умолчанию'}; настройка №{r['cid']}", flush=True)
    path = os.path.join(ROOT, "data", "tests", time.strftime("soak_%Y%m%d_%H%M%S.csv"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(f)
    w.writerow(["t", "agent", "temp", "work", "ts", "cpu0", "cpu1", "pr_s", "cand_s", "pr", "wrst", "udpf", "heap", "rssi"])
    t0 = time.time()
    first, last, limit_hits, minute_pr = {}, {}, {}, {}
    try:
        while time.time() - t0 < minutes * 60:
            time.sleep(15)
            d = get("/api/dash?window=10")
            el = time.time() - t0
            parts = []
            for m in macs:
                a = next((x for x in d["agents"] if x["mac"] == m), None)
                g = d["gen"].get(m) or {}
                if not a:
                    continue
                v = a.get("values") or {}
                rate = g.get("rate") or {}
                row = {"temp": v.get("temp"), "work": v.get("work"), "ts": v.get("ts"), "cpu0": v.get("cpu0"), "cpu1": v.get("cpu1"),
                       "pr_s": rate.get("g_pr_s"), "cand_s": rate.get("g_cand_s"), "pr": g.get("pr"), "wrst": v.get("wrst"),
                       "udpf": v.get("udpf"), "heap": v.get("heap"), "rssi": v.get("rssi")}
                w.writerow([round(el), names[m]] + [row[k] for k in ("temp", "work", "ts", "cpu0", "cpu1", "pr_s", "cand_s", "pr", "wrst", "udpf", "heap", "rssi")])
                first.setdefault(m, row)
                last[m] = row
                if row["work"] is not None and row["work"] < 99.9:
                    limit_hits[m] = limit_hits.get(m, 0) + 1
                minute_pr.setdefault(int(el // 60), {}).setdefault(m, []).append(row["pr_s"] or 0)
                wk = row["work"] if row["work"] is not None else 100.0
                mark = "⚠" if (row["ts"] or 0) >= 2 else "◐" if wk < 99.9 else " "
                parts.append(f"{names[m]} {(row['temp'] or 0):.0f}°{mark}{'' if wk >= 99.9 else f'{wk:.0f}%'} "
                             f"{(row['pr_s'] or 0):.0f}/с" + (f" wrst{row['wrst']}" if row.get("wrst") else ""))
            f.flush()
            tot = sum((last[m]["pr_s"] or 0) for m in macs)
            print(f"  {el / 60:5.1f} мин: {' | '.join(parts)} | всего {tot:.0f} простых/с", flush=True)
    finally:
        post("/api/control", {"targets": macs, "cfg": {**cfg, "run": 0}, "own": {}})
        f.close()
    print("\nитог:")
    for m in macs:
        a, b = first.get(m), last.get(m)
        if not a or not b:
            continue
        print(f"  {names[m]}: кристалл {a['temp']:.0f} → {b['temp']:.0f} °C; простых всего {b['pr']}; "
              f"регулятор срезал работу в {limit_hits.get(m, 0)} замерах из {len(minute_pr) * 4}; "
              f"сторож Wi-Fi {b.get('wrst') or 0}, не ушло пакетов {b.get('udpf') or 0}")
    print("  простых/с по минутам (все вместе): " + ", ".join(
        f"{k + 1}: {sum(sum(v) / len(v) for v in per.values()):.0f}" for k, per in sorted(minute_pr.items())))
    print(f"  файл: {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
