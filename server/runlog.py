# -*- coding: utf-8 -*-
"""Журнал опытов: каждый запуск счёта записывается сам — что было задано и что вышло.

Запуск — настройка с run = 1 у набора агентов (с панели, из сессии, программы, прогона —
откуда угодно). Он открывается, когда сервер отправил такую настройку, и закрывается, когда
тем же агентам ушла остановка или другая настройка (или агенты пропали). Пока запуск идёт,
раз в SAMPLE_S секунд по каждому агенту записываются температура, доля работы, сигнал, темп
простых и кандидатов, потери пакетов — ряды хранятся в самой записи, поэтому графики опыта
открываются и через месяц, без сырья и памяти сервера. При закрытии — итог: параметры
словами, длительность, кандидатов и простых (и выход против теории), диапазон температур,
доля работы, потери пакетов и переподключения Wi-Fi, сбои и согласие группы, рассеяние,
журнал простых, проверка сервером; если запуск был частью сессии или программы — какой.

Хранение: data/runs/<дата>.jsonl — строка на запуск; список последних — в памяти.
Модуль не знает внутренностей сервера: всё нужное передаётся функциями (hooks).
"""
import json, math, os, threading, time

SAMPLE_S = 15
MAX_SAMPLES = 2400          # 10 часов по 15 с
KEEP = 300                  # записей в памяти для списка

SM_NAMES = {0: "тишина", 1: "каждый B — всё число", 2: "россыпь", 3: "эстафета", 4: "пустышка"}
SF_NAMES = {0: "хеш", 1: "XOR", 2: "умножение", 3: "перестановка"}
CF_NAMES = {0: "", 1: "CSI только от роутера", 2: "CSI только с кадров B"}
SRC_NAMES = [(1, "ГСЧ"), (2, "CSI"), (4, "АЦП"), (8, "дрожание"), (16, "эфир B")]
NR_NAMES = {1: "24 Мбит/с", 2: "6 Мбит/с", 0: "1 Мбит/с"}


GV_NAMES = {0: "сообща — все на одном числе", 1: "сообща — эстафета", 2: "сообща — каскад"}


def src_text(mask):
    return " + ".join(n for b, n in SRC_NAMES if (mask or 0) & b) or "—"


def cfg_text(cfg, own, names):
    """Параметры запуска словами: режим, длина, базы, источники, отсев, связь, рассеяние, свои у агентов."""
    c = cfg or {}
    parts = [c["label"]] if c.get("label") else []      # состав: «группа 1 · эстафета», «тушение», «стоп»
    gv = c.get("gv") or 0
    if c.get("mode") == 1:
        parts.append("%s (группа %s)" % (GV_NAMES.get(gv, "сообща"), " → ".join("№%d" % i for i in c.get("grp") or [])))
    else:
        parts.append("каждый сам")
    parts.append("%d бит" % c.get("bits", 0))
    parts.append("баз %d%s" % (c.get("nb", 0), " (все проверяют каждого)" if c.get("all") else ""))
    parts.append("отсев < %d" % c["td"] if c.get("td") else "без отсева")
    parts.append("шум: " + src_text(c.get("src")) + {1: " без хеша", 2: " без хеша, фон Нейман"}.get(c.get("raw") or 0, ""))
    if c.get("dc"):
        parts.append("два ядра")
    if c.get("mode") == 1 and not gv:
        parts.append("K %d%s, связь %s" % (c.get("k", 24), ", кусочки заранее" if c.get("pre", 1) else "", NR_NAMES.get(c.get("nr", 1), "?")))
    elif c.get("mode") == 1:
        parts.append("простых за ход %d, кусочки %s%s, связь %s" % (
            c.get("rk", 1), "ждать не дольше %d мс" % c["pw"] if c.get("pw") else "ждать всех",
            ", заранее" if c.get("pre", 1) else "", NR_NAMES.get(c.get("nr", 1), "?")))
    if c.get("bg"):
        parts.append("сет B: %s — %s%s%s%s" % (", ".join("№%d" % i for i in c["bg"]), SM_NAMES.get(c.get("sm"), "?"),
                                                 ", пустышка" if c.get("sh") else "", ", фон %d/с" % c["bgr"] if c.get("bgr") else "",
                                                 (", " + CF_NAMES[c["cf"]]) if c.get("cf") else ""))
    if c.get("pl"):
        parts.append("журнал простых")
    if c.get("tl") == 2:
        parts.append("молчание телеметрии")
    parts.append("предел %d °C" % c.get("tmax", 70))
    if c.get("cpu", 240) != 240:
        parts.append("%d МГц" % c["cpu"])
    if c.get("txp", 20) != 20:
        parts.append("%d дБм" % c["txp"])
    own_parts = []
    defaults = {"role": 0, "src": 1, "sf": 0, "tl": 0, "tmax": 70, "txp": 20, "cpu": 240, "ping": 100, "led": 1, "rmm": 450,
                "afs": 2, "avg": 2, "tq": 0}
    for mac, o in (own or {}).items():
        bits = []
        for k, v in o.items():
            if defaults.get(k) == v:                   # как по умолчанию — не своё
                continue
            if k == "src":
                bits.append("шум " + src_text(v))
            elif k == "role":
                bits.append({0: "генерация", 1: "рассеяние", 2: "статический"}.get(v, str(v)))
            elif k == "sf":
                bits.append("функция " + SF_NAMES.get(v, str(v)))
            elif k in ("tmax", "txp", "cpu", "ping", "led", "tl", "rmm", "afs", "avg"):
                bits.append("%s %s" % (k, v))
        if bits:
            own_parts.append("%s: %s" % (names.get(mac, mac), ", ".join(bits)))
    if own_parts:
        parts.append("свои — " + "; ".join(own_parts))
    return "; ".join(parts)


def surv_frac(td):
    s = 1.0
    for p in range(3, int(td or 0), 2):
        if all(p % q for q in range(3, int(p ** 0.5) + 1, 2)):
            s *= 1 - 1 / p
    return s


_SURV = {}


def yield_z(cand, tdr, pr, bits, td):
    """Выход простых против теории: (1 на … кандидатов, теория, z по кандидатам, 1 на … уцелевших, теория, z)."""
    if not cand or not bits or cand < 2000:
        return None
    if td not in _SURV:
        _SURV[td] = surv_frac(td)
    dens = 2 / (bits * math.log(2) - 0.307)
    mri = cand - (tdr or 0)
    exp_c = cand * dens
    exp_m = mri * dens / _SURV[td]
    return {"per_cand": round(cand / max(pr, 1), 2), "exp_cand": round(1 / dens, 2),
            "z_cand": round((pr - exp_c) / math.sqrt(exp_c), 2),
            "per_surv": round(mri / max(pr, 1), 3), "exp_surv": round(_SURV[td] / dens, 3),
            "z": round((pr - exp_m) / math.sqrt(exp_m), 2) if exp_m > 0 else None,
            "surv_pct": round(100 * mri / cand, 3), "exp_surv_pct": round(100 * _SURV[td], 3)}


class RunLog:
    """hooks: agent(mac) -> {id, label, values, gen, gstat, verify, plog, lost, udpf…} (снимок под замком сервера),
    context() -> str | None (сессия №…, программа №… шаг «…»), log(text)."""

    def __init__(self, data_dir, hooks):
        self.dir = os.path.join(data_dir, "runs")
        os.makedirs(self.dir, exist_ok=True)
        self.h = hooks
        self.lock = threading.Lock()
        self.open = {}                      # cid -> запись
        self.recent = []                    # закрытые, последние KEEP
        self.next_id = 1
        self._load_recent()

    # ---------- хранение ----------
    def _path(self, t):
        return os.path.join(self.dir, time.strftime("%Y-%m-%d", time.localtime(t)) + ".jsonl")

    def _load_recent(self):
        files = sorted(f for f in os.listdir(self.dir) if f.endswith(".jsonl"))
        rows = []
        for fn in files[-7:]:
            for ln in open(os.path.join(self.dir, fn), encoding="utf-8"):
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
        self.recent = rows[-KEEP:]
        self.next_id = max([r.get("id", 0) for r in rows] + [0]) + 1

    def _store(self, r):
        with open(self._path(r["t0"]), "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- события ----------
    def on_apply(self, cid, targets, cfg, own, new_cid, t, names):
        """Сервер отправил настройку. new_cid — у этих агентов новый номер (счёт с нуля)."""
        with self.lock:
            run = cfg.get("run")
            # закрыть открытые запуски, в которых есть эти агенты, если им ушла остановка или новый номер
            for c, r in list(self.open.items()):
                if set(r["macs"]) & set(targets) and (not run or (new_cid and c != cid)):
                    self._close(r, t, "остановлен" if not run else "сменилась настройка")
                    del self.open[c]
            if run and new_cid and cid not in self.open:
                r = {"id": self.next_id, "cid": cid, "t0": round(t, 3), "t1": None, "macs": list(targets),
                     "agents": {m: names.get(m, m) for m in targets}, "cfg": {k: v for k, v in cfg.items() if k != "run"},
                     "own": {m: o for m, o in (own or {}).items() if m in targets},
                     "text": cfg_text(cfg, {m: o for m, o in (own or {}).items() if m in targets}, names),
                     "context": self.h["context"](), "samples": {m: [] for m in targets}, "state": "идёт"}
                self.next_id += 1
                self.open[cid] = r

    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            for cid, r in list(self.open.items()):
                alive = 0
                for m in r["macs"]:
                    a = self.h["agent"](m)
                    if not a:
                        continue
                    v = a.get("values") or {}
                    g = a.get("gen") or {}
                    rate = g.get("rate") or {}
                    gs = a.get("gstat") or {}
                    grate = gs.get("rate") or {}
                    if (a.get("last") and now - a["last"] < 10) or a.get("quiet"):   # молчит по команде — жив
                        alive += 1
                    s = r["samples"][m]
                    if len(s) < MAX_SAMPLES:
                        s.append([round(now), v.get("temp"), v.get("work"), v.get("rssi"),
                                  rate.get("g_pr_s"), rate.get("g_cand_s"), grate.get("g_cyc_s"),
                                  a.get("run_lost"), v.get("udpf"), v.get("wrst"), v.get("cpu0"), v.get("cpu1"),
                                  v.get("heap")])
                if not alive and now - r["t0"] > 60 and all(len(r["samples"][m]) for m in r["macs"]):
                    last_seen = max((self.h["agent"](m) or {}).get("last") or 0 for m in r["macs"])
                    if now - last_seen > 120:
                        self._close(r, last_seen or now, "агенты пропали")
                        del self.open[cid]

    def _close(self, r, t1, why):
        r["t1"] = round(t1, 3)
        r["dur_s"] = round(r["t1"] - r["t0"], 1)
        r["state"] = why
        res, summ = {}, {"cand": 0, "pr": 0, "tdr": 0, "lost": 0, "udpf": 0, "wrst": 0}
        temps = []
        for m in r["macs"]:
            a = self.h["agent"](m) or {}
            g = {k: v for k, v in (a.get("gen") or {}).items() if k not in ("rate", "e", "bt", "br", "bus", "tph")}
            gs = {k: v for k, v in (a.get("gstat") or {}).items() if k != "rate"}
            s = r["samples"].get(m) or []
            ts = [x[1] for x in s if x[1] is not None]
            ws = [x[2] for x in s if x[2] is not None]
            rs = [x[3] for x in s if x[3] is not None]
            cand, tdr, pr = g.get("cand") or 0, g.get("tdr") or 0, g.get("pr") or 0
            # числа проверяет сам агент: «каждый сам» и каскад; эстафета — нет: уцелевшие после отсева уезжают
            # к следующему проверяющему с ходом и частью теряются, выход против теории выходил бы занижен
            # (07.10.2026: z −3…−4 у всех при чистом потоке)
            own_yield = not g.get("role") and (g.get("mode") == 0 or g.get("gv") == 2)
            if g.get("mode") == 1 and not g.get("role") and not g.get("gv"):
                pr = gs.get("primes_m") or 0
            res[m] = {"id": a.get("id"), "cand": cand, "tdr": tdr, "pr": pr, "role": g.get("role"), "mode": g.get("mode"),
                      "own": bool(own_yield),
                      "temp": [min(ts), max(ts), round(sum(ts) / len(ts), 1)] if ts else None,
                      "work_min": min(ws) if ws else None, "rssi": [min(rs), max(rs)] if rs else None,
                      "lost": (s[-1][7] or 0) - (s[0][7] or 0) if s and s[0][7] is not None and s[-1][7] is not None else None,
                      "udpf": (s[-1][8] or 0) - (s[0][8] or 0) if s and s[0][8] is not None and s[-1][8] is not None else None,
                      "wrst": (s[-1][9] or 0) - (s[0][9] or 0) if s and s[0][9] is not None and s[-1][9] is not None else None,
                      "yield": yield_z(cand, tdr, pr, g.get("bits"), g.get("td")) if own_yield else None,
                      "verify": a.get("verify"), "plog": a.get("plog"),
                      "group": {k: gs.get(k) for k in ("cycles_m", "primes_m", "asks_tx", "brk_pcs", "brk_res", "brk_hand",
                                                        "resyncs", "conflicts", "streak_max", "agr", "split", "dis", "tx_fail",
                                                        "tr")} if gs else None,
                      "scatter": {k: g.get(k) for k in ("ann", "adrop", "heard", "prx", "stx", "sdrop", "swait", "sbg", "etx", "rxdrop")}
                      if g.get("role") is not None else None,
                      "dcq": g.get("dcq"), "fw": a.get("fw"),
                      "heap": next((x[12] for x in reversed(s) if len(x) > 12 and x[12] is not None), None)}
            summ["cand"] += cand
            summ["pr"] += pr
            summ["tdr"] += tdr
            for k in ("lost", "udpf", "wrst"):
                summ[k] += res[m][k] or 0
            temps += ts
        summ["pr_s"] = round(summ["pr"] / r["dur_s"], 3) if r["dur_s"] else None
        summ["temp"] = [min(temps), max(temps)] if temps else None
        gens = [x for x in res.values() if x.get("own")]
        if gens and (r["cfg"].get("mode") == 0 or r["cfg"].get("gv") == 2):
            summ["yield"] = yield_z(sum(x["cand"] for x in gens), sum(x["tdr"] for x in gens), sum(x["pr"] for x in gens),
                                    r["cfg"].get("bits"), r["cfg"].get("td"))
        grp = [x["group"] for x in res.values() if x.get("group")]
        if grp:
            summ["group"] = {"cycles": sum(x.get("cycles_m") or 0 for x in grp), "breaks": sum((x.get("brk_pcs") or 0) + (x.get("brk_res") or 0) + (x.get("brk_hand") or 0) for x in grp),
                             "asks": sum(x.get("asks_tx") or 0 for x in grp), "resyncs": sum(x.get("resyncs") or 0 for x in grp),
                             "agree": sum(x.get("agr") or 0 for x in grp), "split": sum((x.get("split") or 0) + (x.get("dis") or 0) for x in grp),
                             "truncs": sum((x.get("tr") or [0, 0])[0] for x in grp)}
        r["results"] = res
        r["summary"] = summ
        if self.h.get("static"):                       # статические акселерометры за время опыта
            try:
                r["static"] = self.h["static"](r["t0"], t1)
            except Exception as e:                     # noqa: BLE001 — журнал важнее
                r["static"] = {"error": str(e)}
        self._store(r)
        self.recent.append(r)
        self.recent = self.recent[-KEEP:]
        self.h["log"](f"[runs] №{r['id']} {why}: {r['text'][:60]}… — простых {summ['pr']}, {summ.get('pr_s')}/с, "
                      f"температура {summ['temp']}")

    # ---------- наружу ----------
    def brief(self, r):
        b = {k: r.get(k) for k in ("id", "cid", "t0", "t1", "dur_s", "macs", "agents", "text", "context", "state", "summary")}
        st = r.get("static") or {}
        if st.get("sensors"):                          # статика коротко: вывод по каждому датчику
            b["static"] = {"verdict": st.get("verdict"),
                           "sensors": {m: {"verdict": x.get("verdict"), "windows": x.get("windows"),
                                           "reasons": (x.get("reasons") or [])[:2]} for m, x in st["sensors"].items()}}
        return b

    def list(self, n=60):
        with self.lock:
            opened = [{**self.brief(r), "dur_s": round(time.time() - r["t0"], 1)} for r in self.open.values()]
            return opened + [self.brief(r) for r in reversed(self.recent[-n:])]

    def get(self, rid):
        with self.lock:
            for r in list(self.open.values()) + self.recent:
                if r.get("id") == rid:
                    return r
        # не в памяти — поискать в файлах
        for fn in sorted(os.listdir(self.dir), reverse=True):
            if not fn.endswith(".jsonl"):
                continue
            for ln in open(os.path.join(self.dir, fn), encoding="utf-8"):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("id") == rid:
                    return r
        raise ValueError("нет такого опыта")
