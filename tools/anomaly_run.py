"""Прогон по шагам с журналом простых и поиском аномалий в потоке простых.

    python tools/anomaly_run.py [минут на шаг] [шаги через запятую] [--stop] [--rest]

Шаг — режим и шум у всех агентов-генераторов на связи (каждый сам, сообща, эстафета,
каскад; ГСЧ, CSI, АЦП, дрожание, все сразу; с хешем, без хеша, без хеша с фон Нейманом).
Длительность шага — общая или своя: «adc_raw:20» — 20 минут. Готовые наборы шагов:
«hash», «raw», «vn» (см. SETS). На каждом шаге включён журнал простых: сервер
перепроверяет каждое простое и пишет его в data/primes. После шага:

  * отсев: доля кандидатов, отсеянных делителями < td, против теории (у равномерных
    чисел — 1 − ∏(1 − 1/p)); перекос шума сразу меняет её (например, кандидаты чаще
    делятся на 3);
  * выход: простых на проверенных против теории (плотность простых / доля уцелевших);
  * поток простых — tools/prime_stats.py: повторы, остатки по модулям, баланс бит
    (в среднем и по агентам), сцепление соседних разрядов, промежутки между находками.

В конце — повторы простых по всем журналам за все дни. --stop — после прогона все
генераторы «Стоп» (стоят и остывают). --rest — перед каждым шагом отдых, пока температура
не установится (docs/thermal.md, § 4.5): шаги начинаются из одинакового состояния. z больше ~3 — заметить, больше ~5 — аномалия.
Без хеша отклонения в битах ожидаемы: сырьё не равномерно — это и есть то, что режим
должен показать. Отчёт — data/tests/anomaly_<время>.json.
"""
import json, math, os, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prime_stats                                     # noqa: E402

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

SOLO = {"mode": 0, "dc": 1}
ONE = {"mode": 1, "gv": 0, "dc": 0}                                  # наборы как на панели (PRESETS)
RELAY = {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0}
CASCADE = {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}
RNG, CSI, ADC, JIT, ALL = 1, 2, 4, 8, 15
HASH, RAW, VN = 0, 1, 2

# ключ: (название, настройки поверх обычных)
STEPS = {
    "rng":          ("каждый сам, два ядра, ГСЧ с хешем — эталон", {**SOLO, "src": RNG, "raw": HASH}),
    "one":          ("сообща, все на одном числе, ГСЧ с хешем", {**ONE, "src": RNG, "raw": HASH}),
    "relay":        ("эстафета, два ядра, ГСЧ с хешем", {**RELAY, "src": RNG, "raw": HASH}),
    "cascade":      ("каскад, два ядра, срок 20 мс, ГСЧ с хешем", {**CASCADE, "src": RNG, "raw": HASH}),
    "csi_hash":     ("каждый сам, CSI с хешем", {**SOLO, "src": CSI, "raw": HASH}),
    "adc_hash":     ("каждый сам, АЦП с хешем", {**SOLO, "src": ADC, "raw": HASH}),
    "jit_hash":     ("каждый сам, дрожание с хешем", {**SOLO, "src": JIT, "raw": HASH}),
    "all_hash":     ("каждый сам, все четыре источника с хешем", {**SOLO, "src": ALL, "raw": HASH}),
    "rng_raw":      ("каждый сам, ГСЧ без хеша", {**SOLO, "src": RNG, "raw": RAW}),
    "csi_raw":      ("каждый сам, CSI без хеша", {**SOLO, "src": CSI, "raw": RAW}),
    "adc_raw":      ("каждый сам, АЦП без хеша", {**SOLO, "src": ADC, "raw": RAW}),
    "jit_raw":      ("каждый сам, дрожание без хеша", {**SOLO, "src": JIT, "raw": RAW}),
    "all_raw":      ("каждый сам, все четыре без хеша (XOR)", {**SOLO, "src": ALL, "raw": RAW}),
    "one_adc_raw":  ("сообща, АЦП без хеша", {**ONE, "src": ADC, "raw": RAW}),
    "relay_adc_raw": ("эстафета, АЦП без хеша", {**RELAY, "src": ADC, "raw": RAW}),
    "cascade_adc_raw": ("каскад, АЦП без хеша", {**CASCADE, "src": ADC, "raw": RAW}),
    "relay_jit_raw": ("эстафета, дрожание без хеша", {**RELAY, "src": JIT, "raw": RAW}),
    "rng_vn":       ("каждый сам, ГСЧ без хеша, фон Нейман", {**SOLO, "src": RNG, "raw": VN}),
    "csi_vn":       ("каждый сам, CSI без хеша, фон Нейман", {**SOLO, "src": CSI, "raw": VN}),
    "adc_vn":       ("каждый сам, АЦП без хеша, фон Нейман", {**SOLO, "src": ADC, "raw": VN}),
    "jit_vn":       ("каждый сам, дрожание без хеша, фон Нейман", {**SOLO, "src": JIT, "raw": VN}),
    "all_vn":       ("каждый сам, все четыре без хеша, фон Нейман", {**SOLO, "src": ALL, "raw": VN}),
    "one_rng_vn":   ("сообща, ГСЧ без хеша, фон Нейман", {**ONE, "src": RNG, "raw": VN}),
    "relay_rng_vn": ("эстафета, ГСЧ без хеша, фон Нейман", {**RELAY, "src": RNG, "raw": VN}),
    "cascade_rng_vn": ("каскад, ГСЧ без хеша, фон Нейман", {**CASCADE, "src": RNG, "raw": VN}),
}
# наборы шагов на ~1 ч 45 мин (сервер в фоне живёт 2 ч): медленный шум — шаги длиннее
SETS = {
    "hash": "rng:8,one:12,relay:10,cascade:10,csi_hash:15,adc_hash:15,jit_hash:15,all_hash:15",
    "raw": "rng_raw:8,adc_raw:15,jit_raw:15,csi_raw:15,all_raw:12,one_adc_raw:10,relay_adc_raw:12,cascade_adc_raw:12",
    "vn": "rng_vn:8,adc_vn:15,jit_vn:15,csi_vn:15,all_vn:12,one_rng_vn:10,relay_rng_vn:10,cascade_rng_vn:10,rng:5",
}
DEFAULT_ORDER = "rng,relay,cascade,adc_raw,adc_vn,jit_raw,csi_raw,adc_hash,rng_vn,rng"


def get(path):
    for k in range(5):                                  # сервер занят — подождать и повторить
        try:
            return json.load(urllib.request.urlopen(URL + path, timeout=15))
        except OSError:
            if k == 4:
                raise
            time.sleep(3)


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


def surv_frac(td):
    if not td:
        return 1.0
    s, n = 1.0, td
    sieve = bytearray([1]) * n
    for i in range(2, n):
        if sieve[i]:
            if i > 2:
                s *= 1 - 1 / i
            sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
    return s


def dens(bits):
    return 2 / (bits * math.log(2) - 0.307)            # простых среди нечётных длиной ровно bits


def counters(d, macs):
    out = {}
    for m in macs:
        g = d["gen"].get(m) or {}
        gs = g.get("gstat") or {}
        v = (next(a for a in d["agents"] if a["mac"] == m).get("values")) or {}
        out[m] = {k: g.get(k) or 0 for k in ("cand", "tdr", "mri", "pr")}
        out[m].update({k: gs.get(k) or 0 for k in ("cycles_m", "primes_m", "cands")})
        out[m].update({"tr": (gs.get("tr") or [0, 0]), "cid": g.get("cid"), "temp": v.get("temp"), "work": v.get("work")})
    return out


def apply(macs, cfg, run, ids=None):
    """Разослать настройку; вернуть (номер, кто подтвердил). Кто не подтвердил за 2 мин (завис —
    сторож радио его перезапустит), тот в этом шаге не считается; шаг идёт без него."""
    r = post("/api/control", {"targets": macs, "cfg": {**cfg, "run": run}})
    if not r.get("ok"):
        raise SystemExit(f"сервер не принял настройку: {r.get('error')}")
    cid = r["cid"]
    t0 = time.time()
    ok = []
    while time.time() - t0 < 120:                       # все подтвердили номер настройки
        d = get("/api/dash?window=10")
        ok = [m for m in macs if (d["gen"].get(m) or {}).get("cid") == cid and (d["gen"].get(m) or {}).get("run") == run]
        if len(ok) == len(macs):
            return cid, ok
        time.sleep(1)
    miss = [f"№{ids[m]}" if ids else m for m in macs if m not in ok]
    print(f"   ✗ не приняли настройку №{cid} за 2 мин: {', '.join(miss)} — шаг без них", flush=True)
    if not ok:
        raise SystemExit("никто не принял настройку")
    return cid, ok


def rest_until_stable(macs, ids, min_s=60, max_s=360, tol=0.5, window_s=90, say=print):
    """Отдых: генераторы «Стоп» (80 МГц, без записи в общие настройки), пока у каждого за window_s
    температура не упала больше чем на tol °C — но не меньше min_s и не дольше max_s.
    Возвращает {секунд, установилась, температуры}."""
    r = post("/api/compose", {"action": "stop"})
    if not r.get("ok"):
        say(f"   ✗ отдых: {r.get('error')}")
    t0, hist, stable = time.time(), {}, False
    while True:
        el = time.time() - t0
        d = get("/api/dash?window=10")
        for a in d["agents"]:
            if a["mac"] in macs and (a.get("values") or {}).get("temp") is not None:
                hist.setdefault(a["mac"], []).append((el, a["values"]["temp"]))
        if el >= min_s:
            stable = True
            for m in macs:
                pts = [v for tt, v in hist.get(m, []) if tt >= el - window_s]
                k = max(1, len(pts) // 3)
                if len(pts) < 6 or sum(pts[:k]) / k - sum(pts[-k:]) / k > tol:
                    stable = False
                    break
            if stable or el >= max_s:
                break
        time.sleep(5)
    temps = {ids[m]: hist[m][-1][1] for m in macs if hist.get(m)}
    say(f"   отдых {time.time() - t0:.0f} с{' — установилась' if stable else ' — не установилась, дальше'}: "
        + ", ".join(f"№{i} {v:.1f}" for i, v in temps.items()))
    return {"s": round(time.time() - t0, 1), "stable": stable, "temps": temps}


def zbin(k, n, p):
    v = n * p * (1 - p)
    return (k - n * p) / math.sqrt(v) if v > 0 else 0.0


def mark(z, warn=3, bad=5):
    return "✓" if abs(z) < warn else "◐" if abs(z) < bad else "✗"


def parse_order(arg, mins):
    out = []
    for part in SETS.get(arg, arg).split(","):
        key, _, m = part.strip().partition(":")
        if key in SETS:
            out += parse_order(key, mins)
            continue
        if key not in STEPS:
            raise SystemExit(f"нет шага «{key}»; есть: {', '.join(STEPS)}; наборы: {', '.join(SETS)}")
        out.append((key, float(m) if m else mins))
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    stop_after = "--stop" in sys.argv
    rest_each = "--rest" in sys.argv
    mins = float(args[0]) if args else 3
    order = parse_order(args[1] if len(args) > 1 else DEFAULT_ORDER, mins)
    d = get("/api/dash?window=10")
    base = {**d["default"], "pl": 1, "tl": 0}
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and d["gen"].get(a["mac"]) and a.get("kind") != "S"]
    ids = {a["mac"]: a["id"] for a in d["agents"]}
    if not macs:
        raise SystemExit("нет агентов-генераторов на связи")
    print(f"агенты: {', '.join('№%d' % ids[m] for m in macs)}; шагов {len(order)}, "
          f"всего {sum(m for _, m in order):g} мин", flush=True)
    report = {"t": time.time(), "mins": mins, "order": [f"{k}:{m:g}" for k, m in order], "steps": []}
    path = os.path.join(ROOT, "data", "tests", time.strftime("anomaly_%Y%m%d_%H%M%S.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    all_macs = macs
    for n, (key, step_min) in enumerate(order, 1):
        name, over = STEPS[key]
        cfg = {**base, **over}
        print(f"\n{n}. {name} — {step_min:g} мин", flush=True)
        rest = rest_until_stable(all_macs, ids) if rest_each else None
        dd = get("/api/dash?window=10")                 # кто на связи сейчас (выпавший мог вернуться)
        macs = [m for m in all_macs if next(a for a in dd["agents"] if a["mac"] == m)["status"] == "online"] or all_macs
        _, macs = apply(macs, cfg, 0, ids)              # стоя: счёт с нуля
        cid, macs = apply(macs, cfg, 1, ids)
        t0 = time.time()
        rb0 = {m: next(a for a in dd["agents"] if a["mac"] == m).get("reboots") or 0 for m in macs}
        prev, stalls = None, 0
        while (left := step_min * 60 - (time.time() - t0)) > 0:
            time.sleep(min(120, left))
            dd = get("/api/dash?window=10")
            c = counters(dd, macs)
            prn = sum(x["primes_m"] if over.get("mode") == 1 and not over.get("gv") else x["pr"] for x in c.values())
            print(f"   {time.time() - t0:4.0f} с: простых {prn}, "
                  f"температура {', '.join('№%d %s' % (ids[m], c[m]['temp']) for m in macs)}", flush=True)
            if prn == prev:                             # за 2 мин ни одного простого — счёт стоит
                stalls += 1
                off = [f"№{ids[m]}" for m in macs if next(a for a in dd["agents"] if a["mac"] == m)["status"] != "online"]
                print(f"   ✗ счёт стоит" + (f"; не на связи: {', '.join(off)}" if off else ""), flush=True)
            prev = prn
        _, ok = apply(macs, cfg, 0, ids)                # стоп — итоги замирают
        time.sleep(3)
        dd = get("/api/dash?window=10")
        c = counters(dd, macs)
        dur = time.time() - t0
        bits, td = cfg["bits"], cfg["td"]
        if over.get("mode") == 1 and not over.get("gv"):
            # сообща: число одно на всех — кандидаты и проверенные числа считает группа (циклы мастера)
            cand = sum(x["cands"] for x in c.values())
            tested = sum(x["cycles_m"] for x in c.values())
            tdr = max(cand - tested, 0)
            pr = sum(x["primes_m"] for x in c.values())
        else:
            cand = sum(x["cand"] for x in c.values())
            tdr = sum(x["tdr"] for x in c.values())
            pr = sum(x["pr"] for x in c.values())
            # Выход — на проверенных Миллером — Рабином: в эстафете часть уцелевших уходит при передаче хода
            # непроверенной, а в эстафете и каскаде каждое простое ещё раз сверяет сосед (тоже вызов проверки).
            tested = sum(x["mri"] for x in c.values()) - (pr if over.get("mode") == 1 else 0)
        q = 1 - surv_frac(td)
        z_td = zbin(tdr, cand, q)
        surv = cand - tdr
        exp = tested * dens(bits) / surv_frac(td) if tested > 0 else 0
        z_y = (pr - exp) / math.sqrt(exp) if exp > 0 else 0.0
        trs = [sum(x["tr"][k] for x in c.values()) for k in range(2)]
        print(f"   кандидатов {cand} ({cand / dur:.0f}/с), простых {pr} ({pr / dur:.2f}/с); "
              f"проверено {tested} из {surv} уцелевших ({100 * tested / max(surv, 1):.1f} %)"
              + (f"; кругов без чьих-то кусочков {trs[0]}, наборов заменено своим шумом {trs[1]}" if over.get("gv") else ""))
        print(f"   {mark(z_td)} отсеяно делителями {100 * tdr / max(cand, 1):.2f} % (теория {100 * q:.2f} %), z {z_td:+.1f}")
        print(f"   {mark(z_y)} простых на проверенных: 1 на {tested / max(pr, 1):.2f} "
              f"(теория {surv_frac(td) / dens(bits):.2f}), z {z_y:+.1f}")
        time.sleep(2)                                   # журнал простых дописывается
        rows = []
        for day in {time.strftime("%Y-%m-%d", time.localtime(t0)), time.strftime("%Y-%m-%d")}:
            rows += prime_stats.load(day, cid=cid)
        lines = prime_stats.report(rows) if rows else ["журнал пуст"]
        for ln in lines:
            print("   " + ln, flush=True)
        temps = [c[m]["temp"] for m in macs if c[m]["temp"] is not None]
        rb1 = {m: next(a for a in dd["agents"] if a["mac"] == m).get("reboots") or 0 for m in macs}
        rebooted = [f"№{ids[m]}" for m in macs if rb1[m] != rb0.get(m, rb1[m])]
        if rebooted:                                    # счётчики перезапущенного обнулились — итоги шага неполные
            print(f"   ◐ перезапуск во время шага: {', '.join(rebooted)} (журнал простых полный, счётчики — нет)", flush=True)
        report["steps"].append({"key": key, "name": name, "cfg": over, "cid": cid, "dur": round(dur, 1), "cand": cand,
                                "ids": [ids[m] for m in macs], "stalls": stalls, "rebooted": rebooted, "rest": rest,
                                "tdr": tdr, "tested": tested, "pr": pr, "logged": len(rows), "tr": trs,
                                "z_sieve": round(z_td, 2), "z_yield": round(z_y, 2), "stream": lines,
                                "temp": [min(temps), max(temps)] if temps else None,
                                "agents": {ids[m]: c[m] for m in macs}})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    total, dups = prime_stats.global_dups()
    print(f"\n{'✗' if dups else '✓'} повторы по всем журналам за все дни: {len(dups)} из {total} простых", flush=True)
    for h, where in dups[:10]:
        print(f"   {h[:24]}… — " + "; ".join(f"{dd} …{m[-5:]} настройка {ci}" for dd, m, ci in where))
    report["global"] = {"primes": total, "dups": [[h, w] for h, w in dups[:100]]}
    if stop_after:
        r = post("/api/compose", {"action": "stop"})
        print("все генераторы — «Стоп»" if r.get("ok") else f"«Стоп» не вышел: {r.get('error')}", flush=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\nотчёт: {os.path.relpath(path, ROOT)}")
    print("\nсводка:")
    for s in report["steps"]:
        flags = [ln for ln in s["stream"] if ln.startswith("✗") or ln.startswith("◐")]
        print(f"  {s['name']}: простых {s['pr']}, отсев z {s['z_sieve']:+.1f}, выход z {s['z_yield']:+.1f}"
              + (f"; поток: {len(flags)} отметок — " + " | ".join(f[:70] for f in flags) if flags else "; поток чистый"))


if __name__ == "__main__":
    main()
