"""Восстановить журнал опытов (data/runs/<дата>.jsonl) из сырья: data/commands.jsonl и data/raw/<дата>.jsonl.

    python tools/runs_from_raw.py [дата YYYY-MM-DD] [--force]

Нужно для дней до появления журнала (он ведётся сервером с 04.10.2026 вечера) или если сервер
не работал. Делает то же, что runlog.RunLog, только задним числом: запуск открывается командой
с run = 1 и новым номером настройки, закрывается остановкой или другой настройкой тем же
агентам; раз в 15 с по телеметрии — температура, доля работы, сигнал, темп простых и
кандидатов (по разности счётчиков), потери пакетов (по пропускам seq); итог — по последнему
пакету настройки. Проверка сервером и журнал простых задним числом не восстанавливаются.
"""
import csv, json, os, sys, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "server"))
from runlog import cfg_text, yield_z   # noqa: E402

SAMPLE_S = 15


def names_from_registry():
    out = {}
    try:
        for row in csv.DictReader(open(os.path.join(ROOT, "registry", "devices.csv"), encoding="utf-8")):
            out[row["mac"].lower()] = f"№{row['id']}"
    except OSError:
        pass
    return out


def sessions_index(day0, day1):
    out = []
    d = os.path.join(ROOT, "data", "sessions")
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if fn[:-5].isdigit():
            try:
                x = json.load(open(os.path.join(d, fn), encoding="utf-8"))
                if x.get("t_start") and day0 <= x["t_start"] <= day1:
                    out.append((x["t_start"], x.get("t_stop") or x["t_start"] + 1, f"сессия №{x['id']} «{x.get('name', '')}»"))
            except Exception:
                pass
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = args[0] if args else time.strftime("%Y-%m-%d")
    force = "--force" in sys.argv
    out_path = os.path.join(ROOT, "data", "runs", day + ".jsonl")
    if os.path.exists(out_path) and not force:
        raise SystemExit(f"{out_path} уже есть — добавьте --force, чтобы переписать")
    day0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
    day1 = day0 + 86400
    names = names_from_registry()
    sess = sessions_index(day0, day1)
    # --- команды: границы запусков ---
    cmds = []
    for ln in open(os.path.join(ROOT, "data", "commands.jsonl"), encoding="utf-8"):
        try:
            c = json.loads(ln)
        except Exception:
            continue
        if "cfg" in c and day0 <= c["t"] < day1:
            cmds.append(c)
    runs, open_runs, rid = [], {}, 1
    for c in cmds:
        targets = [m.lower() for m in c["targets"]]
        run = c["cfg"].get("run")
        new = c.get("new") or []
        for cid, r in list(open_runs.items()):
            if set(r["macs"]) & set(targets) and (not run or (new and cid != c["cid"])):
                r["t1"] = c["t"]
                r["state"] = "остановлен" if not run else "сменилась настройка"
                runs.append(r)
                del open_runs[cid]
        if run and new and c["cid"] not in open_runs:
            own = {m.lower(): o for m, o in (c.get("own") or {}).items() if m.lower() in targets}
            ctx = next((n for a, b, n in sess if a - 5 <= c["t"] <= b), None)
            open_runs[c["cid"]] = {"id": None, "cid": c["cid"], "t0": c["t"], "t1": None, "macs": targets,
                                   "agents": {m: names.get(m, m) for m in targets},
                                   "cfg": {k: v for k, v in c["cfg"].items() if k != "run"}, "own": own,
                                   "text": cfg_text(c["cfg"], own, names), "context": ctx,
                                   "samples": {m: [] for m in targets}, "state": "идёт"}
    for r in open_runs.values():                       # не закрытые командой — до конца дня
        r["t1"] = min(day1, time.time())
        r["state"] = "конец записи"
        runs.append(r)
    runs = [r for r in runs if r["t1"] - r["t0"] >= 5]
    runs.sort(key=lambda r: r["t0"])
    by_cid = {}
    for r in runs:
        by_cid.setdefault(r["cid"], []).append(r)
    # --- сырьё: ряды и последние пакеты ---
    last_tel, last_grp, prev, lost = {}, {}, {}, {}
    raw = os.path.join(ROOT, "data", "raw", day + ".jsonl")
    for ln in open(raw, encoding="utf-8"):
        try:
            rec = json.loads(ln)
            p = json.loads(rec["raw"])
        except Exception:
            continue
        if p.get("type") not in ("tel", "grp") or "cid" not in p:
            continue
        mac = str(p["mac"]).lower()
        t = rec["rx"]
        for r in by_cid.get(p["cid"], []):
            if mac not in r["samples"] or not (r["t0"] - 1 <= t <= r["t1"] + 3):
                continue
            key = (r["cid"], mac)
            if p["type"] == "grp":
                last_grp[key] = p
                continue
            last_tel[key] = p
            pv = prev.get(key)
            if pv is not None and isinstance(p.get("seq"), int) and p["seq"] > pv["seq"] + 1:
                lost[key] = lost.get(key, 0) + p["seq"] - pv["seq"] - 1
            s = r["samples"][mac]
            if not s or t - s[-1][0] >= SAMPLE_S:
                dt = (t - pv["_t"]) if pv else 0
                pr_s = ((p.get("pr", 0) - pv["pr"]) / dt) if pv and dt > 0 else None
                cand_s = ((p.get("cand", 0) - pv["cand"]) / dt) if pv and dt > 0 else None
                g0 = last_grp.get(key) or {}
                s.append([round(t), p.get("temp"), p.get("work"), p.get("rssi"), None if pr_s is None else round(pr_s, 2),
                          None if cand_s is None else round(cand_s, 1), None, lost.get(key, 0), p.get("udpf"), p.get("wrst"),
                          p.get("cpu0"), p.get("cpu1")])
                prev[key] = {"_t": t, "pr": p.get("pr", 0), "cand": p.get("cand", 0), "seq": p.get("seq", 0)}
            elif pv is not None:
                pv["seq"] = p.get("seq", pv["seq"])
            else:
                prev[key] = {"_t": t, "pr": p.get("pr", 0), "cand": p.get("cand", 0), "seq": p.get("seq", 0)}
    # --- итоги ---
    out = []
    for r in runs:
        if not any(r["samples"][m] for m in r["macs"]):
            continue
        # состав группы и сета B в командах не записан: группа — из телеметрии, сет B — из своих ролей
        for m in r["macs"]:
            g = (last_tel.get((r["cid"], m)) or {}).get("grp")
            if g:
                r["cfg"]["grp"] = g
                break
        bg = sorted(int(names.get(m, "№0")[1:]) for m, o in r["own"].items() if o.get("role") == 1 and names.get(m, "№x")[1:].isdigit())
        if bg:
            r["cfg"]["bg"] = bg
        r["text"] = cfg_text({**r["cfg"], "run": 1}, r["own"], names)
        r["id"] = rid
        rid += 1
        r["dur_s"] = round(r["t1"] - r["t0"], 1)
        res, summ, temps = {}, {"cand": 0, "pr": 0, "tdr": 0, "lost": 0, "udpf": 0, "wrst": 0}, []
        for m in r["macs"]:
            key = (r["cid"], m)
            p = last_tel.get(key) or {}
            g = last_grp.get(key) or {}
            s = r["samples"][m]
            ts = [x[1] for x in s if x[1] is not None]
            ws = [x[2] for x in s if x[2] is not None]
            rs = [x[3] for x in s if x[3] is not None]
            cand, tdr, pr = p.get("cand") or 0, p.get("tdr") or 0, p.get("pr") or 0
            if p.get("mode") == 1 and not p.get("role"):
                pr = g.get("primes_m") or 0
            u = [x[8] for x in s if x[8] is not None]
            w = [x[9] for x in s if x[9] is not None]
            res[m] = {"id": int(names.get(m, "№0")[1:]) if names.get(m, "№x")[1:].isdigit() else 0,
                      "cand": cand, "tdr": tdr, "pr": pr, "role": p.get("role"), "mode": p.get("mode"),
                      "temp": [min(ts), max(ts), round(sum(ts) / len(ts), 1)] if ts else None,
                      "work_min": min(ws) if ws else None, "rssi": [min(rs), max(rs)] if rs else None,
                      "lost": lost.get(key, 0), "udpf": (u[-1] - u[0]) if len(u) > 1 else 0, "wrst": (w[-1] - w[0]) if len(w) > 1 else 0,
                      "yield": yield_z(cand, tdr, pr, p.get("bits"), p.get("td")) if p.get("mode") == 0 and not p.get("role") else None,
                      "verify": None, "plog": None,
                      "group": {k: g.get(k) for k in ("cycles_m", "primes_m", "asks_tx", "brk_pcs", "brk_res", "brk_hand",
                                                       "resyncs", "conflicts", "streak_max", "agr", "split", "dis", "tx_fail")} if g else None,
                      "scatter": {k: p.get(k) for k in ("ann", "adrop", "heard", "prx", "stx", "sdrop", "swait", "sbg", "etx", "rxdrop")}
                      if p.get("role") is not None else None,
                      "dcq": p.get("dcq"), "fw": p.get("fw")}
            summ["cand"] += cand
            summ["pr"] += pr
            summ["tdr"] += tdr
            for k in ("lost", "udpf", "wrst"):
                summ[k] += res[m][k] or 0
            temps += ts
        summ["pr_s"] = round(summ["pr"] / r["dur_s"], 3) if r["dur_s"] else None
        summ["temp"] = [min(temps), max(temps)] if temps else None
        gens = [x for x in res.values() if x["mode"] == 0 and not x["role"]]
        if gens and r["cfg"].get("mode") == 0:
            summ["yield"] = yield_z(sum(x["cand"] for x in gens), sum(x["tdr"] for x in gens), sum(x["pr"] for x in gens),
                                    r["cfg"].get("bits"), r["cfg"].get("td"))
        grp = [x["group"] for x in res.values() if x.get("group")]
        if grp:
            summ["group"] = {"cycles": sum(x.get("cycles_m") or 0 for x in grp),
                             "breaks": sum((x.get("brk_pcs") or 0) + (x.get("brk_res") or 0) + (x.get("brk_hand") or 0) for x in grp),
                             "asks": sum(x.get("asks_tx") or 0 for x in grp), "resyncs": sum(x.get("resyncs") or 0 for x in grp),
                             "agree": sum(x.get("agr") or 0 for x in grp), "split": sum((x.get("split") or 0) + (x.get("dis") or 0) for x in grp)}
        r["results"], r["summary"] = res, summ
        r["t0"], r["t1"] = round(r["t0"], 3), round(r["t1"], 3)
        out.append(r)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{day}: команд {len(cmds)}, запусков {len(out)} → {os.path.relpath(out_path, ROOT)}")
    for r in out[-8:]:
        s = r["summary"]
        print(f"  №{r['id']} {time.strftime('%H:%M', time.localtime(r['t0']))} {r['dur_s']:5.0f} с  {r['text'][:70]}…  простых {s['pr']} "
              f"({s.get('pr_s')}/с) temp {s.get('temp')}")


if __name__ == "__main__":
    main()
