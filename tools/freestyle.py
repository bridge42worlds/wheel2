"""Фристайл (10.11.2026): случайный исследователь по всему пространству установки.

Сам выбирает и всё время меняет: вращение (направления, невесомость, обороты, ШИМ с качанием, иногда стоят), поле
(знаки катушек, уровень, постоянное / переменное 0,3–47 Гц синус или меандр, в такт вращению; «гудим»), счёт (кто
считает, эгоистичный / каскад / эстафета / одно число / поглощение / своё рассеяние), шум (любой набор внутренних
источников; шум колеса по WS; свои датчики; «информация» — код, прелюдия Баха, запись с микрофона компьютера), хеш и
его замены (raw 0–4), длину чисел, частоту процессора, мощность радио, молчание. Шаги — случайной длины от 5 до 90 с
по «мотивам» (мелко, средне, долго, молчим, гудим, думаем), между ними «дом» — счёт стоит, поле выкл, 5–20 с.

Мера — статика №8 по сырью (rawidx): каждый шаг против соседних «домов». Пустые шаги (шаг = дом) — честный ноль:
масштаб z берётся из них. |z| ≥ 5 (вне известного: полосы частоты поля и 64–250 Гц при переменном поле) — сразу 4
повтора; держится (3/4 одного знака, среднее |z| ≥ 2,5) — кандидат; в конце кандидаты — 8 пар по 30 с.
Всё пишется по шагу в data/series/freestyle_<день>_<время>.jsonl; разбор в любой момент:

    python -u tools/freestyle.py [--minutes 230] [--seed N] [--dry]
    python tools/freestyle.py --report [файл.jsonl]      → docs/experiments_2026-11-10_freestyle.md
"""
import argparse, glob, io, json, math, os, random, statistics as S, subprocess, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=230.0)
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--dry", action="store_true", help="только показать случайные шаги, установку не трогать")
ap.add_argument("--report", nargs="?", const="", default=None)
ap.add_argument("--no-mic", action="store_true")
A = ap.parse_args()
_argv, sys.argv = sys.argv, ["wheel_noise.py"]
import wheel_noise as wn  # noqa: E402  (сервер, состав плат, ячейка статики, имена мер)
sys.argv = _argv

ROOT, say, req, beep = wn.ROOT, wn.say, wn.req, wn.beep
NAMES, FLOOR, ID, ALL = wn.NAMES, wn.FLOOR, wn.ID, wn.ALL
J = {n: i for i, n in enumerate(NAMES)}
RNG = random.Random(A.seed)
MIC = "Onboard MIC (Intel® Smart Sound Technology for Digital Microphones)"
FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
SCR = os.path.join(os.environ.get("TEMP", ROOT), "freestyle_mic")
INFO = {"код": os.path.join(ROOT, "data", "info", "code.txt"),
        "Бах": os.path.join(ROOT, "data", "info", "bach", "prelude1_8k_s16.pcm"), "микрофон": None, "камера": None}
SRC_IN = {"ГСЧ": 1, "CSI": 2, "нога": 4, "дрожание": 8, "регистр": 128}
FRAMES_DIR = [(1, -1), (1, 1), (-1, 1), (-1, -1)]
DIRW = {(1, -1): "навстречу", (1, 1): "в одну сторону", (-1, 1): "навстречу наоборот", (-1, -1): "обе назад"}
MOTIFS = {"мелко": ((5, 5), (5, 5), 12), "средне": ((10, 20), (5, 10), 8), "долго": ((40, 90), (10, 20), 4),
          "молчим": ((30, 60), (10, 15), 4), "гудим": ((10, 30), (5, 10), 6), "смесь": ((5, 60), (5, 20), 8)}
WS_LAST = {}
st_frame, info_now = {}, {"name": None}


# ---------- случайное состояние ----------
def rand_field(motif):
    if motif != "гудим" and RNG.random() < 0.12:
        return None
    k = RNG.choice([("+", "-"), ("+", "+"), ("-", "+"), ("-", "-"), ("+", "off"), ("off", "+"), ("-", "off"), ("off", "-")])
    lvl = RNG.choice([400, 650, 800, 1000])
    ac = None
    if motif == "гудим":
        ac, lvl = {"f": RNG.choice([20, 25, 33, 40, 47, 50]), "shape": RNG.choice(["sin", "sq"])}, RNG.choice([800, 1000])
    elif RNG.random() < 0.55:
        if RNG.random() < 0.15 and st_frame.get("spin"):
            ac = {"sync": {"k": RNG.choice([1, 2, 3]), "deg": RNG.choice([0, 90, 180])}, "shape": "sin"}
        else:
            ac = {"f": RNG.choice([0.3, 0.7, 1.2, 2.5, 5, 8, 13, 20, 33, 47]), "shape": RNG.choice(["sin", "sq"])}
    return {"k1": k[0], "k2": k[1], "level": lvl, "ac": ac}


def rand_count(motif):
    if motif != "гудим" and RNG.random() < 0.08:
        return None                                        # только поле: счёт стоит
    kind = RNG.choices(["внутренний", "колесо", "датчики", "информация"], [4, 3, 1.2, 2])[0]
    if kind == "информация" and not info_now["name"]:
        kind = "внутренний"
    if kind == "колесо":
        src = RNG.choice([32, 96, 96, 100])
        if RNG.random() < 0.25:
            src |= RNG.choice(list(SRC_IN.values()))
        who = RNG.choice(["4+4", "4+4", "колесо 1", "колесо 2"])
    elif kind == "датчики":
        src, who = 0, RNG.choice(["датчики", "датчики", "датчики 1", "датчики 2"])
    else:
        names = RNG.sample(list(SRC_IN), RNG.choice([1, 1, 2, 3]))
        src = sum(SRC_IN[n] for n in names) | (16 if kind == "информация" else 0)
        if kind == "информация" and RNG.random() < 0.5:
            src = 16
        who = RNG.choice(["все 12", "4+4", "колесо 1", "колесо 2", "8 обода", "любые"])
    mode = RNG.choices(["эгоистичный", "каскад", "эстафета", "одно число", "поглощение", "своё рассеяние"], [4, 1.5, 1.5, 1.2, 2, 1])[0]
    two = who in ("все 12", "4+4", "8 обода", "любые", "датчики")
    if mode == "поглощение" and not two:
        mode = "эгоистичный"
    c = {"kind": kind, "src": src, "sel": RNG.choice([0, 1, 2, 3]) if kind in ("колесо", "датчики") else 0, "who": who,
         "mode": mode, "raw": RNG.choices([0, 1, 2, 3, 4], [5, 1.5, 1, 1, 1])[0], "bits": RNG.choices([32, 64, 128, 256], [2, 4, 2, 1])[0],
         "cpu": RNG.choices([80, 160, 240], [1, 1, 3])[0], "txp": RNG.choices([2, 8, 14, 20], [1, 1, 1, 3])[0],
         "quiet": motif == "молчим" or RNG.random() < 0.15}
    if mode == "поглощение":
        c["dir"] = RNG.choice(["1→2", "2→1"])
        c["how"] = RNG.choice(list(wn.HOW))
    if who == "любые":
        c["ids"] = sorted(RNG.sample([ID[m] for m in ALL], RNG.randint(2, 9)))
    return c


def rand_x(motif):
    for _ in range(20):
        f, c = rand_field(motif), rand_count(motif)
        if f or c:
            return {"field": f, "count": c}
    return {"field": rand_field("гудим"), "count": None}


# ---------- исполнение ----------
def ws_line(m, c):
    if c and c["kind"] == "колесо":
        return f"WS {wn.WHEEL[m]} {c['sel']}"
    if c and c["kind"] == "датчики" and m in wn.SRCB:
        return f"WS 0 {c['sel']}"
    return "WS off"


def send_ws(c):
    for m in ALL:
        line = ws_line(m, c)
        if WS_LAST.get(m) != line:
            req("/api/cmd", {"mac": m, "line": line})
            WS_LAST[m] = line


def who_macs(c):
    w = c["who"]
    if c["kind"] == "колесо":
        return {"4+4": wn.CNT, "колесо 1": wn.COUNT["1"], "колесо 2": wn.COUNT["2"]}[w]
    if c["kind"] == "датчики":
        return {"датчики": wn.SRCB, "датчики 1": wn.SENS["1"], "датчики 2": wn.SENS["2"]}[w]
    return {"все 12": ALL, "4+4": wn.CNT, "колесо 1": wn.W["1"], "колесо 2": wn.W["2"], "8 обода": wn.CNT,
            "любые": [wn.MAC[i] for i in c.get("ids", []) if i in wn.MAC]}[w]


def apply_count(c):
    send_ws(c)
    if not c:
        wn.stop_all()
        return
    tl = 2 if c["quiet"] else 0
    tlm = lambda m: 0 if ID[m] in wn.ACC_IDS else tl      # №6/№14 на связи и в молчании (договорённость 07.11)
    who = who_macs(c)
    rest = [m for m in ALL if m not in who]
    if rest:
        wn.control(rest, {**wn.BASE, "dc": 0, "run": 0, "tl": tl}, {m: {"role": 0, "ss": 0, "tl": tlm(m)} for m in rest})
    cfg = {**wn.BASE, "src": c["src"] or 32, "raw": c["raw"], "bits": c["bits"], "cpu": c["cpu"], "txp": c["txp"], "run": 1, "tl": tl}
    own = {}
    for m in who:
        src = c["src"]
        if c["kind"] == "датчики":
            src = 32 if ID[m] in wn.ACC_IDS else 64
        elif c["kind"] in ("внутренний", "информация") and m in wn.SRCB and RNG.random() < 0.5:
            src |= 32 if ID[m] in wn.ACC_IDS else 64
        own[m] = {"role": 0, "ss": 0, "src": src, "raw": c["raw"], "bits": c["bits"], "tl": tlm(m)}
    mode = c["mode"]
    if mode in wn.GROUP:
        cfg.update(wn.GROUP[mode])
    elif mode == "поглощение":
        how = wn.HOW[c["how"]]
        cfg.update(how)
        absorb = wn.W["2"] if c["dir"] == "1→2" else wn.W["1"]
        for m in who:
            if m in absorb:
                own[m].update({"role": 1, "ba": [], **how})
    elif mode == "своё рассеяние":
        cfg["ss"] = 1
        for m in who:
            own[m]["ss"] = 1
    wn.control(who, cfg, own)


def apply_field(f):
    if not f:
        req("/api/coils", {"off": True, "src": "фристайл"})
        return
    for w, k in (("1", f["k1"]), ("2", f["k2"])):
        req("/api/coils", {"wheel": int(w), "state": k, "level": f["level"], "ac": f["ac"], "src": "фристайл"})


def apply(x):
    apply_count(x["count"])
    apply_field(x["field"])


HOME = {"field": None, "count": None}


# ---------- вращение и информация ----------
def rand_frame():
    # 10.11 17:45: мотор шумит и греется — вращение кусками 6–8 мин, после каждого столько же стоят (остывают)
    # 19:02: не греются — куски 8–12 мин, отдых 3 мин
    if st_frame.get("spin"):
        return {"name": "стоят (моторы остывают)", "spin": False, "min": 3.0}
    d = RNG.choice(FRAMES_DIR)
    sp = RNG.choices(["невесомость", "45", "60", "ШИМ"], [3, 1.5, 2, 1])[0]
    fr = {"dir": d, "speed": sp, "spin": True, "min": RNG.uniform(8, 12)}
    if sp == "ШИМ":
        fr["pwm"] = RNG.choice([560, 600, 640])
        fr["mod"] = {"a": RNG.choice([0.05, 0.08, 0.1]), "T": RNG.choice([5, 10, 20, 60])} if RNG.random() < 0.6 else None
    fr["name"] = f"{DIRW[d]} · {sp}" + (f" {fr['pwm']} ‰" if sp == "ШИМ" else "") + (" · качание" if fr.get("mod") else "")
    return fr


def mic_record(sec=2.0):
    os.makedirs(SCR, exist_ok=True)
    p = os.path.join(SCR, time.strftime("mic_%H%M%S.pcm"))
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", f"audio={MIC}", "-t", str(sec),
                        "-ac", "1", "-ar", "8000", "-f", "s16le", "-y", p], capture_output=True, timeout=30)
    return p if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 8000 else None


def cam_frame():
    """Кадр камеры ноутбука (смотрит на колёса): 256×128 оттенков серого = 32 КБ."""
    os.makedirs(SCR, exist_ok=True)
    p = os.path.join(SCR, time.strftime("cam_%H%M%S.raw"))
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", "video=HD Camera", "-frames:v", "1",
                        "-vf", "scale=256:128,format=gray", "-f", "rawvideo", "-y", p], capture_output=True, timeout=30)
    return p if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) == 32768 else None


def info_load():
    name = RNG.choice(["код", "Бах", "микрофон", "камера"] if not A.no_mic else ["код", "Бах"])
    path = INFO[name]
    if name in ("микрофон", "камера"):
        path = mic_record() if name == "микрофон" else cam_frame()
        if not path:
            say(f"✗ {name} не записался — беру прелюдию")
            name, path = "Бах", INFO["Бах"]
    off = RNG.randrange(0, max(1, os.path.getsize(path) - 32768)) // 500 * 500 if name in ("код", "Бах") else 0
    tmp = os.path.join(SCR, "info_chunk.bin")
    os.makedirs(SCR, exist_ok=True)
    with open(path, "rb") as f:
        f.seek(off)
        open(tmp, "wb").write(f.read(32768))
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "info_feed.py"), tmp, "--kb", "32"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    tail = (r.stdout.strip().splitlines() or ["?"])[-1]
    info_now["name"] = f"{name}" + (f" @ {off // 1024} КБ" if off else "")
    say(f"информация: «{info_now['name']}» 32 КБ в платы за {time.time() - t0:.0f} с — {tail}")


def set_frame(fr, log):
    if st_frame.get("spin"):
        say(f"вращение → «{fr['name']}»: остановка, выбег; пока кладу информацию")
        req("/api/motors", {"stop": True})
        info_load()
        wn.wait_stopped(90)
    elif not info_now["name"]:
        info_load()
    req("/api/motors", {"mod": None})
    if fr["spin"]:
        req("/api/motors", {"pair": "free"})
        for w, d in zip("ab", fr["dir"]):
            if fr["speed"] == "невесомость":
                req("/api/motors", {"wheel": w, "mode": "zerog", "dir": d, "flip": 0, "zg": 0})
            elif fr["speed"] == "ШИМ":
                req("/api/motors", {"wheel": w, "mode": "pwm", "dir": d, "flip": 0, "pwm": fr["pwm"]})
            else:
                req("/api/motors", {"wheel": w, "mode": "rpm", "dir": d, "flip": 0, "rpm": float(fr["speed"])})
        if fr.get("mod"):
            req("/api/motors", {"mod": fr["mod"]})
    else:
        req("/api/motors", {"stop": True})
    st_frame.clear()
    st_frame.update(fr, t=time.time())
    log.write({"kind": "frame", "t": time.time(), "frame": fr, "info": info_now["name"]})
    say(f"═══ рамка «{fr['name']}» на {fr['min']:.0f} мин; информация «{info_now['name']}»")


# ---------- сторожа ----------
def guard(G):
    """После дома (все говорят): платы, обод, катушки. Вернёт False — пауза до возвращения."""
    if time.time() - G.get("t", 0) < 60:
        return True
    G["t"] = time.time()
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [ID[m] for m in ALL if m not in online]
    w = req("/api/motors").get("wheels") or {}
    rpm = [round((w.get(k) or {}).get("meas") or 0, 1) for k in "ab"]
    bat = {x.get("label"): x.get("soc") for x in (req("/api/battery").get("list") or []) if x.get("alive")}
    G["line"] = f"плат {12 - len(lost)}/12{' нет ' + str(lost) if lost else ''} · обод {rpm} · катушки {bat}"
    bad = None
    if len(lost) >= 3:
        G["lost"] = G.get("lost", 0) + 1
        bad = f"не на связи {lost}" if G["lost"] >= 3 else None
    else:
        G["lost"] = 0
    if st_frame.get("spin") and time.time() - st_frame["t"] > 40 and min(abs(v) for v in rpm) < 2:
        G["still"] = G.get("still", 0) + 1
        bad = bad or (f"колесо стоит: обод {rpm}" if G["still"] >= 2 else None)
    else:
        G["still"] = 0
    if any(v is not None and v <= 8 for v in bat.values()) and not G.get("coil_warned"):
        G["coil_warned"] = True
        say(f"◐ катушки садятся: {bat} — нужна замена")
        beep(2)
    if bad:
        say(f"✗ ПАУЗА: {bad} — всё выкл, жду (нужна помощь)")
        beep(2)
        G["pause"] = bad
        return False
    return True


def wait_back(G, log):
    wn.all_off()
    WS_LAST.clear()
    t0 = time.time()
    while True:
        time.sleep(20)
        online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
        lost = [ID[m] for m in ALL if m not in online]
        if len(lost) <= 1:
            break
        if time.time() - t0 > 600 and int(time.time() - t0) % 300 < 20:
            beep(2)
    say(f"платы вернулись после паузы {time.time() - t0:.0f} с — продолжаю")
    log.write({"kind": "pause", "t0": t0, "t1": time.time(), "why": G.pop("pause", "")})
    G.clear()
    st_frame.clear()


# ---------- мера ----------
class Log:
    def __init__(self, path):
        self.path, self.f = path, open(path, "a", encoding="utf-8")

    def write(self, d):
        self.f.write(json.dumps(d, ensure_ascii=False, default=float) + "\n")
        self.f.flush()


def known_bands(x):
    """Полосы, где переменное поле само видно в статике (частота поля, гармоники; 64–250 Гц) — не повод для повтора."""
    f = (x.get("field") or {}).get("ac")
    if not f:
        return set()
    out = {J[f"{lo}–{hi} Гц {a}"] for a in wn.AX for lo, hi in wn.BANDS if lo >= 64}
    fr = f.get("f") or 0
    if fr >= 5:                                    # гудение катушки трясёт стол: форма распределения (10.11, ⊓33 Гц — эксцесс z 38)
        out |= {J[n] for n in NAMES if n.split()[0] in ("СКО", "эксцесс", "асимметрия", "≠", "горб")}
    for k in (1, 2, 3, 4):
        for a in wn.AX:
            for lo, hi in wn.BANDS:
                if lo <= fr * k < hi:
                    out.add(J[f"{lo}–{hi} Гц {a}"])
    return out


class Scale:
    """Масштаб z по пустым шагам: d нормирована на FLOOR·√(10/длина)·√1,5; k — 1,48·MAD нормированных пустых."""

    def __init__(self):
        self.sh = []

    def norm(self, d, dur):
        return d / (FLOOR * math.sqrt(10.0 / max(4 * int((dur - 1) // 4), 4)) * math.sqrt(1.5))

    def z(self, d, dur):
        n = self.norm(d, dur)
        if len(self.sh) >= 10:
            M = np.array(self.sh)
            k = 1.4826 * np.median(np.abs(M - np.median(M, axis=0)), axis=0)
            return n / np.maximum(k, 0.3)
        return n


def measure(st, t0, t1, win=4.0):
    """Меры — средние по окнам 4 с: форма (≠ гаусс, эксцесс, горб) на коротком окне смещена, длина шага не должна влиять."""
    a, vs, kn = t0 + 1.0, [], 0
    while a + win <= t1 + 0.01:
        c = st.cell(a, a + win)
        if c is not None:
            if c["knock"]:
                kn += 1
            else:
                vs.append(c["vec"])
        a += win
    if not vs:
        return None, kn > 0
    return np.mean(vs, axis=0), False


def desc(x):
    f, c = x.get("field"), x.get("count")
    if not f and not c:
        return "дом"
    s = []
    if f:
        ac = f["ac"]
        acs = ("" if not ac else (f" ⟳×{ac['sync']['k']}" if ac.get("sync") else f" {'∿' if ac['shape'] == 'sin' else '⊓'}{ac['f']:g} Гц"))
        s.append(f"поле К1{f['k1']} К2{f['k2']} {f['level']}{acs}".replace("off", "0"))
    if c:
        src = c["src"]
        noise = {"колесо": f"колесо src{src} sel{c['sel']}", "датчики": f"свои датчики sel{c['sel']}"}.get(
            c["kind"], ("инфо " if src & 16 else "") + "+".join(n for n, v in SRC_IN.items() if src & v))
        s.append(f"{c['mode']}{(' ' + c['dir'] + ' ' + c['how']) if c['mode'] == 'поглощение' else ''} · {c['who']} · {noise}"
                 f" · raw{c['raw']} {c['bits']} бит {c['cpu']} МГц {c['txp']} дБм" + (" · молчим" if c["quiet"] else ""))
    return " | ".join(s)


# ---------- ход ----------
def run():
    path = os.path.join(ROOT, "data", "series", time.strftime("freestyle_%Y-%m-%d_%H%M.jsonl"))
    log, st, sc, G = Log(path), wn.Static(), Scale(), {}
    t_end = time.time() + A.minutes * 60
    t_deep = t_end - 25 * 60
    # заранее: гул ⊓33 Гц против постоянного того же уровня (10.11 эксцесс г2 z 38 — катушка или микроволновка?)
    hum = {"field": {"k1": "-", "k2": "+", "level": 800, "ac": {"f": 33, "shape": "sq"}}, "count": None}
    cands, seen_rep = [{"x": hum, "j": J["эксцесс г2"], "dur": 30,
                        "vs": {"field": {"k1": "-", "k2": "+", "level": 800, "ac": None}, "count": None}}], 0
    say(f"фристайл: {A.minutes:g} мин, журнал {path}")
    for old in sorted(glob.glob(os.path.join(ROOT, "data", "series", time.strftime("freestyle_%Y-%m-%d_*.jsonl")))):
        if os.path.basename(old) < "freestyle_2026-11-10_1741":   # до окон по 4 с — другая мера
            continue
        hh = []                                            # масштаб — сразу из домов прежних ходов дня
        for ln in io.open(old, encoding="utf-8"):
            r = json.loads(ln)
            if r.get("kind") in ("frame", "pause"):
                hh = []
            elif r.get("kind") == "дом" and r.get("vec") and not r.get("knock"):
                hh.append((np.array(r["vec"]), r["t1"] - r["t0"]))
                if len(hh) >= 3:
                    sc.sh.append(sc.norm(hh[-2][0] - (hh[-3][0] + hh[-1][0]) / 2, hh[-2][1]))
            elif r.get("kind") == "дом":
                hh = []
    say(f"масштаб: {len(sc.sh)} нулевых троек из прежних ходов")
    for old in sorted(glob.glob(os.path.join(ROOT, "data", "series", time.strftime("freestyle_%Y-%m-%d_*.jsonl")))):
        if os.path.basename(old) < "freestyle_2026-11-10_1741":
            continue
        for ln in io.open(old, encoding="utf-8"):         # кандидаты прежних ходов — тоже в проверку парами
            r = json.loads(ln)
            if r.get("kind") == "итог повтора" and r.get("ok"):
                cands.append({"x": r["x"], "j": r["j"], "dur": 30})
    say(f"кандидатов к проверке: {len(cands)}")
    wn.all_off()
    wn.wait_stopped(60)
    ta = time.time()
    time.sleep(60)
    ref = st.cells(ta, time.time())
    log.write({"kind": "опора", "t0": ta, "t1": time.time(), "cells": [np.round(v, 3).tolist() for v in ref]})
    if ref:
        say(f"опора «всё выкл»: {wn.summary(np.mean(ref, axis=0))}")
    prev_home = None
    n, H, hum_seen = 0, [], set()

    def home(dur):
        apply(HOME)
        t0 = time.time()
        time.sleep(dur)
        v, kn = measure(st, t0, time.time())
        log.write({"kind": "дом", "t0": t0, "t1": time.time(), "vec": None if v is None else np.round(v, 3).tolist(), "knock": kn})
        v = None if kn else v
        H.append((v, dur, st_frame.get("t")))
        if len(H) >= 3 and all(h[0] is not None for h in H[-3:]) and len({h[2] for h in H[-3:]}) == 1:
            sc.sh.append(sc.norm(H[-2][0] - (H[-3][0] + H[-1][0]) / 2, H[-2][1]))   # ноль: дом против соседних домов
        return v

    def step(x, dur, kind, motif):
        nonlocal prev_home
        apply(x)
        t0 = time.time()
        time.sleep(dur)
        t1 = time.time()
        v, kn = measure(st, t0, t1)
        hd = RNG.randint(*MOTIFS[motif][1])
        h2 = home(hd)
        rec = {"kind": kind, "motif": motif, "t0": t0, "t1": t1, "dur": dur, "x": x, "frame": st_frame.get("name"),
               "spin_age": t0 - st_frame.get("t", t0), "info": info_now["name"],
               "vec": None if v is None else np.round(v, 3).tolist(), "knock": kn}
        z = None
        if v is not None and not kn and prev_home is not None and h2 is not None:
            d = v - (prev_home + h2) / 2
            rec["d"] = np.round(d, 3).tolist()
            if kind == "пустой":
                sc.sh.append(sc.norm(d, dur))
            z = sc.z(d, dur)
            rec["z"] = np.round(z, 2).tolist()
        prev_home = h2
        log.write(rec)
        return z

    try:
        while time.time() < t_end:
            if not st_frame or time.time() - st_frame["t"] > st_frame["min"] * 60:
                set_frame(rand_frame(), log)
                home(35)                                   # разгон / выбег — не в шаги и не в соседи
                prev_home = home(10)
            if time.time() > t_deep:
                break
            motif = RNG.choices(list(MOTIFS) + ["думаем"], [2, 3, 1.5, 1, 1.2, 2, 0.6])[0]
            if motif == "думаем":
                say(f"думаем: дом 45 с · {G.get('line', '')}")
                prev_home = home(45)
                report(path, quiet=True)
                continue
            (lo, hi), _, k = MOTIFS[motif]
            say(f"мотив «{motif}»: {k} шагов по {lo}–{hi} с")
            for _ in range(k):
                if time.time() >= t_end or (st_frame and time.time() - st_frame["t"] > st_frame["min"] * 60):
                    break
                if not guard(G):
                    wait_back(G, log)
                    break
                sham = RNG.random() < 0.15
                x = HOME if sham else rand_x(motif)
                dur = RNG.randint(lo, hi) if lo != hi else lo
                z = step(x, dur, "пустой" if sham else "шаг", motif)
                n += 1
                if z is None or sham:
                    continue
                skip = known_bands(x)
                shape = {J[nm] for nm in NAMES if nm.split()[0] in ("СКО", "эксцесс", "асимметрия", "≠", "горб")}
                # форму при гудении не выбрасываем совсем (пользователь 10.11): повтор, если |z| ≥ 10
                acf = ((x.get("field") or {}).get("ac") or {}).get("f")
                hum_ok = acf not in hum_seen                       # гул: форма — повтор раз на частоту
                zz = np.array([0 if j in skip and not (hum_ok and j in shape and abs(z[j]) >= 10) else z[j] for j in range(len(z))])
                j = int(np.argmax(np.abs(zz)))
                if abs(zz[j]) >= (5 if len(sc.sh) >= 10 else 8):   # пока пустых мало, масштаб грубый
                    say(f"◐ шаг {n} ({dur} с) «{desc(x)}»: {NAMES[j]} z {zz[j]:+.1f} — повторяю ×4")
                    if j in skip:
                        hum_seen.add(acf)
                    rz = []
                    for _r in range(4):
                        z2 = step(x, max(dur, 10), "повтор", motif)
                        if z2 is not None:
                            rz.append(float(z2[j]))
                    seen_rep += 1
                    same = sum(1 for v in rz if np.sign(v) == np.sign(zz[j]))
                    ok = len(rz) >= 3 and same >= 3 and S.median(rz) * np.sign(zz[j]) >= 2.0   # медиана — того же знака
                    say(f"   повторы {['%+.1f' % v for v in rz]} — {'◐ КАНДИДАТ' if ok else 'не держится'}")
                    log.write({"kind": "итог повтора", "t": time.time(), "x": x, "j": j, "z0": float(zz[j]), "rz": rz, "ok": ok})
                    if ok:
                        cands.append({"x": x, "j": j, "dur": max(dur, 10)})
        # ---- кандидаты: 8 пар по 30 с ----
        for cnd in cands[:4]:
            x, j = cnd["x"], cnd["j"]
            say(f"═══ кандидат «{desc(x)}» — {NAMES[j]}: 8 пар по 30 с")
            ds = []
            for r in range(8):
                if not guard(G):
                    wait_back(G, log)
                    break
                z2 = step(x, 30, "кандидат", "долго")
                if cnd.get("vs"):                          # против другого состояния, а не дома: разность z
                    z3 = step(cnd["vs"], 30, "кандидат-против", "долго")
                    z2 = None if z2 is None or z3 is None else z2 - z3
                if z2 is not None:
                    ds.append(float(z2[j]))
            if len(ds) >= 5:
                t = S.mean(ds) / (S.stdev(ds) / math.sqrt(len(ds))) if S.stdev(ds) > 0 else 0
                same = max(sum(v > 0 for v in ds), sum(v < 0 for v in ds))
                hold = abs(t) >= 3 and same >= len(ds) - 1
                say(f"   {NAMES[j]}: z по парам {['%+.1f' % v for v in ds]}, t {t:+.1f} — {'★ ДЕРЖИТСЯ' if hold else 'нет'}")
                log.write({"kind": "итог кандидата", "t": time.time(), "x": x, "j": j, "zs": ds, "t_": t, "hold": hold})
                if hold:
                    beep(3)
    finally:
        wn.all_off()
        req("/api/motors", {"stop": True})
        say(f"фристайл закончен: шагов {n}, повторов {seen_rep}, кандидатов {len(cands)}; всё выключено. Журнал {path}")
        report(path, quiet=True)


# ---------- разбор ----------
def report(path=None, quiet=False):
    if not path:
        ps = sorted(glob.glob(os.path.join(ROOT, "data", "series", "freestyle_*.jsonl")), key=os.path.getmtime)
        if not ps:
            print("нет журналов")
            return
        path = ps[-1]
    rows = [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]
    steps = [r for r in rows if r.get("kind") in ("шаг", "повтор", "кандидат", "пустой") and r.get("z")]
    sham = [r for r in steps if r["kind"] == "пустой"]
    x = [r for r in steps if r["kind"] in ("шаг", "повтор")]          # повторы — такие же честные измерения
    L = [f"# Фристайл — {time.strftime('%d.%m.%Y', time.localtime(rows[0].get('t0') or rows[0].get('t') or time.time()))}\n",
         f"Разбор {time.strftime('%H:%M')}: шагов {len(x)}, пустых {len(sham)}, повторов {sum(1 for r in steps if r['kind'] == 'повтор')}. "
         f"Журнал `{os.path.relpath(path, ROOT)}`.\n"]
    found = []
    reps = [r for r in rows if r.get("kind") == "итог повтора"]
    cnd = [r for r in rows if r.get("kind") == "итог кандидата"]
    for r in cnd:
        if r.get("hold"):
            found.append(f"кандидат «{desc(r['x'])}» — {NAMES[r['j']]} держится (t {r['t_']:+.1f})")
    # пустые шаги — проверка масштаба: доля |z| ≥ 3 у пустых должна быть мала
    if sham:
        Zs = np.abs(np.array([r["z"] for r in sham]))
        L.append(f"Пустые шаги (честный ноль): |z| ≥ 3 у {100 * (Zs >= 3).mean():.1f} % мер, |z| ≥ 5 у {100 * (Zs >= 5).mean():.2f} %.")
    if x:
        Zx = np.abs(np.array([r["z"] for r in x]))
        L.append(f"Шаги: |z| ≥ 3 у {100 * (Zx >= 3).mean():.1f} % мер, |z| ≥ 5 у {100 * (Zx >= 5).mean():.2f} %.\n")
    # по осям: среднее z меры у шагов с этим значением против остальных шагов (и против пустых)
    feats = {}
    for r in x:
        f, c = r["x"].get("field"), r["x"].get("count")
        keys = [f"вращение: {r.get('frame')}", f"мотив: {r['motif']}"]
        keys.append("поле: выкл" if not f else ("поле: переменное" if f.get("ac") else "поле: постоянное"))
        if f:
            keys.append(f"поле: уровень {f['level']}")
            if f.get("ac") and f["ac"].get("f"):
                keys.append(f"поле: {f['ac']['f']:g} Гц")
        if not c:
            keys.append("счёт: стоит")
        else:
            keys += [f"шум: {c['kind']}", f"режим: {c['mode']}", f"кто: {c['who']}", f"raw {c['raw']}", f"длина {c['bits']}",
                     f"процессор {c['cpu']} МГц", f"радио {c['txp']} дБм", "молчим" if c["quiet"] else "говорим"]
            if c["kind"] == "информация":
                keys.append(f"информация: {(r.get('info') or '?').split(' @')[0]}")
        for k in keys:
            feats.setdefault(k, []).append(r["z"])
    M = len(NAMES)
    tab = []
    for k, zs in feats.items():
        Z = np.array(zs)
        if len(Z) < 6:
            continue
        mu = Z.mean(axis=0)
        t = mu / (np.maximum(Z.std(axis=0, ddof=1), 1e-9) / math.sqrt(len(Z)))
        for j in range(M):
            tab.append((abs(t[j]), k, j, mu[j], t[j], len(Z)))
    tab.sort(key=lambda q: -q[0])
    ntests = max(1, len(tab))
    thr = 3.0 + 0.5 * math.log10(ntests)                   # грубо под число сравнений
    L.append(f"## По осям (среднее z шагов с этим значением; сравнений {ntests}, порог |t| ≥ {thr:.1f})\n")
    L.append("| значение | мера | среднее z | t | шагов |\n|---|---|---|---|---|")
    for a, k, j, mu, t, nn in tab[:15]:
        L.append(f"| {k} | {NAMES[j]} | {mu:+.2f} | {t:+.1f} | {nn} |")
        if a >= thr and nn >= 15 and not (k.startswith("поле:") and NAMES[j] != "тяжесть |g|"):   # мало шагов — t с хвостами
            found.append(f"{k} — {NAMES[j]} среднее z {mu:+.2f}, t {t:+.1f} ({nn} шагов)")
    L.append("\n(Поле в форме и спектре — известное: катушки гудят и трясут стол, это видно в статике; в вердикт идёт только"
             " тяжесть |g|.)\n")
    L.append("## Повторы выделившихся шагов\n")
    for r in reps:
        L.append(f"* «{desc(r['x'])}» — {NAMES[r['j']]} z {r['z0']:+.1f} → повторы {', '.join('%+.1f' % v for v in r['rz'])}"
                 f" — {'◐ кандидат' if r['ok'] else 'нет'}")
    if not reps:
        L.append("нет")
    for r in cnd:
        L.append(f"* кандидат «{desc(r['x'])}» — {NAMES[r['j']]}: t {r['t_']:+.1f} — {'★ держится' if r['hold'] else 'нет'}")
    verdict = ("**что-то есть:** " + "; ".join(found)) if found else "**ничего нет** — ни одно значение ни одной оси не сдвигает статику сверх ноля"
    L.insert(1, f"\n{verdict}\n")
    out = os.path.join(ROOT, "docs", f"experiments_{time.strftime('%Y-%m-%d', time.localtime(rows[0].get('t0') or rows[0].get('t') or time.time()))}_freestyle.md")
    io.open(out, "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    say("разбор: " + verdict.replace("**", ""))
    if not quiet:
        print("\n".join(L[2:]))


def dry():
    for _ in range(3):
        fr = rand_frame()
        print(f"═══ {fr['name']} {fr['min']:.0f} мин")
        st_frame.update(fr)
        info_now["name"] = "Бах"
        for _ in range(6):
            m = RNG.choice(list(MOTIFS))
            x = rand_x(m)
            print(f"  [{m}] {desc(x)}")


if __name__ == "__main__":
    if A.report is not None:
        report(A.report or None)
    elif A.dry:
        dry()
    else:
        run()
