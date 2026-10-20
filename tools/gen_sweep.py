"""Прогон генератора по набору настроек через API сервера мониторинга.

    python tools/gen_sweep.py [секунд_на_настройку] [full|csi]

full — все источники, режимы и длины; csi — CSI вместе с каждым другим
источником (проверка, не мешают ли они радио строить CSI).

Для каждой настройки: POST /api/control, ожидание подтверждения агентом,
затем замер; печатает темп, время на простое, долю времени по источникам.
Сервер (server/monitor.py) должен работать, агент — с прошивкой prime_lab.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
HOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 25

SWEEP = [
    ("ГСЧ, 256 бит, 8 баз", {"src": 1, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("CSI, 256 бит", {"src": 2, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("АЦП, 256 бит", {"src": 4, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("дрожание, 256 бит", {"src": 8, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("все четыре, 256 бит", {"src": 15, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("ГСЧ, 256 бит, все базы", {"src": 1, "bits": 256, "nb": 8, "all": 1, "td": 1000}),
    ("ГСЧ, 32 бита, 8 баз, все базы", {"src": 1, "bits": 32, "nb": 8, "all": 1, "td": 1000}),
    ("ГСЧ, 1024 бита", {"src": 1, "bits": 1024, "nb": 8, "all": 0, "td": 1000}),
    ("ГСЧ, 2048 бит", {"src": 1, "bits": 2048, "nb": 8, "all": 0, "td": 1000}),
]
CSI_SET = [
    ("CSI", {"src": 2, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("CSI + ГСЧ", {"src": 3, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("CSI + АЦП", {"src": 6, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("CSI + дрожание", {"src": 10, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
    ("ГСЧ, 256 бит, 8 баз", {"src": 1, "bits": 256, "nb": 8, "all": 0, "td": 1000}),
]
CSI_REPEAT = [   # угасает ли поток CSI со временем, независимо от пары
    ("CSI", CSI_SET[0][1]),
    ("CSI + ГСЧ", CSI_SET[1][1]),
    ("CSI снова", CSI_SET[0][1]),
    ("ГСЧ (CSI выключен)", CSI_SET[4][1]),
    ("CSI после выключения", CSI_SET[0][1]),
    ("ГСЧ, 256 бит, 8 баз", CSI_SET[4][1]),
]
if len(sys.argv) > 2 and sys.argv[2] == "csi":
    SWEEP = CSI_SET
if len(sys.argv) > 2 and sys.argv[2] == "csirep":
    SWEEP = CSI_REPEAT


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def gen_state():
    d = get("/api/dash?window=10&metrics=g_pr_s")
    return next(iter(d["gen"].items()))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    mac, _ = gen_state()
    for name, cfg in SWEEP:
        cid = post("/api/control", {"targets": [mac], "cfg": cfg})["cid"]
        t0 = time.time()
        while time.time() - t0 < 15:
            _, g = gen_state()
            if g["cid"] == cid:
                break
            time.sleep(0.5)
        else:
            print(f"{name}: агент не подтвердил cid={cid}")
            continue
        csi0 = g["csi"]
        time.sleep(HOLD)
        _, g = gen_state()
        age = g["age"] or 1
        csi_rate = (g["csi"][0] - csi0[0]) / HOLD
        pong_rate = (g["csi"][2] - csi0[2]) / HOLD
        ent = [e[2] for e in g["e"]]
        tot_us = sum(ent) + g["tdus"] + g["mrus"]
        tp = g["tps"] / g["tpn"] / 1000 if g["tpn"] else float("nan")
        share = ", ".join(f"{k} {100 * u / (age * 1e6):.0f}%" for k, u in zip(("rng", "csi", "adc", "jit"), ent) if u)
        bus = [round(b / t, 1) if t else None for b, t in zip(g["bus"], g["bt"])]
        print(f"{name}: кандидатов {g['cand'] / age:.1f}/с, простых {g['pr'] / age:.2f}/с "
              f"(1 на {g['cand'] / max(1, g['pr']):.1f}), на простое {tp:.1f} мс; "
              f"время: шум {sum(ent) / max(1, g['cand']):.0f} мкс, делители {g['tdus'] / max(1, g['cand']):.0f}, "
              f"МР {g['mrus'] / max(1, g['cand']):.0f} мкс на кандидата; сбор: {share}; "
              f"база 2: {bus[0]} мкс/проверку; обманули базу {g['dis']}, ложных {g['fp']}, "
              f"ошибок {g['err']}, CSI {csi_rate:.0f} пак/с при {pong_rate:.0f} ответах на пинг/с, "
              f"проверено сервером {g['verify']['n']} (ошибок {g['verify']['fail']})",
              flush=True)


if __name__ == "__main__":
    main()
