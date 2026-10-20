"""Проверка совместного режима: сначала каждый сам за себя (опора), потом сообща.

    python tools/group_test.py [секунд_поодиночке] [секунд_сообща] [src] [bits]

Берёт всех агентов с прошивкой генератора, которые сейчас на связи. Настройки
одинаковые для обоих этапов (src — маска источников, bits — длина). Печатает
опору каждого агента, затем каждые 15 с — состояние группы, в конце итог.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
SOLO_S = float(sys.argv[1]) if len(sys.argv) > 1 else 90
GROUP_S = float(sys.argv[2]) if len(sys.argv) > 2 else 180
SRC = int(sys.argv[3]) if len(sys.argv) > 3 else 1
BITS = int(sys.argv[4]) if len(sys.argv) > 4 else 256
CFG = {"src": SRC, "bits": BITS, "nb": 8, "all": 0, "td": 1000, "tmax": 70, "nr": 1}   # nr=1: ESP-NOW 24 Мбит/с


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(body):
    req = urllib.request.Request(URL + "/api/control", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def dash():
    return get("/api/dash?window=30&metrics=g_pr_s")


def apply(macs, mode):
    cid = post({"targets": macs, "cfg": {**CFG, "mode": mode}})["cid"]
    t0 = time.time()
    while time.time() - t0 < 20:
        d = dash()
        if all((d["gen"].get(m) or {}).get("cid") == cid for m in macs):
            return cid
        time.sleep(0.5)
    raise SystemExit(f"агенты не подтвердили настройку №{cid}")


def name(d, mac):
    a = next(x for x in d["agents"] if x["mac"] == mac)
    return f"№{a['id']}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    d = dash()
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and a["mac"] in d["gen"]]
    if len(macs) < 2:
        print("на связи меньше двух агентов:", [name(d, m) for m in macs])
    print(f"агенты: {', '.join(name(d, m) for m in macs)}; настройки {CFG}", flush=True)

    apply(macs, 0)
    print(f"\n1. Каждый сам за себя, {SOLO_S:.0f} с", flush=True)
    time.sleep(SOLO_S)
    d = dash()
    solo_total = 0.0
    for m in macs:
        g = d["gen"][m]
        r = g["rate"]
        solo_total += g["pr"] / max(1, g["age"])
        print(f"   {name(d, m)}: {g['cand'] / g['age']:.0f} кандидатов/с, {g['pr'] / g['age']:.1f} простых/с "
              f"(за 5 с: {r.get('g_pr_s', 0):.1f}), кристалл {d['agents'][[a['mac'] for a in d['agents']].index(m)]['values'].get('temp')} °C",
              flush=True)
    print(f"   вместе: {solo_total:.1f} простых/с", flush=True)

    cid = apply(macs, 1)
    print(f"\n2. Сообща (настройка №{cid}), {GROUP_S:.0f} с", flush=True)
    t0 = time.time()
    while time.time() - t0 < GROUP_S:
        time.sleep(15)
        d = dash()
        grs = [g for g in d["groups"] if g["cid"] == cid]
        if not grs:
            print("   группа ещё не видна", flush=True)
            continue
        r = grs[0]
        roles = ", ".join(f"№{x['id']}:{'М' if x['role'] == 2 else 'у'}" for x in r["members"])
        avg = r["avg_ms"] or [0] * 5
        print(f"   {time.time() - t0:4.0f} с: циклов {r['cycles']} ({r['cyc_s']:.0f}/с), простых {r['primes']} "
              f"({r['pr_s']:.1f}/с); серия {r['streak']} (лучшая {r['streak_max']}); переспросов {r['asks']}, "
              f"разрывов {r['breaks']}, перехватов {r['resyncs']}, двух мастеров {r['conflicts']}; "
              f"цикл {avg[4]:.2f} мс (сбор {avg[0]:.2f}, отсев {avg[1]:.2f}, проверка {avg[2]:.2f}, "
              f"эстафета {avg[3]:.2f}), синхронизация {r['sync_pct']} %; {roles}", flush=True)

    d = dash()
    r = next(g for g in d["groups"] if g["cid"] == cid)
    age = max(g["age"] for g in (d["gen"][m] for m in macs))
    pr_s = r["primes"] / age
    print(f"\nИтог: сообща {pr_s:.2f} простых/с против {solo_total:.1f} поодиночке — "
          f"медленнее в {solo_total / pr_s:.1f} раза" if pr_s else "\nИтог: простых сообща нет", flush=True)
    print(f"      циклов {r['cycles']} за {age:.0f} с, лучшая серия без сбоев {r['streak_max']} циклов "
          f"({(r['streak_ms_max'] or 0) / 1000:.1f} с), синхронизация {r['sync_pct']} %", flush=True)
    for x in r["members"]:
        print(f"      №{x['id']}: мастером {x['cycles_m']} циклов, нашёл свидетеля {x['found']} раз, "
              f"отправлено {x['tx']}, принято {x['rx']}, не доставлено {x['tx_fail']}", flush=True)


if __name__ == "__main__":
    main()
