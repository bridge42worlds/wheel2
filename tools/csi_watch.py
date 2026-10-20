"""Как меняется поток CSI со временем.

    python tools/csi_watch.py [секунд] [шаг]

Ставит агенту источник «только CSI» (256 бит, 8 баз) и каждые `шаг` секунд
печатает по пакетам телеметрии: ответов на пинг/с, вызовов CSI/с (всего,
коротких, 802.11n, от роутера) и принятых в генератор.
"""
import json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 150
STEP = float(sys.argv[2]) if len(sys.argv) > 2 else 10


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=10))


def snap():
    mac, g = next(iter(get("/api/dash?window=10&metrics=g_pr_s")["gen"].items()))
    return mac, g


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    mac, g = snap()
    if g["src"] != 2:
        cid = post("/api/control", {"targets": [mac], "cfg": {"src": 2, "bits": 256, "nb": 8, "all": 0, "td": 1000}})["cid"]
        while snap()[1]["cid"] != cid:
            time.sleep(0.5)
    t0 = time.time()
    _, g = snap()
    prev, tp = g["csi"], time.time()
    while time.time() - t0 < DUR:
        time.sleep(STEP)
        _, g = snap()
        c, t = g["csi"], time.time()
        if len(c) < 7 or len(prev) < 7:
            prev, tp = c, t
            continue
        dt = t - tp
        r = [(c[i] - prev[i]) / dt for i in range(7)]
        print(f"{time.time() - t0:5.0f} с: пинг {r[2]:5.1f}/с · CSI всего {r[3]:5.1f}/с, "
              f"коротких {r[4]:4.1f}, 802.11n {r[5]:5.1f}, от роутера {r[6]:5.1f} · в генератор {r[0]:5.1f}/с",
              flush=True)
        prev, tp = c, t


if __name__ == "__main__":
    main()
