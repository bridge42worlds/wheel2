"""Имитация агентов для проверки сервера и панели без плат.

    python tools/sim_agents.py [секунд] [--port 5015]

Лучше — с отдельным сервером, чтобы ни данные, ни команды не смешались с настоящими:
    python server/monitor.py --http 8091 --udp 5015 --data data/test/sim --no-beacon

Агенты шлют телеметрию на 127.0.0.1:<порт> в тех же форматах, что прошивка, и
принимают команды CFG на своём адресе 127.0.0.x:5007:
  №1 (127.0.0.1) — с ICM-42688 на ободе: колесо r = 450 мм, обороты ходят
       35 → 55 → 35 об/мин за 4 минуты (через невесомость 44,6); обороты, k и
       |f| считает тот же WheelTracker, что в прошивке;
  №2 (127.0.0.2) — с MLX90393: поле катушки 340 мкТл вдоль трубки плюс поле
       Земли 50 мкТл, вращающееся с колесом;
  три статических (127.0.0.3–5, MAC …5a, …5b, …5c) — роль S: 500 Гц (1000 Гц,
       среднее по 2), тяжесть по z, белый шум 70 мкg/√Гц; у второго всегда
       линия сети 50 Гц. По расписанию (с запуска):
         каждые 75 с, начиная со 180-й, — толчок у всех сразу (15 Гц, 15 мg);
         300–420 с — линия 37 Гц 400 мкg по z у первого;
         480–540 с — у третьего значения кратны 256 (грубый режим датчика).
       Первые три минуты чистые — успеть записать эталон.
"""
import argparse, base64, json, math, os, random, socket, sys, threading, time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
from analysis import G, WheelTracker   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("seconds", nargs="?", type=float, default=300)
ap.add_argument("--port", type=int, default=5005)
ARGS = ap.parse_args()
DUR = ARGS.seconds
SRV = ("127.0.0.1", ARGS.port)
R = 0.45
stop = threading.Event()
T_START = time.time()


def rpm_at(t):
    ph = (t % 240) / 240
    return 35 + 20 * (1 - abs(2 * ph - 1))       # 35 → 55 → 35


class Agent:
    def __init__(self, ip, mac, aid, sk):
        self.ip, self.mac, self.id, self.sk = ip, mac, aid, sk
        self.cfg = {"cid": 0, "run": 0, "src": 1, "bits": 256, "nb": 8, "all": 0, "td": 1000, "mode": 0, "nr": 1,
                    "tmax": 70, "k": 24, "pre": 1, "cpu": 240, "txp": 20, "ping": 100, "led": 1, "tp": 1000,
                    "role": 0, "sm": 1, "sf": 0, "cf": 0, "sh": 0, "bgr": 0, "tl": 0, "tq": 0, "rmm": 450, "afs": 2,
                    "avg": 2}
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind((ip, 5007))
        self.rx.settimeout(0.01)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx.bind((ip, 0))
        self.seq = 0
        self.t0 = time.time()

    def head(self, typ):
        ms = int((time.time() - self.t0) * 1000)
        return {"v": 2, "type": typ, "mac": self.mac, "id": self.id, "fw": "sim", "sk": self.sk, "seq": self.seq,
                "up": ms // 1000, "ms": ms}

    def send(self, p):
        if self.cfg.get("tl") == 2:
            return
        self.tx.sendto(json.dumps(p).encode(), SRV)

    def poll_cmd(self):
        try:
            data, _ = self.rx.recvfrom(4096)
        except socket.timeout:
            return
        for tok in data.decode().split()[1:]:
            k, _, v = tok.partition("=")
            if k in self.cfg:
                try:
                    self.cfg[k] = int(v)
                except ValueError:
                    pass
        ack = {"v": 2, "type": "ack", "mac": self.mac, "id": self.id, "cid": self.cfg["cid"], "ok": 1, "err": ""}
        self.tx.sendto(json.dumps(ack).encode(), SRV)

    def tel(self, sens=0, extra=None):
        p = self.head("tel")
        p.update({"rssi": -40, "temp": 45.0, "heap": 160000, "cpu0": 3.0, "cpu1": 0.0, "rf": 1, "st": 1,
                  "work": 100.0, "ts": 0, "udpf": 0, "wdisc": 0, "wrsn": 0, "age": 1.0, "cand": 0, "pr": 0,
                  "txr": self.cfg["txp"], "tls": self.cfg["tl"], "sens": sens, **self.cfg})
        if extra:
            p.update(extra)
        self.send(p)


def rot_axis():
    """Ориентация датчика в трубке: ось колеса — по x датчика, к центру — по -z (как получилось)."""
    return [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]


def mul(m, v):
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def run_imu(ag):
    tr = WheelTracker(r_mm=450)
    rot = rot_axis()
    fs, theta, t, nxt = 200, 0.0, 0.0, 1.0
    revs, samples, raw, n = [], [], [], 0
    last = time.time()
    while not stop.is_set():
        ag.poll_cmd()
        now = time.time()
        steps = int((now - last) * fs)
        if steps <= 0:
            time.sleep(0.005)
            continue
        last += steps / fs
        for _ in range(steps):
            dt = 1 / fs
            t += dt
            w = rpm_at(t) / 60 * 2 * math.pi
            theta += w * dt
            c, s = math.cos(theta), math.sin(theta)
            f_lab = (-w * w * R * c, -w * w * R * s + G, 0.0)
            f_body = [-(f_lab[0] * c + f_lab[1] * s) / G, (-f_lab[0] * s + f_lab[1] * c) / G, 0.0]
            f = [v + random.gauss(0, 0.004) for v in mul(rot, f_body)]
            wv = [v + random.gauss(0, 0.002) for v in mul(rot, [0, 0, w])]
            r = tr.add(f, wv, dt)
            if r:
                revs.append([round(r["t"], 2), round(r["period"], 4), round(r["rpm"], 3), round(r["k"], 4),
                             round(r["k_r"], 4), round(r["r_mm"], 1), round(r["fmin"], 4), round(r["fmax"], 4)])
            n += 1
            if n % 10 == 0:
                wf = tr.wheel_frame(f)
                samples.append([round(x * 1000) for x in wf] if wf else [0, 0, 0, round(math.sqrt(sum(x * x for x in f)) * 1000)])
                raw.append([round(x * 2048) for x in f] + [round(x * 180 / math.pi * 16.4) for x in wv])
        if t >= nxt:
            nxt += ag.cfg["tp"] / 1000
            ag.tel(1, {"imu": {"addr": 104, "who": 71, "cfg": [15, 7, 7, 68], "err": 0}})
            ua = tr.axis_unit()
            p = ag.head("imu")
            p.update({"n": n, "hz": 200.0, "w": round(tr.w_now, 4), "t": 31.5, "revs": len(tr.revs),
                      "r_set": ag.cfg["rmm"], "err": 0, "rev": revs[-6:], "frame": 1 if tr.radial else 0,
                      "s": samples[-20:]})
            if ua and tr.radial:
                p["ax"] = [round(x * 1000) for x in ua]
                p["rd"] = [round(x * 1000) for x in tr.radial]
            ag.send(p)
            q = ag.head("imr")
            q.update({"alsb": 2048, "glsb": 16.4, "raw": raw[-20:]})
            ag.send(q)
            ag.seq += 1
            revs, samples, raw, n = [], [], [], 0


def run_mag(ag):
    t, nxt = 0.0, 1.0
    buf = []
    last = time.time()
    while not stop.is_set():
        ag.poll_cmd()
        now = time.time()
        if now - last < 0.05:
            time.sleep(0.01)
            continue
        last = now
        t += 0.05
        th = 2 * math.pi * rpm_at(t) / 60 * t
        b = [340 + random.gauss(0, 0.5), 50 * math.cos(th) + random.gauss(0, 0.5), 50 * math.sin(th) + random.gauss(0, 0.5)]
        buf.append(b)
        if t >= nxt:
            nxt += ag.cfg["tp"] / 1000
            ag.tel(2, {"mag": {"addr": 12, "gain": 7, "err": 0}})
            ab = [math.sqrt(sum(x * x for x in v)) for v in buf]
            p = ag.head("mag")
            p.update({"n": len(buf), "gain": 7, "err": 0, "b": [round(sum(v[i] for v in buf) / len(buf), 2) for i in range(3)],
                      "babs": round(sum(ab) / len(ab), 2), "bmin": round(min(ab), 2), "bmax": round(max(ab), 2),
                      "t": 30.0, "s": [[round(x * 10) for x in v] for v in buf[-20:]]})
            ag.send(p)
            ag.seq += 1
            buf = []


def run_static(ag, k):
    """Статический акселерометр, роль S: отсчёты осей пакетами "acc" по 140, tel раз в tp.
    В молчании отсчёты копятся (кольцо 12288), переполнилось — старые пропадают, как в прошивке."""
    hz, lsb = 500.0, 16384.0
    sig = 70e-6 * math.sqrt(hz / 2)
    rng = np.random.default_rng(100 + k)
    tilt = rng.normal(0, 0.01, 2)
    ring, head, sent, lost = np.zeros((12288, 3), np.int16), 0, 0, 0
    nxt_tel, mark_ms, last_pkt = 0.0, 0, 0.0
    ag.cfg.update({"role": 2, "led": 0})
    while not stop.is_set():
        ag.poll_cmd()
        now = time.time()
        due = int((now - ag.t0) * hz) - head                  # сколько отсчётов «набрал датчик»
        if due < 50:
            time.sleep(0.01)
            continue
        i = head + np.arange(due)
        t = i / hz
        g = rng.normal(0, sig, (due, 3))
        g[:, 0] += tilt[0]
        g[:, 1] += tilt[1]
        g[:, 2] += 1.0
        el = now - T_START
        if k == 1:                                            # сеть 50 Гц — и в эталоне, и всегда
            g[:, 2] += 150e-6 * np.sin(2 * np.pi * 50 * t)
        if k == 0 and 300 <= el < 420:
            g[:, 2] += 400e-6 * np.sin(2 * np.pi * 37 * t)
        if el >= 180:                                         # толчок у всех сразу — каждые 75 с
            tk = math.floor((el - 180) / 75) * 75 + 180       # последний толчок, с от запуска имитации
            m = (i - (T_START + tk - ag.t0) * hz) / hz        # с от толчка для каждого отсчёта
            on = (m >= 0) & (m < 1.5)
            kick = 0.015 * np.exp(-m[on] / 0.15) * np.sin(2 * np.pi * 15 * m[on])
            g[on, 0] += kick
            g[on, 2] += kick / 2
        raw = np.clip(np.round(g * lsb), -32767, 32767).astype(np.int64)
        if k == 2 and 480 <= el < 540:
            raw = (raw // 256) * 256
        ring[i % len(ring)] = raw.astype(np.int16)
        head += due
        mark_ms = int((now - ag.t0) * 1000)
        quiet = ag.cfg["tl"] == 2
        if not quiet and now >= nxt_tel:
            nxt_tel = now + ag.cfg["tp"] / 1000
            ag.send({**ag.head("tel"), "rssi": -45, "temp": 38.0 + k, "heap": 180000, "cpu0": 2.0, "cpu1": 1.0,
                     "udpf": 0, "wdisc": 0, "wrsn": 0, "role": 2, "cid": ag.cfg["cid"], "run": ag.cfg["run"],
                     "cpu": ag.cfg["cpu"], "txp": ag.cfg["txp"], "txr": ag.cfg["txp"], "led": ag.cfg["led"],
                     "tp": ag.cfg["tp"], "tl": ag.cfg["tl"], "tq": ag.cfg["tq"], "tls": ag.cfg["tl"],
                     "afs": ag.cfg["afs"], "avg": ag.cfg["avg"], "sens": 1,
                     "imu": {"addr": 104, "who": 71, "cfg": [3, 15, 102, 48], "fifo": [64, 5, 21], "err": 0, "ovf": 0},
                     "acc": {"ok": 1, "odr": 1000, "avg": 2, "afs": 2, "lsb": lsb, "hz": hz, "raw": head * 2,
                             "out": head, "bad": 0, "rderr": 0, "cap": len(ring), "wait": head - sent, "lost": lost,
                             "t": 31.0 + k}})
            ag.seq += 1
        if quiet:
            continue
        if head - sent > len(ring):
            lost += head - len(ring) - sent
            sent = head - len(ring)
        while head - sent >= 140 or (head > sent and now - last_pkt > 0.3):
            n = min(140, head - sent)
            part = ring[np.arange(sent, sent + n) % len(ring)]
            p = {"v": 2, "type": "acc", **{kk: v for kk, v in ag.head("acc").items() if kk not in ("v", "type")},
                 "i0": sent, "n": n, "hz": hz, "avg": 2, "afs": 2, "lsb": lsb, "mi": head, "mms": mark_ms,
                 "t": 31.0 + k, "lost": lost, "b64": base64.b64encode(part.astype("<i2").tobytes()).decode()}
            ag.tx.sendto(json.dumps(p, separators=(",", ":")).encode(), SRV)
            sent += n
            last_pkt = now


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    a1 = Agent("127.0.0.1", "68:ee:8f:51:a0:18", 1, "prime_lab")
    a2 = Agent("127.0.0.2", "68:ee:8f:51:ae:20", 2, "prime_lab")
    st = [Agent(f"127.0.0.{3 + k}", f"68:ee:8f:00:00:5{'abc'[k]}", 0, "prime_lab") for k in range(3)]
    ths = [threading.Thread(target=run_imu, args=(a1,), daemon=True),
           threading.Thread(target=run_mag, args=(a2,), daemon=True)]
    ths += [threading.Thread(target=run_static, args=(a, k), daemon=True) for k, a in enumerate(st)]
    for th in ths:
        th.start()
    print(f"имитация {DUR:.0f} с на порт {SRV[1]}: №1 колесо, №2 поле, статические …5a …5b …5c", flush=True)
    time.sleep(DUR)
    stop.set()
    print("конец", flush=True)


if __name__ == "__main__":
    main()
