"""Программа автоматического эксперимента: шаги режимов, контроль, аномалии.

Программа — список шагов. Шаг: общие параметры (cfg), свои у агентов (own),
длительность, метка «контроль», молчание телеметрии. Повторы, случайный порядок
внутри повтора, время на установление после смены режима. Можно начать с записи
эталона шума статических акселерометров (baseline_min): основные группы стоят.

Каждый шаг запускается с новым номером настройки — счётчики агентов считают
ровно этот шаг. После шага сохраняются итоги генерации и рассеяния, показатели
статических акселерометров за окно шага (без времени установления) и вывод
слежения за ними (static_watch): продолжать, повторить, проверить.

Повторить: в шаге был толчок, датчик сдвинули, сбой прибора — шаг испорчен, он
не идёт в сравнение и повторяется сразу (не больше redo раз).

Аномалия: показатель опытного шага отличается от контрольных шагов больше чем
на z их стандартных отклонений (нужно не меньше трёх контрольных). Показатели —
двух родов: статических акселерометров (полосы, разброс, форма шума) и самой
генерации по каждому агенту и всем вместе (простых и кандидатов в секунду, время
Миллера — Рабина, темп циклов группы, кадров рассеяния, температура кристалла).
Найдена — программа сама добавляет проверку: пары «тот же опыт — контроль» в
случайном порядке. Правило задаётся до запуска и не меняется по ходу (иначе
случайный выброс где-нибудь найдётся всегда).

Шаг, на котором кто-то из агентов не подтвердил настройку, испорчен так же, как
шаг с толчком: он не идёт в сравнение и повторяется.

Модуль не знает внутренностей сервера: всё нужное передаётся в Runner (hooks).
"""
import json, math, os, random, threading, time

# Ковариаты: пишутся в итоги шага, но аномалией не считаются — режимы заведомо греют по-разному,
# и «температура отличается от контроля» сказала бы только это. Сравнивать показатели нужно с
# поправкой на них (пока — глазами: они лежат рядом в записи шага).
COVARIATES = ("temp", "work", "rssi")


def metric_keys(m):
    """Показатели за шаг, которые сравниваются с контролем: статические (полосы, разброс, среднее,
    форма шума) и генерация; без служебного n и без ковариат."""
    return [k for k in m if k != "n" and k not in COVARIATES]


# ---------- критерий аномалии ----------
# Контрольных шагов мало (3–10), и «z = отклонение / sd контролей» — это Стьюдент с n−1 степенями
# свободы, а не нормальное распределение: при трёх контролях |z| ≥ 4 случается в 7 % случаев, а не в
# 0,006 %. Показателей на шаг ~50–100 (полосы × оси × датчики + генерация по агентам), и случайный
# выброс где-нибудь находится почти на каждом шаге. Поэтому: интервал предсказания (множитель
# √(1 + 1/n)), p-вероятность по Стьюденту и поправка Бонферрони на число показателей шага; порог z
# задаёт вероятность ложной тревоги как у нормального распределения — на весь шаг.
def _betacf(a, b, x):
    maxit, eps, fpmin = 300, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = fpmin if abs(d) < fpmin else d
    d = 1 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = fpmin if abs(d) < fpmin else d
        c = 1 + aa / c
        c = fpmin if abs(c) < fpmin else c
        d = 1 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = fpmin if abs(d) < fpmin else d
        c = 1 + aa / c
        c = fpmin if abs(c) < fpmin else c
        d = 1 / d
        de = d * c
        h *= de
        if abs(de - 1) < eps:
            break
    return h


def _betai(a, b, x):
    """Регуляризованная неполная бета-функция I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1 - bt * _betacf(b, a, 1 - x) / b


def t_p(t, df):
    """Двусторонняя вероятность |T| ≥ |t| для Стьюдента с df степенями свободы."""
    if df <= 0:
        return 1.0
    return _betai(df / 2, 0.5, df / (df + t * t))


def norm_p(z):
    """Двусторонняя вероятность |Z| ≥ |z| для нормального распределения."""
    return math.erfc(abs(z) / math.sqrt(2))


def counted(runs):
    """Шаги, которые идут в сравнение: не испорченные."""
    return [r for r in runs if not r.get("spoiled")]


def chans(r):
    """Каналы показателей шага: статические (по MAC) и генерация (gen:№…, gen:все)."""
    out = {m: v for m, v in (r.get("static") or {}).items() if v}
    out.update({"gen:" + k: v for k, v in (r.get("gen") or {}).items() if v})
    return out


def gen_metrics(snap, dur, snap0=None):
    """Показатели генерации за шаг по каждому агенту и всем вместе. Счётчики агентов идут с момента
    применения настройки, а меряется шаг с t0 (после установления) — поэтому берётся разность со
    снимком на t0 (snap0), если он есть. Температура и доля работы — ковариаты (COVARIATES)."""
    out, tot = {}, {"pr_s": 0.0, "cand_s": 0.0}
    dur = max(dur, 1e-6)
    for mac, a in (snap or {}).items():
        g = a.get("gen") or {}
        if not g:
            continue
        a0 = (snap0 or {}).get(mac) or {}
        g0, gs0 = a0.get("gen") or {}, a0.get("gstat") or {}
        gs = a.get("gstat") or {}
        d = lambda x, x0, k: (x.get(k) or 0) - (x0.get(k) or 0) if (x0.get("cid") or x.get("cid")) == x.get("cid") else (x.get(k) or 0)
        m = {}
        pr = d(g, g0, "pr")
        if g.get("mode") == 1 and not g.get("role") and not g.get("gv"):   # все на одном числе: простые и циклы — у мастера
            pr = d(gs, gs0, "primes_m")
            m["cycles_s"] = d(gs, gs0, "cycles_m") / dur
        elif g.get("mode") == 1 and not g.get("role"):
            m["cycles_s"] = d(gs, gs0, "cycles_m") / dur
        m["pr_s"] = pr / dur
        cand = d(g, g0, "cand")
        if cand > 0:
            m["cand_s"] = cand / dur
            m["mr_us"] = d(g, g0, "mrus") / cand
        if g.get("role") == 1:
            m["stx_s"] = d(g, g0, "stx") / dur
        if a.get("temp") is not None:
            m["temp"] = a["temp"]
        if a.get("work") is not None:
            m["work"] = a["work"]
        out[f"№{a.get('id') or mac[-5:]}"] = m
        tot["pr_s"] += m["pr_s"]
        tot["cand_s"] += m.get("cand_s", 0.0)
    if out:
        out["все"] = tot
    return out


def _tests(r, ctrl, only=None):
    """Все сравнения опытного шага r с контролями ctrl: (канал, показатель, значение, среднее, sd, n, t).
    only — названные заранее показатели (program.metrics): точное имя или начало имени; пусто — все."""
    out = []
    for mac, m in chans(r).items():
        for key in metric_keys(m):
            if only and not any(key == o or key.startswith(o) for o in only):
                continue
            base = [chans(c)[mac][key] for c in ctrl if (chans(c).get(mac) or {}).get(key) is not None]
            if len(base) < 3:
                continue
            mu = sum(base) / len(base)
            sd = math.sqrt(sum((x - mu) ** 2 for x in base) / (len(base) - 1))
            if sd <= 0:
                continue
            n = len(base)
            t = (m[key] - mu) / (sd * math.sqrt(1 + 1 / n))     # интервал предсказания нового наблюдения
            out.append((mac, key, m[key], mu, sd, n, t))
    return out


def controls_for(runs, r):
    """Контрольные шаги для опытного r: названные в control_for (парный контроль) или все."""
    ctrl = [c for c in counted(runs) if c.get("control") and chans(c)]
    want = r.get("control_for")
    return [c for c in ctrl if c["name"] == want] if want else ctrl


def anomalies(runs, z_thr, only=None):
    """Опытные шаги против контрольных. Для каждого опытного шага (номер в runs) — список превышений:
    показатель, у которого вероятность по Стьюденту с поправкой на число показателей шага не больше
    вероятности |Z| ≥ z у нормального распределения. only — показатели, названные до запуска
    (program.metrics): меньше показателей — мягче поправка. Испорченные — мимо. Заодно в каждом
    опытном шаге пишется разведка (scan): три самых больших |t| с их вероятностями — чтобы было
    видно, что близко к порогу, без ложных «аномалий»."""
    found = {}
    p_thr = norm_p(z_thr)
    for idx, r in enumerate(runs):
        if r.get("control") or not chans(r) or r.get("spoiled"):
            continue
        ctrl = controls_for(runs, r)
        if len(ctrl) < 3:
            continue
        tests = _tests(r, ctrl, only)
        if not tests:
            continue
        m_tests = len(tests)
        rows = []
        for mac, key, v, mu, sd, n, t in tests:
            p = t_p(t, n - 1)
            rows.append({"mac": mac, "metric": key, "z": round(t, 2), "value": round(v, 2), "ctrl": round(mu, 2),
                         "sd": round(sd, 3), "n": n, "p": p, "p_adj": min(1.0, p * m_tests), "tests": m_tests})
        rows.sort(key=lambda x: x["p"])
        r["scan"] = [{**x, "p": f"{x['p']:.2g}", "p_adj": f"{x['p_adj']:.2g}"} for x in rows[:3]]
        hits = [{**x, "p": f"{x['p']:.2g}", "p_adj": f"{x['p_adj']:.2g}"} for x in rows if x["p_adj"] <= p_thr]
        if hits:
            found[idx] = hits
    return found


def verdicts(runs, found, z_thr):
    """Вывод по каждой проверке. Шаг, на котором нашлась аномалия, повторён в паре
    с контролем несколько раз. Подтвердилось — если хотя бы в двух третях повторов
    тот же показатель того же агента отклонился в ту же сторону хотя бы на z/2
    (в тех же единицах — интервал предсказания по контролям). Иначе — случайный выброс."""
    out = {}
    for idx, hits in found.items():
        r = runs[idx]
        if r.get("confirm"):
            continue                    # аномалия внутри самой проверки — не повод для новой
        reps = [j for j, q in enumerate(runs) if q.get("confirm") and not q.get("control") and not q.get("spoiled")
                and q["step"] == r["step"]]
        if not reps:
            continue
        ctrl = controls_for(runs, r)
        best = None
        for h in hits:
            base = [chans(c)[h["mac"]][h["metric"]] for c in ctrl
                    if (chans(c).get(h["mac"]) or {}).get(h["metric"]) is not None]
            if len(base) < 3:
                continue
            n = len(base)
            mu = sum(base) / n
            sd = (math.sqrt(sum((x - mu) ** 2 for x in base) / (n - 1)) or 1e-9) * math.sqrt(1 + 1 / n)
            same = 0
            for j in reps:
                v = (chans(runs[j]).get(h["mac"]) or {}).get(h["metric"])
                if v is not None and (v - mu) / sd * math.copysign(1, h["z"]) >= z_thr / 2:
                    same += 1
            ok = same >= math.ceil(2 * len(reps) / 3)
            if best is None or same > best["same"]:
                best = {"metric": h["metric"], "mac": h["mac"], "z": h["z"], "same": same, "of": len(reps),
                        "confirmed": ok}
        if best:
            out[str(idx)] = best
    return out


def validate(prog):
    """Проверить программу; вернуть нормализованную. ValueError — что не так."""
    if not isinstance(prog, dict):
        raise ValueError("программа — объект JSON")
    steps = prog.get("steps") or []
    if not steps:
        raise ValueError("нет шагов")
    out = {"name": str(prog.get("name") or "программа")[:80], "steps": [],
           "repeat": max(1, min(100, int(prog.get("repeat", 1)))), "shuffle": bool(prog.get("shuffle", True)),
           "settle_s": max(0.0, float(prog.get("settle_s", 10))), "targets": prog.get("targets", "all"),
           "z": float(prog.get("z", 4.0)), "confirm": max(0, min(10, int(prog.get("confirm", 3)))),
           "baseline_min": max(0.0, min(120.0, float(prog.get("baseline_min", 0)))),
           "redo": max(0, min(5, int(prog.get("redo", 2)))),
           # отдых перед каждым шагом (docs/thermal.md, § 4.5): "stable" — пока температура не установится
           # (за rest_window_s упала не больше чем на rest_tol °C; не меньше rest_min_s и не дольше rest_max_s),
           # число — столько секунд; "below" — пока все генераторы не остынут до rest_below °C (не дольше rest_max_s);
           # 0 или нет — без отдыха
           "rest": prog.get("rest") if prog.get("rest") in ("stable", "below") else max(0.0, float(prog.get("rest") or 0)),
           "rest_below": float(prog["rest_below"]) if prog.get("rest_below") is not None else None,
           "rest_min_s": max(0.0, float(prog.get("rest_min_s", 60))),
           "rest_max_s": max(10.0, float(prog.get("rest_max_s", 360))),
           "rest_tol": max(0.1, float(prog.get("rest_tol", 0.5))),
           "rest_window_s": max(30.0, float(prog.get("rest_window_s", 90))),
           # зерно случайного порядка: с ним программу можно повторить в точности; нет — выбирается и записывается
           "seed": int(prog["seed"]) if prog.get("seed") is not None else random.randrange(1 << 30),
           # показатели, названные до запуска (точное имя или начало: "pr_s", "band"): только они проверяются
           # на аномалию, и поправка на их число мягче; пусто — все показатели (разведка)
           "metrics": [str(x)[:40] for x in (prog.get("metrics") or []) if str(x).strip()][:20]}
    if 0 < out["baseline_min"] < 1:
        raise ValueError("эталон — не меньше 1 минуты (или 0 — взять уже записанный)")
    if out["rest"] == "below" and not (out["rest_below"] and 20 <= out["rest_below"] <= 80):
        raise ValueError("отдых до температуры: rest_below от 20 до 80 °C")
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise ValueError(f"шаг {i + 1}: нужен объект")
        minutes = float(s.get("minutes", 1))
        if not 0.05 <= minutes <= 24 * 60:
            raise ValueError(f"шаг {i + 1}: длительность от 0,05 до 1440 минут")
        step = {"name": str(s.get("name") or f"шаг {i + 1}")[:60], "cfg": dict(s.get("cfg") or {}),
                "own": s.get("own"), "minutes": minutes, "control": bool(s.get("control")),
                "quiet": bool(s.get("quiet")),
                # шаг на агенте: длительность уходит в настройку (dur), агент сам останавливается и сдаёт итоги
                # (done = cid); сервер ждёт «сдали все». Радио выкл на шаге (ro в cfg группы) — только так
                "agent": bool(s.get("agent")),
                # парный контроль: сравнивать этот опытный шаг только с контролем такого имени
                "control_for": str(s["control_for"])[:60] if s.get("control_for") else None,
                # планшетка: несколько групп в одном шаге (колесо 1 генерирует, колесо 2 поглощает или тоже
                # генерирует своей группой) — каждая группа со своей настройкой и своим номером; агенты
                # программы вне групп на этом шаге стоят
                "groups": None,
                "coil": s.get("coil") if isinstance(s.get("coil"), dict) else None,       # поле катушек (пока запись)
                "layers": s.get("layers") if isinstance(s.get("layers"), dict) else None}  # слои словами — для ленты
        if s.get("groups") is not None:
            gl = s.get("groups")
            if not isinstance(gl, list) or not gl:
                raise ValueError(f"шаг {i + 1}: groups — непустой список")
            step["groups"] = []
            for j, g in enumerate(gl):
                if not isinstance(g, dict):
                    raise ValueError(f"шаг {i + 1}, группа {j + 1}: нужен объект")
                tg = [str(m).lower() for m in (g.get("targets") or []) if m]
                if not tg:
                    raise ValueError(f"шаг {i + 1}, группа {j + 1}: нет агентов")
                step["groups"].append({"targets": tg, "cfg": dict(g.get("cfg") or {}), "own": dict(g.get("own") or {}),
                                       "label": str(g.get("label") or f"группа {j + 1}")[:60]})
        if not step["agent"] and (step["cfg"].get("ro") or any(g["cfg"].get("ro") for g in (step["groups"] or []))):
            raise ValueError(f"шаг {i + 1}: радио выключается только на шаге с длительностью на агенте (agent: true)")
        if step["agent"] and minutes * 60 + 60 > 7200:
            raise ValueError(f"шаг {i + 1}: шаг на агенте — не дольше 118 минут")
        out["steps"].append(step)
    if out["targets"] == "all" and any(s["groups"] for s in out["steps"]):
        out["targets"] = sorted({m for s in out["steps"] for g in (s["groups"] or []) for m in g["targets"]})
    names = {s["name"] for s in out["steps"] if s["control"]}
    for s in out["steps"]:
        if s["control_for"] and s["control_for"] not in names:
            raise ValueError(f"шаг «{s['name']}»: control_for «{s['control_for']}» — нет контрольного шага с таким именем")
        if s["control_for"] and s["control"]:
            raise ValueError(f"шаг «{s['name']}»: контрольному шагу control_for не нужен")
    if out["confirm"] and not any(s["control"] for s in out["steps"]):
        raise ValueError("для проверки аномалий нужен хотя бы один контрольный шаг (control: true)")
    return out


TEMPLATE = {
    "name": "рассеяние против пустышки и тишины",
    "baseline_min": 5, "repeat": 4, "shuffle": True, "settle_s": 10, "rest": "stable", "z": 4, "confirm": 3, "redo": 2,
    "targets": "all",
    "steps": [
        {"name": "рассеяние: каждый сам", "minutes": 3, "cfg": {"sm": 1, "sh": 0}, "control_for": "пустышка"},
        {"name": "пустышка", "minutes": 3, "cfg": {"sm": 1, "sh": 1}, "control": True},
        {"name": "тишина", "minutes": 3, "cfg": {"sm": 0}, "control": True},
    ],
}


class Runner:
    """Выполнение программы в своём потоке. hooks — функции сервера:
      apply(targets, cfg, own, force) -> cid       применить настройки
      confirmed(macs, cid, timeout) -> [не подтвердили]
      targets(spec) -> [mac]                        кому
      snapshot(macs) -> dict                        итоги агентов (счётчики шага)
      verdict(t0, t1) -> dict | None                слежение за статическими: продолжать / повторить / проверить
      metrics(t0, t1) -> {mac: {показатель: число}} показатели статических за отрезок
      static_macs() -> set                          кто из агентов статический
      baseline(minutes, stop) -> dict               записать эталон статических (ждёт конца)
      base_cfg() -> dict, base_own() -> dict        общие и свои с панели (основа шагов)
      log(text)                                     в журнал сервера
      rest(macs)                                    генераторам — стоять и остывать (80 МГц), общие не трогать
      temps(macs) -> {mac: °C}                      температура кристалла сейчас
    """

    def __init__(self, hooks, store_dir):
        self.h = hooks
        self.dir = store_dir
        self.active = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        os.makedirs(store_dir, exist_ok=True)

    # ---------- хранение ----------
    def _path(self, pid):
        return os.path.join(self.dir, f"{pid}.json")

    def store(self, x):
        with open(self._path(x["id"]) + ".tmp", "w", encoding="utf-8") as f:
            json.dump(x, f, ensure_ascii=False, indent=1)
        # Windows не даёт заменить файл, пока его читает другой поток (запрос /api/programs) —
        # 06.10.2026 программа из-за этого падала на первом шаге. Повторить, а не падать.
        for k in range(20):
            try:
                os.replace(self._path(x["id"]) + ".tmp", self._path(x["id"]))
                return
            except PermissionError:
                if k == 19:
                    raise
                time.sleep(0.05)

    def list(self):
        out = []
        for fn in os.listdir(self.dir):
            if fn.endswith(".json"):
                try:
                    x = json.load(open(os.path.join(self.dir, fn), encoding="utf-8"))
                    out.append({k: x.get(k) for k in ("id", "state", "created", "t_start", "t_stop", "note")}
                               | {"name": (x.get("program") or {}).get("name"),
                                  "runs": len(x.get("runs", [])), "anomalies": len(x.get("anomalies", {})),
                                  "confirmed": sum(1 for v in (x.get("verdicts") or {}).values() if v["confirmed"]),
                                  "spoiled": sum(1 for r in x.get("runs", []) if r.get("spoiled")),
                                  "checks": sum(1 for r in x.get("runs", [])
                                                if (r.get("watch") or {}).get("verdict") == "check")})
                except Exception:
                    pass
        return sorted(out, key=lambda r: r["id"], reverse=True)

    def get(self, pid):
        p = self._path(int(pid))
        if not os.path.exists(p):
            raise ValueError("нет такой программы")
        return json.load(open(p, encoding="utf-8"))

    # ---------- запуск ----------
    def start(self, prog, busy):
        if busy:
            raise ValueError(busy)
        with self.lock:
            if self.active:
                raise ValueError("программа уже идёт — сначала остановите её")
            prog = validate(prog)
            pid = max([r["id"] for r in self.list()] + [0]) + 1
            self.rng = random.Random(prog["seed"])        # случайный порядок воспроизводим по зерну
            plan = []
            for r in range(prog["repeat"]):
                order = list(range(len(prog["steps"])))
                if prog["shuffle"]:
                    self.rng.shuffle(order)
                plan += [{"step": i, "repeat": r + 1} for i in order]
            x = {"id": pid, "created": round(time.time(), 3), "state": "running", "program": prog, "plan": plan,
                 "runs": [], "anomalies": {}, "verdicts": {}, "confirming": [], "t_start": None, "t_stop": None,
                 "note": "", "now": None,
                 # основа шагов — настройки панели на момент старта; шаги их не портят, в конце — вернуть
                 "base": {"cfg": self.h["base_cfg"](), "own": self.h["base_own"]()}}
            self.active = x
            self.stop.clear()
        self.store(x)
        threading.Thread(target=self._run, args=(x,), daemon=True).start()
        self.h["log"](f"[prog] №{pid} «{prog['name']}»: {len(plan)} шагов")
        return pid

    def request_stop(self):
        if not self.active:
            raise ValueError("программа не идёт")
        self.stop.set()

    def brief(self):
        x = self.active
        if not x:
            return None
        last = x["runs"][-1] if x["runs"] else None
        return {"id": x["id"], "name": x["program"]["name"], "state": x["state"], "now": x["now"],
                "done": len(x["runs"]), "total": len(x["plan"]), "anomalies": len(x["anomalies"]),
                "spoiled": sum(1 for r in x["runs"] if r.get("spoiled")),
                "last": None if not last else {"name": last["name"], "watch": last.get("watch"),
                                               "spoiled": last.get("spoiled")}}

    def _run(self, x):
        prog = x["program"]
        macs = self.h["targets"](prog["targets"])
        x["targets"] = macs
        x["t_start"] = round(time.time(), 3)
        try:
            if not macs:
                raise RuntimeError("нет агентов")
            if prog["baseline_min"]:                       # эталон шума: основные группы стоят
                x["now"] = {"n": 0, "of": len(x["plan"]), "name": f"эталон шума статических, {prog['baseline_min']:g} мин",
                            "control": False, "t0": round(time.time(), 3), "minutes": prog["baseline_min"],
                            "baseline": True}
                self.store(x)
                self.h["apply"](macs, {**x["base"]["cfg"], "run": 0}, x["base"]["own"], False)
                self.stop.wait(3)
                x["baseline"] = self.h["baseline"](prog["baseline_min"], self.stop)
                self.store(x)
                self.h["log"](f"[prog] №{x['id']}: эталон записан")
            i = 0
            while i < len(x["plan"]) and not self.stop.is_set():
                item = x["plan"][i]
                st = prog["steps"][item["step"]]
                x["now"] = {"n": i + 1, "of": len(x["plan"]), "name": st["name"], "control": st["control"],
                            "agent": st.get("agent", False),
                            "ro": bool(st["cfg"].get("ro") or any(g["cfg"].get("ro") for g in (st.get("groups") or []))),
                            "t0": round(time.time(), 3), "minutes": st["minutes"], "confirm": item.get("confirm")}
                self.store(x)
                rest = self._rest(x, macs) if prog["rest"] else None
                if self.stop.is_set():
                    break
                run = self._one(macs, st, item, x["base"])
                if rest:
                    run["rest"] = rest
                x["runs"].append(run)
                # шаг испорчен: статические говорят «повторить» (толчок, сдвиг, сбой прибора) или кто-то
                # не подтвердил настройку (считал не то) — в сравнение не идёт, повторяется сразу
                why = ("статические: повторить" if (run.get("watch") or {}).get("verdict") == "repeat"
                       else "не подтвердили настройку" if run.get("not_confirmed")
                       else "не сдали итоги" if run.get("not_done")
                       else "не вышли из молчания" if run.get("not_seen") else None)
                if why:
                    run["spoiled"], run["spoiled_why"] = True, why
                    if item.get("redo", 0) < prog["redo"] and not self.stop.is_set():
                        x["plan"].insert(i + 1, {**item, "redo": item.get("redo", 0) + 1})
                        self.h["log"](f"[prog] №{x['id']}: шаг «{st['name']}» испорчен ({why}) — повторяю")
                found = anomalies(x["runs"], prog["z"], prog.get("metrics"))
                x["anomalies"] = {str(k): v for k, v in found.items()}
                x["verdicts"] = verdicts(x["runs"], found, prog["z"])
                # новая аномалия у опытного шага, ещё не проверявшегося — добавить пары «опыт — контроль»
                last = len(x["runs"]) - 1
                if (last in found and prog["confirm"] and not item.get("confirm")
                        and item["step"] not in x["confirming"]):
                    x["confirming"].append(item["step"])
                    ctrls = [j for j, s in enumerate(prog["steps"]) if s["control"]]
                    pairs = []
                    want = st.get("control_for")             # парный контроль — только он
                    if want:
                        ctrls = [j for j in ctrls if prog["steps"][j]["name"] == want] or ctrls
                    for _ in range(prog["confirm"]):
                        pair = [{"step": item["step"], "repeat": 0, "confirm": True},
                                {"step": self.rng.choice(ctrls), "repeat": 0, "confirm": True}]
                        self.rng.shuffle(pair)
                        pairs += pair
                    x["plan"] = x["plan"][:i + 1] + pairs + x["plan"][i + 1:]
                    self.h["log"](f"[prog] №{x['id']}: аномалия на шаге «{st['name']}» — добавлена проверка")
                self.store(x)
                i += 1
            x["state"] = "stopped" if self.stop.is_set() else "done"
        except Exception as e:
            x["state"], x["note"] = "failed", str(e)
        finally:
            with self.lock:                                   # сначала освободить, потом всё остальное:
                self.active = None                            # иначе сбой записи оставлял «программа уже идёт» навсегда
            try:                                              # как было до программы, но стоять
                self.h["apply"](macs, {**x["base"]["cfg"], "run": 0}, x["base"]["own"], False)
            except Exception:
                pass
            if self.h.get("coil_off"):                        # программа кончилась — поле выкл
                self.h["coil_off"]("конец программы")
            x["t_stop"] = round(time.time(), 3)
            x["now"] = None
            try:
                self.store(x)
            except Exception as e:                            # noqa: BLE001
                self.h["log"](f"[prog] №{x['id']}: итог не записан: {e!r}")
            self.h["log"](f"[prog] №{x['id']} {x['state']}: шагов {len(x['runs'])}, аномалий {len(x['anomalies'])}")

    def _one(self, macs, st, item, base):
        t_apply = time.time()
        fixed = self.h["static_macs"]()
        # План шага: одна настройка всем (как раньше) или несколько групп, каждая со своей настройкой и
        # своим номером (планшетка: колесо 1 — генерация, колесо 2 — поглощение или своя генерация).
        # шаг на агенте: срок — длительность плюс установление (измерение начинается после него)
        dur = {"dur": min(7200, int(st["minutes"] * 60 + self.h_settle()))} if st.get("agent") else {}
        plan = []
        if st.get("groups"):
            for g in st["groups"]:
                tg = [m for m in g["targets"] if m in macs]
                if not tg:
                    continue
                own = {m: dict(o) for m, o in (base["own"] or {}).items()}
                for m, o in (g.get("own") or {}).items():
                    own[m] = {**own.get(m, {}), **o}
                # run 0 в настройке группы — контрольный шаг «все стоят»: агенты стоят, меряют только статические
                run1 = g["cfg"].get("run") != 0
                plan.append((tg, {**base["cfg"], **g["cfg"], "run": 1 if run1 else 0, **(dur if run1 else {})}, own, g["label"]))
            used = {m for tg, _, _, _ in plan for m in tg}
            idle = [m for m in macs if m not in used and m not in fixed]
        else:
            own = st["own"] if st["own"] is not None else base["own"]
            plan.append((list(macs), {**base["cfg"], **st["cfg"], "run": 1, **dur}, own, None))
            idle = []
        applied = []
        if self.h.get("coil"):                                 # поле катушек шага: «+», «−», «выкл» (агенты катушек)
            self.h["coil"](st.get("coil") or {}, f"шаг «{st['name']}»")
        for tg, cfg, own, label in plan:
            cid = self.h["apply"](tg, cfg, own, True)          # новый номер: счётчики — с нуля
            applied.append({"targets": tg, "cid": cid, "cfg": cfg, "own": own, "label": label})
        if idle:                                               # не заняты на этом шаге — стоят и остывают
            self.h["apply"](idle, {**base["cfg"], "run": 0, "cpu": 80, "led": 0}, base["own"], False)
        head = {"step": item["step"], "name": st["name"], "control": st["control"], "control_for": st.get("control_for"),
                "confirm": bool(item.get("confirm")), "repeat": item.get("repeat"), "redo": item.get("redo", 0),
                "cid": applied[0]["cid"] if applied else None, "cids": [a["cid"] for a in applied],
                "groups": [{"targets": a["targets"], "cid": a["cid"], "label": a["label"]} for a in applied] if st.get("groups") else None,
                "coil": st.get("coil"), "layers": st.get("layers"), "t_apply": round(t_apply, 3)}
        bad = []
        for a in applied:
            bad += self.h["confirmed"](a["targets"], a["cid"], 20)
        if bad:                                                # кто-то считал не то: шаг испорчен — не ждать его целиком
            now = round(time.time(), 3)
            return {**head, "t0": now, "t1": now, "not_confirmed": bad, "agents": {}, "gen": {}, "static": {}, "watch": None}
        if st["quiet"]:                                        # молчат генераторы; статические меряют и шлют
            for a in applied:
                own_q = {m: {**(a["own"] or {}).get(m, {}), "tl": (a["own"] or {}).get(m, {}).get("tl", 0) if m in fixed else 2}
                         for m in a["targets"]}
                self.h["apply"](a["targets"], {**a["cfg"], "tl": 2}, own_q, False)
        self.stop.wait(self.h_settle())
        t0 = time.time()
        snap0 = self.h["snapshot"](macs)                       # счётчики на начало измерения: итог шага — разность
        if self.active and self.active.get("now"):
            self.active["now"]["t_meas"] = round(t0, 3)        # с этого момента шаг меряется
        not_done = []
        if dur:                                                # шаг на агенте: ждать «сдали все», но не дольше срока + минута
            pairs = [(m, a["cid"]) for a in applied if a["cfg"].get("run") for m in a["targets"] if m not in fixed]
            deadline = t_apply + dur["dur"] + 60
            while not self.stop.is_set():
                not_done = self.h["done"](pairs) if self.h.get("done") else []
                if not not_done or time.time() >= deadline:
                    break
                self.stop.wait(2)
        else:
            self.stop.wait(st["minutes"] * 60)
        t1 = time.time()
        not_seen = []
        if st["quiet"] and dur:                                # агент сам вернул телеметрию; повтор настройки запустил бы счёт заново
            pass
        elif st["quiet"]:                                      # вернуть телеметрию, чтобы забрать итоги
            for a in applied:
                self.h["apply"](a["targets"], {**a["cfg"], "tl": 0}, a["own"], False)
            if self.h.get("seen"):                             # дождаться живого пакета от каждого (повторяя настройку)
                not_seen = self.h["seen"](macs, t1, 15)
            else:
                self.stop.wait(3)
        if dur:                                                # шаг на агенте: окно — ровно срок агента, от применения до его
            t0, t1, snap0 = t_apply, t_apply + dur["dur"], None   # остановки; счётчики с нуля — снимок на t0 не нужен
        snap = self.h["snapshot"](macs)
        return {**head, "t0": round(t0, 3), "t1": round(t1, 3), "not_confirmed": bad, "not_seen": not_seen, "not_done": not_done,
                "agents": snap, "gen": gen_metrics(snap, t1 - t0, snap0),
                "static": self.h["metrics"](t0, t1), "watch": self.h["verdict"](t0, t1),
                "field": self.h["field"](t0, t1) if self.h.get("field") else {}}

    def _rest(self, x, macs):
        if self.h.get("coil_off"):                             # на отдыхе катушки выключены — не греются
            self.h["coil_off"]("отдых")
        """Отдых перед шагом: генераторы стоят на 80 МГц, пока температура не установится (или сколько велено).
        Каждый режим начинается из одинакового состояния; начальная температура — в итогах шага."""
        prog = x["program"]
        gens = [m for m in macs if m not in self.h["static_macs"]()]
        if not gens:
            return None
        x["now"] = {**(x.get("now") or {}), "rest": True, "t_rest": round(time.time(), 3)}
        self.store(x)
        self.h["rest"](gens)
        t0 = time.time()
        hist, stable = {}, False
        while not self.stop.is_set():
            el = time.time() - t0
            cur = self.h["temps"](gens) or {}
            for m, v in cur.items():
                if v is not None:
                    hist.setdefault(m, []).append((el, v))
            if prog["rest"] == "below":                     # до температуры: все генераторы остыли ниже порога
                vals = [v for v in cur.values() if v is not None]
                if vals and max(vals) <= prog["rest_below"]:
                    stable = True
                    break
                if el >= prog["rest_max_s"]:
                    break
            elif prog["rest"] != "stable":
                if el >= prog["rest"]:
                    break
            elif el >= prog["rest_min_s"]:
                stable = True
                for m in gens:                                 # за окно упала не больше чем на rest_tol
                    pts = [v for tt, v in hist.get(m, []) if tt >= el - prog["rest_window_s"]]
                    k = max(1, len(pts) // 3)
                    if len(pts) < 6 or sum(pts[:k]) / k - sum(pts[-k:]) / k > prog["rest_tol"]:
                        stable = False
                        break
                if stable or el >= prog["rest_max_s"]:
                    break
            self.stop.wait(5)
        temps = self.h["temps"](gens) or {}
        out = {"s": round(time.time() - t0, 1), "stable": stable, "temps": temps}
        self.h["log"](f"[prog] №{x['id']}: отдых {out['s']:.0f} с" + ((" — остыли" if prog["rest"] == "below" else " — установилась") if stable else "") +
                      f"; температура {', '.join(f'{v:.1f}' for v in temps.values() if v is not None)}")
        if x.get("now"):
            x["now"]["rest"] = False
        return out

    def h_settle(self):
        return self.active["program"]["settle_s"] if self.active else 0
