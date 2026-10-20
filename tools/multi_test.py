"""Прогон агентов в разных составах и режимах с проверкой логики протокола.

    python tools/multi_test.py [секунд на случай] [случаи через запятую] [--ids=3,4,6]

Агенты — генераторы на связи (не статические), по порядку номеров. Случаи
строятся от их числа N: каждый сам; сообща все; две группы сразу; сеты A и B
(N−1 + 1, …, 1 + N−1) во всех режимах рассеяния; петля через эфир сета B;
две пары A+B сразу. Случай — один или несколько составов одновременно, у
каждого свои настройки и свой номер настройки. Порядок: настройка стоя (счёт
с нуля) → общий старт → работа → стоп (итоги замирают) → снимок → проверки.

Проверки — соотношения счётчиков, которые при верной логике сходятся: сколько
объявил сет A и сколько получил каждый агент B, сколько B выпустил, сколько
услышал A, эстафета по цепочке, фон, состав групп, изоляция одновременных
составов. Отчёт — data/tests/multi_<дата-время>.json.
"""
import json, math, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"   # не localhost: Windows сначала пробует IPv6 и теряет 2 с
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LOSS = 0.05                     # широковещательные кадры без подтверждения: терять можно до 5 %


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=15))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def dash():
    return get("/api/dash?window=10")


# ---------- случаи ----------
def sub(A, B=(), mode=0, own_a=None, own_b=None, **cfg):
    """Состав: агенты A (генерация) и B (рассеяние) — номера; mode 0 — каждый сам, 1 — сообща;
    own_a, own_b — свои параметры агентов A и B (источники, мощность…)."""
    return {"A": list(A), "B": list(B), "mode": mode, "own_a": own_a or {}, "own_b": own_b or {}, "cfg": cfg}


def slow(s):
    """Состав, где простых мало: медленные источники шума или длинные числа — темп не требуем."""
    return (s["own_a"].get("src", 1) != 1) or s["cfg"].get("bits", 256) >= 512


def case(key, name, *subs, **opt):
    """opt: quiet — во время работы телеметрия молчит (tl=2), итоги — после остановки."""
    return {"key": key, "name": name, "subs": list(subs), **opt}


def build_cases(ids):
    n = len(ids)
    C = [case("solo", f"каждый сам, все {n}", sub(ids)),
         case("group", f"сообща все {n}", sub(ids, mode=1))]
    if n >= 4:
        C.append(case("g22", "две группы сразу: 2 + 2", sub(ids[:2], mode=1), sub(ids[2:4], mode=1)))
    if n >= 3:
        C.append(case("g13", f"две группы сразу: 1 + {n - 1}", sub(ids[:1], mode=1), sub(ids[1:], mode=1)))
    if n >= 2:
        C.append(case("ab_each", f"сообща {n - 1} A + 1 B, каждый B — всё число, хеш",
                      sub(ids[:-1], ids[-1:], mode=1, sm=1, sf=0)))
    if n >= 4:
        C.append(case("ab_pieces", "сообща 2 A + 2 B, россыпь, умножение", sub(ids[:2], ids[2:], mode=1, sm=2, sf=2)))
        C.append(case("ab_chain2", "каждый сам 2 A + 2 B, эстафета, перестановка", sub(ids[:2], ids[2:], sm=3, sf=3)))
    if n >= 3:
        C.append(case("ab_chain3", f"каждый сам 1 A + {n - 1} B, эстафета, хеш", sub(ids[:1], ids[1:], sm=3)))
        C.append(case("ab_pieces_xor", f"каждый сам 1 A + {n - 1} B, россыпь, XOR",
                      sub(ids[:1], ids[1:], sm=2, sf=1)))
        C.append(case("ab_sham", f"сообща 1 A + {n - 1} B, каждый B, пустышка",
                      sub(ids[:1], ids[1:], mode=1, sm=1, sh=1)))
    if n >= 2:
        h = max(1, n // 2)
        C.append(case("ab_silent_bg", f"каждый сам {h} A + {n - h} B, тишина и фон 20/с",
                      sub(ids[:h], ids[h:], sm=0, bgr=20)))
        C.append(case("loop", f"петля: {h} A только из эфира B + {n - h} B, фон 50/с, каждый B",
                      sub(ids[:h], ids[h:], sm=1, bgr=50, own_a={"src": 16})))
    if n >= 4:
        C.append(case("pairs", "две пары A+B сразу", sub(ids[:1], ids[1:2], sm=1), sub(ids[2:3], ids[3:4], sm=1)))
    if n >= 2:
        C.append(case("quiet", f"сообща все {n}, телеметрия молчит во время работы", sub(ids, mode=1), quiet=True))
    # --- развёртка параметров (ключи sweep_*): длина, источники, связь, частота, отсев ---
    C.append(case("sweep_g512", f"сообща все {n}, 512 бит", sub(ids, mode=1, bits=512)))
    C.append(case("sweep_g1024", f"сообща все {n}, 1024 бита", sub(ids, mode=1, bits=1024), secs=60))
    C.append(case("sweep_s1024", f"каждый сам, все {n}, 1024 бита", sub(ids, bits=1024), secs=45))
    C.append(case("sweep_gcsi", f"сообща все {n}, шум только CSI", sub(ids, mode=1, own_a={"src": 2}), secs=60))
    C.append(case("sweep_gadc", f"сообща все {n}, шум только АЦП", sub(ids, mode=1, own_a={"src": 4}), secs=60))
    C.append(case("sweep_gjit", f"сообща все {n}, шум только дрожание", sub(ids, mode=1, own_a={"src": 8}), secs=60))
    C.append(case("sweep_gall", f"сообща все {n}, все четыре источника", sub(ids, mode=1, own_a={"src": 15}), secs=120))
    C.append(case("sweep_gk4", f"сообща все {n}, K = 4", sub(ids, mode=1, k=4)))
    C.append(case("sweep_gpre0", f"сообща все {n}, без кусочков заранее", sub(ids, mode=1, pre=0)))
    C.append(case("sweep_gnr6", f"сообща все {n}, связь 6 Мбит/с", sub(ids, mode=1, nr=2)))
    C.append(case("sweep_gtxp2", f"сообща все {n}, передатчик 2 дБм", sub(ids, mode=1, own_a={"txp": 2})))
    C.append(case("sweep_gcpu80", f"сообща все {n}, процессор 80 МГц", sub(ids, mode=1, cpu=80)))
    C.append(case("sweep_gtd0", f"сообща все {n}, без отсева делителями", sub(ids, mode=1, td=0)))
    C.append(case("sweep_sall", f"каждый сам, все {n}, все базы на каждого", sub(ids, all=1)))
    if n >= 2:
        h = max(1, n // 2)
        C.append(case("sweep_bcsi", f"каждый сам {h} A + {n - h} B, шум B — CSI (медленный)",
                      sub(ids[:h], ids[h:], sm=1, own_b={"src": 2}), secs=45))
        C.append(case("sweep_loopcf1", f"петля {h} A + {n - h} B, CSI только от роутера",
                      sub(ids[:h], ids[h:], sm=1, bgr=50, cf=1, own_a={"src": 16})))
        C.append(case("sweep_loopcf2", f"петля {h} A + {n - h} B, CSI только с кадров B",
                      sub(ids[:h], ids[h:], sm=1, bgr=50, cf=2, own_a={"src": 2})))
    C.append({"key": "srole", "name": f"роль S у №{ids[-1]} и обратно: перезапуск, память роли", "agent": ids[-1]})
    # --- контроль целостности: журнал простых (сервер перепроверяет каждое) и согласие группы ---
    C.append(case("plog", f"каждый сам, все {n}, журнал простых", sub(ids, pl=1)))
    C.append(case("dc", f"каждый сам, все {n}, два ядра (конвейер)", sub(ids, dc=1, pl=1)))
    C.append(case("dc_all", f"каждый сам, все {n}, два ядра, все базы на каждого", sub(ids, dc=1, all=1)))
    if n >= 2:
        C.append(case("plog_g", f"сообща все {n}, журнал простых, режим согласия (все базы у каждого)",
                      sub(ids, mode=1, pl=1, all=1)))
    # --- эстафета и каскад (gv = 1, 2): число из шума всех, проверяет один или все ---
    C.append(case("relay", f"эстафета все {n}, журнал простых", sub(ids, mode=1, gv=1, pl=1)))
    C.append(case("relay_dc", f"эстафета все {n}, два ядра, журнал простых", sub(ids, mode=1, gv=1, dc=1, pl=1)))
    C.append(case("cascade", f"каскад все {n}, журнал простых", sub(ids, mode=1, gv=2, pl=1)))
    C.append(case("cascade_dc", f"каскад все {n}, два ядра, журнал простых", sub(ids, mode=1, gv=2, dc=1, pl=1)))
    C.append(case("cascade_pw", f"каскад все {n}, два ядра, ждать кусочки 20 мс", sub(ids, mode=1, gv=2, dc=1, pw=20)))
    C.append(case("relay_rk4", f"эстафета все {n}, два ядра, 4 простых за ход", sub(ids, mode=1, gv=1, dc=1, rk=4)))
    C.append(case("relay_1024", f"эстафета все {n}, два ядра, 1024 бита", sub(ids, mode=1, gv=1, dc=1, bits=1024), secs=60))
    if n >= 2:
        C.append(case("relay_b", f"эстафета {n - 1} A + 1 B, каждый B — всё число",
                      sub(ids[:-1], ids[-1:], mode=1, gv=1, dc=1, sm=1)))
    if n >= 4:
        C.append(case("r22", "две эстафеты сразу: 2 + 2", sub(ids[:2], mode=1, gv=1, dc=1), sub(ids[2:4], mode=1, gv=1, dc=1)))
    return C


# ---------- прогон ----------
class Run:
    def __init__(self):
        d = dash()
        self.default = d["default"]
        self.by_id = {}
        for a in d["agents"]:
            g = d["gen"].get(a["mac"])
            if a["status"] == "online" and g is not None:
                self.by_id[a["id"]] = a["mac"]
        self.ids = sorted(i for i in self.by_id if i > 0)

    def cfg(self, s, run, extra=None):
        return {**self.default, **s["cfg"], "mode": s["mode"], "run": run, **(extra or {})}

    def own(self, subs):
        o = {}
        for s in subs:
            for i in s["A"]:
                o[self.by_id[i]] = {"role": 0, **s["own_a"]}
            for i in s["B"]:
                o[self.by_id[i]] = {"role": 1, **s["own_b"]}
        return o

    def apply(self, subs, run, reset=False, extra=None):
        own = self.own(subs)
        cids = []
        for s in subs:
            macs = [self.by_id[i] for i in s["A"] + s["B"]]
            r = post("/api/control", {"targets": macs, "cfg": self.cfg(s, run, extra), "own": own, "reset": reset})
            if not r.get("ok"):
                raise RuntimeError(f"настройка не принята сервером: {r.get('error')}")
            cids.append(r["cid"])
        return cids

    def wait(self, subs, cids, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            d = dash()
            bad = [i for s, c in zip(subs, cids) for i in s["A"] + s["B"]
                   if (d["gen"].get(self.by_id[i]) or {}).get("cid") != c]
            if not bad:
                return []
            time.sleep(0.5)
        return bad

    def counters(self):
        d = dash()
        out = {}
        for a in d["agents"]:
            g = d["gen"].get(a["mac"]) or {}
            v = g.get("verify") or {}
            pl = a.get("plog") or {}
            out[a["mac"]] = {"reboots": a.get("reboots", 0), "lost": a.get("lost", 0),
                             "vn": v.get("n", 0), "vfail": v.get("fail", 0), "packets": a.get("packets", 0),
                             "pln": pl.get("n", 0), "plf": pl.get("fail", 0)}
        return out

    def case(self, c, secs):
        subs = c["subs"]
        before = self.counters()
        cids0 = self.apply(subs, 0, reset=True)
        bad = self.wait(subs, cids0, 25)
        if bad:
            return {"error": "не приняли настройку: " + ", ".join(f"№{i}" for i in bad)}
        t_start = time.time()
        cids = self.apply(subs, 1)
        bad = self.wait(subs, cids, 15)
        if bad:
            self.apply(subs, 0)
            return {"error": "не стартовали: " + ", ".join(f"№{i}" for i in bad)}
        quiet = None
        if c.get("quiet"):                           # молчать — «на ходу»: номер прежний, счёт идёт
            if self.apply(subs, 1, extra={"tl": 2}) != cids:
                return {"error": "молчание сменило номер настройки — счёт обнулился бы"}
            time.sleep(3)                            # последний пакет до молчания
            q0 = self.counters()
        while (left := secs - (time.time() - t_start)) > 0:
            time.sleep(min(15, left))
            if left > 16 and not c.get("quiet"):
                print(f"      {time.time() - t_start:4.0f} с: {progress(dash(), self, subs)}", flush=True)
        if c.get("quiet"):
            q1 = self.counters()
            quiet = {i: q1[self.by_id[i]]["packets"] - q0[self.by_id[i]]["packets"]
                     for s in subs for i in s["A"] + s["B"]}
        t_stop = time.time()
        cids_s = self.apply(subs, 0)
        bad = self.wait(subs, cids_s, 15)
        time.sleep(2.5)                              # пакеты с замершими итогами (tel и grp)
        d = dash()
        after = self.counters()
        snap = {}
        for s in subs:
            for i in s["A"] + s["B"]:
                m = self.by_id[i]
                g = d["gen"].get(m) or {}
                snap[i] = {"gen": {k: v for k, v in g.items() if k not in ("rate", "verify", "baseline", "gstat")},
                           "gstat": {k: v for k, v in (g.get("gstat") or {}).items() if k != "rate"} or None,
                           "d": {k: after[m][k] - before[m][k] for k in before[m]}}
        return {"cids": cids, "dur": round(t_stop - t_start, 2), "snap": snap,
                "stop_bad": [f"№{i}" for i in bad], "quiet": quiet}

    def srole(self, i):
        """Роль S (статический акселерометр) и обратно: агент запоминает роль и перезапускается."""
        mac = self.by_id[i]
        out = []
        say = lambda lvl, text: out.append((lvl, text))
        r0 = self.counters()[mac]["reboots"]
        for role, kind, name in ((2, "S", "статический"), (0, "A", "генератор")):
            r = post("/api/control", {"targets": [mac], "cfg": {**self.default, "run": 0}, "own": {mac: {"role": role}}})
            if not r.get("ok"):
                say("fail", f"сервер не принял роль {role}: {r.get('error')}")
                return out
            t0, got = time.time(), None
            while time.time() - t0 < 45:
                d = dash()
                a = next(x for x in d["agents"] if x["mac"] == mac)
                cid = (d["gen"].get(mac) or {}).get("cid") if kind == "A" else (a.get("values") or {}).get("cid")
                if a.get("kind") == kind and a["status"] == "online" and cid == r["cid"]:
                    got = time.time() - t0
                    break
                time.sleep(1)
            say("ok" if got is not None else "fail",
                f"№{i} — {name}: " + (f"перезапустился и принял настройку №{r['cid']} за {got:.0f} с"
                                       if got is not None else "не стал за 45 с"))
            if kind == "S" and got is not None:
                st = get("/api/static")
                row = next((x for x in st.get("sensors", []) if x["mac"] == mac), None)
                say("ok" if row else "fail", f"№{i} на втором экране: " + (row.get("text", "") if row else "нет"))
        n = self.counters()[mac]["reboots"] - r0
        say("ok" if n == 2 else "fail", f"№{i}: перезапусков {n} (ждём 2 — туда и обратно)")
        return out


def progress(d, run, subs):
    parts = []
    for s in subs:
        if s["mode"] == 1:
            ms = {run.by_id[i] for i in s["A"]}
            gr = [g for g in d["groups"] if {x["mac"] for x in g["members"]} & ms]
            if gr:
                g = gr[0]
                parts.append(f"группа {','.join(str(i) for i in s['A'])}: {g['cyc_s']:.0f} цикл/с, "
                             f"{g['pr_s']:.1f} простых/с, переспросов {g['asks']}, разрывов {g['breaks']}")
        else:
            pr = sum(((d["gen"].get(run.by_id[i]) or {}).get("rate") or {}).get("g_pr_s", 0) for i in s["A"])
            parts.append(f"A {','.join(str(i) for i in s['A'])}: {pr:.1f} простых/с")
        if s["B"]:
            stx = sum(((d["gen"].get(run.by_id[i]) or {}).get("rate") or {}).get("g_stx_s", 0) for i in s["B"])
            parts.append(f"B {','.join(str(i) for i in s['B'])}: выпускают {stx:.1f}/с")
    return "; ".join(parts)


# ---------- проверки ----------
def checks(c, r):
    """Список (уровень, текст): ok — сходится, warn — заметить, fail — логика не сходится."""
    out = []
    say = lambda lvl, text: out.append((lvl, text))
    snap, dur = r["snap"], r["dur"]
    if r.get("stop_bad"):
        say("fail", "не подтвердили остановку: " + ", ".join(r["stop_bad"]))
    for i, x in snap.items():
        if x["d"]["reboots"]:
            say("fail", f"№{i} перезапускался ({x['d']['reboots']})")
        if x["d"]["vfail"]:
            say("fail", f"№{i}: сервер не подтвердил простых — {x['d']['vfail']}")
        for k in ("err", "stall"):
            if x["gen"].get(k):
                say("warn", f"№{i}: {k} = {x['gen'][k]}")
    lost = {i: x["d"]["lost"] for i, x in snap.items() if x["d"]["lost"]}
    if lost:
        say("warn", "потеряно пакетов телеметрии: " + ", ".join(f"№{i} — {n}" for i, n in lost.items()))
    if r.get("quiet") is not None:
        loud = {i: n for i, n in r["quiet"].items() if n > 1}
        say("fail" if loud else "ok", "телеметрия во время работы молчала" +
            (f", кроме: {loud}" if loud else f" (пакетов {sum(r['quiet'].values())})"))
    for i, x in snap.items():
        e = x["gen"].get("etx") or [0, 0, 0, 0]
        if e[1] or e[2]:
            say("warn", f"№{i}: ESP-NOW — выброшено из очереди {e[1]}, радио не приняло {e[2]} (из {e[0]})")
        if x["gen"].get("rxdrop"):
            say("warn", f"№{i}: принятых кадров не влезло в очередь — {x['gen']['rxdrop']}")
    for s in c["subs"]:
        tag = f"[{'+'.join(str(i) for i in s['A'])}" + (f" | B {'+'.join(str(i) for i in s['B'])}]" if s["B"] else "]")
        A = {i: snap[i] for i in s["A"]}
        B = {i: snap[i] for i in s["B"]}
        if s["mode"] == 1:
            primes = group_checks(say, tag, A, dur, slow(s), s["cfg"].get("gv", 0))
        else:
            primes = 0
            for i, x in A.items():
                g = x["gen"]
                if not g.get("pr"):
                    say("warn" if slow(s) else "fail", f"{tag} №{i}: простых нет (кандидатов {g.get('cand')})")
                primes += g.get("pr") or 0
            say("ok", f"{tag} каждый сам: {primes} простых, {primes / dur:.1f}/с "
                      f"({', '.join(f'№{i} {(x['gen'].get('pr') or 0) / dur:.1f}' for i, x in A.items())})")
            for i, x in A.items():
                g = x["gen"]
                yield_check(say, tag, f"№{i}", g.get("cand") or 0, (g.get("cand") or 0) - (g.get("tdr") or 0),
                            g.get("pr") or 0, g.get("bits"), g.get("td"))
            if s["cfg"].get("dc"):
                for i, x in A.items():
                    q = x["gen"].get("dcq") or [0, 0]
                    say("ok", f"{tag} №{i} два ядра: кандидатов {x['gen'].get('cand')} ({(x['gen'].get('cand') or 0) / dur:.0f}/с), "
                              f"ядро счёта ждало {q[0]}, ядро связи ждало {q[1]}")
        if s["cfg"].get("pl"):
            for i, x in A.items():
                g = x["gen"]
                mine = (x["gstat"] or {}).get("primes_m") if s["mode"] == 1 else g.get("pr")
                mine = mine or 0
                got, bad_p = x["d"]["pln"], x["d"]["plf"]
                lvl = "ok" if bad_p == 0 and mine - 20 <= got <= mine else "fail"
                say(lvl, f"{tag} журнал простых №{i}: нашёл {mine}, сервер получил {got} и подтвердил"
                         f"{' все' if not bad_p else f', НЕ ПРОСТЫХ {bad_p}'}; агент не записал {g.get('pll') or 0}")
        if B:
            scatter_checks(say, tag, s, A, B, primes, dur)
        elif any((x["gen"].get("ann") or 0) for x in A.values()):
            say("fail", f"{tag} сета B нет, а A объявлял простые")
    return out


def surv_frac(td):
    """Доля нечётных чисел без делителей среди нечётных простых < td."""
    s = 1.0
    for p in range(3, int(td or 0), 2):
        if all(p % q for q in range(3, int(p ** 0.5) + 1, 2)):
            s *= 1 - 1 / p
    return s


def yield_check(say, tag, who, cand, mri, pr, bits, td):
    """Выход простых против теории: 1 на 88,6 кандидатов при 256 битах; на уцелевшего — с учётом отсева.
    |z| > 3 — заметить (статистика, не логика)."""
    if cand < 5000 or not bits:
        return
    dens = 2 / (bits * math.log(2) - 0.307)
    exp_m = mri * dens / surv_frac(td)
    z = (pr - exp_m) / math.sqrt(exp_m) if exp_m > 0 else 0.0
    say("ok" if abs(z) <= 3 else "warn", f"{tag} выход {who}: 1 простое на {cand / max(pr, 1):.1f} кандидатов "
                                         f"(теория {1 / dens:.1f}), на {mri / max(pr, 1):.2f} уцелевших "
                                         f"(теория {surv_frac(td) / dens:.2f}), z {z:+.1f}")


def group_checks(say, tag, A, dur, slow_ok=False, gv=0):
    gs = {i: x["gstat"] for i, x in A.items()}
    miss = [i for i, g in gs.items() if not g]
    if miss:
        say("fail", f"{tag} нет статистики группы у " + ", ".join(f"№{i}" for i in miss))
        return 0
    want = sorted(A)
    for i, x in A.items():
        if sorted(x["gen"].get("grp") or []) != want:
            say("fail", f"{tag} №{i}: состав группы {x['gen'].get('grp')}, а задан {want}")
    cyc = sum(g.get("cycles_m", 0) for g in gs.values())
    pr = sum(g.get("primes_m", 0) for g in gs.values())
    asks = sum(g.get("asks_tx", 0) for g in gs.values())
    brk = sum(g.get("brk_pcs", 0) + g.get("brk_res", 0) + g.get("brk_hand", 0) for g in gs.values())
    res = sum(g.get("resyncs", 0) for g in gs.values())
    conf = sum(g.get("conflicts", 0) for g in gs.values())
    t = [sum((g.get("t") or [0] * 5)[k] for g in gs.values()) for k in range(5)]
    cc = sum(sum(g.get("c") or [0] * 3) for g in gs.values())
    sync = round(100 * max(0, t[4] - cc) / t[4], 1) if t[4] else None
    lvl = "ok" if cyc and (pr or slow_ok) else "fail"
    name = {1: "эстафета", 2: "каскад"}.get(gv, "сообща")
    say(lvl, f"{tag} {name}: {cyc} {'ходов' if gv else 'циклов'} ({cyc / dur:.0f}/с), {pr} простых ({pr / dur:.2f}/с), "
             f"{'ход' if gv else 'цикл'} {t[4] / cyc / 1000 if cyc else 0:.2f} мс, {'ожидание' if gv else 'синхронизация'} {sync} %")
    if gv and cyc:                                    # эстафета, каскад: кандидаты и уцелевшие — у проверяющих
        bits = next(iter(A.values()))["gen"].get("bits")
        td = next(iter(A.values()))["gen"].get("td")
        cands = sum(g.get("cands", 0) for g in gs.values())
        surv = sum(g.get("surv", 0) for g in gs.values())
        if pr:
            yield_check(say, tag, "группы", cands, surv, pr, bits, td)
        tr = [sum((g.get("tr") or [0, 0])[k] for g in gs.values()) for k in range(2)]
        gath = sum(g.get("gath", 0) for g in gs.values())
        say("ok", f"{tag} кусочки: кругов {gath}, без чьих-то кусочков {tr[0]}, наборов заменено своим шумом {tr[1]}")
    elif cyc and pr:
        bits = next(iter(A.values()))["gen"].get("bits")
        td = next(iter(A.values()))["gen"].get("td")
        cands = sum(g.get("cands", 0) for g in gs.values())
        yield_check(say, tag, "группы", cands or cyc, cyc, pr, bits, td)
    gath = sum(g.get("gath", 0) for g in gs.values())
    surv = sum(g.get("surv", 0) for g in gs.values())
    cands = sum(g.get("cands", 0) for g in gs.values())
    pre_hit = sum((g.get("pre") or [0, 0])[0] for g in gs.values())
    pre_miss = sum((g.get("pre") or [0, 0])[1] for g in gs.values())
    say("ok", f"{tag} запас: сборов {gath}, кандидатов {cands}, уцелело {surv}"
              f"{f' ({100 * surv / cands:.0f} %)' if cands else ''}; кусочки заранее готовы "
              f"{pre_hit} из {pre_hit + pre_miss}; ожиданий числа {sum(g.get('fillw', 0) for g in gs.values())}")
    say("ok" if not brk and not conf else ("warn" if conf == 0 else "fail"),
        f"{tag} сбои: переспросов {asks}, разрывов {brk}, перехватов {res}, два мастера {conf}; "
        f"лучшая серия {min(g.get('streak_max', 0) for g in gs.values())} циклов")
    agr = sum(g.get("agr", 0) for g in gs.values())
    bad = sum(g.get("split", 0) + g.get("dis", 0) for g in gs.values())
    if agr:
        gls = next((g["gls"] for g in gs.values() if g.get("gls")), "")
        say("ok" if not bad else "fail", f"{tag} {'сверка простых соседей другой базой' if gv else 'согласие агентов'}: "
                                         f"{'сверено' if gv else 'сравнено'} {agr}, расхождений {bad}"
                                         + (f" — последнее {gls}" if gls else ""))
    if len(A) >= 2:
        idle = [i for i, g in gs.items() if not g.get("cycles_m")]
        say(("warn" if slow_ok else "fail") if idle else "ok", "%s %s: %s" % (tag, "ходы" if gv else "мастерство", ", ".join(
            f"№{i} {g.get('cycles_m', 0)}" for i, g in gs.items())) + (f" — не был мастером: {idle}" if idle else ""))
        nf = [i for i, g in gs.items() if not g.get("found")]
        say(("warn" if slow_ok else "fail") if nf else "ok",
            f"{tag} свидетелей нашли: " + ", ".join(f"№{i} {g.get('found', 0)}" for i, g in gs.items()))
    return pr


def scatter_checks(say, tag, s, A, B, primes, dur):
    gA = {i: x["gen"] for i, x in A.items()}
    gB = {i: x["gen"] for i, x in B.items()}
    ann = sum(g.get("ann") or 0 for g in gA.values())
    sm, sh, bgr = s["cfg"].get("sm", 1), s["cfg"].get("sh", 0), s["cfg"].get("bgr", 0)
    # 1. A объявил каждое своё простое
    if primes:
        lvl = "ok" if ann >= primes * 0.98 and ann <= primes + 2 else "fail"
        say(lvl, f"{tag} A объявил {ann} из {primes} простых")
    # 2. каждый B получил объявления — не больше, чем объявили свои A (больше — слышит чужой состав)
    for i, g in gB.items():
        prx = g.get("prx") or 0
        if ann == 0:
            lvl = "fail" if prx else "warn"
        else:
            lvl = "ok" if ann * (1 - LOSS) <= prx <= ann else ("fail" if prx > ann else "warn")
        say(lvl, f"{tag} B №{i} получил {prx} из {ann} объявленных" + (" — больше, чем объявили свои A" if prx > ann else ""))
        for k in ("sdrop", "swait"):
            if g.get(k):
                say("warn", f"{tag} B №{i}: {k} = {g[k]}")
    # 3. выпуск по режиму
    order = sorted(gB)
    if sm == 0:
        tx = {i: gB[i].get("stx") or 0 for i in order}
        say("ok" if not any(tx.values()) else "fail", f"{tag} тишина: выпущено {tx}")
    elif sm in (1, 2, 4):
        slack = 6 if s["own_b"].get("src", 1) != 1 else 2      # медленный шум: до 4 простых ещё ждут в очереди
        for i in order:
            g = gB[i]
            prx, stx = g.get("prx") or 0, g.get("stx") or 0
            due = prx - (g.get("sdrop") or 0)
            lvl = "ok" if due - slack <= stx <= due else "fail"
            say(lvl, f"{tag} B №{i} ({'россыпь' if sm == 2 else 'всё число'}{', пустышка' if sh else ''}): "
                     f"выпустил {stx} на {prx} полученных")
    elif sm == 3:
        first, last = order[0], order[-1]
        chs = gB[first].get("chs") or 0
        say("ok" if abs(chs - (gB[first].get("prx") or 0)) <= 2 else "fail",
            f"{tag} эстафета: первый №{first} начал {chs} из {gB[first].get('prx')} полученных")
        for i in order[1:-1]:
            say("ok" if chs - 3 <= (gB[i].get("chf") or 0) <= chs else "fail",
                f"{tag} эстафета: №{i} передал дальше {gB[i].get('chf')} из {chs}")
        chd = gB[last].get("chd") or 0
        say("ok" if chs - 3 <= chd <= chs else "fail", f"{tag} эстафета: последний №{last} выпустил {chd} из {chs}")
        others = {i: gB[i].get("stx") for i in order[:-1] if gB[i].get("stx")}
        if len(order) > 1:
            say("fail" if others else "ok", f"{tag} эстафета: в эфир — только последний" + (f"; ещё {others}" if others else ""))
    # 4. фон
    for i in order:
        sbg = gB[i].get("sbg") or 0
        if bgr:
            lvl = "ok" if 0.8 * bgr * dur <= sbg <= 1.05 * bgr * dur + 2 else "fail"
            say(lvl, f"{tag} B №{i} фон: {sbg} кадров за {dur:.0f} с ({sbg / dur:.1f}/с при заданных {bgr})")
        elif sbg:
            say("fail", f"{tag} B №{i} фон не задан, а выпущено {sbg}")
    # 5. кто что слышит: каждый слышит кадры рассеяния и фона всех B, кроме своих
    frames = {i: (gB[i].get("stx") or 0) + (gB[i].get("sbg") or 0) for i in order}
    total = sum(frames.values())
    for i, g in {**gA, **gB}.items():
        heard = g.get("heard") or 0
        exp = total - frames.get(i, 0)
        lvl = "ok" if exp * (1 - LOSS) <= heard <= exp + 2 else ("fail" if heard > exp + 2 else "warn")
        say(lvl, f"{tag} №{i} услышал {heard} кадров рассеяния из {exp} выпущенных другими")
    # 6. петля: A берёт шум только из эфира B
    for i, g in gA.items():
        if (g.get("src") or 0) == 16:
            say("ok" if g.get("pr") or g.get("cand") else "fail",
                f"{tag} петля: №{i} из эфира B — {g.get('cand')} кандидатов, {g.get('pr')} простых")


# ---------- главное ----------
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = [x for x in sys.argv[1:] if not x.startswith("--ids=")]
    secs = float(args[0]) if args else 45
    only = args[1].split(",") if len(args) > 1 else None
    run = Run()
    pick = next((x[6:] for x in sys.argv[1:] if x.startswith("--ids=")), None)
    if pick:                                         # только эти агенты (номера через запятую)
        want = {int(i) for i in pick.split(",")}
        run.ids = [i for i in run.ids if i in want]
    if not run.ids:
        raise SystemExit("нет агентов-генераторов на связи")
    C = [c for c in build_cases(run.ids) if not only or c["key"] in only]
    print(f"агенты: {', '.join(f'№{i}' for i in run.ids)}; случаев {len(C)} по {secs:.0f} с", flush=True)
    report = {"t": time.time(), "agents": {i: run.by_id[i] for i in run.ids}, "secs": secs, "cases": []}
    path = os.path.join(ROOT, "data", "tests", time.strftime("multi_%Y%m%d_%H%M%S.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    totals = {"ok": 0, "warn": 0, "fail": 0}
    for n, c in enumerate(C, 1):
        print(f"\n{n}. {c['name']}", flush=True)
        try:
            r = {"srole": True} if c["key"] == "srole" else run.case(c, c.get("secs") or secs)
            res = run.srole(c["agent"]) if c["key"] == "srole" else None
        except Exception as e:
            r, res = {"error": str(e)}, None
        if r.get("error"):
            res = [("fail", r["error"])]
        elif res is None:
            res = checks(c, r)
        for lvl, text in res:
            totals[lvl] += 1
            print(f"   {'✓' if lvl == 'ok' else '◐' if lvl == 'warn' else '✗'} {text}", flush=True)
        report["cases"].append({**c, "result": r, "checks": res})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    # вернуть всех в «каждый сам, генерация, стоять»
    post("/api/control", {"targets": [run.by_id[i] for i in run.ids], "cfg": {**run.default, "run": 0}, "own": {}})
    print(f"\nпроверок: сходится {totals['ok']}, заметить {totals['warn']}, не сходится {totals['fail']}")
    print(f"отчёт: {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
