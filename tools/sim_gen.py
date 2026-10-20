"""Имитация агентов-генераторов prime_lab: проверить сервер, главную панель и живую схему без плат.

    python tools/sim_gen.py [агентов] [--port 5016] [--seconds 3600]

Лучше — с отдельным сервером, чтобы данные и команды не смешались с настоящими:
    python server/monitor.py --http 8092 --udp 5016 --data data/test/gen --no-beacon

Агенты №11, №12, … на адресах 127.0.0.11, 127.0.0.12, … принимают команды CFG на своём
адресе (UDP 5007), подтверждают их и раз в период телеметрии шлют пакеты tel (и grp сообща)
с накопительными счётчиками — темпы как у плат по замерам 03–04.10.2026:
  каждый сам — 2300 кандидатов/с (два ядра — 4100), 1 простое на 88,6 кандидата;
  сообща — цикл 3,0 / 4,3 / 6,2 / 7,6 мс для 1 / 2 / 3 / 4 агентов, мастерство по кругу;
  сет B — получает объявления сета A, выпускает кадры по режиму, фон с заданной частотой.
Температура растёт с нагрузкой (два ядра — горячее), остывает в простое.
"""
import argparse, json, math, random, socket, sys, threading, time

ap = argparse.ArgumentParser()
ap.add_argument("n", nargs="?", type=int, default=4)
ap.add_argument("--port", type=int, default=5016)
ap.add_argument("--seconds", type=float, default=3600)
ARGS = ap.parse_args()
SRV = ("127.0.0.1", ARGS.port)
stop = threading.Event()
CYCLE_MS = {1: 2.95, 2: 4.3, 3: 6.2, 4: 7.6, 5: 9.0, 6: 10.5}
STAGES = {1: [0.03, 0.30, 1.47, 0.0], 2: [0.03, 0.28, 2.6, 1.4], 3: [0.034, 0.272, 4.79, 1.3], 4: [0.03, 0.27, 5.4, 1.9]}
DENS = 2 / (256 * math.log(2) - 0.307)
SURV = 0.16193
agents = []


class Agent:
    def __init__(self, k):
        self.id = 11 + k
        self.ip = f"127.0.0.{11 + k}"
        self.mac = f"68:ee:8f:00:00:{0x11 + k:02x}"
        self.cfg = {"cid": 0, "run": 0, "src": 1, "bits": 256, "nb": 8, "all": 0, "td": 1000, "mode": 0, "nr": 1, "tmax": 70,
                    "k": 24, "pre": 1, "cpu": 240, "txp": 20, "ping": 100, "led": 1, "tp": 1000, "role": 0, "sm": 1, "sf": 0,
                    "cf": 0, "sh": 0, "bgr": 0, "tl": 0, "tq": 0, "rmm": 450, "afs": 2, "avg": 2, "pl": 0, "dc": 0,
                    "hb": 1, "dur": 0, "ro": 0}
        self.grp, self.bg = [], []
        self.done = 0            # шаг на агенте: какой cid закончен сам
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind((self.ip, 5007))
        self.rx.settimeout(0.02)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.bind((self.ip, 0))
        self.seq, self.t0, self.temp = 0, time.time(), 52.0 + k
        self.reset()

    def reset(self):
        self.c = {k: 0.0 for k in ("cand", "pr", "ann", "heard", "prx", "stx", "sbg", "slat", "sn", "cycles", "cycles_m", "primes_m",
                                  "asks", "gath", "tg", "ts", "tt", "th", "tc", "cp", "cs", "ct", "tx", "rx")}
        self.t_apply = time.time()

    def head(self, typ):
        ms = int((time.time() - self.t0) * 1000)
        return {"v": 2, "type": typ, "mac": self.mac, "id": self.id, "fw": "sim-gen", "sk": "prime_lab", "seq": self.seq,
                "up": ms // 1000, "ms": ms}

    def send(self, p):
        radio_off = self.cfg["ro"] and self.cfg["run"] and time.time() - self.t_apply > 2.5   # шаг без радио: молчит до конца
        if self.cfg["tl"] != 2 and not radio_off:
            self.tx.sendto(json.dumps(p).encode(), SRV)

    def poll(self):
        try:
            data, _ = self.rx.recvfrom(4096)
        except socket.timeout:
            return
        line = data.decode()
        if not line.startswith("CFG "):
            return
        new = dict(self.cfg)
        for tok in line.split()[1:]:
            k, _, v = tok.partition("=")
            if k in ("grp", "bg"):
                setattr(self, k, [int(x) for x in v.split(",") if x])
            elif k in new:
                try:
                    new[k] = int(v)
                except ValueError:
                    pass
        hard = any(new[k] != self.cfg[k] for k in ("cid", "run", "mode", "role", "src", "bits", "nb", "td", "dc", "sm", "sh", "bgr"))
        keep = self.cfg["run"] and not new["run"] and new["cid"] != self.cfg["cid"]
        self.cfg = new
        if hard and not keep:
            self.reset()
        ack = {"v": 2, "type": "ack", "mac": self.mac, "id": self.id, "cid": new["cid"], "ok": 1, "err": ""}
        self.tx.sendto(json.dumps(ack).encode(), SRV)


def step(dt):
    for a in agents:                       # шаг на агенте: срок вышел — стоп с теми же настройками, итоги в tel (done)
        if a.cfg["run"] and a.cfg["dur"] and time.time() - a.t_apply >= a.cfg["dur"]:
            a.cfg["run"], a.cfg["tl"], a.cfg["tq"], a.done = 0, 0, 0, a.cfg["cid"]
    """Счётчики за dt секунд по режиму каждого."""
    A = [a for a in agents if a.cfg["run"] and a.cfg["role"] == 0]
    B = [a for a in agents if a.cfg["run"] and a.cfg["role"] == 1]
    ann_total = 0.0
    groups = {}
    for a in A:
        if a.cfg["mode"] == 1:
            groups.setdefault(tuple(a.grp), []).append(a)
            continue
        rate = 2300 * (1.78 if a.cfg["dc"] else 1.0)
        if a.cfg["src"] != 1:
            rate = {2: 12, 4: 18, 8: 15, 16: 50}.get(a.cfg["src"], 2)
        a.c["cand"] += rate * dt
        npr = rate * dt * DENS * random.uniform(0.9, 1.1)
        a.c["pr"] += npr
        if B:
            a.c["ann"] += npr
            ann_total += npr
    for grp, mem in groups.items():
        n = len(mem)
        cyc_ms = CYCLE_MS.get(n, 7.6 + 1.5 * (n - 4)) * (1 if mem[0].cfg["src"] == 1 else 15)
        cycles = dt * 1000 / cyc_ms
        st = STAGES.get(n, STAGES[4])
        master = int(time.time() * 3) % n
        for i, a in enumerate(mem):
            share = cycles / n
            a.c["cycles"] += cycles
            a.c["cycles_m"] += share
            p = share * DENS / SURV * random.uniform(0.85, 1.15)
            a.c["primes_m"] += p
            if B:
                a.c["ann"] += p
                ann_total += p
            for key, v in zip(("tg", "ts", "tt", "th"), st):
                a.c[key] += share * v * 1000
            a.c["tc"] += share * cyc_ms * 1000
            a.c["ct"] += share * 1050
            a.c["cs"] += share * 0.25 * 260
            a.c["cp"] += share * 0.25 * 1000
            a.c["asks"] += share * 0.002 * n
            a.c["gath"] += share * 0.26
            a.c["tx"] += share * (n + 1)
            a.c["rx"] += share * (n + 1)
            a.is_master = i == master
    nb = len(B)
    for i, b in enumerate(B):
        got = ann_total * random.uniform(0.97, 1.0)
        b.c["prx"] += got
        sm = b.cfg["sm"]
        out = 0.0 if sm == 0 else (got if sm in (1, 2, 4) else (got if i == nb - 1 else 0.0))
        b.c["stx"] += out
        b.c["slat"] += out * (550 + 1000 * i if sm in (1, 2, 4) else 600 * (i + 1))
        b.c["sn"] += out
        b.c["sbg"] += b.cfg["bgr"] * dt
    frames = sum(b.c["stx"] for b in B)
    for a in agents:
        if a.cfg["run"]:
            a.c["heard"] = sum(b.c["stx"] + b.c["sbg"] for b in B if b is not a) * 0.98
    for a in agents:                                   # температура: к нагрузке, остывает в простое
        load = 0 if not a.cfg["run"] else (1.6 if a.cfg["dc"] else 1.0) if a.cfg["role"] == 0 else 0.3
        target = 54 + 9 * load
        a.temp += (target - a.temp) * min(1, dt / 40) + random.gauss(0, 0.05)


def tel(a):
    c, cfg = a.c, a.cfg
    cand, pr = int(c["cand"]), int(c["pr"])
    tdr = int(cand * (1 - SURV))
    p = a.head("tel")
    p.update({"rssi": -45 - a.id % 5, "temp": round(a.temp, 1), "heap": 99000, "cpu0": 30.0 if cfg["dc"] else 4.0,
              "cpu1": 55.0 if cfg["dc"] else (98.0 if cfg["run"] and cfg["role"] == 0 else 2.0), "rf": 1, "st": 1,
              "work": 100.0 if a.temp < cfg["tmax"] - 1 else 80.0, "ts": 0, "udpf": 0, "wdisc": 0, "wrsn": 0, "wrst": 0,
              "txr": float(cfg["txp"]), "tls": cfg["tl"], "age": round(time.time() - a.t_apply, 1),
              "dur": cfg["dur"], "ro": cfg["ro"], "roff": 0, "done": a.done, "hb": cfg["hb"],
              "cand": cand, "tdr": tdr, "mri": cand - tdr, "pr": pr, "fp": 0, "dis": 0, "err": 0, "stall": 0, "jobs": 0,
              "bt": [cand - tdr] + [pr] * 7, "br": [cand - tdr - pr] + [0] * 7, "bus": [1050] * 8,
              "tdus": int(cand * 43), "mrus": int((cand - tdr) * 1050 + pr * 7 * 1050), "tpn": pr, "tps": int(pr * 38000),
              "tpmin": 900, "tpmax": 260000, "e": [[cand * 8, cand * 256, int(cand * 166), cand * 4, cand * 8, cand * 4, cand * 8],
                                                   [0] * 7, [0] * 7, [0] * 7, [0] * 7],
              "sha": 0, "csi": [0] * 7, "ann": int(c["ann"]), "adrop": 0, "heard": int(c["heard"]), "bclost": 0, "rxdrop": 0,
              "etx": [int(c["tx"]), 0, 0, 0], "pl": cfg["pl"], "pln": 0, "pll": 0, "dc": cfg["dc"], "dcq": [0, 0],
              "ota": [0, 0, "app0"], **{k: cfg[k] for k in cfg}})
    if cfg["mode"] == 1:
        p["grp"] = a.grp
    if cfg["role"] == 1:
        p.update({"prx": int(c["prx"]), "stx": int(c["stx"]), "sdrop": 0, "swait": 0,
                  "chs": int(c["prx"]) if cfg["sm"] == 3 and a.bg and a.bg[0] == a.id else 0, "chf": 0,
                  "chd": int(c["stx"]) if cfg["sm"] == 3 else 0, "slat": int(c["slat"]), "sn": int(c["sn"]), "sbg": int(c["sbg"])})
    a.send(p)
    if cfg["mode"] == 1 and cfg["role"] == 0:
        n = len(a.grp)
        g = a.head("grp")
        g.update({"cid": cfg["cid"], "on": 1 if cfg["run"] else 0, "role": 2 if getattr(a, "is_master", False) else 1, "master": a.id,
                  "phase": 12, "cycle": int(c["cycles"]), "cycles": int(c["cycles"]), "cycles_m": int(c["cycles_m"]),
                  "primes_m": int(c["primes_m"]), "regathers": 0, "found": int(c["cycles"] * 0.93), "gath": int(c["gath"]),
                  "cands": int(c["gath"]) * 24, "surv": int(c["gath"] * 24 * SURV), "rdrop": 0, "starts": 1,
                  "fillw": int(c["gath"] * 0.8), "pre": [int(c["gath"] * 0.99), 1], "early": 0, "hop": 1900,
                  "asks_tx": int(c["asks"]), "asks_rx": 0, "brk_pcs": 0, "brk_res": 0, "brk_hand": 0, "resyncs": 0,
                  "conflicts": 0, "stale": 0, "streak": int(c["cycles"]), "streak_max": int(c["cycles"]),
                  "streak_ms": int(time.time() - a.t_apply) * 1000, "streak_ms_max": int(time.time() - a.t_apply) * 1000,
                  "clean_ms": int(time.time() - a.t_apply) * 1000,
                  "t": [int(c["tg"]), int(c["ts"]), int(c["tt"]), int(c["th"]), int(c["tc"])], "c": [int(c["cp"]), int(c["cs"]), int(c["ct"])],
                  "tx": int(c["tx"]), "rx": int(c["rx"]), "tx_fail": 0, "espnow": 2, "tx_cb": int(c["tx"]), "tx_drop": 0, "txq_max": 3,
                  "tx_stuck": 0, "ok_to": [int(c["tx"] / max(1, n - 1))] * n, "fail_to": [0] * n, "rssi_from": [-30] * n,
                  "agr": int(c["cycles_m"]), "split": 0, "dis": 0})
        a.send(g)
    a.seq += 1


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    global agents
    agents = [Agent(k) for k in range(ARGS.n)]
    print(f"агенты: {', '.join('№%d %s' % (a.id, a.ip) for a in agents)} → сервер 127.0.0.1:{ARGS.port}", flush=True)
    t_end = time.time() + ARGS.seconds
    last, nxt = time.time(), {a.id: time.time() for a in agents}
    while time.time() < t_end:
        for a in agents:
            a.poll()
        now = time.time()
        step(now - last)
        last = now
        for a in agents:
            if now >= nxt[a.id]:
                nxt[a.id] = now + a.cfg["tp"] / 1000
                tel(a)
        time.sleep(0.02)


if __name__ == "__main__":
    main()
