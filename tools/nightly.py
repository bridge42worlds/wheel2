"""Ежевечерняя «зарядка»: один и тот же набор коротких прогонов, таблица по платам и сравнение
с прошлыми вечерами — плата, которая начинает «сдавать», видна по сползанию цифр до того, как
испортит опыт.

    python tools/nightly.py [минут на шаг] [--steps rng,relay,cascade,csi_hash] [--only-compare]

Шаги — из tools/anomaly_run.py (STEPS); перед каждым — минута простоя на 80 МГц, чтобы все шаги
начинались из похожего состояния. По каждому шагу и плате из записи журнала опытов берутся:
простых/с, выход против теории (z), кристалл (мин/макс), наименьшая доля работы регулятора,
потери пакетов, переподключения Wi-Fi. В конце — кто кого слышит по пульсу ESP-NOW (сигнал по
парам). Строки дописываются в data/nightly.jsonl. Сравнение: медиана и MAD по той же плате и
шагу за прошлые вечера; ◐ — отклонение больше 3 MAD или 10 %, ✗ — больше 6 MAD или 20 %.
--only-compare — ничего не гонять, только пересчитать сравнение по записям.
"""
import json, os, statistics, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anomaly_run import STEPS                           # noqa: E402

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PATH = os.path.join(ROOT, "data", "nightly.jsonl")
DEFAULT_STEPS = "rng,relay,cascade,one"      # CSI отдельно (--steps=csi_hash): 07.10.2026 с ним платы уходили в «зомби»
STANDBY_S = 60


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=20))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


def generators():
    d = get("/api/dash?window=10")
    macs = [a["mac"] for a in d["agents"] if a["status"] == "online" and d["gen"].get(a["mac"]) is not None
            and (d["gen"][a["mac"]].get("role") or 0) == 0]
    names = {a["mac"]: f"№{a['id']}" for a in d["agents"]}
    return d, macs, names


def run_step(key, minutes, macs, default):
    name, over = STEPS[key]
    cfg = {**default, **over, "run": 1, "pl": 1}
    r = post("/api/control", {"targets": macs, "cfg": cfg, "own": {}, "reset": True})
    if not r.get("ok"):
        raise SystemExit(f"шаг {key}: настройка не принята: {r.get('error')}")
    cid = r["cid"]
    print(f"  {key}: «{name}», настройка №{cid}, {minutes:g} мин", flush=True)
    time.sleep(minutes * 60)
    post("/api/control", {"targets": macs, "cfg": {**cfg, "run": 0, "cpu": 80, "led": 0}, "own": {}})
    time.sleep(3)
    for _ in range(10):                                  # запись закрывается при остановке — найти её
        rec = next((x for x in get("/api/runs?n=10")["list"] if x.get("cid") == cid and x.get("t1")), None)
        if rec:
            return get(f"/api/run?id={rec['id']}")
        time.sleep(2)
    raise SystemExit(f"шаг {key}: запись опыта №{cid} не нашлась")


def rows_of(night, key, run, names):
    out = []
    dur = run.get("dur_s") or 1
    for mac, x in (run.get("results") or {}).items():
        y = x.get("yield") or {}
        out.append({"night": night, "step": key, "agent": names.get(mac, mac), "mac": mac, "run": run["id"],
                    "pr_s": round((x.get("pr") or 0) / dur, 3), "cand_s": round((x.get("cand") or 0) / dur, 2),
                    "yield_z": y.get("z"), "temp": x.get("temp"), "work_min": x.get("work_min"),
                    "lost": x.get("lost"), "udpf": x.get("udpf"), "wrst": x.get("wrst"), "fw": x.get("fw"),
                    "heap": x.get("heap")})     # свободная память в конце шага: утечка видна по вечерам
    return out


def hearing(names):
    """Кто кого слышит по пульсу: {агент: {сосед: сигнал}}."""
    out = {}
    for a in get("/api/state")["agents"]:
        nb = (a.get("nbr") or {}).get("nb")
        if nb is None:
            continue
        out[names.get(a["mac"], a["mac"])] = {f"№{x[0]}": x[1] for x in nb if x[2] < 30}   # слышали за полминуты
    return out


def load_rows():
    rows = []
    if os.path.exists(PATH):
        for ln in open(PATH, encoding="utf-8"):
            try:
                rows.append(json.loads(ln))
            except ValueError:
                pass
    return rows


def compare(rows, night):
    """Сегодняшние строки против медианы прошлых вечеров по той же плате и шагу."""
    past = [r for r in rows if r.get("night") != night and r.get("step")]
    today = [r for r in rows if r.get("night") == night and r.get("step")]
    print(f"\nсравнение с прошлыми вечерами ({len({r['night'] for r in past})} шт.):")
    print(f"  {'шаг':12} {'плата':6} {'простых/с':>10} {'раньше':>8} {'откл.':>8} {'выход z':>8} {'кристалл':>10} {'работа':>7} {'потери':>7} {'wrst':>5}")
    for r in sorted(today, key=lambda x: (x["step"], x["agent"])):
        hist = [p["pr_s"] for p in past if p["step"] == r["step"] and p["agent"] == r["agent"] and p.get("pr_s")]
        mark, prev, dev = " ", "—", "—"
        if len(hist) >= 2:
            med = statistics.median(hist)
            mad = statistics.median(abs(h - med) for h in hist) or 0.05 * med or 0.01
            pct = 100 * (r["pr_s"] - med) / med if med else 0
            k = abs(r["pr_s"] - med) / mad
            mark = "✗" if k > 6 or abs(pct) > 20 else "◐" if k > 3 or abs(pct) > 10 else "✓"
            prev, dev = f"{med:.1f}", f"{pct:+.0f} %"
        t = r.get("temp") or []
        print(f"{mark} {r['step']:12} {r['agent']:6} {r['pr_s']:10.1f} {prev:>8} {dev:>8} "
              f"{(r.get('yield_z') if r.get('yield_z') is not None else '—'):>8} "
              f"{(f'{t[0]:.0f}–{t[1]:.0f}' if len(t) >= 2 else '—'):>10} "
              f"{(f'{r['work_min']:.0f} %' if r.get('work_min') is not None else '—'):>7} "
              f"{(r.get('lost') if r.get('lost') is not None else '—'):>7} {(r.get('wrst') or 0):>5}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    minutes = float(args[0]) if args else 3
    steps = DEFAULT_STEPS
    for a in sys.argv[1:]:
        if a.startswith("--steps="):
            steps = a.split("=", 1)[1]
    keys = [k.strip() for k in steps.split(",") if k.strip()]
    bad = [k for k in keys if k not in STEPS]
    if bad:
        raise SystemExit(f"нет таких шагов: {bad}; есть: {', '.join(STEPS)}")
    night = time.strftime("%Y-%m-%d")
    if "--only-compare" in sys.argv:
        compare(load_rows(), night)
        return
    d, macs, names = generators()
    if not macs:
        raise SystemExit("нет генераторов на связи")
    default = d["default"]
    print(f"зарядка {night}: {', '.join(names[m] for m in macs)}; шаги {', '.join(keys)} по {minutes:g} мин", flush=True)
    rows = []
    try:
        for k in keys:
            post("/api/control", {"targets": macs, "cfg": {**default, "run": 0, "cpu": 80, "led": 0}, "own": {}})
            print(f"  простой {STANDBY_S} с", flush=True)
            time.sleep(STANDBY_S)
            run = run_step(k, minutes, macs, default)
            rows += rows_of(night, k, run, names)
            s = run.get("summary") or {}
            print(f"    итог: простых {s.get('pr')}, {s.get('pr_s')}/с, кристалл {s.get('temp')}", flush=True)
    finally:
        post("/api/control", {"targets": macs, "cfg": {**default, "run": 0, "cpu": 80, "led": 0}, "own": {}})
    h = hearing(names)
    with open(PATH, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if h:
            f.write(json.dumps({"night": night, "hearing": h}, ensure_ascii=False) + "\n")
    if h:
        ids = sorted(h, key=lambda s: int(s.lstrip("№") or 0))
        print("\nкто кого слышит (сигнал, дБм; строка — кто слышит):")
        print("        " + " ".join(f"{i:>6}" for i in ids))
        for a in ids:
            print(f"  {a:6}" + " ".join(f"{(h[a].get(b, '—') if b != a else '·'):>6}" for b in ids))
    compare(load_rows(), night)
    print(f"\nзаписи: {os.path.relpath(PATH, ROOT)}")


if __name__ == "__main__":
    main()
