"""Совместный режим: новая настройка группе и наблюдение за ней.

    python tools/group_watch.py [секунд] [nr] [bits] [src]

Ставит всем агентам на связи режим «сообща» с новым номером настройки (счётчики
с нуля) и каждые 20 с печатает: циклы и простые, серию без сбоев, переспросы и
разрывы, время цикла по этапам, долю синхронизации, температуру и долю работы.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 120
NR = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BITS = int(sys.argv[3]) if len(sys.argv) > 3 else 256
SRC = int(sys.argv[4]) if len(sys.argv) > 4 else 1


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    d = get("/api/dash?window=10&metrics=g_cyc_s")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and a["mac"] in d["gen"]]
    body = {"targets": macs, "cfg": {"src": SRC, "bits": BITS, "nb": 8, "all": 0, "td": 1000, "tmax": 70,
                                     "mode": 1, "nr": NR}}
    req = urllib.request.Request(URL + "/api/control", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    cid = json.load(urllib.request.urlopen(req, timeout=10))["cid"]
    print(f"настройка №{cid}: сообща, агентов {len(macs)}, ESP-NOW {'24' if NR else '1'} Мбит/с, {BITS} бит, src={SRC}",
          flush=True)
    t0 = time.time()
    while time.time() - t0 < DUR:
        time.sleep(20)
        d = get("/api/dash?window=20&metrics=g_cyc_s")
        ag = {a["mac"]: a for a in d["agents"]}
        r = next((g for g in d["groups"] if g["cid"] == cid), None)
        if not r:
            print(f"{time.time() - t0:4.0f} с: группа ещё не собралась", flush=True)
            continue
        avg, comp = r["avg_ms"] or [0] * 5, r["comp_ms"] or [0] * 3
        th = "; ".join(f"№{ag[m]['id']} {ag[m]['values'].get('temp')}°C работа {ag[m]['values'].get('work')}%"
                       for m in macs)
        print(f"{time.time() - t0:4.0f} с: циклов {r['cycles']} ({r['cyc_s']:.0f}/с), простых {r['primes']} "
              f"({r['pr_s']:.1f}/с) | серия {r['streak']}, лучшая {r['streak_max']} | переспросов {r['asks']}, "
              f"разрывов {r['breaks']}, перехватов {r['resyncs']}, двух мастеров {r['conflicts']} | цикл "
              f"{avg[4]:.2f} мс = сбор {avg[0]:.2f} (счёт {comp[0]:.2f}) + отсев {avg[1]:.2f} + проверка "
              f"{avg[2]:.2f} (счёт {comp[2]:.2f}) + эстафета {avg[3]:.2f}; синхронизация {r['sync_pct']} % | {th}",
              flush=True)


if __name__ == "__main__":
    main()
