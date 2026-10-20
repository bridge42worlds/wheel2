"""Одна сессия «сообща» со всеми агентами на связи — и разбор связи по парам.

    python tools/group_session.py [минут] [nr] [bits] [src]

nr — скорость ESP-NOW: 1 — 24 Мбит/с, 2 — 6 Мбит/с, 0 — 1 Мбит/с.
Печатает итоги сессии и по каждой паре агентов: сколько кадров не доставлено
и с каким сигналом агенты слышат друг друга.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
MIN = float(sys.argv[1]) if len(sys.argv) > 1 else 1
NR = int(sys.argv[2]) if len(sys.argv) > 2 else 1
BITS = int(sys.argv[3]) if len(sys.argv) > 3 else 256
SRC = int(sys.argv[4]) if len(sys.argv) > 4 else 1
RATE = {0: "1 Мбит/с", 1: "24 Мбит/с", 2: "6 Мбит/с"}


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    d = get("/api/state")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online"]
    n = len(macs)
    name = f"сообща, {n} агента, ESP-NOW {RATE[NR]}, {MIN:g} мин"
    sid = post("/api/session/start", {"name": name, "targets": macs, "minutes": MIN,
                                      "cfg": {"src": SRC, "bits": BITS, "nb": 8, "all": 0, "td": 1000,
                                              "tmax": 70, "mode": 1, "nr": NR}})["id"]
    print(f"Сессия №{sid} «{name}»", flush=True)
    while True:
        time.sleep(3)
        x = get(f"/api/session?id={sid}")
        if x["state"] in ("done", "stopped", "failed", "interrupted"):
            break
    for st in x["steps"]:
        print(f"   {'✓' if st['ok'] else '✗'} {st['name']}: {st['note']}", flush=True)
    g = x.get("summary") or {}
    if x["state"] != "done" or not g:
        print(f"сессия: {x['state']} {x.get('note', '')}")
        return
    cm, cp = g.get("cycle_ms") or [0] * 5, g.get("comp_ms") or [0] * 3
    print(f"итог: {g['primes']} простых = {g['primes_s']:.2f}/с, циклов {g['cycles']} ({g['cycles_s']:.1f}/с); "
          f"цикл {cm[4]:.2f} мс (ждали кусочки {cm[0]:.2f}, ждали отсев {cm[1]:.2f}, проверка {cm[2]:.2f}, "
          f"итог до нового мастера {cm[3]:.2f}), "
          f"синхронизация {g['sync_pct']} %", flush=True)
    print(f"когерентность: лучшая серия {g['streak_max']} циклов ({g['streak_ms_max'] / 1000:.1f} с), "
          f"повторов {g['asks']}, разрывов {g['breaks']}, перехватов {g['resyncs']}", flush=True)
    if g.get("gath_per_cycle") is not None:
        print(f"запас: сборов на цикл {g['gath_per_cycle']}, уцелело {g['surv_pct']} % кандидатов, "
              f"цикл ждал число {g['wait_pct']} %, кусочки готовы заранее {g['pre_pct']} %, "
              f"итог до нового мастера {g['hop_ms']} мс, раньше итога пришло {g['early']}", flush=True)
    grp = sorted(r["id"] for r in x["results"].values())   # группа — по порядку номеров
    for r in x["results"].values():
        gs = r["gstat"] or {}
        ok, bad, rs = gs.get("ok_to"), gs.get("fail_to"), gs.get("rssi_from")
        parts = []
        for i, pid in enumerate(grp):
            if pid == r["id"]:
                continue
            if ok is None:
                parts.append(f"→№{pid}: (старая прошивка, нет счётчиков)")
                continue
            tot = ok[i] + bad[i]
            parts.append(f"→№{pid}: не доставлено {100 * bad[i] / tot if tot else 0:.1f} % из {tot}, "
                         f"слышит №{pid} на {rs[i]} дБм")
        print(f"   №{r['id']} (мастером {gs.get('cycles_m')} циклов, кристалл {r['temp']} °C): " + "; ".join(parts),
              flush=True)


if __name__ == "__main__":
    main()
