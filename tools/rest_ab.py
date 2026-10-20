"""Отдых по очереди: польза для группы — опыт «без» и «с» (docs/thermal.md, § 4.3).

    python tools/rest_ab.py [минут на опыт] [режим cascade|relay] [порядок off,turns,group]

Каждый опыт — из отдыха до стабилизации температуры, одна и та же группа из всех генераторов
на связи: сначала отдых по очереди выключен, потом включён (или в заданном порядке). Итог:
простых/с за опыт и за вторую половину (устойчивый режим), доля кругов, собранных из кусочков
всех (когерентность), отдыхов, температуры и наименьшая доля работы. Отчёт — data/tests/rest_ab_<время>.json.
В конце генераторы «Стоп», отдых по очереди — как был.
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anomaly_run import get, post, rest_until_stable, ROOT   # noqa: E402

MODES = {"cascade": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20},
         "relay": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0}}


def snap(d, macs):
    pr = tr0 = tr1 = gath = rests = 0
    temps, works = {}, {}
    for m in macs:
        g = d["gen"].get(m) or {}
        gs = g.get("gstat") or {}
        pr += g.get("pr") or 0
        tr = gs.get("tr") or [0, 0]
        tr0 += tr[0]
        tr1 += tr[1]
        gath += gs.get("gath") or 0
        rests += (gs.get("rest") or [0, 0, 0])[2]
        a = next(x for x in d["agents"] if x["mac"] == m)
        v = a.get("values") or {}
        temps[a["id"]], works[a["id"]] = v.get("temp"), v.get("work")
    return {"t": time.time(), "pr": pr, "truncs": tr0, "gath": gath, "rests": rests, "temps": temps, "works": works}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    mins = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    mode = sys.argv[2] if len(sys.argv) > 2 else "cascade"
    order = (sys.argv[3] if len(sys.argv) > 3 else "off,on").split(",")
    d = get("/api/dash?window=10")
    was_on = (d.get("rest_rot") or {}).get("on", True)
    was_mode = (d.get("rest_rot") or {}).get("mode", "turns")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and d["gen"].get(a["mac"]) and a.get("kind") != "S"]
    ids = {a["mac"]: a["id"] for a in d["agents"]}
    base = {**d["default"], "cpu": 240, "pl": 0, "tl": 0, "src": 1, "raw": 0, **MODES[mode]}
    print(f"агенты: {', '.join('№%d' % ids[m] for m in macs)}; {mode}, по {mins:g} мин, порядок {order}", flush=True)
    rep = {"t": time.time(), "mode": mode, "mins": mins, "runs": []}
    path = os.path.join(ROOT, "data", "tests", time.strftime("rest_ab_%Y%m%d_%H%M%S.json"))
    for which in order:
        on = which != "off"
        rmode = "group" if which == "group" else "turns"
        print(f"\nотдых: {'вся группа' if which == 'group' else 'по очереди' if on else 'ВЫКЛ'}", flush=True)
        post("/api/rest_rot", {"on": False})            # на время отдыха перед опытом — без диспетчера
        rest = rest_until_stable(macs, ids)
        post("/api/rest_rot", {"on": on, "mode": rmode})
        r = post("/api/control", {"targets": macs, "cfg": {**base, "run": 1}})
        cid = r.get("cid")
        t0 = time.time()
        while time.time() - t0 < 30:
            d = get("/api/dash?window=10")
            if all((d["gen"].get(m) or {}).get("cid") == cid for m in macs):
                break
            time.sleep(0.5)
        s0 = snap(get("/api/dash?window=10"), macs)
        half = None
        wmin = {}
        while time.time() - s0["t"] < mins * 60:
            time.sleep(20)
            d = get("/api/dash?window=10")
            s = snap(d, macs)
            for i, w in s["works"].items():
                if w is not None:
                    wmin[i] = min(wmin.get(i, 100.0), w)
            el = time.time() - s0["t"]
            if half is None and el >= mins * 30:
                half = s
            rs = [f"№{x['id']}" for x in ((d.get("rest_rot") or {}).get("now") or {}).values()]
            print(f"   {el:4.0f} с: простых {s['pr'] - s0['pr']}, "
                  + ", ".join(f"№{i} {t}°/{w:.0f}%" for i, t, w in
                              ((i, s['temps'][i], s['works'][i] or 0) for i in s['temps']))
                  + (f"; отдыхают {', '.join(rs)}" if rs else ""), flush=True)
        s1 = snap(get("/api/dash?window=10"), macs)
        dur = s1["t"] - s0["t"]
        rate = (s1["pr"] - s0["pr"]) / dur
        rate2 = (s1["pr"] - half["pr"]) / (s1["t"] - half["t"]) if half else None
        gath = s1["gath"] - s0["gath"]
        full = 1 - (s1["truncs"] - s0["truncs"]) / gath if gath > 0 else None
        res = {"rest_rot": which, "cid": cid, "rest_before": rest, "rate": round(rate, 2),
               "rate_2nd_half": round(rate2, 2) if rate2 is not None else None,
               "full_rounds": round(full, 4) if full is not None else None,
               "rests": s1["rests"] - s0["rests"], "temps_end": s1["temps"], "work_min": wmin}
        rep["runs"].append(res)
        print(f"   итог: {rate:.1f} простых/с (вторая половина {rate2 or 0:.1f}); кругов из кусочков всех "
              f"{100 * (full or 0):.1f} %; отдыхов {res['rests']}", flush=True)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    post("/api/compose", {"action": "stop"})
    post("/api/rest_rot", {"on": was_on, "mode": was_mode})
    print("\nсравнение:")
    for x in rep["runs"]:
        full = x["full_rounds"] or 0
        print(f"  отдых {x['rest_rot']:>5}: {x['rate']:.1f} простых/с, вторая половина {x['rate_2nd_half'] or 0:.1f}, "
              f"из кусочков всех {100 * full:.1f} % (когерентных ~{x['rate'] * full:.1f}/с), отдыхов {x['rests']}")
    print(f"отчёт: {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
