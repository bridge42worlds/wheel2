"""Проверка через сессии: сначала каждый сам за себя, потом сообща — и сравнение.

    python tools/session_test.py [минут_поодиночке] [минут_сообща] [src] [bits]

Обе сессии — со всеми агентами на связи и одинаковыми параметрами. Печатает
ход шагов, итоги каждой сессии и сравнение: во сколько раз сообща медленнее,
время цикла по этапам, когерентность. Итоги сессий — в data/sessions/.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
SOLO_MIN = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5
GROUP_MIN = float(sys.argv[2]) if len(sys.argv) > 2 else 3
SRC = int(sys.argv[3]) if len(sys.argv) > 3 else 1
BITS = int(sys.argv[4]) if len(sys.argv) > 4 else 256
CFG = {"src": SRC, "bits": BITS, "nb": 8, "all": 0, "td": 1000, "tmax": 70, "nr": 1}


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def run(name, mode, minutes, macs):
    sid = post("/api/session/start", {"name": name, "targets": macs, "minutes": minutes,
                                      "cfg": {**CFG, "mode": mode}})["id"]
    print(f"\nСессия №{sid} «{name}»: {minutes} мин", flush=True)
    shown = 0
    while True:
        time.sleep(2)
        x = get(f"/api/session?id={sid}")
        for st in x["steps"][shown:]:
            if st["t1"] is None:
                break
            mark = "✓" if st["ok"] else "✗"
            print(f"   {mark} {st['name']}: {st['note']}", flush=True)
            shown += 1
        if x["state"] in ("done", "stopped", "failed", "interrupted"):
            break
    if x["state"] != "done":
        raise SystemExit(f"сессия №{sid}: {x['state']} {x.get('note', '')}")
    return x


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    d = get("/api/state")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online"]
    ids = [a["id"] for a in d["agents"] if a["status"] == "online"]
    print(f"агенты на связи: {', '.join('№' + str(i) for i in ids)}; параметры {CFG}", flush=True)

    solo = run(f"каждый сам, {len(macs)} агента", 0, SOLO_MIN, macs)
    s = solo["summary"]
    print(f"   итог: {s['primes']} простых за {s['duration_s']:.0f} с = {s['primes_s']:.2f}/с, "
          f"кандидатов {s['cand_s']:.0f}/с, ложных {s['false_primes']}", flush=True)
    for r in solo["results"].values():
        g = r["gen"]
        print(f"      №{r['id']}: простых {g.get('pr')}, кандидатов {g.get('cand')}, кристалл {r['temp']} °C", flush=True)

    grp = run(f"сообща, {len(macs)} агента", 1, GROUP_MIN, macs)
    g = grp["summary"]
    cm, cp = g.get("cycle_ms") or [0] * 5, g.get("comp_ms") or [0] * 3
    print(f"   итог: {g['primes']} простых за {g['duration_s']:.0f} с = {g['primes_s']:.2f}/с, "
          f"циклов {g['cycles']} ({g['cycles_s']:.1f}/с)", flush=True)
    print(f"   цикл {cm[4]:.2f} мс: сбор {cm[0]:.2f} (счёт {cp[0]:.2f}), отсев {cm[1]:.2f}, проверка {cm[2]:.2f} "
          f"(счёт {cp[2]:.2f}), эстафета {cm[3]:.2f}; синхронизация {g['sync_pct']} %", flush=True)
    print(f"   когерентность: лучшая серия {g['streak_max']} циклов ({g['streak_ms_max'] / 1000:.1f} с), "
          f"повторов {g['asks']}, разрывов {g['breaks']}, перехватов {g['resyncs']}", flush=True)
    for r in grp["results"].values():
        gs = r["gstat"] or {}
        print(f"      №{r['id']}: мастером {gs.get('cycles_m')} циклов, простых {gs.get('primes_m')}, "
              f"нашёл свидетеля {gs.get('found')}, ESP-NOW {gs.get('tx')}/{gs.get('rx')}, "
              f"не доставлено {gs.get('tx_fail')}, кристалл {r['temp']} °C", flush=True)
    print(f"\nСравнение: сообща {g['primes_s']:.2f} простых/с против {s['primes_s']:.2f} поодиночке — "
          f"медленнее в {s['primes_s'] / g['primes_s']:.1f} раза", flush=True)


if __name__ == "__main__":
    main()
