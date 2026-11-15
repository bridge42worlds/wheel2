"""Проверка новых механизмов перед сетом (без вращения): прошивка и самопроверка на всех платах; статика по sx —
перестаёт мерить и возвращается; катушки К1 и К2 — ток при постоянном поле, ∿ 1,2 Гц и ∿ 20 Гц.

    python tools/mech_check.py
"""
import json, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
import program as P  # noqa: E402


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


# 1. прошивка и самопроверка
dash = req("/api/dash")["agents"]
vers, bad = {}, []
for a in dash:
    if a.get("dev") == "phone" or a.get("status") != "online":
        continue
    vers.setdefault(a.get("fw"), []).append(a.get("id"))
    st = (a.get("gen") or {}).get("st", (a.get("values") or {}).get("st"))
    if a.get("kind") in ("A", "B") and st != 1:
        bad.append(a.get("id"))
print("прошивка:", " · ".join(f"{v}: №{', №'.join(str(i) for i in sorted(ids))}" for v, ids in vers.items()))
print("самопроверка:", "✓ у всех генераторов st = 1" if not bad else f"✗ st ≠ 1 у №{bad}")

# 2. статика по sx: 30 с не мерит, потом снова
SP = [a["mac"] for a in dash if a.get("kind") == "S" and a.get("dev") != "phone" and a.get("status") == "online"]
if SP:
    st = lambda sx, name, mins: {"name": name, "minutes": mins, "control": False,
                                 "groups": [{"targets": SP, "cfg": {"mode": 0, "dc": 0, "run": 0, "sg": 0, "src": 1, "sx": sx},
                                             "own": {m: {"role": 2, "cpu": 80, "sg": 0} for m in SP}, "label": "статика"}]}
    prog = {"name": "проверка: статика не мерит и снова мерит", "baseline_min": 0, "repeat": 1, "shuffle": False, "seed": 1,
            "rest": 0, "z": 4, "confirm": 0, "redo": 0, "again": 0, "static_spoil": False, "metrics": [], "targets": SP,
            "steps": [st(1, "статика не мерит", 0.5), st(0, "статика снова мерит", 0.5)]}
    P.validate(json.loads(json.dumps(prog)))
    r = req("/api/program/start", {"program": prog})
    if r.get("ok"):
        pid = r["id"]
        while req(f"/api/program?id={pid}").get("state") in ("running", "starting", "paused"):
            time.sleep(3)
        x = req(f"/api/program?id={pid}")
        win = [(q["t0"], q["t1"]) for q in x.get("runs") or []]
        day = time.strftime("%Y-%m-%d")
        # пакеты отсчётов в сырой журнал не пишутся — по счётчику прочитанных отсчётов в телеметрии статики (acc.raw)
        pts = [[], []]
        macs = set(SP)
        f = os.path.join(ROOT, "data", "raw", f"{day}.jsonl")
        if len(win) == 2 and os.path.exists(f):
            for line in open(f, encoding="utf-8", errors="replace"):
                if not line.startswith('{"rx": ') or '\\"type\\":\\"tel\\"' not in line:
                    continue
                rx = float(line[7:line.index(",", 7)])
                for k, (t0, t1) in enumerate(win):
                    if t0 + 8 <= rx <= t1:             # первые секунды — подтверждение настройки
                        q = json.loads(json.loads(line)["raw"])
                        if q.get("mac") in macs and isinstance(q.get("acc"), dict):
                            pts[k].append((rx, q["acc"].get("raw", 0), q.get("sx")))
        grow = [(max(v[1] for v in p) - min(v[1] for v in p)) if p else None for p in pts]
        ok = grow[0] == 0 and (grow[1] or 0) > 0
        print(f"статика (sx): отсчётов прочитано, пока не мерит — {grow[0]}, после — {grow[1]} · {'✓' if ok else '✗'}")
    else:
        print("✗ проверка статики не запустилась:", r.get("error"))


# 3. катушки: ток при разных видах поля (колёса стоят)
def coil_i(w, sec):
    time.sleep(sec)
    for b in req("/api/battery").get("list") or []:
        if b.get("kind") == "coil" and b.get("wheel") == w and b.get("alive"):
            return b.get("i_now"), b.get("v_now")
    return None, None


for w in ("1", "2"):
    i0, v0 = coil_i(w, 0)
    if i0 is None:
        print(f"катушка К{w} — не на связи")
        continue
    res = []
    for name, body in (("постоянное +", {"state": "+", "ac": None}), ("∿ 1,2 Гц", {"state": "+", "ac": {"f": 1.2, "shape": "sin"}}),
                       ("∿ 20 Гц", {"state": "+", "ac": {"f": 20, "shape": "sin"}})):
        req("/api/coils", {"wheel": w, **body, "src": "проверка механизмов"})
        i, v = coil_i(w, 8)
        res.append(f"{name}: {i:.0f} мА" if i is not None else f"{name}: нет данных")
    req("/api/coils", {"off": True, "src": "проверка механизмов"})
    time.sleep(6)                                      # огибающая гаснет — следующая катушка с нуля
    print(f"катушка К{w} (без поля {i0:.0f} мА, {v0:.2f} В): " + " · ".join(res))
