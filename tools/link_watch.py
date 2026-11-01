"""Сторож связи: прогон колеса и запись каждого пробоя связи, потерь, перезапусков, памяти и температуры.

    python tools/link_watch.py [--wheel 1] [--minutes 30] [--step 5] [--ways cascade,relay,solo,one] [--no-run]

Запускает программу: генераторы колеса считают по кругу способами ways по step минут, пока не выйдет minutes
(без —no-run — только смотрит). Раз в секунду читает /api/dash и по каждому агенту ведёт: пробои (телеметрии
нет дольше 3 с — с началом, длительностью и что было до: RSSI, температура), потерянные пакеты, перезапуски
(и причину), переподключения Wi-Fi сторожем, наименьшую свободную память, наибольшую температуру, RSSI.
Строка раз в минуту, в конце — итог по агентам. Файлы: data/tests/link_watch_<дата-время>.log и .csv (раз в 10 с).
"""
import argparse, csv, json, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
GEN_CFG = {"solo": {"mode": 0, "dc": 1}, "cascade": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20},
           "relay": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0}, "one": {"mode": 1, "gv": 0, "dc": 0}}
WAY_WORD = {"solo": "каждый сам", "cascade": "каскад", "relay": "эстафета", "one": "одно число"}
GAP_S = 3.0                       # телеметрия раз в секунду: нет дольше 3 с — пробой


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", default="1")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--step", type=float, default=5)
    ap.add_argument("--ways", default="cascade,relay,solo")
    ap.add_argument("--no-run", action="store_true")
    a = ap.parse_args()

    d = get("/api/dash?window=10")
    wh = d["compose"]["wheels"]
    agents = {x["mac"]: x for x in d["agents"]}
    macs = sorted((m for m, w in wh.items() if w == a.wheel and m in agents and agents[m]["status"] == "online"
                   and d["compose"]["slot"].get(m) not in ("S", "coil")), key=lambda m: agents[m]["id"])
    if not macs:
        sys.exit(f"колесо {a.wheel}: генераторов на связи нет")
    name = {m: f"№{agents[m]['id']}" for m in macs}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(ROOT, "data", "tests"), exist_ok=True)
    base = os.path.join(ROOT, "data", "tests", f"link_watch_{stamp}")
    logf = open(base + ".log", "w", encoding="utf-8")

    def say(s):
        line = time.strftime("%H:%M:%S ") + s
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    say(f"колесо {a.wheel}: {', '.join(name[m] for m in macs)}; {a.minutes:g} мин")
    pid = None
    if not a.no_run:
        ways = [w for w in a.ways.split(",") if w in GEN_CFG]
        n = max(1, round(a.minutes / a.step))
        steps = [{"name": f"{WAY_WORD[w]} · колесо {a.wheel}", "minutes": a.step,
                  "groups": [{"targets": macs, "cfg": {**GEN_CFG[w], "pl": 1}, "label": f"колесо {a.wheel} · {WAY_WORD[w]}"}]}
                 for w in ways]
        prog = {"name": f"стабильность связи: колесо {a.wheel}, {a.minutes:g} мин", "steps": steps,
                "repeat": max(1, -(-n // len(steps))), "shuffle": False, "settle_s": 10, "confirm": 0, "redo": 0,
                "again": 0, "rest": 0, "baseline_min": 0, "pause_lost": False}
        r = post("/api/program/start", {"program": prog})
        if not r.get("ok"):
            sys.exit(f"программа не пошла: {r.get('error')}")
        pid = r["id"]
        say(f"программа №{pid}: {' → '.join(WAY_WORD[w] for w in ways)} по {a.step:g} мин, кругов {prog['repeat']}")

    st = {m: {"gap0": None, "gaps": [], "lost0": None, "lost": 0, "rb0": None, "rb": 0, "rb_why": [], "up": None,
              "wrst0": None, "wrst": 0, "wdisc0": None, "wdisc": 0, "heap": None, "hblk": None, "tmax": None,
              "rssi": [], "pre": None} for m in macs}
    t_start = time.time()
    t_end = t_start + a.minutes * 60 + (90 if pid else 0)
    last_min, last_csv, pr_prev = 0, 0, None     # pr_prev — (время, сумма простых) на прошлой минутной строке
    cw = csv.writer(open(base + ".csv", "w", newline="", encoding="utf-8"))
    cw.writerow(["t", "agent", "online", "age", "rssi", "temp", "heap", "hblk", "lost", "reboots", "wrst", "pr", "phase"])
    prog_done = False
    while time.time() < t_end and not prog_done:
        time.sleep(1)
        try:
            d = get("/api/dash?window=10")
        except Exception as e:               # noqa: BLE001 — сервер на миг недоступен: не пробой агентов
            say(f"сервер не ответил: {e}")
            continue
        now = time.time()
        ag = {x["mac"]: x for x in d["agents"]}
        act = d.get("program") or {}                 # идущая программа (кратко) или None
        phase = (act.get("now") or {}).get("phase") or ""
        if pid and not act and now - t_start > 30:
            prog_done = True
        gen = d.get("gen") or {}
        for m in macs:
            x, s = ag.get(m) or {}, st[m]
            v = x.get("values") or {}
            age = x.get("age")
            off = age is None or age > GAP_S
            if off and s["gap0"] is None:
                s["gap0"] = now - (age or 0)
                s["gap_pre"] = s["pre"]
                say(f"✗ {name[m]}: нет связи (последний пакет {age:.1f} с назад; до этого RSSI "
                    f"{(s['pre'] or {}).get('rssi')}, {(s['pre'] or {}).get('temp')} °C, фаза {phase or '—'})")
            elif not off and s["gap0"] is not None:
                dur = now - s["gap0"]
                s["gaps"].append(round(dur, 1))
                say(f"✓ {name[m]}: связь вернулась через {dur:.1f} с")
                s["gap0"] = None
            if off:
                continue
            s["pre"] = {"rssi": v.get("rssi"), "temp": v.get("temp")}
            lt, rb = x.get("lost_total") or 0, x.get("reboots") or 0
            if s["lost0"] is None:
                s["lost0"], s["rb0"], s["wrst0"], s["wdisc0"] = lt, rb, v.get("wrst") or 0, v.get("wdisc") or 0
            if lt - s["lost0"] > s["lost"]:
                say(f"· {name[m]}: потеряно пакетов {lt - s['lost0']} (+{lt - s['lost0'] - s['lost']})")
                s["lost"] = lt - s["lost0"]
            up = v.get("up")
            if s["up"] is not None and up is not None and up + 5 < s["up"]:
                rbt = x.get("rbt")
                s["rb"] += 1
                s["rb_why"].append(rbt)
                say(f"↻ {name[m]}: ПЕРЕЗАПУСК (было {s['up']} с работы, причина {rbt})")
            s["up"] = up
            for k in ("wrst", "wdisc"):
                cur = (v.get(k) or 0) - (s[k + "0"] or 0)
                if cur > s[k]:
                    say(f"· {name[m]}: {'переподключение Wi-Fi сторожем' if k == 'wrst' else 'Wi-Fi отключался'} ×{cur}")
                    s[k] = cur
            for k, key, f in (("heap", "heap", min), ("hblk", "hblk", min), ("tmax", "temp", max)):
                if v.get(key) is not None:
                    s[k] = v[key] if s[k] is None else f(s[k], v[key])
            if v.get("rssi") is not None:
                s["rssi"].append(v["rssi"])
        if now - last_csv >= 10:
            last_csv = now
            for m in macs:
                x = ag.get(m) or {}
                v = x.get("values") or {}
                cw.writerow([round(now, 1), name[m], int((x.get("age") or 99) <= GAP_S), x.get("age"), v.get("rssi"),
                             v.get("temp"), v.get("heap"), v.get("hblk"), x.get("lost_total"), x.get("reboots"),
                             v.get("wrst"), (gen.get(m) or {}).get("pr"), phase])
        if now - t_start >= (last_min + 1) * 60:
            last_min = int((now - t_start) // 60)
            on = sum(1 for m in macs if (ag.get(m) or {}).get("age") is not None and ag[m]["age"] <= GAP_S)
            cur = (act.get("now") or {}).get("name") or (act.get("now") or {}).get("step") or ""
            pr_sum = sum((gen.get(m) or {}).get("pr") or 0 for m in macs)     # счётчики с начала опыта
            prs = (pr_sum - pr_prev[1]) / (now - pr_prev[0]) if pr_prev and pr_sum >= pr_prev[1] else 0.0
            pr_prev = (now, pr_sum)
            temps = [((ag.get(m) or {}).get("values") or {}).get("temp") for m in macs]
            temps = [t for t in temps if t is not None]
            heaps = [((ag.get(m) or {}).get("values") or {}).get("heap") for m in macs]
            heaps = [h for h in heaps if h]
            say(f"{last_min:2d} мин: на связи {on}/{len(macs)} · пробоев {sum(len(st[m]['gaps']) for m in macs)} · "
                f"потеряно {sum(st[m]['lost'] for m in macs)} · перезапусков {sum(st[m]['rb'] for m in macs)} · "
                f"{prs:.1f} пр/с · {min(temps or [0]):.0f}–{max(temps or [0]):.0f} °C · память от "
                f"{min(heaps or [0]) // 1024} КБ · {phase} {cur}")

    say("итог по агентам:")
    for m in macs:
        s = st[m]
        rs = s["rssi"]
        say(f"  {name[m]}: пробоев {len(s['gaps'])}{' (' + ', '.join(f'{g:g} с' for g in s['gaps']) + ')' if s['gaps'] else ''}"
            f"{' + идёт сейчас' if s['gap0'] else ''} · потеряно пакетов {s['lost']} · перезапусков {s['rb']}"
            f"{' ' + str(s['rb_why']) if s['rb_why'] else ''} · Wi-Fi сторож {s['wrst']} · память от "
            f"{(s['heap'] or 0) // 1024} КБ (кусок {(s['hblk'] or 0) // 1024}) · до {s['tmax']} °C · RSSI "
            f"{min(rs) if rs else '—'}…{max(rs) if rs else '—'}")
    if pid:
        try:
            x = get(f"/api/program?id={pid}")
            runs = x.get("runs") or []
            for r in runs:
                g = (r.get("gen") or {}).get("все") or {}
                say(f"  опыт «{r.get('name')}»: {g.get('pr_s', 0):.1f} пр/с"
                    f"{' · испорчен: ' + str(r.get('not_confirmed') or r.get('bad')) if r.get('not_confirmed') or r.get('bad') else ''}")
        except Exception as e:               # noqa: BLE001
            say(f"  итоги программы не прочитал: {e}")
    say(f"файлы: {base}.log, .csv")


if __name__ == "__main__":
    main()
