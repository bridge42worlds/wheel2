# -*- coding: utf-8 -*-
"""Сервер онлайн-мониторинга и управления агентами «Колесо-2».

    python server/monitor.py [--http 8090] [--udp 5005]

Что делает:
  - принимает телеметрию агентов по UDP: одна JSON-строка на пакет;
  - каждый пакет как есть дописывает в data/raw/<дата>.jsonl — приём отделён
    от хранения, разбор можно переделать потом, сырьё не меняется;
  - держит в памяти состояние агентов и ряды для графиков; для генератора
    простых (прошивка prime_lab) сам считает темп по разности накопительных
    счётчиков соседних пакетов — потеря пакета ничего не теряет;
  - проверяет присланные агентом простые своим Миллером — Рабином;
  - раз в 2 с рассылает по локальной сети маяк, по которому агенты находят
    сервер сами, без вписанного в прошивку адреса;
  - управление: настройки генератора с дашборда уходят агенту строкой
    "CFG cid=.." на UDP 5007; агент подтверждает номером cid в телеметрии,
    пока номер не совпал — сервер повторяет команду раз в 2 с. Желаемые
    настройки хранятся в data/control.json (переживают перезапуск сервера
    и агента), каждая команда записывается в data/commands.jsonl;
  - статические акселерометры (роль S): отсчёты осей — в data/accel, спектр,
    эталон шума и тревоги — static_watch.py; их страница — /acc (второй экран);
  - отдаёт страницу и JSON-API по HTTP.

Стандартная библиотека Python; для статических акселерометров — numpy.

    --data DIR     другая папка данных (проверка на имитации: tools/sim_agents.py)
    --no-beacon    без маяка — настоящие агенты такой сервер не найдут
"""
import argparse, hashlib, hmac, math, csv, json, os, queue, random, re, shutil, socket, subprocess, sys, threading, time
from collections import deque
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from program import Runner, TEMPLATE as PROGRAM_TEMPLATE
from runlog import RunLog
from agent_rating import Ratings
from motors import Motors
from coils import Coils
from battery import Batteries
from primecheck import BASES, is_probable_prime, Pool as PrimePool
try:
    from static_watch import Watch
except ImportError as e:                       # нет numpy — всё работает, кроме статических
    Watch = None
    WATCH_ERR = f"статические акселерометры не считаются: {e} (pip install numpy)"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "server", "static")
DOCS_HTML = os.path.join(ROOT, "docs", "html")     # документация в HTML: tools/build_docs.py из docs/*.md
REGISTRY = os.path.join(ROOT, "registry", "devices.csv")


def set_data_dir(path):
    """Папка данных: сырьё, управление, сессии, программы, статические."""
    global DATA, RAW_DIR, CONTROL, COMMANDS, SESS_DIR, SESS_INDEX, PROG_DIR, PRIME_DIR, RIG_FILE
    DATA = path
    RAW_DIR = os.path.join(DATA, "raw")
    PRIME_DIR = os.path.join(DATA, "primes")
    CONTROL = os.path.join(DATA, "control.json")
    COMMANDS = os.path.join(DATA, "commands.jsonl")
    SESS_DIR = os.path.join(DATA, "sessions")
    SESS_INDEX = os.path.join(SESS_DIR, "index.json")
    PROG_DIR = os.path.join(DATA, "programs")
    RIG_FILE = os.path.join(DATA, "rig.json")           # установка руками: расстояние между колёсами


set_data_dir(os.path.join(ROOT, "data"))

BEACON_PORT = 5006
CMD_PORT = 5007
HTTP_PORT = 8090           # свой порт HTTP — агентам в адресе образа прошивки (OTA)
FW_DIR = os.path.join(ROOT, "build", "prime_lab")
OTA_KEY_FILE = os.path.join(ROOT, "secrets", "ota.key")
RESEND_S = 2.0             # повтор команды, пока агент не подтвердил cid
SERIES_LEN = 7200          # точек на метрику на агента: 2 часа при пакете в секунду
RECENT_LEN = 200           # последних сырых пакетов на агента для просмотра
MAX_POINTS = 600           # точек на ряд в ответе дашборду (длинное окно усредняется)
META = {"v", "type", "mac", "id", "fw", "sk", "seq", "up", "label"}
# Прошивка с генератором шлёт много накопительных счётчиков; рядами храним
# только эти поля и вычисленные сервером g_* (темп, время, доли).
V2_SERIES = {"rssi", "temp", "heap", "cpu0", "cpu1", "work", "tmax", "udpf", "wrst"}

# Источники случайности: бит маски, ключ, группа, название, пояснение (как в config.h).
# Две группы: внутренние — шум рождается в кристалле; внешние — приходит снаружи платы.
SOURCE_GROUPS = [
    {"key": "int", "name": "Внутренние — шум внутри кристалла",
     "note": "от окружения почти не зависят; у каждого агента свой, независимый"},
    {"key": "ext", "name": "Внешние — шум из окружения",
     "note": "радиоканал и наводки: агенты рядом могут видеть общую часть шума"},
    {"key": "loop", "name": "Петля — шум из эфира сета B",
     "note": "агент берёт шум из кадров, которые выпустил сет B (рассеяние и фон)"},
]
SOURCES = [
    {"bit": 1, "key": "rng", "group": "int", "name": "ГСЧ чипа + шум радио",
     "note": "esp_random(): аппаратный генератор; при включённом Wi-Fi в него подмешивается и шум приёмника; ~44 800 слов/с"},
    {"bit": 8, "key": "jit", "group": "int", "name": "Дрожание RC-генератора",
     "note": "такты кварца за 8 периодов внутреннего RC 136 кГц; 1/4 бита на отсчёт"},
    {"bit": 2, "key": "csi", "group": "ext", "name": "CSI Wi-Fi",
     "note": "замеры радиоканала по ответам роутера на пинг 100 Гц; ~97 пакетов/с, 32 бита на пакет"},
    {"bit": 4, "key": "adc", "group": "ext", "name": "Шум АЦП (D0)",
     "note": "наводки на ножку D0 и шум подтяжек; ничего к ней не подключать; 1/4 бита на отсчёт"},
    {"bit": 16, "key": "bc", "group": "loop", "name": "Эфир сета B",
     "note": "содержимое услышанных кадров рассеяния и фона сета B; 4 бита на байт; "
             "без фона сета B петля не запустится — рассеивать будет нечего"},
    {"bit": 32, "key": "acc", "group": "ext", "name": "Акселерометр на ободе",
     "note": "ICM-42688: младшие байты осей ускорения и гироскопа, 200 отсчётов/с, 1 бит на байт; с другими шумами — "
             "подмешивается накопленное, один — ждёт; датчика нет — вместо него ГСЧ"},
    {"bit": 64, "key": "mag", "group": "ext", "name": "Магнитометр",
     "note": "MLX90393: младшие байты осей поля, 20 замеров/с, 2 бита на байт; с другими шумами — подмешивается"},
]
SRC_INDEX = {"rng": 0, "csi": 1, "adc": 2, "jit": 3, "bc": 4, "acc": 5, "mag": 6}     # порядок в массиве "e" телеметрии
OPTIONS = {"bits": [32, 64, 128, 256, 512, 1024, 2048], "nb_max": len(BASES), "bases": BASES,
           "td": [0, 100, 1000, 10000, 20000], "sources": SOURCES, "source_groups": SOURCE_GROUPS,
           "tmax": [50, 55, 60, 65, 70, 73, 75],
           "modes": [{"mode": 0, "name": "каждый сам за себя"}, {"mode": 1, "name": "сообща (группа)"}],
           # вариант группы: как делится работа над числами из кусочков всех
           "gv": [{"gv": 0, "name": "все на одном числе — проверяют все, базы поделены"},
                  {"gv": 1, "name": "эстафета — проверяет один, ход по кругу"},
                  {"gv": 2, "name": "каскад — проверяют все сразу, каждый свои числа"}],
           "rk": [1, 2, 4, 8, 16],
           # как брать шум: хеш размешивает структуру источника, без хеша она остаётся в числах
           "raw": [{"raw": 0, "name": "с хешем SHA-256 — числа равномерны, структура шума размешана"},
                   {"raw": 1, "name": "без хеша — младшие биты сырья прямо в число"},
                   {"raw": 2, "name": "без хеша, выравнивание фон Неймана — снят перекос 0 и 1"}],
           "pw": [0, 5, 10, 20, 50, 100, 500, 2000],
           # 1 Мбит/с: кадр висит в эфире в 20 раз дольше — плата греется и группа тормозит
           "nr": [{"nr": 1, "name": "24 Мбит/с"}, {"nr": 2, "name": "6 Мбит/с (берёт слабее сигнал)"},
                  {"nr": 0, "name": "1 Мбит/с (медленно, греет)"}],
           "txp": [20, 17, 14, 11, 8, 5, 2], "cpu": [240, 160, 80], "ping": [200, 100, 50, 20, 10, 0],
           "hb": [1, 2, 5, 10, 20],    # пульс: кадров «я жив» в секунду — соседям это CSI без пинга роутера
           "tp": [500, 1000, 2000, 5000], "k": [64, 48, 32, 24, 16, 12, 8, 4],
           "tl": [{"tl": 0, "name": "обычная"}, {"tl": 1, "name": "редкая (в 10 раз реже)"},
                  {"tl": 2, "name": "молчание"}],
           "afs": [2, 4, 8, 16],
           "avg": [{"avg": 1, "name": "1000 Гц (без усреднения)"}, {"avg": 2, "name": "500 Гц (среднее по 2)"},
                   {"avg": 4, "name": "250 Гц (среднее по 4)"}, {"avg": 8, "name": "125 Гц (среднее по 8)"}],
           "roles": [{"role": 0, "name": "генерация (сет A)"}, {"role": 1, "name": "рассеяние (сет B)"},
                     {"role": 2, "name": "статический акселерометр (только меряет)"}],
           "sm": [{"sm": 1, "name": "каждый сам — всё число"}, {"sm": 2, "name": "россыпь — каждый свой кусок"},
                  {"sm": 3, "name": "эстафета — по цепочке, последний в эфир"},
                  {"sm": 0, "name": "тишина — только слушать"}],
           "cf": [{"cf": 0, "name": "с любых кадров — рассеяние B попадает в шум A"},
                  {"cf": 1, "name": "только с ответов роутера — рассеяние B мимо"},
                  {"cf": 2, "name": "только с кадров сета B"},
                  {"cf": 3, "name": "опыт: пинг роутера без CSI (разбор «зомби»)"}],
           "bgr": [0, 5, 10, 20, 50, 100],
           "sf": [{"sf": 0, "name": "хеш SHA-256"}, {"sf": 1, "name": "XOR с шумом"},
                  {"sf": 2, "name": "умножение на случайное"}, {"sf": 3, "name": "перестановка бит"}]}
DEFAULT_CFG = {"src": 1, "bits": 256, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "mode": 0, "nr": 1,
               "k": 24, "pre": 1, "tp": 1000, "cpu": 240, "txp": 20, "ping": 100, "led": 1,
               "role": 0, "sm": 1, "sf": 0, "cf": 0, "sh": 0, "bgr": 0, "tl": 0, "tq": 0, "rmm": 450, "afs": 2,
               "avg": 2, "pl": 0, "dc": 0, "gv": 0, "rk": 1, "pw": 0, "raw": 0, "hb": 1, "dur": 0, "ro": 0}
# Свои у каждого агента (не задано — как общие).
OWN_KEYS = ("src", "raw", "tmax", "txp", "cpu", "ping", "led", "role", "sf", "tl", "rmm", "afs", "avg")
# Меняются на ходу: номер настройки прежний, счёт не обнуляется.
SOFT_KEYS = ("tmax", "txp", "ping", "hb", "led", "tp", "tl", "tq", "rmm", "afs", "avg", "pl", "dur", "ro")
# Всё остальное — «работа»: смена даёт новый номер, счёт с нуля (в группе — у всех).
HARD_KEYS = ("src", "bits", "nb", "all", "td", "mode", "nr", "k", "pre", "cpu", "run", "grp", "gm",
             "role", "sm", "sf", "cf", "sh", "bgr", "bg", "bm", "dc", "gv", "rk", "pw", "raw")
SCAT_KEYS = ("role", "sm", "sf", "cf", "sh", "bgr", "ann", "adrop", "heard", "prx", "stx", "sdrop", "swait",
             "chs", "chf", "chd", "slat", "sn", "sbg", "bclost", "rxdrop")

lock = threading.Lock()     # порядок: замок watch (статические) — потом этот, никогда наоборот
watch = None                # static_watch.Watch — статические акселерометры
runlog = None               # runlog.RunLog — журнал опытов (создаётся в main)
ratings = None              # agent_rating.Ratings — история ошибок и рейтинг агентов (создаётся в main)
agents = {}                # mac -> состояние
series = {}                # (mac, метрика) -> deque[(t, v)]
recent = {}                # mac -> deque[сырой пакет]
stats = {"rx": 0, "bad": 0, "err": 0, "err_last": "", "udp_t": 0.0, "started": time.time()}   # err — исключения в приёмнике
control = {"next_cid": 1, "desired": {}, "common": None, "own": {}, "compose": {}}
# desired: mac -> настройки с cid; common — общие с панели; own: mac -> свои параметры агента
cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def load_registry():
    reg = {}
    if os.path.exists(REGISTRY):
        for r in csv.DictReader(open(REGISTRY, encoding="utf-8")):
            reg[r["mac"].strip().lower()] = {"id": int(r["id"]), "label": r["label"], "role": r.get("role", "")}
    return reg


def agent_entry(mac, reg):
    if mac not in agents:
        info = reg.get(mac, {})
        agents[mac] = {
            "mac": mac, "id": info.get("id", 0), "label": info.get("label", "unregistered"),
            "registered": mac in reg, "fw": "", "sk": "", "ip": "", "first": None, "last": None,
            "packets": 0, "lost": 0, "reboots": 0, "last_seq": None, "values": {},
            # текущий запуск прошивки: сбрасывается при перезапуске агента или смене версии
            "run_start": None, "run_packets": 0, "run_lost": 0,
            "recent_rx": deque(maxlen=600),      # моменты приёма для темпа за последнюю минуту
            "gen": None, "gprev": None, "ack": None, "cfg_sent": 0.0,
            "gstat": None, "grhist": None, "baseline": {},
            "verify": {"n": 0, "fail": 0, "last": "", "last_ok": None, "fail_list": []},
            # журнал простых (пакеты prm): проверено, не простых, потеряно агентом, моменты приёма для темпа
            "plog": {"n": 0, "fail": 0, "lost": 0, "last": None, "fail_list": [], "t": deque(maxlen=600)},
            "ota": None, "ota_cmd": None,        # прошивка по Wi-Fi: состояние от агента, последняя команда
        }
    return agents[mac]


def raw_path(t):
    return os.path.join(RAW_DIR, datetime.fromtimestamp(t).strftime("%Y-%m-%d") + ".jsonl")


# ---------------- проверка простых ----------------
# is_probable_prime и помощники проверки (PrimePool) — в primecheck.py


def verify_prime(a, hexstr, bits):
    v = a["verify"]
    if not hexstr or hexstr == v["last"]:
        return
    try:
        n = int(hexstr, 16)
    except ValueError:
        return
    ok = n.bit_length() == bits and n % 2 == 1 and is_probable_prime(n)
    v["n"] += 1
    v["last"], v["last_ok"] = hexstr, ok
    if not ok:
        v["fail"] += 1
        v["fail_list"] = (v["fail_list"] + [hexstr])[-10:]
        if ratings:
            ratings.on_prime_fail(time.time(), a["mac"], a["id"], hexstr, True)


# ---------------- генератор: темп по разности счётчиков ----------------
RATE_WINDOW_MS = 5000      # темп считается за последние ~5 с: в медленных режимах
                           # за одну секунду счётчики могут не измениться вовсе


def derive(a, p):
    """Темп и доли за последние ~5 с одного запуска и одних настроек."""
    hist = a["gprev"]
    if (not hist or hist[-1].get("cid") != p.get("cid") or p.get("ms", 0) <= hist[-1].get("ms", 0)
            or p.get("cand", 0) < hist[-1].get("cand", 0)):
        a["gprev"] = deque([p], maxlen=32)     # новые настройки или перезапуск агента
        return {}
    hist.append(p)
    prev = hist[0]
    for x in hist:                             # самый поздний пакет не моложе окна
        if p["ms"] - x["ms"] >= RATE_WINDOW_MS:
            prev = x
    if prev is p:
        return {}
    dt = (p["ms"] - prev["ms"]) / 1000.0
    if dt <= 0 or dt > 60:
        return {}
    d = lambda k: (p.get(k) or 0) - (prev.get(k) or 0)
    out = {"g_cand_s": d("cand") / dt, "g_pr_s": d("pr") / dt}
    dc = d("cand")
    e, pe = p.get("e") or [], prev.get("e") or []
    if dc > 0:
        ent_us = sum(x[2] for x in e) - sum(x[2] for x in pe)
        out["g_td_pct"] = 100.0 * d("tdr") / dc
        out["g_mr_pct"] = 100.0 * (d("mri") - d("pr")) / dc
        out["g_us_ent"] = ent_us / dc
        out["g_us_td"] = d("tdus") / dc
        out["g_us_mr"] = d("mrus") / dc
        out["g_us_cand"] = out["g_us_ent"] + out["g_us_td"] + out["g_us_mr"]
    if d("tpn") > 0:
        out["g_ms_prime"] = d("tps") / d("tpn") / 1000.0
    for k in ("ann", "prx", "stx", "heard", "sdrop", "sbg"):   # рассеяние: штук в секунду
        if k in p:
            out[f"g_{k}_s"] = d(k) / dt
    if d("sn") > 0:
        out["g_slat_ms"] = d("slat") / d("sn") / 1000.0
    for s in SOURCES:                        # e[i] = [единиц, бит, мкс, единиц мл. бита, всего, совпало, пар]
        i = SRC_INDEX[s["key"]]
        if i < len(e) and i < len(pe):
            out["g_units_" + s["key"]] = (e[i][0] - pe[i][0]) / dt
            out["g_bits_" + s["key"]] = (e[i][1] - pe[i][1]) / dt
            out["g_time_" + s["key"]] = (e[i][2] - pe[i][2]) / dt / 1e4    # % времени на сбор
    return out


GEN_KEYS = ("cid", "src", "bits", "nb", "all", "td", "age", "cand", "tdr", "mri", "pr", "fp", "dis",
            "err", "stall", "bt", "br", "bus", "tdus", "mrus", "tpn", "tps", "tpmin", "tpmax", "tph",
            "e", "sha", "csi", "rf", "st", "lp", "tmax", "work", "ts", "mode", "grp", "jobs",
            "udpf", "wdisc", "wrsn", "nr", "run", "k", "pre", "cpu", "txp", "txr", "ping", "led", "tp",
            "tl", "tq", "tls", "rmm", "sens", "imu", "mag", "etx", "pl", "pln", "pll", "dc", "dcq",
            "gv", "rk", "pw", "raw", "dur", "ro", "roff", "done", "net", "i2c") + SCAT_KEYS


def derive_grp(a, p):
    """Совместный режим: темп циклов мастера и время цикла по этапам за ~5 с."""
    hist = a["grhist"]
    if (not hist or hist[-1].get("cid") != p.get("cid") or p.get("ms", 0) <= hist[-1].get("ms", 0)
            or p.get("cycles", 0) < hist[-1].get("cycles", 0)):
        a["grhist"] = deque([p], maxlen=32)
        return {}
    hist.append(p)
    prev = hist[0]
    for x in hist:
        if p["ms"] - x["ms"] >= RATE_WINDOW_MS:
            prev = x
    if prev is p:
        return {}
    dt = (p["ms"] - prev["ms"]) / 1000.0
    if dt <= 0 or dt > 60:
        return {}
    d = lambda k: (p.get(k) or 0) - (prev.get(k) or 0)
    out = {"g_cyc_s": d("cycles_m") / dt, "g_gpr_s": d("primes_m") / dt, "g_seen_s": d("cycles") / dt,
           "g_streak": p.get("streak", 0), "g_asks_s": (d("asks_tx") + d("asks_rx")) / dt}
    dc = d("cycles_m")
    if dc > 0 and p.get("t") and prev.get("t"):
        t = [p["t"][i] - prev["t"][i] for i in range(5)]
        c = [p["c"][i] - prev["c"][i] for i in range(3)]
        cyc, comp = t[4] / dc, sum(c) / dc
        out.update({"g_tcycle_ms": cyc / 1000, "g_tcomp_ms": comp / 1000,
                    "g_tg_ms": t[0] / dc / 1000, "g_ts_ms": t[1] / dc / 1000,
                    "g_tt_ms": t[2] / dc / 1000, "g_th_ms": t[3] / dc / 1000})
        if cyc > 0:
            out["g_sync_pct"] = 100.0 * max(0.0, cyc - comp) / cyc
    return out


def baseline_key(g):
    return f"{g.get('bits')}/{g.get('nb')}/{g.get('src')}/{g.get('td')}/{g.get('dc') or 0}"


# ---------------- приём ----------------
REG = {}
REG_MTIME = [0.0, 0.0]     # время изменения файла реестра и когда последний раз смотрели


def refresh_registry():
    """Реестр правится руками (новая плата) — перечитать, если файл изменился.
    Вызывается под lock не чаще раза в 5 с."""
    global REG
    now = time.time()
    if now - REG_MTIME[1] < 5:
        return
    REG_MTIME[1] = now
    try:
        m = os.path.getmtime(REGISTRY)
    except OSError:
        return
    if m == REG_MTIME[0]:
        return
    REG_MTIME[0] = m
    REG = load_registry()
    for mac, info in REG.items():
        a = agent_entry(mac, REG)
        a["id"], a["label"], a["registered"] = info["id"], info["label"], True


def ingest(t, ip, text, live=False):
    """Разбор одного пакета в память. Вызывается и для живых пакетов, и при
    перечитывании сырья с диска на старте — поэтому сервер можно перезапускать
    без потери графиков. Команды агентам и проверка простых — только для живых."""
    try:
        p = json.loads(text)
        mac = str(p["mac"]).lower()
    except Exception:
        with lock:
            stats["bad"] += 1
        return
    if p.get("type") == "prm":                         # журнал простых: проверка — вне замка, она не мгновенная
        if live:                                       # живые — в очередь проверки: приём UDP не ждёт
            prm_queue.put((ingest_prm, (t, ip, mac, p, True)))
        else:
            ingest_prm(t, ip, mac, p, False)
        return
    resend = None
    with lock:
        stats["rx"] += 1
        refresh_registry()                             # новая плата прошита по USB — реестр изменился (раз в 5 с)
        a = agent_entry(mac, REG)
        a["ip"], a["last"] = ip, t
        if p.get("type") == "ack":                     # подтверждение команды
            a["ack"] = {"t": round(t, 3), "cid": p.get("cid"), "ok": p.get("ok"), "err": p.get("err", "")}
            recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append({"rx": round(t, 3), **p})
            return
        if p.get("type") == "nbr":                     # соседи по пульсу: [номер, сигнал, с давности, кадров, считает, °C]
            a["nbr"] = {"t": round(t, 3), "nb": [x for x in (p.get("nb") or []) if isinstance(x, list) and len(x) >= 4]}
            recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append({"rx": round(t, 3), **p})
            return
        if p.get("type") == "grp":                     # статистика совместного режима
            der = derive_grp(a, p)
            tt = round(t, 3)
            for k, v in der.items():
                series.setdefault((mac, k), deque(maxlen=SERIES_LEN)).append((tt, round(v, 4)))
            gs = {k: v for k, v in p.items() if k not in ("v", "type", "mac", "fw", "sk")}
            gs["rate"] = {k: round(v, 3) for k, v in der.items()} or (a["gstat"] or {}).get("rate", {})
            a["gstat"] = gs
            if ratings:
                ratings.on_grp(t, mac, a["id"], gs, (a["gen"] or {}).get("grp"), id_to_mac, live, was_resting)
            if live and a["gen"] and p.get("glp"):     # проверка простого — вне замка, в задаче проверки
                prm_queue.put((verify_prime, (a, p.get("glp"), a["gen"].get("bits"))))
            recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append({"rx": tt, **p})
            return
        if p.get("type") in SENSOR_INGEST:            # датчики: колесо, поле, статический
            tt = round(t, 3)
            SENSOR_INGEST[p["type"]](a, mac, p, tt)
            recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append({"rx": tt, **p})
            return
        a["first"] = a["first"] or t
        fw_new = p.get("fw", a["fw"])
        new_run = a["run_start"] is None or fw_new != a["fw"]
        a["fw"], a["sk"] = fw_new, p.get("sk", a["sk"])
        if not a["registered"] and p.get("id"):
            a["id"] = p["id"]
        seq, up = p.get("seq"), p.get("up")
        if isinstance(seq, int) and a["last_seq"] is not None and seq < a["last_seq"] - 5:
            new_run = True                     # счётчик начался заново — агент перезапускался
        if isinstance(up, int) and a.get("last_up") is not None and up < a["last_up"] - 5:
            new_run = True                     # часы агента пошли заново (одна переставленная датаграмма — не перезапуск)
        if isinstance(up, int):
            a["last_up"] = up
        rebooted = new_run and a["run_start"] is not None
        if new_run:
            if a["run_start"] is not None:
                a["reboots"] += 1
            a["run_start"], a["run_packets"], a["run_lost"] = t, 0, 0
            a["last_seq"] = None
            a["gprev"] = None
        a["packets"] += 1
        a["run_packets"] += 1
        a["recent_rx"].append(t)
        if isinstance(seq, int):
            if a["last_seq"] is not None and seq > a["last_seq"] + 1:
                a["lost"] += seq - a["last_seq"] - 1
                a["run_lost"] += seq - a["last_seq"] - 1
            a["last_seq"] = seq
        a["values"] = {k: v for k, v in p.items()
                       if k not in {"v", "type", "mac", "fw", "sk"} and not isinstance(v, (list, dict))}
        if p.get("ota") is not None:
            a["ota"] = p["ota"]                       # [состояние, ждёт подтверждения, раздел]
        if isinstance(p.get("rbt"), list):
            a["rbt"] = p["rbt"]                       # [причина сброса чипа, сторож: 1 радио / 2 Wi-Fi, раз]
        if ratings:
            ratings.on_tel(t, mac, a["id"], p, live, rebooted)
        static = p.get("role") == 2                   # статический акселерометр: только меряет
        motor = p.get("role") == 3                    # агент моторов: крутит колёса
        gen = "cid" in p and not static and not motor
        tt = round(t, 3)
        for k, v in p.items():
            if k in META or not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            if (gen or static) and k not in V2_SERIES:
                continue
            series.setdefault((mac, k), deque(maxlen=SERIES_LEN)).append((tt, v))
        if gen:
            der = derive(a, p)
            for k, v in der.items():
                series.setdefault((mac, k), deque(maxlen=SERIES_LEN)).append((tt, round(v, 4)))
            g = {k: p.get(k) for k in GEN_KEYS}
            g["rate"] = {k: round(v, 3) for k, v in der.items()} or (a["gen"] or {}).get("rate", {})
            a["gen"] = g
            a["kind"] = "B" if p.get("role") == 1 else "A"
            a.pop("stel", None)                       # был статическим — теперь генератор
            a.pop("mtel", None)
            if p.get("mode", 0) == 0 and p.get("role", 0) == 0 and "g_pr_s" in der and (p.get("age") or 0) > 10:
                # опора для сравнения: «каждый сам» при тех же настройках, сглаженно
                b = a["baseline"].setdefault(baseline_key(p), {"pr_s": der["g_pr_s"], "cand_s": der["g_cand_s"]})
                b["pr_s"] += 0.1 * (der["g_pr_s"] - b["pr_s"])
                b["cand_s"] += 0.1 * (der["g_cand_s"] - b["cand_s"])
                b["t"] = round(t)
            if live:
                if p.get("lp"):                       # проверка простого — вне замка (2048 бит — 0,3 с)
                    prm_queue.put((verify_prime, (a, p.get("lp"), p.get("bits"))))
                want = control["desired"].get(mac)
                # шаг на агенте кончился сам (done = наш номер): агент стоит с обычной телеметрией — так и запомнить,
                # иначе повтор настройки (run 1, молчание) запустил бы счёт заново и стёр итоги
                if want and want.get("run") and want.get("dur") and p.get("done") == want["cid"] and p.get("cid") == want["cid"]:
                    want["run"], want["tl"], want["tq"] = 0, 0, 0
                if want and (want["cid"] != p.get("cid") or soft_mismatch(want, p)) and t - a["cfg_sent"] >= RESEND_S:
                    a["cfg_sent"] = t
                    resend = (cfg_line(want), ip)
        if motor:                                     # агент моторов: что велено и что на мостах
            a["kind"], a["gen"], a["gstat"] = "M", None, None
            a.pop("stel", None)
            a["mtel"] = {k: p.get(k) for k in ("cid", "role", "run", "mot", "temp", "rssi", "heap", "net", "tl")}
            want = control["desired"].get(mac)
            if live and want and want["cid"] != p.get("cid") and t - a["cfg_sent"] >= RESEND_S:
                a["cfg_sent"] = t
                resend = (cfg_line(want), ip)
        if static:                                    # статический акселерометр: состояние, своя настройка
            a["kind"], a["gen"], a["gstat"] = "S", None, None
            a.pop("mtel", None)
            a["stel"] = {k: p.get(k) for k in STATIC_TEL_KEYS}
            if live:
                adopt_static(mac)
            want = control["desired"].get(mac)
            if live and want and p.get("dev") != "phone" and (want["cid"] != p.get("cid") or soft_mismatch(want, p)) \
                    and t - a["cfg_sent"] >= RESEND_S:
                a["cfg_sent"] = t
                resend = (cfg_line(want), ip)
        if isinstance(p.get("bat"), dict):          # аккумуляторы контура (INA219): шина колеса или катушка
            batteries.ingest(mac, p["bat"], t)
        recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append({"rx": tt, **p})
    if resend:
        send_line(*resend)


# ---------------- датчики: колесо (imu), поле (mag); статические — static_watch ----------------
SENSOR_LEN = 12000                  # точек: 10 мин показаний 20 Гц
STATIC_TEL_KEYS = ("cid", "role", "run", "cpu", "tp", "txp", "txr", "led", "tl", "tq", "tls", "afs", "avg", "sens",
                   "dev", "adxl", "ph", "imu", "acc", "temp", "rssi", "heap", "cpu0", "cpu1", "udpf", "net")
AXES = "xyz"


def put(mac, key, t, v, n=SERIES_LEN):
    series.setdefault((mac, key), deque(maxlen=n)).append((t, v))


def ingest_imu(a, mac, p, tt):
    """Колесо: каждый законченный оборот — точка; оси колеса — показания 20 Гц."""
    a["imu"] = {k: p.get(k) for k in ("n", "hz", "w", "t", "revs", "r_set", "err", "frame", "ax", "rd")}
    a["imu"]["t_rx"] = tt
    revs = p.get("rev") or []          # [t, период, об/мин, k, k по радиусу, радиус, |f| верх, низ]
    if revs:
        t_last = revs[-1][0]
        a.setdefault("wrevs", deque(maxlen=3000))
        for r in revs:
            ts = round(tt - (t_last - r[0]), 3)    # конец оборота по часам сервера
            rev = dict(zip(("t_agent", "period", "rpm", "k", "k_r", "r_mm", "fmin", "fmax"), r), ts=ts)
            a["wrevs"].append(rev)
            for key, v in (("w_rpm", r[2]), ("w_k", r[3]), ("w_kr", r[4]), ("w_r", r[5]),
                           ("w_fmin", r[6]), ("w_fmax", r[7])):
                put(mac, key, ts, v)
        a["imu"]["last_rev"] = a["wrevs"][-1]
    put(mac, "w_rpm_now", tt, round((p.get("w") or 0) * 60 / (2 * math.pi), 3))
    s_ = p.get("s") or []                # к центру, по ходу, вдоль оси, |f| — мg, 20 Гц
    for i, v in enumerate(s_):
        ts = round(tt - (len(s_) - 1 - i) * 0.05, 3)
        for key, x in zip(("w_ar", "w_at", "w_aa", "w_am"), v):
            put(mac, key, ts, x / 1000.0, SENSOR_LEN)


def ingest_imr(a, mac, p, tt):
    """Сырые оси датчика — только в журнал (data/raw) и для просмотра последних пакетов."""
    a["imr"] = {"alsb": p.get("alsb"), "glsb": p.get("glsb"), "n": len(p.get("raw") or []), "t_rx": tt}


def ingest_mag(a, mac, p, tt):
    """Поле: среднее за пакет по осям и модуль; отдельные замеры (MLX90393 — 20 Гц, мкТл × 10; телефон шлёт
    свою частоту hz, множитель div и ступень step) — в кольцо за 15 мин, модуль — в ряд не чаще 20 Гц."""
    hz = float(p.get("hz") or 20.0)
    div = float(p.get("div") or 10.0)
    a["mag"] = {k: p.get(k) for k in ("n", "gain", "err", "b", "babs", "bmin", "bmax", "t", "step")}
    a["mag"]["t_rx"], a["mag"]["hz"] = tt, hz
    b = p.get("b") or [None] * 3
    for i, ax in enumerate(AXES):
        if b[i] is not None:
            put(mac, "m_b" + ax, tt, b[i])
    for key in ("babs", "bmin", "bmax", "gain"):
        if p.get(key) is not None:
            put(mac, "m_" + key, tt, p[key])
    s_ = p.get("s") or []                # мкТл × div
    keep = int(hz * MAG_KEEP_S)
    ring = a.get("mag_s")
    if ring is None or ring.maxlen != keep:   # замеры за 15 мин — окно «поле вживую»
        ring = a["mag_s"] = deque(ring or (), maxlen=keep)
    thin = max(1, int(round(hz / 20.0)))
    # телефон 1.1+: номер первого замера (i0) и время первого и последнего по часам датчика (mt0, mt1, ms — «сейчас»
    # там же) — накопленное за обрыв связи встаёт на свои места, повторы отбрасываются. Без них — назад от приёма
    t_first = t_last = None
    if p.get("i0") is not None and p.get("mt1") is not None and p.get("ms") is not None and s_:
        i0, nxt = int(p["i0"]), a.get("mag_i")
        if nxt is not None and i0 < nxt:
            if nxt - i0 > hz * 3600:                 # номера пошли заново — приложение перезапускалось
                nxt = None
            else:
                skip = min(len(s_), nxt - i0)        # уже было: повтор после потерянного ответа
                s_, i0 = s_[skip:], i0 + skip
                p = {**p, "mt0": None} if skip else p
        a["mag_i"] = i0 + len(s_)
        if not s_:
            return
        t_last = tt - (float(p["ms"]) - float(p["mt1"])) / 1000.0
        t_first = tt - (float(p["ms"]) - float(p["mt0"])) / 1000.0 if p.get("mt0") is not None else None
    for i, v in enumerate(s_):
        if t_last is None:
            ts = round(tt - (len(s_) - 1 - i) / hz, 4)
        elif t_first is not None and len(s_) > 1:
            ts = round(t_first + (t_last - t_first) * i / (len(s_) - 1), 4)
        else:
            ts = round(t_last - (len(s_) - 1 - i) / hz, 4)
        if i % thin == 0:
            put(mac, "m_sabs", ts, round(math.sqrt(sum(x * x for x in v)) / div, 2), SENSOR_LEN)
        if len(v) >= 3:
            ring.append((ts, v[0] / div, v[1] / div, v[2] / div))


MAG_KEEP_S = 900                          # замеры поля в памяти на агента: 15 мин


def field_step(t0, t1):
    """Магнитометры за шаг опыта: у каждого агента с замерами поля (MLX90393, телефон) — по x, y, z и |B|
    среднее, СКО, размах, асимметрия, эксцесс (static_watch.axes_stats). Замеры держатся 15 мин — у шага
    длиннее берутся последние 15 мин (part: 1)."""
    import numpy as np
    from static_watch import axes_stats
    with lock:
        got = {m: ([r for r in a["mag_s"] if t0 <= r[0] <= t1], a.get("id"), (a.get("mag") or {}).get("hz"),
                   a["mag_s"][0][0] if a["mag_s"] else None)
               for m, a in agents.items() if a.get("mag_s")}
    out = {}
    for m, (rows, aid, hz, first) in got.items():
        if len(rows) < 10:
            continue
        v = np.array(rows, dtype=float)[:, 1:4]
        out[m] = {"id": aid, "n": len(v), "hz": hz, "part": int(first is not None and first > t0 + 1),
                  "stats": axes_stats(v, ("x", "y", "z", "|B|"))}
    return out
MLX_STEP_XY = (0.751, 0.601, 0.451, 0.376, 0.300, 0.250, 0.200, 0.150)   # мкТл на отсчёт по усилению (HALLCONF 0xC)


def field_live(mac, sec=60.0, since=None):
    """Магнитометр вживую (планшетка, окно агента): за последние sec секунд по x, y, z и |B| — то же, что у статики
    (static_watch.axes_live): сейчас, сводка, гистограммы, спектр (нТл/√Гц), волна последних 30 с; колебания по
    секундам; ступеньки поля — средние двух соседних двухсекундных отрезков разошлись больше чем на 0,5 мкТл
    (переключили катушку, поднесли железо)."""
    import numpy as np
    from static_watch import axes_live, axes_stats
    with lock:
        a = agents.get(mac)
        if not a or not a.get("mag_s"):
            return {"ok": False, "error": "замеров поля нет: у агента нет MLX90393 или он не шлёт"}
        arr = np.array(list(a["mag_s"]), dtype=float)
        m = dict(a.get("mag") or {})
        ph = (a.get("stel") or {}).get("ph") or {}
        sensor, mavg = ph.get("mag"), ph.get("ma")      # телефон: какой у него магнитометр и по скольким замерам он сам усредняет
    now = time.time()
    w = arr[arr[:, 0] >= now - sec]
    names = ("x", "y", "z", "|B|")
    gain = int(m.get("gain") or 7)
    hz = float(m.get("hz") or 20.0)
    step = float(m.get("step") or MLX_STEP_XY[max(0, min(7, gain))])
    L = axes_live(w[:, 1:4], hz, step, names, wave_s=30.0, wave_pts=600) if len(w) >= 2 else None
    if not L:
        return {"ok": False, "error": "в окне нет замеров"}
    res = {"ok": True, "kind": "mag", "unit": "мкТл", "spec_unit": "нТл/√Гц", "hz": hz, "gain": gain, "err": m.get("err"), "sensor": sensor, "mavg": mavg,
           "temp": m.get("t"), "age": round(now - m["t_rx"], 1) if m.get("t_rx") else None, **L, "sec": sec,
           "gap": max(0, int(round(min(sec, now - arr[0, 0]) * hz)) - len(w)), "knocks": [], "spikes": [], "conds": [], "done": []}
    sec_t, sds, peaks = [], {ax: [] for ax in "xyz"}, []
    for t0 in np.unique(np.floor(w[:, 0])):              # колебания по секундам: разброс за секунду
        q = w[np.floor(w[:, 0]) == t0, 1:4]
        if len(q) < hz / 2:
            continue
        d = q - q.mean(0)
        sec_t.append(round(float(t0 + 0.5 - now), 2))
        for i, ax in enumerate("xyz"):
            sds[ax].append(round(float(d[:, i].std()), 4))
        peaks.append(round(float(np.abs(d).max()), 3))
    res["secs"] = {"t": sec_t, **sds, "peak": peaks}
    jumps, blk = [], np.floor((w[:, 0] - w[0, 0]) / 2.0)
    means = [(float(w[blk == k, 0].mean()), w[blk == k, 1:4].mean(0)) for k in np.unique(blk) if (blk == k).sum() >= hz]
    for (t1, m1), (t2, m2) in zip(means, means[1:]):
        d = m2 - m1
        db = float(np.linalg.norm(m2) - np.linalg.norm(m1))
        if np.abs(d).max() > 0.5:
            jumps.append({"t": round(t2 - now, 1), "db": round(db, 2), "d": [round(float(x), 2) for x in d]})
    res["jumps"] = jumps[-10:]
    if since:
        q = arr[arr[:, 0] >= since]
        res["since"] = axes_stats(q[:, 1:4], names)
        res["since_n"] = int(len(q))
    return res


SENSOR_INGEST = {"imu": ingest_imu, "imr": ingest_imr, "mag": ingest_mag}


def ingest_acc(t, ip, text):
    """Отсчёты статического акселерометра: в сырой журнал не пишутся (их много) — static_watch
    хранит их сам в data/accel как есть и считает спектр. Агенту — отметка, что он на связи."""
    try:
        p = json.loads(text)
        mac = str(p["mac"]).lower()
    except Exception:
        with lock:
            stats["bad"] += 1
        return
    with lock:
        stats["rx"] += 1
        a = agent_entry(mac, REG)
        a["ip"], a["last"] = ip, t
        a["kind"] = "S"
        a["acc_rx"] = a.get("acc_rx", 0) + 1
    if watch:
        watch.add(mac, p, t)                     # вне замка сервера: порядок замков — watch, потом lock


def put_locked(mac, key, t, v):
    with lock:
        put(mac, key, t, v)


prm_queue = queue.Queue()      # (функция, аргументы): проверка простых — в своей задаче (prm_worker)
prime_pool = PrimePool(log=lambda text: print(text, flush=True))


def prm_numbers(p):
    out = []
    for rec in p.get("p") or []:
        try:
            out.append(int(str(rec[1]), 16))
        except (TypeError, ValueError, IndexError):
            pass
    return out


def prm_worker():
    """Проверка живых простых (пакеты prm и последнее простое из tel/grp): полный Миллер — Рабин
    стоит ~1,5 мс на 256-битное число и 0,3 с на 2048-битное; в приёмнике UDP или под общим замком
    это теряло бы пакеты. Очередь разбирается по порядку, пачками: числа журналов всей пачки проверяют
    процессы-помощники (primecheck.Pool, по ядрам) — одному ядру 12 плат не по силам; помощники
    не отвечают — пачка проверяется здесь же."""
    while True:
        batch = [prm_queue.get()]
        while len(batch) < 200:
            try:
                batch.append(prm_queue.get_nowait())
            except queue.Empty:
                break
        nums = [n for fn, args in batch if fn is ingest_prm for n in prm_numbers(args[3])]
        known = {}
        if nums:
            try:
                known = dict(zip(nums, prime_pool.check(nums)))
            except Exception as e:                     # noqa: BLE001
                print(f"[prm] помощники проверки: {e!r} — проверяю сам", flush=True)
        for fn, args in batch:
            try:
                if fn is ingest_prm:
                    fn(*args, known=known)
                else:
                    fn(*args)
            except Exception as e:                     # noqa: BLE001 — одна плохая строка не должна остановить проверку
                print(f"[prm] {e!r}", flush=True)


def ingest_prm(t, ip, mac, p, live, known=None):
    """Журнал простых (пакет prm): каждое число проверяется своим Миллером — Рабином и, если пакет
    живой, дописывается в data/primes/<дата>/<mac>.txt строкой: время сервера, часы агента (мс),
    номер настройки, бит, откуда (0 — каждый сам, 1 — сообща), число, проверка (1/0).
    При перечитывании сырья (live=False) проверка короткая — одной базой. known — {число: проверка}
    от помощников (prm_worker)."""
    rows, fails = [], []
    for rec in p.get("p") or []:
        try:
            ms, hx, src = int(rec[0]), str(rec[1]).upper(), int(rec[2]) if len(rec) > 2 else 0
            n = int(hx, 16)
        except (TypeError, ValueError, IndexError):
            continue
        bits = len(hx) * 4
        ok = n.bit_length() == bits and n % 2 == 1 and (known[n] if known and n in known
                                                         else is_probable_prime(n, quick=not live))
        if not ok:
            fails.append(hx)
        rows.append(f"{t:.3f} {ms} {p.get('cid', 0)} {bits} {src} {hx} {1 if ok else 0}\n")
    tt = round(t, 3)
    with lock:
        stats["rx"] += 1
        a = agent_entry(mac, REG)
        a["ip"], a["last"] = ip, max(a["last"] or 0, t)   # журнал разбирается с опозданием — «последний пакет» не отодвигать назад
        pl = a["plog"]
        pl["n"] += len(rows)
        pl["fail"] += len(fails)
        pl["fail_list"] = (pl["fail_list"] + fails)[-10:]
        guard_add(p.get("cid", 0), [r.split()[5] for r in rows], fails)
        if ratings:
            for hx in fails:
                ratings.on_prime_fail(t, mac, a["id"], hx, live)
        pl["lost"] = p.get("lost", pl["lost"])
        if rows:
            pl["last"] = rows[-1].split()[5]
            for _ in rows:
                pl["t"].append(tt)
        recent.setdefault(mac, deque(maxlen=RECENT_LEN)).append(
            {"rx": tt, "type": "prm", "cid": p.get("cid"), "n": len(rows), "lost": p.get("lost")})
    if live and rows:
        d = os.path.join(PRIME_DIR, time.strftime("%Y-%m-%d", time.localtime(t)))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, mac.replace(":", "") + ".txt"), "a", encoding="utf-8") as f:
            f.writelines(rows)


def plog_brief(a, now):
    pl = a["plog"]
    recent5 = sum(1 for x in pl["t"] if x >= now - 5)
    return {"n": pl["n"], "fail": pl["fail"], "lost": pl["lost"], "last": pl["last"], "rate": round(recent5 / 5.0, 1),
            "fail_list": pl["fail_list"]}


def known_lines():
    """Известные источники линий в спектре статических: частота, название, сколько гармоник."""
    out = [{"f": 50.0, "name": "сеть 50 Гц", "harm": 20},
           {"f": 1 / 0.1024, "name": "маяки роутера (102,4 мс)", "harm": 25}]
    now = time.time()
    with lock:
        pings, tps, rpms = set(), set(), []
        for a in agents.values():
            g = a["gen"]
            if g and g.get("run") and (g.get("src") or 0) & 2 and g.get("ping"):
                pings.add(g["ping"])                 # пинг роутера для CSI
            if g and g.get("tp"):
                tps.add(g["tp"])
            if a.get("stel") and a["stel"].get("tp"):
                tps.add(a["stel"]["tp"])
            # обороты — только по свежему пакету обода: иначе после остановки или пропажи агента «оборотная»
            # линия с гармониками оставалась известной навсегда и прятала настоящие линии
            if a.get("imu") and abs(a["imu"].get("w") or 0) > 0.3 and now - (a["imu"].get("t_rx") or 0) < 3:
                rpms.append(abs(a["imu"]["w"]) / (2 * math.pi))
    out += [{"f": float(p), "name": f"пинг CSI агентов ({p}/с)", "harm": 5} for p in sorted(pings)]
    out += [{"f": 1000.0 / tp, "name": f"телеметрия раз в {tp} мс", "harm": 5} for tp in sorted(tps)]
    out += [{"f": f, "name": "обороты колеса", "harm": 10} for f in rpms[:2]]
    return out


def busy_generators():
    """Агенты, которые сейчас считают, — во время записи эталона их быть не должно."""
    with lock:
        return [f"№{a['id']}" for a in agents.values()
                if a["gen"] and a["gen"].get("run") and a["last"] and time.time() - a["last"] < 5]


# Перечитывание сырья на старте. Ряды в памяти держат SERIES_LEN точек (2 ч), поэтому
# перечитывать весь день незачем: к вечеру это 150+ МБ и ~4 минуты, всё это время агенты на
# странице «давно». Берём только хвост файла — последние REPLAY_S секунд (начало находится
# двоичным поиском по полю rx, строки записаны по порядку приёма). Перечитывание идёт в
# отдельной задаче, а приём UDP открыт сразу: живые пакеты пишутся на диск как всегда, а в
# память попадают после хвоста, по порядку (иначе старый пакет пришёл бы после нового и темп
# и счётчики потерь сбились бы).
REPLAY_S = SERIES_LEN + 600
_replay = {"on": False, "pending": [], "lock": threading.Lock()}
_RX_RE = re.compile(rb'^\{"rx": ([0-9.]+)')


def _line_rx(line):
    m = _RX_RE.match(line)
    if m:
        return float(m.group(1))
    try:
        return float(json.loads(line)["rx"])
    except Exception:
        return None


def _seek_rx(f, size, cutoff):
    """Смещение первой строки с rx >= cutoff (двоичный поиск по файлу, строки по порядку времени)."""
    lo, hi = 0, size
    while hi - lo > 1 << 20:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()                                   # остаток строки, на которую попали
        pos = f.tell()
        rx = _line_rx(f.readline())
        if rx is None:                                 # конец файла или мусор — искать левее
            hi = mid
        elif rx < cutoff:
            lo = pos
        else:
            hi = mid
    return lo


def replay_today(upto=None, cutoff=None):
    """Перечитать хвост сегодняшнего сырья (последние REPLAY_S секунд) в память.
    upto — читать не дальше этого смещения (файл растёт, пока читаем)."""
    path = raw_path(time.time())
    if not os.path.exists(path):
        return 0
    cutoff = time.time() - REPLAY_S if cutoff is None else cutoff
    n = 0
    with open(path, "rb") as f:
        size = os.path.getsize(path) if upto is None else upto
        f.seek(_seek_rx(f, size, cutoff))
        while f.tell() < size:
            line = f.readline()
            if not line:
                break
            rx = _line_rx(line)
            if rx is None or rx < cutoff:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ingest(r["rx"], r.get("ip", ""), r["raw"])
            n += 1
    return n


def _replay_task(upto):
    t0 = time.time()
    try:
        n = replay_today(upto)
    except Exception as e:                             # noqa: BLE001 — без хвоста жить можно, без приёма нет
        print(f"[udp] сырьё не перечитано: {e}", flush=True)
        n = 0
    with _replay["lock"]:                              # живые пакеты, скопившиеся за это время, — по порядку
        pend = _replay["pending"]
        for t, ip, text in pend:
            ingest(t, ip, text, live=True)
        k = len(pend)
        pend.clear()
        _replay["on"] = False
    print(f"[udp] перечитано с диска: {n} пакетов за последние {REPLAY_S // 60} мин, {time.time() - t0:.1f} с; "
          f"живых за это время: {k}", flush=True)


def handle_datagram(t, ip, text):
    """Один пакет: от агента по UDP или от телефона (POST /api/ingest — те же пакеты строками)."""
    if '"type":"acc"' in text[:40]:                  # отсчёты статических — свои файлы и свой разбор
        ingest_acc(t, ip, text)
        return
    # 1. сырьё — на диск, до любого разбора
    with open(raw_path(t), "a", encoding="utf-8") as f:
        f.write(json.dumps({"rx": round(t, 3), "ip": ip, "raw": text}, ensure_ascii=False) + "\n")
    # 2. разбор — в память (пока перечитывается хвост с диска — в очередь, после него)
    with _replay["lock"]:
        if _replay["on"]:
            _replay["pending"].append((t, ip, text))
            return
    ingest(t, ip, text, live=True)


def udp_receiver(port):
    global REG
    REG = load_registry()
    with lock:
        for mac in REG:
            agent_entry(mac, REG)
    os.makedirs(RAW_DIR, exist_ok=True)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:                                               # буфер приёма побольше: залп от 16 плат или занятый замок
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    s.bind(("0.0.0.0", port))
    print(f"[udp] приём телеметрии на порту {port}", flush=True)
    path = raw_path(time.time())
    _replay["on"] = True
    threading.Thread(target=_replay_task, args=(os.path.getsize(path) if os.path.exists(path) else 0,),
                     daemon=True).start()
    while True:
        data, (ip, _) = s.recvfrom(65535)
        t = time.time()
        stats["udp_t"] = t
        try:                                           # один плохой пакет (или занятый файл) не должен убить приёмник
            handle_datagram(t, ip, data.decode("utf-8", "replace").strip())
        except Exception as e:                         # noqa: BLE001
            stats["err"] += 1
            stats["err_last"] = f"{time.strftime('%H:%M:%S')} {e!r}"
            if stats["err"] <= 20 or stats["err"] % 100 == 0:
                print(f"[udp] ошибка разбора #{stats['err']}: {e!r} — {data[:100]!r}", flush=True)


# ---------------- управление ----------------
def load_control():
    if os.path.exists(CONTROL):
        try:
            c = json.load(open(CONTROL, encoding="utf-8"))
            control["next_cid"] = int(c.get("next_cid", 1))
            control["desired"] = dict(c.get("desired", {}))
            control["common"] = c.get("common")
            control["own"] = dict(c.get("own", {}))
            control["compose"] = dict(c.get("compose", {}))
        except Exception as e:
            print(f"[ctl] не прочитан {CONTROL}: {e}", flush=True)


def replace_retry(src, dst):
    """os.replace с повтором: Windows не даёт заменить файл, пока его читает другой поток
    (06.10.2026 из-за этого падала программа эксперимента)."""
    for k in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == 19:
                raise
            time.sleep(0.05)


def save_control():
    os.makedirs(DATA, exist_ok=True)
    tmp = CONTROL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(control, f, ensure_ascii=False, indent=1)
    replace_retry(tmp, CONTROL)


def cfg_line(c):
    line = (f"CFG cid={c['cid']} src={c['src']} bits={c['bits']} nb={c['nb']} all={c['all']} td={c['td']}"
            f" tmax={c.get('tmax', 73)} mode={c.get('mode', 0)} nr={c.get('nr', 1)} run={c.get('run', 0)}"   # без поля run — стоять
            f" k={c.get('k', 24)} pre={c.get('pre', 1)} cpu={c.get('cpu', 240)} txp={c.get('txp', 20)}"
            f" ping={c.get('ping', 100)} hb={c.get('hb', 1)} led={c.get('led', 1)} tp={c.get('tp', 1000)}"
            f" role={c.get('role', 0)} sm={c.get('sm', 1)} sf={c.get('sf', 0)} cf={c.get('cf', 0)}"
            f" sh={c.get('sh', 0)} bgr={c.get('bgr', 0)} tl={c.get('tl', 0)} tq={c.get('tq', 0)}"
            f" rmm={c.get('rmm', 450)} afs={c.get('afs', 2)} avg={c.get('avg', 2)} pl={c.get('pl', 0)}"
            f" dc={c.get('dc', 0)} gv={c.get('gv', 0)} rk={c.get('rk', 1)} pw={c.get('pw', 0)} raw={c.get('raw', 0)}"
            f" dur={c.get('dur', 0)} ro={c.get('ro', 0)}"                 # шаг на агенте: длительность, радио выкл
            f" bg={','.join(str(i) for i in c.get('bg', []))}"          # сет B — всегда, пустой тоже:
            f" bm={','.join(m.replace(':', '') for m in c.get('bm', []))}")   # иначе агент помнит старый
    if c.get("mode") == 1:
        line += " grp=" + ",".join(str(i) for i in c.get("grp") or [])   # пусто — стоп без группы
        if c.get("gm"):                    # MAC участников: агенты узнают о новых без перепрошивки
            line += " gm=" + ",".join(m.replace(":", "") for m in c["gm"])
    return line


def send_line(line, ip):
    try:
        cmd_sock.sendto(line.encode(), (ip, CMD_PORT))
    except OSError as e:
        print(f"[ctl] не отправлено {ip}: {e}", flush=True)


def validate_cfg(c):
    try:
        out = {"src": int(c["src"]), "bits": int(c["bits"]), "nb": int(c["nb"]),
               "all": 1 if c.get("all") else 0, "td": int(c["td"]),
               "tmax": int(c.get("tmax", 73)), "mode": int(c.get("mode", 0)), "nr": int(c.get("nr", 1)),
               "run": 1 if c.get("run", 1) else 0,
               "k": int(c.get("k", 24)), "pre": 1 if c.get("pre", 1) else 0, "tp": int(c.get("tp", 1000)),
               "cpu": int(c.get("cpu", 240)), "txp": int(c.get("txp", 20)), "ping": int(c.get("ping", 100)),
               "led": 1 if c.get("led", 1) else 0,
               "role": int(c.get("role", 0)), "sm": int(c.get("sm", 1)), "sf": int(c.get("sf", 0)),
               "cf": int(c.get("cf", 0)), "sh": 1 if c.get("sh", 0) else 0, "bgr": int(c.get("bgr", 0)),
               "tl": int(c.get("tl", 0)), "tq": int(c.get("tq", 0)), "rmm": int(c.get("rmm", 450)),
               "afs": int(c.get("afs", 2)), "avg": int(c.get("avg", 2)), "pl": 1 if c.get("pl", 0) else 0,
               "dc": 1 if c.get("dc", 0) else 0,
               "gv": int(c.get("gv", 0)), "rk": int(c.get("rk", 1)), "pw": int(c.get("pw", 0)),
               "raw": int(c.get("raw", 0))}
    except (KeyError, TypeError, ValueError):
        raise ValueError("нужны поля src, bits, nb, td")
    if not 40 <= out["tmax"] <= 75:
        raise ValueError("предел температуры от 40 до 70 °C")
    if out["mode"] not in (0, 1):
        raise ValueError("режим 0 или 1")
    if out["nr"] not in (0, 1, 2):
        raise ValueError("скорость ESP-NOW: 1 (24 Мбит/с), 2 (6 Мбит/с) или 0 (1 Мбит/с)")
    if out["src"] <= 0 or out["src"] & ~127:
        raise ValueError("отметьте хотя бы один источник")
    if out["bits"] not in OPTIONS["bits"]:
        raise ValueError(f"длина должна быть одной из {OPTIONS['bits']}")
    if not 1 <= out["nb"] <= len(BASES):
        raise ValueError(f"баз Миллера — Рабина от 1 до {len(BASES)}")
    if not 0 <= out["td"] <= 20000:
        raise ValueError("порог отсева от 0 до 20000")
    if not 4 <= out["k"] <= 64:
        raise ValueError("кандидатов за сбор от 4 до 64")
    if out["gv"] not in (0, 1, 2):
        raise ValueError("вариант группы: 0 — все на одном числе, 1 — эстафета, 2 — каскад")
    if not 1 <= out["rk"] <= 16:
        raise ValueError("простых за ход от 1 до 16")
    if not 0 <= out["pw"] <= 5000:
        raise ValueError("ожидание кусочков от 0 (ждать всех) до 5000 мс")
    if out["raw"] not in (0, 1, 2):
        raise ValueError("шум: 0 — с хешем, 1 — без хеша, 2 — без хеша с выравниванием фон Неймана")
    if out["cpu"] not in (80, 160, 240):
        raise ValueError("частота процессора 80, 160 или 240 МГц")
    if not 2 <= out["txp"] <= 20:
        raise ValueError("мощность передатчика от 2 до 20 дБм")
    out["hb"] = int(c.get("hb", 1))
    if not 1 <= out["hb"] <= 20:
        raise ValueError("hb: 1–20 кадров пульса в секунду")
    out["dur"] = int(c.get("dur", 0))                               # шаг на агенте: сам останавливается через dur с
    out["ro"] = 1 if c.get("ro") else 0                              # радио выкл на шаге (эфир не нужен)
    if not 0 <= out["dur"] <= 7200:
        raise ValueError("длительность шага на агенте от 0 (без срока) до 7200 с")
    if out["ro"] and not out["dur"]:
        raise ValueError("выключать радио можно только на шаге с длительностью на агенте (dur)")
    if out["ping"] != 0 and not 10 <= out["ping"] <= 200:          # 0 — без пинга (CSI только с маяков)
        raise ValueError("пинг для CSI от 10 до 200 в секунду")
    if not 500 <= out["tp"] <= 5000:
        raise ValueError("период телеметрии от 500 до 5000 мс")
    if out["role"] not in (0, 1, 2, 3):
        raise ValueError("роль: 0 — генерация (A), 1 — рассеяние (B), 2 — статический акселерометр, 3 — агент моторов")
    if not 0 <= out["sm"] <= 4:
        raise ValueError("режим рассеяния от 0 до 4")
    if not 0 <= out["sf"] <= 3:
        raise ValueError("функция рассеяния от 0 до 3")
    if out["cf"] not in (0, 1, 2, 3):                              # 3 — только пинг, без CSI (опыт)
        raise ValueError("CSI: 0 — любые кадры, 1 — только роутер, 2 — только сет B")
    if not 0 <= out["bgr"] <= 200:
        raise ValueError("фон сета B от 0 до 200 кадров в секунду")
    if out["tl"] not in (0, 1, 2):
        raise ValueError("телеметрия: 0 — обычная, 1 — редкая, 2 — молчание")
    if not 0 <= out["tq"] <= 65535:
        raise ValueError("молчание от 0 (пока не отменят) до 65535 с")
    if not 50 <= out["rmm"] <= 2000:
        raise ValueError("радиус датчика вращения от 50 до 2000 мм")
    if out["afs"] not in (2, 4, 8, 16):
        raise ValueError("предел статического акселерометра: 2, 4, 8 или 16 g")
    if out["avg"] not in (1, 2, 4, 8):
        raise ValueError("усреднение статического акселерометра: 1, 2, 4 или 8 отсчётов")
    return out


def validate_own(own):
    """Свои параметры агентов: только OWN_KEYS; пусто — как общие."""
    out = {}
    for mac, d in (own or {}).items():
        o = {k: v for k, v in (d or {}).items() if k in OWN_KEYS and v is not None and v != ""}
        if not o:
            continue
        name = agents[mac.lower()]["id"] if mac.lower() in agents else mac
        try:
            v = validate_cfg({**DEFAULT_CFG, **o})
        except ValueError as e:
            raise ValueError(f"агент №{name}: {e}")
        out[mac.lower()] = {k: v[k] for k in o}
    return out


def soft_mismatch(want, p):
    """Агент работает не с теми параметрами «на ходу», что заданы (старая прошивка их не шлёт)."""
    return any(p.get(k) is not None and p.get(k) != want.get(k, DEFAULT_CFG[k]) for k in SOFT_KEYS)


def apply_control(targets, cfg, own=None, force=False, remember=True, label=None):
    """Настройки выбранным агентам: общие + свои у каждого. Возвращает номер (cid).

    Номер новый, если сменилось что-то кроме параметров «на ходу» (или force —
    «сбросить счётчики»); в группе номер один на всех участников. Сменились
    только параметры «на ходу» — номер прежний, агент применит их без сброса счёта.
    remember — запомнить как «общие» формы (составы этого не делают: у каждой группы свои);
    label — подпись запуска в журнале опытов (например «группа 1 · эстафета»)."""
    cfg = validate_cfg(cfg)
    sends = []
    with lock:
        if own is None:
            own = control.get("own") or {}
        else:                                      # свои параметры дополняют прежние (не стирают остальных агентов);
            cleared = [m.lower() for m, d in own.items() if not d]   # пустые {} — снять свои у этого агента
            merged = {**(control.get("own") or {}), **validate_own(own)}
            for m in cleared:
                merged.pop(m, None)
            control["own"] = own = merged
        if remember:
            control["common"] = {k: v for k, v in cfg.items() if k != "run"}
        if targets == "all":
            targets = [m for m, a in agents.items() if a["gen"] is not None or a.get("stel")]
        targets = [m.lower() for m in targets if m.lower() in agents]
        if not targets:
            raise ValueError("нет агентов с прошивкой prime_lab")
        eff = {m: validate_cfg({**cfg, **own.get(m, {})}) for m in targets}
        for m in targets:                          # измерителю, агенту моторов и катушки группа не нужна: прошивка
            if eff[m]["role"] in (2, 3):           # отвергает «сообща» без списка группы (grp missing)
                eff[m]["mode"] = 0
        set_a = [m for m in targets if eff[m]["role"] == 0]
        set_b = [m for m in targets if eff[m]["role"] == 1]
        by_id = {agents[m]["id"]: m for m in targets}
        if set_b:                                      # сет B — по порядку номеров
            ids_b = sorted(agents[m]["id"] for m in set_b)
            if 0 in ids_b:
                raise ValueError("в сете B может быть только агент из реестра (с номером)")
            if len(ids_b) > 16:
                raise ValueError("в сете B не больше 16 агентов")
            for m in targets:
                eff[m]["bg"] = ids_b
                eff[m]["bm"] = [by_id[i] for i in ids_b]
        # группа — все выбранные из сета A, по порядку номеров. Остановке (run 0) группа не нужна: стоп перед
        # прошивкой по Wi-Fi одному агенту катушки при «сообща» в общих настройках отказывал (25.10.2026)
        if cfg["mode"] == 1 and not set_a and not cfg.get("run"):
            for m in targets:                      # стоп без генераторов — без группы
                eff[m]["mode"] = 0
        if cfg["mode"] == 1 and (set_a or cfg.get("run")):
            ids = sorted(agents[m]["id"] for m in set_a)
            if not ids:
                raise ValueError("режим «сообща»: нет ни одного агента с ролью «генерация»")
            if 0 in ids:
                raise ValueError("в группе может быть только агент из реестра (с номером)")
            if len(ids) > 16:
                raise ValueError("в группе не больше 16 агентов")
            for m in targets:
                eff[m]["grp"] = ids
                eff[m]["gm"] = [by_id[i] for i in ids]   # адреса — из реестра сервера, в порядке эстафеты
        prev = control["desired"]
        hard = lambda c: {k: c.get(k) for k in HARD_KEYS}
        changed = [m for m in targets if force or m not in prev or hard(prev[m]) != hard(eff[m])]
        if (cfg["mode"] == 1 or set_b) and (changed or len({prev[m]["cid"] for m in targets}) > 1):
            changed = list(targets)                    # группа и сет B: номер один на всех
        cid = None
        if changed:
            cid = control["next_cid"]
            control["next_cid"] += 1
        now = time.time()
        for mac in targets:
            prev[mac] = {**eff[mac], "cid": cid if mac in changed else prev[mac]["cid"]}
            a = agents[mac]
            if a["ip"]:
                a["cfg_sent"] = now
                sends.append((cfg_line(prev[mac]), a["ip"]))
        if cid is None:
            cid = prev[targets[0]]["cid"]
        save_control()
        with open(COMMANDS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(now, 3), "cid": cid, "targets": targets, "cfg": cfg, "own": own,
                                "new": changed}, ensure_ascii=False) + "\n")
    for line, ip in sends:
        send_line(line, ip)
    print(f"[ctl] cid={cid} {'новый' if changed else 'на ходу'} -> {', '.join(targets)}: {cfg} свои: {own}",
          flush=True)
    if runlog:                                         # журнал опытов: что задано — запись откроется или закроется
        with lock:
            names = {m: f"№{agents[m]['id']}" if agents[m]["id"] else m for m in targets}
        e0 = eff[targets[0]]
        runlog.on_apply(cid, targets, {**cfg, **{k: e0[k] for k in ("grp", "bg") if e0.get(k)},
                                       **({"label": label} if label else {})}, own, bool(changed), now, names)
    return cid


def apply_all(targets, cfg, own=None, force=False):
    """Кнопка «Применить»: всем агентам prime_lab, статическим тоже (роль S — своя у агента)."""
    return apply_control(targets, cfg, own, force)


# ---------------- составы: колёса, логические группы, наборы ----------------
# Колесо — где агент стоит физически (задаётся раз, по месту установки). Логическая группа —
# как агенты работают сейчас: группа 1…4, в ней генерация (A) и рассеяние (B); у группы свой
# набор. «Пуск» применяет все группы сразу (у каждой свой номер настройки — группы друг другу
# не мешают), не попавшие в группы стоят и остывают; «Стоп» — все стоят и остывают; «Тушение» —
# все только рассеивают свой шум (шум в шум, простых нет). Новый агент появляется сам, стоит.
PRESETS = [
    {"key": "solo", "name": "Каждый сам", "cfg": {"mode": 0, "dc": 0},
     "desc": "каждый агент сам набирает шум, складывает число, отсеивает и проверяет; ~25 простых/с на плату"},
    {"key": "solo_dc", "name": "Каждый сам, два ядра", "cfg": {"mode": 0, "dc": 1},
     "desc": "ядро связи набирает шум и отсеивает, ядро счёта только проверяет; ~45 простых/с на плату, греет сильнее"},
    {"key": "one", "name": "Сообща — все на одном числе", "cfg": {"mode": 1, "gv": 0, "dc": 0},
     "desc": "одно число на всех, базы поделены по кругу, каждое число проверяют все; медленно (~10 простых/с на четверых)"},
    {"key": "relay", "name": "Эстафета", "cfg": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0},
     "desc": "число из шума всех, проверяет один, ход по кругу; ~32 простых/с на группу при любом составе, платы прохладнее"},
    {"key": "cascade", "name": "Каскад", "cfg": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20},
     "desc": "числа из шума всех, проверяют все сразу; ~90 простых/с на четверых; чужие кусочки ждёт не дольше 20 мс"},
    {"key": "relay_b", "name": "Эстафета + рассеяние", "cfg": {"mode": 1, "gv": 1, "dc": 1, "sm": 1, "sh": 0, "bgr": 0}, "b": True,
     "desc": "агенты «рассеяние» этой группы выпускают в эфир каждое её простое, смешав со своим шумом"},
    {"key": "sham", "name": "Пустышка — контроль", "cfg": {"mode": 1, "gv": 1, "dc": 1, "sm": 1, "sh": 1, "bgr": 0}, "b": True,
     "desc": "тот же трафик рассеяния, но вместо числа — шум: отделяет действие числа от действия радио"},
    {"key": "loop", "name": "Петля через эфир", "cfg": {"mode": 0, "dc": 0, "sm": 1, "sh": 0, "bgr": 50}, "a_src": 16, "b": True,
     "desc": "генерация берёт шум только из эфира рассеяния своей группы; рассеяние выпускает фон 50 кадров/с"},
]
PRESET_BY_KEY = {p["key"]: p for p in PRESETS}
QUENCH_CFG = {"mode": 0, "gv": 0, "dc": 0, "sm": 1, "sh": 1, "bgr": 50, "run": 1}   # все — рассеяние: свой шум в эфир
STANDBY_CFG = {"run": 0, "cpu": 80, "led": 0}                                         # стоять и остывать
SLOTS = ["idle"] + [f"{g}{r}" for g in "1234" for r in "AB"] + ["S", "coil"]   # coil — агент M катушки колеса (wheels)
# Свои параметры группы поверх набора (пульт, docs/ui_concept.md): что ищем, шум, проверка, связь, тепло, журнал.
GROUP_CFG_KEYS = ("bits", "nb", "all", "td", "src", "raw", "dc", "rk", "pw", "k", "pre", "nr", "txp", "cpu", "tmax", "pl",
                  "sm", "sh", "bgr", "sf", "cf")
GROUP_KEYS = "1234"
SUBGROUPS = "абвгдежз"        # подгруппы на колесе (планшетка): буква у агента; без буквы — «а»


def compose_state():
    """Составы (под lock): колёса, места агентов, наборы групп; недостающее — по умолчанию."""
    c = control.setdefault("compose", {})
    c.setdefault("wheels", {})
    c.setdefault("slot", {})
    c.setdefault("sub", {})                        # подгруппа агента на колесе: буква а–з (планшетка)
    c.setdefault("forced", {})                     # что набор подменил у агента (src петли) — вернуть потом
    g = c.setdefault("groups", {})
    for k in GROUP_KEYS:
        g.setdefault(k, {"preset": "relay"})
    c.setdefault("state", "")
    return c


def compose_brief():
    with lock:
        c = compose_state()
        return {"wheels": dict(c["wheels"]), "slot": dict(c["slot"]), "sub": dict(c["sub"]),
                "groups": {k: dict(v) for k, v in c["groups"].items()},
                "state": c["state"], "t": c.get("t")}


def compose_save(body):
    """Колёса, места агентов и наборы групп — из панели, без применения."""
    with lock:
        c = compose_state()
        for mac, w in (body.get("wheels") or {}).items():
            mac = mac.lower()
            if w in ("", None):
                c["wheels"].pop(mac, None)
            elif str(w) in "1234" and len(str(w)) == 1:
                c["wheels"][mac] = str(w)
            else:
                raise ValueError(f"колесо «{w}»: 1–4 или пусто")
        for mac, s in (body.get("sub") or {}).items():         # подгруппа на колесе: буква а–з или пусто (= «а»)
            mac = mac.lower()
            if s in ("", None):
                c["sub"].pop(mac, None)
            elif str(s) in SUBGROUPS and len(str(s)) == 1:
                c["sub"][mac] = str(s)
            else:
                raise ValueError(f"подгруппа «{s}»: буква а–з или пусто")
        for mac, sl in (body.get("slot") or {}).items():
            if sl not in SLOTS:
                raise ValueError(f"место «{sl}»: стоит, группа 1–4 (генерация A или рассеяние B), статический, катушка")
            c["slot"][mac.lower()] = sl
        for k, gr in (body.get("groups") or {}).items():
            if k not in GROUP_KEYS:
                raise ValueError(f"группа «{k}»: 1–4")
            if gr.get("preset") is not None:
                if gr["preset"] not in PRESET_BY_KEY:
                    raise ValueError(f"набор «{gr['preset']}» не известен")
                c["groups"][k]["preset"] = gr["preset"]
            if isinstance(gr.get("cfg"), dict):          # свои параметры группы: None — убрать (как в наборе)
                cur = c["groups"][k].setdefault("cfg", {})
                for key, v in gr["cfg"].items():
                    if key not in GROUP_CFG_KEYS:
                        raise ValueError(f"параметр группы «{key}» не задаётся")
                    if v is None:
                        cur.pop(key, None)
                    else:
                        cur[key] = int(v)
                validate_cfg({**DEFAULT_CFG, **cur})
        save_control()
    return compose_brief()


def compose_members():
    """Агенты prime_lab по местам: {группа: {"A": [...], "B": [...]}}, стоящие, статические. Под lock."""
    c = compose_state()
    groups, idle, static = {}, [], []
    for m, a in agents.items():
        if a["gen"] is None and not a.get("stel"):
            continue
        sl = c["slot"].get(m, "S" if a.get("stel") else "idle")
        if sl == "S":
            static.append(m)
        elif sl == "idle":
            idle.append(m)
        else:
            groups.setdefault(sl[0], {"A": [], "B": []})[sl[1]].append(m)
    return groups, idle, static


def _own_with(own, macs, **kw):
    for m in macs:
        own[m] = {**own.get(m, {}), **kw}


def compose_apply():
    """Пуск: каждая группа — своей настройкой со своим номером; остальные стоят и остывают."""
    with lock:
        c = compose_state()
        base = dict(control.get("common") or DEFAULT_CFG)
        own = {m: dict(o) for m, o in (control.get("own") or {}).items()}
        for m, f in c["forced"].items():          # вернуть то, что прошлый набор подменил
            o = own.setdefault(m, {})
            for k, v in f.items():
                if v is None:
                    o.pop(k, None)
                else:
                    o[k] = v
        c["forced"] = {}
        groups, idle, static = compose_members()
        plan = []
        for g in sorted(groups):
            mem = groups[g]
            if not mem["A"] and not mem["B"]:
                continue
            p = PRESET_BY_KEY.get(c["groups"][g]["preset"], PRESETS[0])
            _own_with(own, mem["A"], role=0)
            _own_with(own, mem["B"], role=1)
            if p.get("a_src"):                     # петля: генерация — только из эфира рассеяния
                for m in mem["A"]:
                    c["forced"][m] = {"src": own[m].get("src")}
                    own[m]["src"] = p["a_src"]
            plan.append((mem["A"] + mem["B"], {**base, **p["cfg"], **(c["groups"][g].get("cfg") or {}), "run": 1},
                         f"группа {g} · {p['name']}"))
        if idle:
            plan.append((idle, {**base, **STANDBY_CFG, "mode": 0}, "стоят"))
        _own_with(own, static, role=2)
        if static:
            plan.append((static, {**base, "run": 1}, "статические"))
        if not plan:
            raise ValueError("никого нет: расставьте агентов по группам")
        c["state"] = "run"
        c["t"] = round(time.time(), 1)
        save_control()
    out = []
    for targets, cfg, label in plan:
        cid = apply_control(targets, cfg, own, remember=False, label=label)
        out.append({"label": label, "cid": cid, "n": len(targets)})
    return out


def compose_all_gen():
    """Все агенты prime_lab, кроме статических (под lock)."""
    return [m for m, a in agents.items() if a["gen"] is not None and not a.get("stel")]


def compose_stop():
    """Стоп: все стоят и остывают (80 МГц, без светодиода); статические меряют дальше."""
    with lock:
        targets = compose_all_gen()
        base = dict(control.get("common") or DEFAULT_CFG)
        c = compose_state()
        own = {m: dict(o) for m, o in (control.get("own") or {}).items()}
        for m in targets:                          # роли — как в составе (после тушения все были рассеянием)
            sl = c["slot"].get(m, "idle")
            own[m] = {**own.get(m, {}), "role": 1 if sl.endswith("B") else 0}
        c["state"] = "stop"
        c["t"] = round(time.time(), 1)
        save_control()
    if not targets:
        raise ValueError("нет агентов-генераторов")
    return [{"label": "стоп", "cid": apply_control(targets, {**base, **STANDBY_CFG, "mode": 0}, own, remember=False, label="стоп"),
             "n": len(targets)}]


def compose_quench():
    """Тушение: все только рассеивают свой шум — фон, пустышка, ни одного простого (шум в шум)."""
    with lock:
        targets = compose_all_gen()
        base = dict(control.get("common") or DEFAULT_CFG)
        own = {m: dict(o) for m, o in (control.get("own") or {}).items()}
        _own_with(own, targets, role=1)
        c = compose_state()
        c["state"] = "quench"
        c["t"] = round(time.time(), 1)
        save_control()
    if not targets:
        raise ValueError("нет агентов-генераторов")
    return [{"label": "тушение", "cid": apply_control(targets, {**base, **QUENCH_CFG}, own, remember=False, label="тушение — шум в шум"),
             "n": len(targets)}]


def adopt_static(mac):
    """Агент стартовал статическим (роль S в его памяти), а на сервере роль не задана — принять её,
    а не переключать агент обратно. Под lock."""
    o = control.setdefault("own", {}).setdefault(mac, {})
    if "role" in o:
        return
    o["role"] = 2
    if mac in control["desired"]:
        control["desired"][mac]["role"] = 2
    save_control()
    print(f"[ctl] {mac}: стартовал статическим — роль S принята", flush=True)


APPLIED_KEYS = ("cid", "src", "bits", "nb", "all", "td", "tmax", "mode", "grp", "nr", "run", "k", "pre", "cpu", "txp", "hb",
                "txr", "ping", "led", "tp", "role", "sm", "sf", "cf", "sh", "bgr", "tl", "tq", "tls", "rmm", "afs", "avg",
                "pl", "dc", "gv", "rk", "pw", "raw", "dur", "ro")


# ---------------- прошивка по Wi-Fi (OTA) ----------------
# Образ — build/prime_lab/prime_lab.ino.bin (собирает tools/build.sh), версия — version.txt рядом.
# Команда агенту подписана ключом secrets/ota.key (HMAC-SHA256 от "url|md5|len|ver|t"): без ключа
# прошивку в плату по сети не залить. t растёт — старую команду агент не примет повторно.
_fw_cache = {"mtime": None, "info": None}


def fw_info():
    path = os.path.join(FW_DIR, "prime_lab.ino.bin")
    vf = os.path.join(FW_DIR, "version.txt")
    if not os.path.isfile(path):
        return None
    m = os.path.getmtime(path)
    key = (m, os.path.getmtime(vf) if os.path.isfile(vf) else None)   # version.txt пишется после образа
    if _fw_cache["mtime"] != key:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        ver = open(vf, encoding="utf-8").read().strip() if os.path.isfile(vf) else "?"
        _fw_cache.update(mtime=key, info={"version": ver.replace(" ", "_") or "?", "md5": h.hexdigest(),
                                          "size": os.path.getsize(path), "mtime": round(m)})
    return _fw_cache["info"]


def ota_key():
    """Ключ из secrets/ota.key (64 знака hex) или None. Сам ключ наружу не отдаётся."""
    try:
        k = open(OTA_KEY_FILE, encoding="utf-8").read().strip().lower()
    except OSError:
        return None
    return k if len(k) == 64 and all(c in "0123456789abcdef" for c in k) else None


def server_ip_for(agent_ip):
    ips = local_ipv4()
    same = [ip for ip in ips if ip.rsplit(".", 1)[0] == str(agent_ip).rsplit(".", 1)[0]]
    return (same or ips or ["127.0.0.1"])[0]


def ota_start(targets, force=False):
    """Разослать выбранным агентам (на связи) подписанную команду прошиться по Wi-Fi. Сначала всем — стоять."""
    if session_active["s"] is not None or runner.active:
        raise ValueError("идёт сессия или программа — сначала остановите")
    key = ota_key()
    if not key:
        raise ValueError("нет ключа secrets/ota.key: выполните tools/gen_secrets.py и прошейте платы по USB")
    fw = fw_info()
    if not fw:
        raise ValueError("нет собранного образа build/prime_lab/prime_lab.ino.bin — соберите: tools/build.sh prime_lab")
    now = time.time()
    with lock:
        if targets == "all":
            macs = [m for m, a in agents.items() if a["last"] and now - a["last"] < 10 and a["ip"]
                    and (a["gen"] is not None or a.get("stel"))]
        else:
            macs = [m.lower() for m in targets if m.lower() in agents and agents[m.lower()]["ip"]]
        todo = [(m, agents[m]["ip"], agents[m]["fw"], agents[m].get("ota")) for m in macs]
    if not todo:
        raise ValueError("нет агентов на связи")
    apply_control([m for m, _, _, _ in todo], {**(control.get("common") or DEFAULT_CFG), "run": 0})
    t = int(now)
    sent, skipped = [], {}
    for m, ip, fwv, ota in todo:
        if str(fwv or "").startswith("phone"):        # телефон — приложение, не плата: обновляется установкой APK
            skipped[m] = "телефон — обновляется приложением"
            continue
        if fwv == fw["version"] and not force:
            skipped[m] = "уже эта версия"
            continue
        if ota and ota[1]:
            skipped[m] = "прошлая прошивка ещё не подтверждена — подождите минуту"
            continue
        url = f"http://{server_ip_for(ip)}:{HTTP_PORT}/fw/prime_lab.bin"
        msg = f"{url}|{fw['md5']}|{fw['size']}|{fw['version']}|{t}"
        sig = hmac.new(bytes.fromhex(key), msg.encode(), hashlib.sha256).hexdigest()
        send_line(f"OTA url={url} md5={fw['md5']} len={fw['size']} ver={fw['version']} t={t} sig={sig}", ip)
        with lock:
            agents[m]["ota_cmd"] = {"t": t, "ver": fw["version"], "from": fwv}
        sent.append(m)
    with open(COMMANDS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(now, 3), "ota": fw["version"], "sent": sent, "skipped": skipped},
                           ensure_ascii=False) + "\n")
    print(f"[ota] {fw['version']} -> {', '.join(sent) or 'никому'}; пропущены: {skipped}", flush=True)
    return {"sent": sent, "skipped": skipped, "version": fw["version"]}


# ---------------- новые платы по USB ----------------
# Новая плата прошивается по USB один раз (tools/new_board.py): так в неё попадает prime_lab с прошивкой
# по Wi-Fi. Сервер видит платы на USB (серийный номер USB у ESP32-S3 — MAC, плату не трогаем) и по кнопке
# записывает новые в реестр, собирает и заливает. Кнопка — только с этого компьютера: платы подключены к нему.
usb = {"mod": None, "job": None, "boards": [], "err": None}


def usb_ticker():
    while True:
        try:
            usb["boards"], usb["err"] = usb["mod"].scan(), None
        except Exception as e:                         # noqa: BLE001 — нет pyserial и т. п.
            usb["boards"], usb["err"] = [], str(e)
        time.sleep(3)


def usb_brief():
    if not usb["mod"]:
        return {"boards": [], "err": usb["err"] or "нет tools/new_board.py", "job": None}
    now, out = time.time(), []
    with lock:
        for port, mac in usb["boards"]:
            a, reg = (agents.get(mac), REG.get(mac)) if mac else (None, None)
            on = bool(a and a["last"] and now - a["last"] < 10)
            out.append({"port": port, "mac": mac, "id": reg["id"] if reg else None, "fw": (a or {}).get("fw") or "",
                        "status": "unknown" if not mac else "new" if not reg else "online" if on else "offline"})
    return {"boards": out, "err": usb["err"], "job": usb["job"].brief()}


def usb_flash(ports, client_ip):
    if client_ip not in ("127.0.0.1", "::1") and client_ip not in local_ipv4():
        raise ValueError("прошивка по USB — только с компьютера, к которому подключены платы")
    if not usb["mod"]:
        raise ValueError(usb["err"] or "нет tools/new_board.py")
    found = dict(usb["mod"].scan())
    todo = [(p, found[p]) for p in ports if p in found]
    if not todo:
        raise ValueError("этих плат на USB уже нет — переподключите кабель")
    usb["job"].start(todo)
    print(f"[usb] прошивка по USB: {', '.join(p for p, _ in todo)}", flush=True)
    return usb["job"].brief()


def usb_done(res, err):
    with open(COMMANDS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "usb_flash": [{"port": p, "mac": m, "id": i} for p, m, i in res or []],
                            "error": err}, ensure_ascii=False) + "\n")
    print(f"[usb] {'прошиты: ' + ', '.join(f'№{i} {p}' for p, _, i in res) if res else 'ошибка: ' + str(err)}", flush=True)


# ---------------- отдых по очереди (docs/thermal.md, § 4.3) ----------------
# В эстафете и каскаде урезанный регулятором агент тормозит всю группу: его кусочки и ходы ждут.
# Диспетчер раз в 2 с отправляет самого горячего из урезанных отдыхать («REST s=…»): агент говорит группе
# «отдыхаю» (его сразу не ждут), стоит на 80 МГц и ничего не считает; вернувшись — «снова в деле».
# Одновременно отдыхает не больше четверти группы (из 4 — один), агент — не чаще раза в gap_s.
REST_ROT = {"hot": 2.0,        # у предела: не ниже tmax − 2 °C …
            "work_lt": 85.0,   # … и регулятор режет работу ниже 85 %
            "frac": 0.25, "min_s": 60, "max_s": 150, "cool": 5.0, "gap_s": 180, "min_group": 3}
# Вся группа разом (06.10.2026: по очереди в каскаде не дало прироста в установившемся режиме, а кругов из
# кусочков всех стало меньше — отдыхающего всё время заменяет чужой шум). Работают все вместе на полной
# мощности, пока регулятор не начнёт резать самого горячего; тогда отдыхают все вместе, пока он не остынет
# на cool °C (не меньше min_s, не больше max_s), и вместе возвращаются: пока работают — круги из кусочков всех.
REST_GROUP = {"hot": 2.0, "work_lt": 95.0, "min_s": 40, "max_s": 90, "cool": 6.0, "min_group": 2}
rest_rot = {}                  # mac -> {on, t0, temp0, cid, end}
rest_grp = {}                  # cid -> {t0, hot (mac), temp0} — вся группа отдыхает


def rest_rot_on():                   # по умолчанию выключен: опыты 06.10.2026 прироста не дали (docs/thermal.md)
    return bool((control.get("rest_rot") or {}).get("on", False))


def was_resting(mac):
    """Отдыхает по команде диспетчера (или только что вернулся — «снова в деле» ещё в пути)."""
    st = rest_rot.get(mac) or {}
    return bool(st.get("on")) or time.time() - st.get("end", 0) < 15


def rest_rot_mode():
    return (control.get("rest_rot") or {}).get("mode", "turns")


def rest_tick():
    now = time.time()
    acts = []
    with lock:
        on = rest_rot_on()
        groups = {}
        for mac, a in agents.items():
            g, st = a["gen"], rest_rot.get(mac)
            live = a["last"] and now - a["last"] < 5
            if not g or g.get("mode") != 1 or g.get("gv") not in (1, 2) or not g.get("run") or g.get("role") or not live:
                if st and st.get("on"):                # группа встала или агент пропал — отдых кончился сам
                    st.update(on=False, end=now)
                continue
            if st and st.get("on") and st.get("cid") != g.get("cid"):
                st.update(on=False, end=now)           # новая настройка — отдых сброшен у агента
            groups.setdefault(g["cid"], []).append(mac)
        for cid in [c for c in rest_grp if c not in groups]:
            rest_grp.pop(cid)                          # группа встала или новая настройка
        if on and rest_rot_mode() == "group":
            for cid, mem in groups.items():
                gr = rest_grp.get(cid)
                if gr:                                 # вся группа отдыхает: вернуть, как остынет самый горячий
                    temp = (agents.get(gr["hot"], {}).get("values") or {}).get("temp")
                    dur = now - gr["t0"]
                    if dur >= REST_GROUP["max_s"] or \
                            (dur >= REST_GROUP["min_s"] and temp is not None and temp <= gr["temp0"] - REST_GROUP["cool"]):
                        rest_grp.pop(cid)
                        for m in mem:
                            st = rest_rot.get(m) or {}
                            st.update(on=False, end=now)
                            acts.append(("BACK", m, agents[m]["ip"], agents[m]["id"], st.get("temp0", 0),
                                         (agents[m].get("values") or {}).get("temp"), dur))
                    continue
                if len(mem) < REST_GROUP["min_group"]:
                    continue
                hot = []
                for m in mem:
                    v = agents[m].get("values") or {}
                    temp, work, tmax = v.get("temp"), v.get("work"), v.get("tmax") or 73
                    if temp is not None and work is not None and temp >= tmax - REST_GROUP["hot"] and work < REST_GROUP["work_lt"]:
                        hot.append((temp, m))
                if hot:                                # регулятор начал резать самого горячего — отдыхают все
                    temp, mh = max(hot)
                    rest_grp[cid] = {"t0": now, "hot": mh, "temp0": temp}
                    for m in mem:
                        tm = (agents[m].get("values") or {}).get("temp") or 0
                        rest_rot[m] = {"on": True, "t0": now, "temp0": tm, "cid": cid, "end": 0, "grp": True}
                        acts.append(("REST", m, agents[m]["ip"], agents[m]["id"], tm, None, 0))
            groups = {}                                # по очереди — не в этом режиме
        elif rest_grp:                                 # режим сменили или выключили — вернуть всех
            for cid, mem in groups.items():
                if rest_grp.pop(cid, None):
                    for m in mem:
                        st = rest_rot.get(m) or {}
                        st.update(on=False, end=now)
                        acts.append(("BACK", m, agents[m]["ip"], agents[m]["id"], st.get("temp0", 0),
                                     (agents[m].get("values") or {}).get("temp"), now - st.get("t0", now)))
            rest_grp.clear()
        for cid, mem in groups.items():
            for m in mem:                              # конец отдыха: остыл на cool °C, или вышел срок
                st = rest_rot.get(m)
                if not st or not st.get("on"):
                    continue
                temp = (agents[m].get("values") or {}).get("temp")
                dur = now - st["t0"]
                if not on or dur >= REST_ROT["max_s"] or \
                        (dur >= REST_ROT["min_s"] and temp is not None and temp <= st["temp0"] - REST_ROT["cool"]):
                    st.update(on=False, end=now)
                    acts.append(("BACK", m, agents[m]["ip"], agents[m]["id"], st["temp0"], temp, dur))
            if not on or len(mem) < REST_ROT["min_group"]:
                continue
            cap = max(1, int(len(mem) * REST_ROT["frac"]))
            if sum(1 for m in mem if (rest_rot.get(m) or {}).get("on")) >= cap:
                continue
            cands = []
            for m in mem:
                st = rest_rot.get(m) or {}
                if st.get("on") or now - st.get("end", 0) < REST_ROT["gap_s"]:
                    continue
                v = agents[m].get("values") or {}
                temp, work, tmax = v.get("temp"), v.get("work"), v.get("tmax") or 73
                if temp is not None and work is not None and temp >= tmax - REST_ROT["hot"] and work < REST_ROT["work_lt"]:
                    cands.append((temp, m))
            if cands:
                temp, m = max(cands)                   # самый горячий
                rest_rot[m] = {"on": True, "t0": now, "temp0": temp, "cid": cid, "end": 0}
                acts.append(("REST", m, agents[m]["ip"], agents[m]["id"], temp, None, 0))
    for kind, m, ip, aid, t0, t1, dur in acts:
        if kind == "REST":
            send_line(f"REST s={int(max(REST_ROT['max_s'], REST_GROUP['max_s']) + 30)}", ip)   # сам вернётся, если сервер пропадёт
            print(f"[rest] №{aid} отдыхает: {t0:.1f} °C", flush=True)
        else:
            send_line("BACK", ip)
            print(f"[rest] №{aid} снова в деле: {t0:.1f} → {t1 if t1 is not None else '?'} °C за {dur:.0f} с", flush=True)
            if ratings:
                ratings.add(now, m, aid, "rest", True, t0=f"{t0:.1f}", t1=f"{t1:.1f}" if t1 is not None else "?",
                            s=f"{dur:.0f}")
    return acts


batteries = Batteries(lambda: os.path.join(DATA, "battery.json"), log=lambda text: print(text, flush=True))


rig = {"gap_cm": None, "t": None, "hist": []}      # установка руками: расстояние между колёсами, см, и его история


def rig_load():
    try:
        d = json.load(open(RIG_FILE, encoding="utf-8"))
        rig.update(gap_cm=d.get("gap_cm"), t=d.get("t"), hist=d.get("hist") or [])
    except (OSError, ValueError):
        pass


def rig_set(gap):
    """Расстояние между колёсами (ставится руками): пишется в каждый шаг программы и сессию; изменения — в историю."""
    g = None if gap in (None, "") else round(float(gap), 1)
    if g is not None and not 0 <= g <= 500:
        raise ValueError("расстояние между колёсами: 0–500 см")
    with lock:
        was = rig["gap_cm"]
        rig.update(gap_cm=g, t=round(time.time(), 1))
        rig["hist"] = (rig["hist"] + [{"t": rig["t"], "gap_cm": g, "was": was}])[-500:]
        body = json.dumps(rig, ensure_ascii=False)
    with open(RIG_FILE, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[rig] расстояние между колёсами: {was} → {g} см", flush=True)


def rig_brief():
    return {"gap_cm": rig["gap_cm"], "t": rig["t"]}


def battery_label(mac):
    """Чей INA219: шина колеса (агент на шине) или катушка (агент катушки)."""
    with lock:
        c = compose_state()
        a = agents.get(mac) or {}
        w, coil = c["wheels"].get(mac, ""), c["slot"].get(mac) == "coil"
        alive = bool(a.get("last") and time.time() - a["last"] < 10)
    return {"wheel": w, "kind": "coil" if coil else "bus", "id": a.get("id"), "alive": alive,
            "label": (f"катушка К{w}" if coil else f"шина К{w}") if w else f"№{a.get('id')}"}


def battery_brief():
    return batteries.state(battery_label)


def battery_ticker():
    """Пара села до «мало» — идущая программа встаёт на паузу перед следующим шагом (заменить и «Продолжить»)."""
    while True:
        time.sleep(2)
        try:
            for mac, v in batteries.check_low():
                lab = battery_label(mac)["label"]
                print(f"[bat] {lab}: {v:.2f} В — пора менять аккумуляторы", flush=True)
                if runner.active:
                    runner.request_pause(f"аккумуляторы {lab} сели ({v:.2f} В): заменить и продолжить")
        except Exception as e:                     # noqa: BLE001
            print(f"[bat] {e}", flush=True)


def rest_ticker():
    while True:
        time.sleep(2)
        try:
            rest_tick()
        except Exception as e:                         # noqa: BLE001
            print(f"[rest] {e}", flush=True)


def rest_brief():
    now = time.time()
    with lock:
        return {"on": rest_rot_on(), "mode": rest_rot_mode(), "cfg": REST_ROT, "cfg_group": REST_GROUP,
                "groups": len(rest_grp),
                "now": {m: {"id": agents[m]["id"], "s": round(now - st["t0"]), "temp0": st["temp0"],
                            "temp": (agents[m].get("values") or {}).get("temp")}
                        for m, st in rest_rot.items() if st.get("on") and m in agents}}


# ---------------- телеметрия всей установки (docs/ui_concept.md) ----------------
# Обычная / редкая / молчание — у всех генераторов сразу; статические говорят всегда. Команда уходит
# каждому сразу: агент в молчании сам ничего не шлёт — сервер не дождался бы пакета, чтобы ответить.
def telemetry_set(tl):
    if tl not in (0, 1, 2):
        raise ValueError("tl: 0 обычная, 1 редкая, 2 молчание")
    lines = []
    with lock:
        own = control.setdefault("own", {})
        for m, a in agents.items():
            # измерители и агенты моторов/катушек говорят всегда: в «молчании» агент M жил бы только на ack'ах команд,
            # и первый разрыв связи дольше 10 с выводил бы моторы и катушки из управления насовсем
            if a.get("kind") in ("S", "M") or a.get("stel") or a.get("mtel") or not a["ip"] or own.get(m, {}).get("role") in (2, 3):
                continue
            own.setdefault(m, {})["tl"] = tl
            want = control["desired"].get(m)
            if want:
                want["tl"] = tl
                lines.append((cfg_line(want), a["ip"]))
        control["tele"] = tl
        save_control()
    for line, ip in lines:
        send_line(line, ip)
    print(f"[tele] телеметрия {['обычная', 'редкая', 'молчание'][tl]} → {len(lines)} генераторам", flush=True)
    return len(lines)


# ---------------- страж статистики простых (экран «Наблюдение») ----------------
# По каждой настройке (cid) — на лету из журнала простых: баланс бит, остатки по 3, 5, 7, 11, повторы,
# ошибки счёта. Выход и отсев против теории — из счётчиков агентов (как tools/anomaly_run.py).
GUARD_KEEP = 12
GUARD_SEEN = 300_000          # сколько последних простых настройки помнить для поиска повторов (полная проверка — tools/prime_stats.py)
guard = {}                    # cid -> {n, bits, ones, mod{q: [счёт по остаткам]}, seen(set), dups, fails, t}


def guard_add(cid, hexes, fails):
    g = guard.get(cid)
    if g is None:
        if len(guard) >= GUARD_KEEP:
            guard.pop(min(guard, key=lambda c: guard[c]["t"]))
        g = guard[cid] = {"n": 0, "bits": 0, "ones": 0, "mod": {q: [0] * q for q in (3, 5, 7, 11)}, "seen": set(),
                          "order": deque(), "dups": 0, "fails": 0, "t": 0}
    for hx in hexes:
        n = int(hx, 16)
        bits = n.bit_length()
        if not g["bits"]:
            g["bits"] = bits
        if bits != g["bits"]:
            continue
        g["n"] += 1
        g["ones"] += bin(n >> 1).count("1") - 1           # разряды 1…bits−2 (старший и младший выставлены)
        for q, cnt in g["mod"].items():
            cnt[n % q] += 1
        h = hash(hx)
        if h in g["seen"]:
            g["dups"] += 1
        else:                                             # повторы ищем среди последних GUARD_SEEN (память: 10 ч × 180/с — 6 млн)
            g["seen"].add(h)
            g["order"].append(h)
            if len(g["order"]) > GUARD_SEEN:
                g["seen"].discard(g["order"].popleft())
    g["fails"] += len(fails)
    g["t"] = time.time()


def _chi2_p(x, df):
    if df <= 0:
        return 1.0
    z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return 0.5 * math.erfc(z / math.sqrt(2))


def _surv_frac(td, _cache={}):
    if td not in _cache:
        s, sieve = 1.0, bytearray([1]) * max(td, 2)
        for i in range(2, td):
            if sieve[i]:
                if i > 2:
                    s *= 1 - 1 / i
                sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
        _cache[td] = s if td else 1.0
    return _cache[td]


def guard_brief():
    """Страж по каждой настройке, где сейчас считают: z и p — ✓ |z| < 3, ◐ < 5, ✗ дальше."""
    out = {}
    with lock:
        by = {}
        for m, a in agents.items():
            g = a["gen"]
            if g and g.get("run") and not g.get("role") and a["last"] and time.time() - a["last"] < 30:
                by.setdefault(g["cid"], []).append(a)
        for cid, mem in by.items():
            g0 = mem[0]["gen"]
            bits, td = g0.get("bits") or 256, g0.get("td") or 0
            one = g0.get("mode") == 1 and not g0.get("gv")
            cand = sum((x["gstat"] or {}).get("cands", 0) if one else (x["gen"].get("cand") or 0) for x in mem)
            tested = sum((x["gstat"] or {}).get("cycles_m", 0) if one else (x["gen"].get("mri") or 0) for x in mem)
            pr = sum((x["gstat"] or {}).get("primes_m", 0) if one else (x["gen"].get("pr") or 0) for x in mem)
            tdr = max(cand - tested, 0) if one else sum(x["gen"].get("tdr") or 0 for x in mem)
            if g0.get("mode") == 1 and not one:
                tested -= pr                           # эстафета, каскад: сверка соседа — тоже вызов проверки
            q = 1 - _surv_frac(td)
            v = cand * q * (1 - q)
            z_sieve = (tdr - cand * q) / math.sqrt(v) if v > 0 else None
            dens = 2 / (bits * math.log(2) - 0.307)
            exp = tested * dens / _surv_frac(td) if tested > 0 else 0
            z_yield = (pr - exp) / math.sqrt(exp) if exp > 0 else None
            r = {"cid": cid, "ids": sorted(x["id"] for x in mem), "pr": pr, "cand": cand,
                 "pr_s": round(sum(((x["gen"] or {}).get("rate") or {}).get("g_pr_s", 0) for x in mem), 2),
                 "z_sieve": None if z_sieve is None else round(z_sieve, 2),
                 "z_yield": None if z_yield is None else round(z_yield, 2),
                 # чувствительность: отклонение выхода, которое уже набранные простые заметили бы на 3σ (%)
                 "sens_pct": round(300 / math.sqrt(exp), 2) if exp > 0 else None}
            gd = guard.get(cid)
            if gd and gd["n"]:
                n, b = gd["n"], gd["bits"]
                r["logged"] = n
                r["z_bits"] = round((gd["ones"] - n * (b - 2) / 2) / math.sqrt(n * (b - 2) / 4), 2) if b > 2 else None
                r["mod_p"] = {str(qq): round(_chi2_p(sum((c - n / (qq - 1)) ** 2 / (n / (qq - 1)) for c in cnt[1:]), qq - 2), 4)
                              for qq, cnt in gd["mod"].items() if n >= 5 * (qq - 1)}
                r["div"] = sum(cnt[0] for cnt in gd["mod"].values())   # делится на 3, 5, 7, 11 — не простое
                r["dups"], r["fails"] = gd["dups"], gd["fails"]
            out[cid] = r
    return out


def control_status():
    out = {}
    for mac, a in agents.items():
        st = a.get("stel")
        if a["gen"] is None and not st and mac not in control["desired"]:
            continue
        want = control["desired"].get(mac)
        g = a["gen"] or st or {}
        applied = {k: g.get(k) for k in APPLIED_KEYS} if (a["gen"] or st) else None
        out[mac] = {"desired": want, "applied": applied, "ack": a["ack"], "static": bool(st),
                    "pending": bool(want and (not applied or applied["cid"] != want["cid"]
                                              or soft_mismatch(want, applied)))}
    return out


# ---------------- маяк для поиска сервера ----------------
def local_ipv4():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def beacon(udp_port):
    msg = f"W2SRV {udp_port}".encode()
    while True:
        for ip in local_ipv4():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, 0))
                s.sendto(msg, (ip.rsplit(".", 1)[0] + ".255", BEACON_PORT))
                s.close()
            except OSError:
                pass
        time.sleep(2)


# ---------------- HTTP ----------------
def quiet_now(a, now):
    """Агенту велено молчать, и срок молчания (если задан) ещё не вышел."""
    want = control["desired"].get(a["mac"])
    if not want or want.get("tl") != 2:
        return False
    return not want.get("tq") or now - a["cfg_sent"] < want["tq"] + 10


def id_to_mac(aid):
    return next((m for m, x in agents.items() if x["id"] == aid), None)


def state():
    now = time.time()
    rb = ratings.brief(now) if ratings else {}
    with lock:
        out = []
        for a in agents.values():
            age = None if a["last"] is None else now - a["last"]
            if age is None:
                status = "never"
            elif age < 3:
                status = "online"
            elif quiet_now(a, now):
                status = "quiet"
            elif age < 10:
                status = "late"
            else:
                status = "offline"
            if REG.get(a["mac"], {}).get("role") == "retired" and status in ("never", "offline"):
                continue                               # выбывшие платы (реестр: role retired) — не показывать, пока молчат
            last_min = [x for x in a["recent_rx"] if x >= now - 60]
            span = (last_min[-1] - last_min[0]) if len(last_min) > 1 else 0
            expected = a["run_packets"] + a["run_lost"]
            out.append({
                "mac": a["mac"], "id": a["id"], "label": a["label"], "registered": a["registered"],
                "fw": a["fw"], "sk": a["sk"], "ip": a["ip"], "status": status,
                "age": None if age is None else round(age, 1),
                "packets": a["run_packets"], "lost": a["run_lost"], "reboots": a["reboots"],
                "packets_total": a["packets"], "lost_total": a["lost"],
                "run_age": None if a["run_start"] is None else round(now - a["run_start"]),
                "loss_pct": round(100 * a["run_lost"] / expected, 2) if expected else 0,
                "rate": round((len(last_min) - 1) / span, 2) if span > 0 else 0,
                "values": a["values"], "kind": a.get("kind"),
                "sens": ((a["gen"] or {}).get("sens") or (a.get("stel") or {}).get("sens") or 0),
                "dev": (a.get("stel") or {}).get("dev") or "",   # статический: adxl355 / icm42688 / phone
                "ph": (a.get("stel") or {}).get("ph"),           # телефон: фон 50 Гц, радио, батарея
                "mot": (a.get("mtel") or {}).get("mot"),         # агент моторов: цель, ШИМ, состояние
                "plog": plog_brief(a, now), "ota": a.get("ota"), "ota_cmd": a.get("ota_cmd"), "rbt": a.get("rbt"),
                "nbr": a.get("nbr"),                  # кого слышит по пульсу ESP-NOW и с каким сигналом
                "rating": rb.get(a["mac"]) or {"score": 100, "cat": "ok", "sys": [], "n": 0, "recent": []},
            })
        out.sort(key=lambda x: (x["id"] == 0, x["id"], x["mac"]))
        metrics = sorted({k for (_, k) in series})
        return {"time": now, "uptime": round(now - stats["started"]), "rx": stats["rx"],
                "bad": stats["bad"], "err": stats["err"], "err_last": stats["err_last"],
                "udp_age": round(now - stats["udp_t"], 1) if stats["udp_t"] else None,   # давность последнего пакета
                "replaying": _replay["on"], "agents": out, "metrics": metrics}


ADB = shutil.which("adb") or r"C:\Android\Sdk\platform-tools\adb.exe"
phone_usb = {}              # серийный номер телефона -> состояние adb (device / unauthorized / …)


def phone_link():
    """Телефон по USB: раз в 15 с — adb reverse tcp:8090, чтобы приложение на телефоне слало пакеты
    на 127.0.0.1:8090 и без Wi-Fi. Телефон, не разрешивший отладку, — в журнал (разрешить на его экране)."""
    nowin = 0x08000000 if os.name == "nt" else 0          # без мелькающих окон консоли
    def run(*args):
        return subprocess.run([ADB, *args], capture_output=True, text=True, timeout=15, creationflags=nowin).stdout
    while True:
        try:
            seen = {}
            for ln in run("devices").splitlines()[1:]:
                parts = ln.split()
                if len(parts) >= 2:
                    seen[parts[0]] = parts[1]
            for serial, st in seen.items():
                if st == "device" and f"tcp:{HTTP_PORT}" not in run("-s", serial, "reverse", "--list"):
                    run("-s", serial, "reverse", f"tcp:{HTTP_PORT}", f"tcp:{HTTP_PORT}")
                    print(f"[phone] {serial}: USB-мост tcp:{HTTP_PORT} включён", flush=True)
                if phone_usb.get(serial) != st:
                    print(f"[phone] {serial}: {st}" + (" — разрешите отладку по USB на экране телефона"
                                                       if st == "unauthorized" else ""), flush=True)
            phone_usb.clear()
            phone_usb.update(seen)
        except Exception as e:                         # noqa: BLE001
            phone_usb["err"] = repr(e)
        time.sleep(15)


def wheel_rpm(wheel):
    """Обороты колеса ("1"/"2") сейчас — по ICM-42688 агентов на его ободе (пакет imu не старше 3 с); нет — None."""
    now = time.time()
    with lock:
        wheels = compose_state()["wheels"]
        ws = [abs(a["imu"].get("w") or 0) * 60 / (2 * math.pi) for m, a in agents.items()
              if wheels.get(m) == wheel and a.get("imu") and now - (a["imu"].get("t_rx") or 0) < 3]
    return sum(ws) / len(ws) if ws else None


def is_m_agent(m, a, c):
    """Агент моторов или катушки: по телеметрии (kind M) или по назначению — место «coil» в составе либо своя роль 3.
    По назначению — чтобы после перезапуска сервера плата, ещё не вышедшая на связь, не считалась несуществующей
    (планшетка писала «агента нет», хотя он назначен). Под lock."""
    return a.get("kind") == "M" or c["slot"].get(m) == "coil" or (control.get("own") or {}).get(m, {}).get("role") == 3


def motor_agents():
    """Агенты моторов: роль M, не назначенные катушкой."""
    now = time.time()
    with lock:
        c = compose_state()
        return [(m, a["ip"], bool(a["last"] and now - a["last"] < 10)) for m, a in agents.items()
                if is_m_agent(m, a, c) and c["slot"].get(m) != "coil"]


def coil_agents():
    """Агенты катушек: роль M, место «coil» и колесо в составе → {колесо: [(mac, ip, на связи), …]} — на колесе их
    может быть несколько (катушка из двух плат): команда уходит каждому, иначе второго останавливал бы его сторож."""
    now = time.time()
    out = {}
    with lock:
        c = compose_state()
        for m, a in sorted(agents.items(), key=lambda x: x[1]["id"] or 999):
            if is_m_agent(m, a, c) and c["slot"].get(m) == "coil" and c["wheels"].get(m) in ("1", "2"):
                out.setdefault(c["wheels"][m], []).append((m, a["ip"], bool(a["last"] and now - a["last"] < 10)))
    return out


coils = Coils(coil_agents, lambda line, ip: send_line(line, ip), log=lambda text: print(text, flush=True))


def coils_state():
    st = coils.brief()
    now = time.time()
    with lock:
        c = compose_state()
        st["agents"] = {}                             # колесо → первый агент катушки; остальные — в его "more"
        for m, a in sorted(agents.items(), key=lambda x: x[1]["id"] or 999):
            if not (is_m_agent(m, a, c) and c["slot"].get(m) == "coil"):
                continue
            mt = a.get("mtel") or {}
            row = {"mac": m, "id": a["id"], "alive": bool(a["last"] and now - a["last"] < 10),
                   "age": round(now - a["last"], 1) if a["last"] else None, "mot": mt.get("mot"), "temp": mt.get("temp")}
            w = c["wheels"].get(m, "")
            if w in st["agents"]:
                st["agents"][w].setdefault("more", []).append(row)
            else:
                st["agents"][w] = row
    return st


def wheel_radius(wheel):
    """Радиус, который меряет датчик на ободе колеса (медиана последних 5 оборотов не старше 15 с), и k последнего
    оборота (по этому радиусу): (мм, k) или (None, None)."""
    now = time.time()
    with lock:
        wheels = compose_state()["wheels"]
        revs = [r for m, a in agents.items() if wheels.get(m) == wheel for r in list(a.get("wrevs") or [])[-5:]
                if now - r["ts"] < 15 and r.get("r_mm")]
    if not revs:
        return None, None
    rs = sorted(r["r_mm"] for r in revs)
    last = max(revs, key=lambda r: r["ts"])
    # k — измеренный (|среднее f| за оборот), а не k_r по радиусу из настроек: невесомость держится по измеренному
    # радиусу, и k_r при ней равен rmm/r_изм — значок «✓ невесомость наверху» у моторов врал бы при любом другом rmm
    return rs[len(rs) // 2], last.get("k")


motors = Motors(motor_agents, lambda line, ip: send_line(line, ip), wheel_rpm, radius_fn=wheel_radius,
                rmm_fn=lambda: (control.get("common") or DEFAULT_CFG).get("rmm", 450), log=lambda text: print(text, flush=True))


def motors_state():
    st = motors.brief()
    now = time.time()
    with lock:
        c = compose_state()
        st["agents"] = [{"mac": m, "id": a["id"], "alive": bool(a["last"] and now - a["last"] < 10),
                         "age": round(now - a["last"], 1) if a["last"] else None, "fw": a["fw"],
                         "mot": (a.get("mtel") or {}).get("mot"), "temp": (a.get("mtel") or {}).get("temp")}
                        for m, a in agents.items() if is_m_agent(m, a, c) and c["slot"].get(m) != "coil"]
    return st


threads = {}                # имя -> задача сервера (приём, проверка простых, маяк, статика, отдых, журнал)


def spawn(name, target, *args):
    t = threading.Thread(target=target, args=args, daemon=True, name=name)
    threads[name] = t
    t.start()
    return t


def health():
    """Самопроверка сервера: живы ли задачи, давно ли был пакет, ошибки разбора, очередь проверки,
    место на диске, свежесть файлов дня — для нижней строки пульта и tools/*."""
    now = time.time()
    try:
        free_mb = shutil.disk_usage(DATA).free // (1 << 20)
    except OSError:
        free_mb = None
    raw = raw_path(now)
    with lock:
        ages = {f"№{a['id']}" if a["id"] else m: round(now - a["last"], 1) for m, a in agents.items() if a["last"]}
        quiet = [f"№{a['id']}" if a["id"] else m for m, a in agents.items() if quiet_now(a, now)]
    problems = []
    for k, t in threads.items():
        if not t.is_alive():
            problems.append(f"задача «{k}» остановилась")
    if stats["udp_t"] and now - stats["udp_t"] > 30 and ages:
        problems.append(f"пакетов нет {round(now - stats['udp_t'])} с")
    if stats["err"]:
        problems.append(f"ошибок разбора {stats['err']} (последняя: {stats['err_last']})")
    if free_mb is not None and free_mb < 2000:
        problems.append(f"на диске {free_mb} МБ")
    if prm_queue.qsize() > 1000:
        problems.append(f"очередь проверки простых {prm_queue.qsize()}")
    return {"ok": not problems, "problems": problems, "time": now, "uptime": round(now - stats["started"]),
            "threads": {k: t.is_alive() for k, t in threads.items()}, "udp_age": round(now - stats["udp_t"], 1) if stats["udp_t"] else None,
            "rx": stats["rx"], "bad": stats["bad"], "err": stats["err"], "err_last": stats["err_last"],
            "replaying": _replay["on"], "prm_queue": prm_queue.qsize(), "disk_free_mb": free_mb,
            "raw_file": {"name": os.path.basename(raw), "bytes": os.path.getsize(raw) if os.path.exists(raw) else 0},
            "agents_age": ages, "quiet": quiet, "fw": fw_info()}


def downsample(pts, n=MAX_POINTS):
    """Не больше n точек: соседние усредняются."""
    if len(pts) <= n:
        return pts
    k = len(pts) / n
    out = []
    for i in range(n):
        chunk = pts[int(i * k):int((i + 1) * k)]
        if chunk:
            out.append((chunk[-1][0], sum(v for _, v in chunk) / len(chunk)))
    return out


def series_for(metric, window):
    t0 = time.time() - window
    with lock:
        res = {}
        for (mac, k), d in series.items():
            if k == metric:
                pts = [(t, v) for t, v in d if t >= t0]
                if pts:
                    res[mac] = {"id": agents[mac]["id"], "label": agents[mac]["label"], "points": downsample(pts)}
        return res


def group_summary():
    """Группы (по номеру настройки): темп и время цикла — суммы по мастерам,
    когерентность — худшая из участников, сравнение с «каждый сам»."""
    out = {}
    for mac, a in agents.items():
        g, gs = a["gen"], a["gstat"]
        if not g or g.get("mode") != 1 or not gs or gs.get("cid") != g.get("cid"):
            continue
        r = out.setdefault(g["cid"], {"cid": g["cid"], "grp": g.get("grp"), "bits": g.get("bits"),
                                      "gv": g.get("gv") or 0, "truncs": 0, "trunc_pcs": 0, "mute": 0, "mute_now": [], "resting": [],
                                      "members": [], "cycles": 0, "primes": 0, "cyc_s": 0.0, "pr_s": 0.0,
                                      "t": [0] * 5, "c": [0] * 3, "asks": 0, "breaks": 0, "resyncs": 0,
                                      "conflicts": 0, "streak": None, "streak_max": None,
                                      "streak_ms": None, "streak_ms_max": None, "solo_pr_s": 0.0,
                                      "solo_known": 0, **{k: 0 for k in RSV_KEYS}, "pre_hit": 0,
                                      "pre_miss": 0, "hops": [], "agree": 0, "split": 0, "disagree": 0,
                                      "gls": None})
        rate = gs.get("rate", {})
        r["members"].append({"mac": mac, "id": a["id"], "role": gs.get("role"), "cycles_m": gs.get("cycles_m"),
                             "streak": gs.get("streak"), "found": gs.get("found"), "rx": gs.get("rx"),
                             "tx": gs.get("tx"), "tx_fail": gs.get("tx_fail"), "ok_to": gs.get("ok_to"),
                             "fail_to": gs.get("fail_to"), "rssi_from": gs.get("rssi_from")})
        r["cycles"] += gs.get("cycles_m", 0)
        r["primes"] += gs.get("primes_m", 0)
        r["cyc_s"] += rate.get("g_cyc_s", 0)
        r["pr_s"] += rate.get("g_gpr_s", 0)
        for i in range(5):
            r["t"][i] += (gs.get("t") or [0] * 5)[i]
        for i in range(3):
            r["c"][i] += (gs.get("c") or [0] * 3)[i]
        r["asks"] += gs.get("asks_tx", 0)
        r["breaks"] += gs.get("brk_pcs", 0) + gs.get("brk_res", 0) + gs.get("brk_hand", 0)
        r["resyncs"] += gs.get("resyncs", 0)
        r["conflicts"] += gs.get("conflicts", 0)
        tr = gs.get("tr") or [0, 0]                    # эстафета, каскад: круги без чьих-то кусочков
        r["truncs"] += tr[0]
        r["trunc_pcs"] += tr[1]
        mu = gs.get("mute") or [0, 0]                  # замолкали участники; кто молчит сейчас (бит — место в группе)
        r["mute"] += mu[0]
        if (gs.get("rest") or [0])[0] == 1 and a["id"] not in r["resting"]:
            r["resting"].append(a["id"])               # отдыхает по команде диспетчера
        for i, mid in enumerate(g.get("grp") or []):  # маска — только в пакете из трёх чисел (прошивка с fb4bf53)
            if len(mu) >= 3 and (mu[1] >> i) & 1 and mid not in r["mute_now"]:
                r["mute_now"].append(mid)
        r["agree"] += gs.get("agr", 0)                 # согласие: ответы всех про одно число сошлись?
        r["split"] += gs.get("split", 0)
        r["disagree"] += gs.get("dis", 0)
        if gs.get("gls"):
            r["gls"] = gs["gls"]
        for k in RSV_KEYS:
            r[k] += gs.get(k, 0)
        pre = gs.get("pre") or [0, 0]
        r["pre_hit"] += pre[0]
        r["pre_miss"] += pre[1]
        if gs.get("hop"):
            r["hops"].append(gs["hop"])
        for k in ("streak", "streak_max", "streak_ms", "streak_ms_max"):
            v = gs.get(k, 0)
            r[k] = v if r[k] is None else min(r[k], v)
        b = a["baseline"].get(baseline_key(g))
        if b:
            r["solo_pr_s"] += b["pr_s"]
            r["solo_known"] += 1
    c = compose_state()
    for r in out.values():                             # логическая группа состава — по месту участников
        sl = {c["slot"].get(m["mac"], "") for m in r["members"]}
        gs_ = {x[0] for x in sl if x and x[0] in GROUP_KEYS}
        if len(gs_) == 1:
            g = gs_.pop()
            r["name"] = f"группа {g}"
            r["preset"] = PRESET_BY_KEY.get(c["groups"][g]["preset"], {}).get("name")
        n = r["cycles"]
        r["avg_ms"] = [round(x / n / 1000, 3) for x in r["t"]] if n else None
        r["comp_ms"] = [round(x / n / 1000, 3) for x in r["c"]] if n else None
        r["sync_pct"] = (round(100 * max(0, r["t"][4] - sum(r["c"])) / r["t"][4], 1) if r["t"][4] else None)
        r["slowdown"] = round(r["solo_pr_s"] / r["pr_s"], 2) if r["pr_s"] > 0 and r["solo_known"] else None
        r.update(reserve_summary(r, n))
    return list(out.values())


RSV_KEYS = ("gath", "cands", "surv", "rdrop", "starts", "fillw", "early")


def sensor_summary():
    """Колесо (imu), поле (mag), статические акселерометры — последнее по каждому агенту."""
    wheel, field, stat = [], [], []
    for mac, a in agents.items():
        if a.get("imu"):
            i = a["imu"]
            wheel.append({"mac": mac, "id": a["id"], **{k: i.get(k) for k in ("hz", "w", "t", "revs", "r_set",
                                                                              "err", "frame", "t_rx", "last_rev")},
                          "rpm_now": round((i.get("w") or 0) * 60 / (2 * math.pi), 2),
                          "set": a.get("kind")})
        if a.get("mag"):
            field.append({"mac": mac, "id": a["id"], **a["mag"], "set": a.get("kind")})
    return {"wheel": sorted(wheel, key=lambda x: x["id"]), "field": sorted(field, key=lambda x: x["id"])}


def static_summary(now=None):
    """Статические акселерометры: состояние анализа (static_watch) + агент (номер, телеметрия).
    Сначала замок watch, потом lock — не вложенно."""
    now = now or time.time()
    w = watch.summary(now) if watch else {"sensors": [], "events": [], "rec": None, "error": WATCH_ERR}
    with lock:
        for s in w["sensors"]:
            a = agents.get(s["mac"]) or {}
            s.update({"id": a.get("id", 0), "label": a.get("label", ""), "tel": a.get("stel")})
        for mac, a in agents.items():               # статический на связи, а отсчётов ещё не было
            if a.get("stel") and not any(s["mac"] == mac for s in w["sensors"]):
                w["sensors"].append({"mac": mac, "id": a["id"], "label": a["label"], "tel": a["stel"],
                                     "status": "none", "text": "✗ отсчётов ещё нет", "conds": []})
        w["sensors"].sort(key=lambda s: (s.get("id") or 999, s["mac"]))
        w["generators_busy"] = [f"№{a['id']}" for a in agents.values() if a["gen"] and a["gen"].get("run")]
    w["now_verdict"] = current_verdict(now)
    return w


def current_verdict(now):
    """Вывод для того, что идёт сейчас: шаг программы, сессия или последние 2 минуты."""
    if not watch:
        return None
    pr = runner.active
    if pr and pr.get("now") and pr["now"].get("t_meas"):
        t0, what = pr["now"]["t_meas"], f"шаг программы «{pr['now']['name']}»"
    elif session_active["s"] and session_active["s"].get("t_start"):
        t0, what = session_active["s"]["t_start"], f"сессия №{session_active['s']['id']}"
    else:
        t0, what = now - 120, "последние 2 минуты"
    v = watch.verdict(t0, now)
    v["what"] = what
    return v


def scatter_summary():
    """Рассеяние: кто в каком сете, сколько объявлено, получено, выпущено, темп и задержка."""
    rows = []
    for mac, a in agents.items():
        g = a["gen"]
        if not g or g.get("role") is None:
            continue
        r = g.get("rate") or {}
        rows.append({"mac": mac, "id": a["id"], "role": g.get("role"), "sm": g.get("sm"), "sf": g.get("sf"),
                     "cf": g.get("cf"), "run": g.get("run"), "src": g.get("src"),
                     **{k: g.get(k) for k in SCAT_KEYS[4:]},
                     "lat_ms": round(g["slat"] / g["sn"] / 1000, 3) if g.get("sn") else None,
                     **{k: r.get(k) for k in ("g_ann_s", "g_prx_s", "g_stx_s", "g_heard_s", "g_slat_ms",
                                              "g_sbg_s", "g_bits_bc")}})
    rows.sort(key=lambda x: (x["role"], x["id"]))
    return rows if any(x["role"] == 1 for x in rows) else []


def reserve_summary(t, cycles):
    """Запас кандидатов и кусочки заранее: доли для панели и итогов сессии."""
    hops = t.get("hops") or []
    pre = t["pre_hit"] + t["pre_miss"]
    return {"surv_pct": round(100 * t["surv"] / t["cands"], 1) if t["cands"] else None,
            "gath_per_cycle": round(t["gath"] / cycles, 3) if cycles else None,
            "wait_pct": round(100 * (t["starts"] + t["fillw"]) / cycles, 2) if cycles else None,
            "pre_pct": round(100 * t["pre_hit"] / pre, 1) if pre else None,
            "hop_ms": round(sum(hops) / len(hops) / 1000, 3) if hops else None}


def dash(window, metrics, t0=None, t1=None):
    """Всё для дашборда одним ответом: агенты, генератор, управление, ряды.
    t0, t1 — промежуток рядов (просмотр прошлой сессии); иначе последние window секунд."""
    st = state()
    stat = static_summary()                      # до lock: у static_watch свой замок
    if t0 is None:
        t0, t1 = time.time() - window, None
    wanted = set(metrics)
    with lock:
        ser = {}
        for (mac, k), d in series.items():
            if k not in wanted:
                continue
            pts = downsample([(t, v) for t, v in d if t >= t0 and (t1 is None or t <= t1)])
            if pts:
                ser.setdefault(k, {})[mac] = [[t for t, _ in pts], [v for _, v in pts]]
        gen = {mac: {**a["gen"], "verify": {k: v for k, v in a["verify"].items()},
                     "gstat": a["gstat"] if (a["gen"] or {}).get("mode") == 1 else None,
                     "baseline": a["baseline"].get(baseline_key(a["gen"]))}
               for mac, a in agents.items() if a["gen"] is not None}
        groups = group_summary()
        sensors = sensor_summary()
        ctl = control_status()
        cmp = {"wheels": dict(compose_state()["wheels"]), "slot": dict(compose_state()["slot"]),
               "sub": dict(compose_state()["sub"]),
               "groups": {k: dict(v) for k, v in compose_state()["groups"].items()}, "state": compose_state()["state"],
               "t": compose_state().get("t")}
        scat = scatter_summary()                 # под замком: иначе «dictionary changed size» при первом пакете новой платы
        common = dict(control.get("common") or {}) or None
        own = {m: dict(o) for m, o in (control.get("own") or {}).items()}   # копии: сериализуются уже без замка
    st.update({"series": ser, "gen": gen, "groups": groups, "control": ctl, "options": OPTIONS,
               "default": DEFAULT_CFG, "common": common, "own": own,
               "scatter": scat, **sensors, "static": stat["sensors"], "static_rec": stat["rec"],
               "static_verdict": stat["now_verdict"], "program": runner.brief(),
               "session": session_brief(session_active["s"]) if session_active["s"] else None,
               "fw": fw_info(), "ota_key": bool(ota_key()), "compose": cmp, "presets": PRESETS, "slots": SLOTS,
               "usb": usb_brief(), "rest_rot": rest_brief(), "tele": control.get("tele", 0), "guard": guard_brief()})
    st["battery"] = battery_brief()
    st["rig"] = rig_brief()
    return st


# ---------------- сессии ----------------
# Сессия — отрезок работы агентов по шагам: все собрались (на связи) →
# параметры выставились (агенты подтвердили настройку, стоя) → поехали (общий
# старт) → работа (до таймера или кнопки «Остановить») → стоп и итоги (агенты
# останавливаются, счётчики замирают, сервер забирает итоги). Каждая сессия
# хранится в data/sessions/<номер>.json, список — в data/sessions/index.json.
STEP_NAMES = {"gather": "все собрались", "config": "параметры выставились", "start": "поехали",
              "run": "работа", "stop": "стоп и итоги"}
sessions = []                                      # краткие записи по порядку номеров
session_active = {"s": None, "stop": threading.Event()}
sess_lock = threading.Lock()


def sessions_load():
    if os.path.exists(SESS_INDEX):
        try:
            sessions[:] = json.load(open(SESS_INDEX, encoding="utf-8"))
        except Exception as e:
            print(f"[sess] не прочитан {SESS_INDEX}: {e}", flush=True)
    for x in sessions:                             # прервана перезапуском сервера
        if x["state"] in ("new", "gathering", "configuring", "starting", "running", "stopping"):
            x["state"] = "interrupted"


def session_brief(x):
    return {k: x.get(k) for k in ("id", "name", "state", "created", "t_start", "t_stop", "duration_s",
                                   "targets_ids", "cfg", "steps", "summary", "note", "watch")}


def session_store(x):
    os.makedirs(SESS_DIR, exist_ok=True)
    path = os.path.join(SESS_DIR, f"{x['id']}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(x, f, ensure_ascii=False, indent=1)
    replace_retry(path + ".tmp", path)
    with sess_lock:
        brief = session_brief(x)
        for i, y in enumerate(sessions):
            if y["id"] == x["id"]:
                sessions[i] = brief
                break
        else:
            sessions.append(brief)
        with open(SESS_INDEX + ".tmp", "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=1)
        replace_retry(SESS_INDEX + ".tmp", SESS_INDEX)


def step_begin(x, key):
    st = {"key": key, "name": STEP_NAMES[key], "t0": round(time.time(), 3), "t1": None, "ok": None, "note": ""}
    x["steps"].append(st)
    session_store(x)
    return st


def step_end(x, st, ok, note=""):
    st["t1"], st["ok"], st["note"] = round(time.time(), 3), ok, note
    session_store(x)


def agent_online(a):
    return a["last"] is not None and time.time() - a["last"] < 3 and a["gen"] is not None


def wait_all(macs, cond, timeout):
    """Ждать, пока условие не выполнится для всех; вернуть тех, для кого не выполнилось."""
    t_end = time.time() + timeout
    while True:
        with lock:
            bad = [m for m in macs if not cond(agents[m])]
        if not bad or time.time() > t_end:
            return bad
        time.sleep(0.3)


def names(macs):
    return ", ".join(f"№{agents[m]['id']}" for m in macs)


def snapshot_results(macs):
    with lock:
        out = {}
        for m in macs:
            a = agents[m]
            g = {k: v for k, v in (a["gen"] or {}).items() if k != "rate"}
            in_group = g.get("mode") == 1 and not g.get("role")      # сет B в группе не участвует
            gst = a["gstat"] if in_group else None
            out[m] = {"id": a["id"], "label": a["label"], "gen": g, "gstat": gst,
                      "verify": {k: v for k, v in a["verify"].items()},
                      "temp": a["values"].get("temp"), "work": a["values"].get("work")}
        return out


def summarize(x, res):
    """Итоги: суммы по агентам и темп за время работы."""
    dur = max(1e-6, (x["t_stop"] or time.time()) - (x["t_start"] or time.time()))
    summ = {"duration_s": round(dur, 1), "agents": len(res)}
    if x["cfg"].get("mode") == 1:
        gs = [r["gstat"] for r in res.values() if r.get("gstat")]   # только участники группы (не сет B)
        cyc = sum(g.get("cycles_m", 0) for g in gs)
        pr = sum(g.get("primes_m", 0) for g in gs)
        t = [sum((g.get("t") or [0] * 5)[i] for g in gs) for i in range(5)]
        c = [sum((g.get("c") or [0] * 3)[i] for g in gs) for i in range(3)]
        summ.update({
            "cycles": cyc, "primes": pr, "primes_s": round(pr / dur, 3), "cycles_s": round(cyc / dur, 2),
            "asks": sum(g.get("asks_tx", 0) for g in gs),
            "breaks": sum(g.get("brk_pcs", 0) + g.get("brk_res", 0) + g.get("brk_hand", 0) for g in gs),
            "resyncs": sum(g.get("resyncs", 0) for g in gs),
            "agree": sum(g.get("agr", 0) for g in gs), "split": sum(g.get("split", 0) for g in gs),
            "disagree": sum(g.get("dis", 0) for g in gs),
            "streak_max": min((g.get("streak_max", 0) for g in gs), default=0),
            "streak_ms_max": min((g.get("streak_ms_max", 0) for g in gs), default=0),
            "cycle_ms": [round(v / cyc / 1000, 3) for v in t] if cyc else None,
            "comp_ms": [round(v / cyc / 1000, 3) for v in c] if cyc else None,
            "sync_pct": round(100 * max(0, t[4] - sum(c)) / t[4], 1) if t[4] else None})
        tot = {k: sum(g.get(k, 0) for g in gs) for k in RSV_KEYS}
        tot["pre_hit"] = sum((g.get("pre") or [0, 0])[0] for g in gs)
        tot["pre_miss"] = sum((g.get("pre") or [0, 0])[1] for g in gs)
        tot["hops"] = [g["hop"] for g in gs if g.get("hop")]
        summ.update({k: v for k, v in tot.items() if k != "hops"})
        summ.update(reserve_summary(tot, cyc))
    else:
        tot = {k: sum((r["gen"].get(k) or 0) for r in res.values()) for k in ("cand", "pr", "fp", "dis", "mri", "tdr")}
        summ.update({"cand": tot["cand"], "primes": tot["pr"], "primes_s": round(tot["pr"] / dur, 3),
                     "cand_s": round(tot["cand"] / dur, 1), "false_primes": tot["fp"], "fooled": tot["dis"]})
    gens = [r["gen"] or {} for r in res.values()]
    if any(g.get("role") == 1 for g in gens):          # рассеяние: суммы по сетам
        b = [g for g in gens if g.get("role") == 1]
        sn = sum(g.get("sn") or 0 for g in b)
        summ["scatter"] = {
            "sm": b[0].get("sm"), "sh": b[0].get("sh"), "bgr": b[0].get("bgr"),
            "agents_b": len(b), "agents_a": len(gens) - len(b), "sbg": sum(g.get("sbg") or 0 for g in b),
            "ann": sum(g.get("ann") or 0 for g in gens if g.get("role") == 0),
            "adrop": sum(g.get("adrop") or 0 for g in gens if g.get("role") == 0),
            **{k: sum(g.get(k) or 0 for g in b) for k in ("prx", "stx", "sdrop", "swait", "chs", "chf", "chd")},
            "heard_a": sum(g.get("heard") or 0 for g in gens if g.get("role") == 0),
            "lat_ms": round(sum(g.get("slat") or 0 for g in b) / sn / 1000, 3) if sn else None,
            "stx_s": round(sum(g.get("stx") or 0 for g in b) / dur, 2)}
    summ["verified"] = sum(r["verify"].get("n", 0) for r in res.values())
    summ["verify_fail"] = sum(r["verify"].get("fail", 0) for r in res.values())
    return summ


def run_session(x, stop):
    macs = x["targets"]
    started = False
    try:
        st = step_begin(x, "gather")
        x["state"] = "gathering"
        bad = wait_all(macs, agent_online, 30)
        if bad:
            step_end(x, st, False, "нет на связи: " + names(bad))
            raise RuntimeError("не все агенты на связи")
        step_end(x, st, True, f"на связи {names(macs)}")

        st = step_begin(x, "config")
        x["state"] = "configuring"
        cid = apply_control(macs, {**x["cfg"], "run": 0}, x.get("own"), force=True)
        x["cid_set"] = cid
        bad = wait_all(macs, lambda a: (a["gen"] or {}).get("cid") == cid, 20)
        if bad:
            step_end(x, st, False, "не подтвердили: " + names(bad))
            raise RuntimeError("не все приняли параметры")
        step_end(x, st, True, f"настройка №{cid} принята всеми")

        st = step_begin(x, "start")
        x["state"] = "starting"
        t_cmd = time.time()
        cid = apply_control(macs, {**x["cfg"], "run": 1}, x.get("own"))
        started = True
        x["cid_run"] = cid
        bad = wait_all(macs, lambda a: (a["gen"] or {}).get("cid") == cid, 15)
        if bad:
            step_end(x, st, False, "не стартовали: " + names(bad))
            raise RuntimeError("не все стартовали")
        x["t_start"] = round(t_cmd, 3)
        step_end(x, st, True, f"настройка №{cid}: все считают")
        if x.get("quiet"):                              # молчать во время работы: это «на ходу», номер прежний
            own_q = {m: {**(x.get("own") or {}).get(m, {}), "tl": 2} for m in macs}
            apply_control(macs, {**x["cfg"], "run": 1, "tl": 2}, own_q)

        st = step_begin(x, "run")
        x["state"] = "running"
        while not stop.is_set():
            if x["duration_s"] and time.time() - x["t_start"] >= x["duration_s"]:
                break
            stop.wait(0.5)
        step_end(x, st, True, "остановлена вручную" if stop.is_set() else "по таймеру")

        st = step_begin(x, "stop")
        x["state"] = "stopping"
        t_cmd = time.time()
        cid = apply_control(macs, {**x["cfg"], "run": 0}, x.get("own"))
        started = False
        x["cid_stop"], x["t_stop"] = cid, round(t_cmd, 3)
        bad = wait_all(macs, lambda a: (a["gen"] or {}).get("cid") == cid, 15)
        time.sleep(1.5)                            # пакет группы с замершими итогами
        x["results"] = snapshot_results(macs)
        x["summary"] = summarize(x, x["results"])
        if watch:                                  # статические: продолжать или повторить
            x["watch"] = watch.verdict(x["t_start"], x["t_stop"])
        step_end(x, st, not bad, "итоги собраны" + (f"; не подтвердили остановку: {names(bad)}" if bad else ""))
        x["state"] = "stopped" if stop.is_set() else "done"
    except Exception as e:
        x["state"], x["note"] = "failed", str(e)
        if started:
            try:
                apply_control(macs, {**x["cfg"], "run": 0}, x.get("own"))
            except Exception:
                pass
    finally:
        session_store(x)
        session_active["s"] = None
        print(f"[sess] №{x['id']} {x['state']}: {x.get('summary')}", flush=True)


def session_start(body):
    if session_active["s"] is not None:
        raise ValueError("уже идёт сессия — сначала остановите её")
    if runner.active:
        raise ValueError("идёт программа эксперимента — сначала остановите её")
    cfg = validate_cfg(body.get("cfg", {}))
    cfg.pop("run", None)
    with lock:
        own = validate_own(body["own"]) if body.get("own") is not None else dict(control.get("own") or {})
    targets = body.get("targets", "all")
    with lock:
        if targets == "all":
            targets = [m for m, a in agents.items() if agent_online(a)]
        targets = [m.lower() for m in targets if m.lower() in agents]
        ids = [agents[m]["id"] for m in targets]
    if not targets:
        raise ValueError("нет агентов на связи")
    minutes = float(body.get("minutes") or 0)
    if minutes < 0 or minutes > 24 * 60:
        raise ValueError("длительность от 0 (до остановки) до 1440 минут")
    with sess_lock:
        sid = max((y["id"] for y in sessions), default=0) + 1
    x = {"id": sid, "name": (body.get("name") or "").strip()[:80] or f"сессия {sid}",
         "created": round(time.time(), 3), "state": "new", "targets": targets, "targets_ids": ids,
         "cfg": cfg, "own": own, "quiet": bool(body.get("quiet")),
         "duration_s": int(round(minutes * 60)), "steps": [], "t_start": None, "t_stop": None,
         "summary": None, "note": "", "rig": rig_brief()}
    stop = threading.Event()
    session_active["s"], session_active["stop"] = x, stop
    session_store(x)
    threading.Thread(target=run_session, args=(x, stop), daemon=True).start()
    print(f"[sess] №{sid} «{x['name']}»: {names(targets)}, {cfg}, {minutes} мин", flush=True)
    return sid


def session_get(sid):
    path = os.path.join(SESS_DIR, f"{int(sid)}.json")
    if not os.path.exists(path):
        raise ValueError("нет такой сессии")
    return json.load(open(path, encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), code)

    def send_bytes(self, body, code=200):
        """Готовый JSON — его можно собрать под замком, а отправить уже без него (медленный клиент не держит замок)."""
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self._get()
        except ValueError as e:                          # плохой запрос (не число и т. п.) — ответ, а не обрыв связи
            self.send_json({"ok": False, "error": str(e)}, 400)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as e:                           # noqa: BLE001
            self.send_json({"ok": False, "error": f"ошибка сервера: {e!r}"}, 500)

    def _get(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html", "/plan", "/plan.html"):   # основной экран (установка); подробное — его окнами
            return self.send_file(os.path.join(STATIC, "plan.html"), "text/html; charset=utf-8")
        if u.path in ("/old", "/old.html"):              # прежняя панель со всеми счётчиками — окно «⋯ ещё» установки
            return self.send_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if u.path in ("/acc", "/acc.html"):              # статические акселерометры — второй экран
            return self.send_file(os.path.join(STATIC, "acc.html"), "text/html; charset=utf-8")
        if u.path in ("/pult", "/pult.html"):            # экран 1: пульт (docs/ui_concept.md)
            return self.send_file(os.path.join(STATIC, "pult.html"), "text/html; charset=utf-8")
        if u.path in ("/watch", "/watch.html"):          # экран 2: наблюдение — статика и простые
            return self.send_file(os.path.join(STATIC, "watch.html"), "text/html; charset=utf-8")
        if u.path in ("/report", "/report.html"):        # журнал-отчёт: все шаги программ с параметрами и итогами
            return self.send_file(os.path.join(STATIC, "report.html"), "text/html; charset=utf-8")
        if u.path in ("/ui.js", "/ui.css", "/nav.js"):   # общее для экранов: помощники, параметры, вид, меню страниц
            return self.send_file(os.path.join(STATIC, u.path[1:]),
                                  "text/javascript; charset=utf-8" if u.path.endswith(".js") else "text/css; charset=utf-8")
        if u.path in ("/flow", "/flow.html"):            # живая схема: протокол по шагам, замедленно
            return self.send_file(os.path.join(STATIC, "flow.html"), "text/html; charset=utf-8")
        if u.path in ("/docs", "/docs/"):                # документация в HTML (tools/build_docs.py)
            return self.send_file(os.path.join(DOCS_HTML, "index.html"), "text/html; charset=utf-8")
        if u.path.startswith("/docs/") and u.path.endswith(".html"):
            return self.send_file(os.path.join(DOCS_HTML, os.path.basename(u.path)), "text/html; charset=utf-8")
        if u.path.startswith("/docs/img/") and u.path.endswith(".svg"):    # схемы в документации
            return self.send_file(os.path.join(DOCS_HTML, "img", os.path.basename(u.path)), "image/svg+xml")
        if u.path == "/fw/prime_lab.bin":                # образ прошивки для агентов (OTA)
            return self.send_file(os.path.join(FW_DIR, "prime_lab.ino.bin"), "application/octet-stream")
        if u.path == "/api/agent_events":                # история ошибок агента (рейтинг)
            return self.send_json({"events": ratings.history(q.get("mac", "").lower() or None, int(q.get("n", 200)))
                                   if ratings else []})
        if u.path == "/api/fw":
            return self.send_json({"fw": fw_info(), "key": bool(ota_key())})
        if u.path == "/api/state":
            return self.send_json(state())
        if u.path == "/api/static":
            return self.send_json(static_summary())
        if u.path == "/api/static/spectrum":
            if not watch:
                return self.send_json({"f": [], "axes": []})
            return self.send_json(watch.spectrum(q.get("mac", "").lower(), int(q.get("n", 6))))
        if u.path == "/api/motors":                    # моторы колёс: режимы, что шлём, что на мостах
            return self.send_json(motors_state())
        if u.path == "/api/rig":                       # установка руками: расстояние между колёсами и история изменений
            with lock:
                body = {**rig_brief(), "hist": list(rig["hist"][-50:])}
            return self.send_json(body)
        if u.path == "/api/battery":                   # аккумуляторы контуров (INA219): напряжение, ток, осталось
            return self.send_json(battery_brief())
        if u.path == "/api/coils":                     # катушки колёс: поле, кто задал, агенты катушек
            return self.send_json(coils_state())
        if u.path == "/api/field/live":                # магнитометр вживую (планшетка, окно агента)
            return self.send_json(field_live(q.get("mac", "").lower(), max(5.0, min(900.0, float(q.get("sec", 60)))),
                                             float(q["since"]) if q.get("since") else None))
        if u.path == "/api/static/live":               # живой показ статического датчика (планшетка, окно «вживую»)
            if not watch:
                return self.send_json({"ok": False, "error": WATCH_ERR})
            return self.send_json(watch.live(q.get("mac", "").lower(), max(5.0, min(900.0, float(q.get("sec", 60)))),
                                             float(q["since"]) if q.get("since") else None))
        if u.path == "/api/static/verdict":
            if not watch:
                return self.send_json({"verdict": "nodata", "sensors": {}})
            return self.send_json(watch.verdict(float(q["t0"]), float(q.get("t1") or time.time())))
        if u.path == "/api/dash":
            metrics = [m for m in q.get("metrics", "").split(",") if m]
            t0 = float(q["t0"]) if q.get("t0") else None
            t1 = float(q["t1"]) if q.get("t1") else None
            return self.send_json(dash(float(q.get("window", 300)), metrics, t0, t1))
        if u.path == "/api/runs":
            return self.send_json({"list": runlog.list(int(q.get("n", 60))) if runlog else []})
        if u.path == "/api/run":
            try:
                return self.send_json(runlog.get(int(q.get("id", "0"))))
            except ValueError as e:
                return self.send_json({"ok": False, "error": str(e)}, 404)
        if u.path == "/api/sessions":
            with sess_lock:
                lst = list(reversed(sessions))
            a = session_active["s"]
            return self.send_json({"active": session_brief(a) if a else None, "list": lst})
        if u.path == "/api/session":
            try:
                return self.send_json(session_get(q.get("id", "0")))
            except ValueError as e:
                return self.send_json({"ok": False, "error": str(e)}, 404)
        if u.path == "/api/programs":
            return self.send_json({"active": runner.brief(), "list": runner.list(), "template": PROGRAM_TEMPLATE})
        if u.path == "/api/program":
            try:
                return self.send_json(runner.get(q.get("id", "0")))
            except ValueError as e:
                return self.send_json({"ok": False, "error": str(e)}, 404)
        if u.path == "/api/series":
            return self.send_json(series_for(q.get("metric", ""), float(q.get("window", 600))))
        if u.path == "/api/control":
            with lock:                                   # собрать под замком, отправить без него
                body = json.dumps({"control": control_status(), "options": OPTIONS, "default": DEFAULT_CFG,
                                   "common": control.get("common"), "own": control.get("own") or {}},
                                  ensure_ascii=False).encode("utf-8")
            return self.send_bytes(body)
        if u.path == "/api/recent":
            with lock:
                body = json.dumps(list(recent.get(q.get("mac", "").lower(), [])), ensure_ascii=False).encode("utf-8")
            return self.send_bytes(body)
        if u.path == "/api/health":                      # живы ли задачи сервера и когда был последний пакет
            return self.send_json(health())
        if u.path == "/api/files":
            files = []
            if os.path.isdir(RAW_DIR):
                for n in sorted(os.listdir(RAW_DIR), reverse=True):
                    files.append({"name": n, "bytes": os.path.getsize(os.path.join(RAW_DIR, n))})
            return self.send_json(files)
        if u.path == "/api/sensor_noise":              # шум и точность акселерометра агента: tools/imu_noise.py → картинка и сводка
            aid = int(q.get("id", "0") or 0)
            minutes = max(1.0, min(120.0, float(q.get("min", "10") or 10)))
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "imu_noise.py"), str(aid), str(minutes)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            out = (r.stdout or "").strip()
            if r.returncode or "картинка:" not in out:
                return self.send_json({"ok": False, "error": (out or r.stderr or "ошибка анализа").strip().splitlines()[-1]})
            text, _, png = out.rpartition("картинка:")
            return self.send_json({"ok": True, "text": text.strip(), "png": "/sensor/" + os.path.basename(png.strip())})
        if u.path.startswith("/sensor/") and u.path.endswith(".png"):
            return self.send_file(os.path.join(ROOT, "data", "sensor", os.path.basename(u.path)), "image/png")
        if u.path.startswith("/data/raw/"):
            name = os.path.basename(u.path)
            return self.send_file(os.path.join(RAW_DIR, name), "application/x-ndjson; charset=utf-8")
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/ingest":                    # телефон: пакеты строками — как агенты шлют по UDP
            n, t, k, bad = int(self.headers.get("Content-Length", 0)), time.time(), 0, 0
            for line in self.rfile.read(n).decode("utf-8", "replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    handle_datagram(t, self.client_address[0], line)
                    k += 1
                except Exception as e:                 # noqa: BLE001
                    bad += 1
                    stats["err"] += 1
                    stats["err_last"] = f"{time.strftime('%H:%M:%S')} ingest {e!r}"
            return self.send_json({"ok": True, "n": k, "bad": bad, "t": round(t, 3)})
        if u.path not in ("/api/control", "/api/session/start", "/api/session/stop", "/api/compose",
                          "/api/program/start", "/api/program/stop", "/api/program/again", "/api/program/resume",
                          "/api/program/pause", "/api/program/continue",
                          "/api/static/baseline", "/api/ota",
                          "/api/usb", "/api/rest_rot", "/api/telemetry", "/api/ident", "/api/motors", "/api/coils",
                          "/api/battery", "/api/rig"):
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            if u.path == "/api/static/baseline":
                if not watch:
                    raise ValueError(WATCH_ERR)
                act = body.get("action")
                if act == "start":
                    r = watch.rec_start(body.get("macs", "all"), body.get("minutes", 5))
                    return self.send_json({"ok": True, "rec": {k: r[k] for k in ("t0", "t1", "macs")},
                                           "busy": busy_generators()})
                if act == "stop":
                    return self.send_json({"ok": True, "result": watch.rec_stop()})
                if act == "clear":
                    watch.rec_clear(body.get("macs", "all"))
                    return self.send_json({"ok": True})
                raise ValueError("action: start, stop или clear")
            if u.path == "/api/rig":                    # {gap_cm}: расстояние между колёсами, см (пусто — не задано)
                rig_set(body.get("gap_cm"))
                return self.send_json({"ok": True, **rig_brief()})
            if u.path == "/api/battery":                # ёмкость пары по умолчанию: {cap: мА·ч}
                batteries.set_cap(body.get("cap"))
                return self.send_json({"ok": True, **battery_brief()})
            if u.path == "/api/coils":                  # катушки: {wheel, state: off|+|-, level} или {off: true}
                if body.get("off"):
                    coils.all_off(body.get("src") or "вручную")
                else:
                    coils.set(str(body.get("wheel")), body.get("state", "off"), body.get("level"), src="вручную")
                return self.send_json({"ok": True, **coils_state()})
            if u.path == "/api/motors":                 # моторы: {wheel, mode, dir, pwm, rpm, flip, ramp, wd} или {stop}
                motors.set(body)
                return self.send_json({"ok": True, **motors_state()})
            if u.path == "/api/ota":
                return self.send_json({"ok": True, **ota_start(body.get("targets", "all"), bool(body.get("force")))})
            if u.path == "/api/telemetry":              # телеметрия всей установки: 0 обычная, 1 редкая, 2 молчание
                return self.send_json({"ok": True, "n": telemetry_set(int(body.get("tl", 0))), "tl": control.get("tele", 0)})
            if u.path == "/api/rest_rot":               # отдых по очереди в группах: вкл / выкл
                with lock:
                    mode = body.get("mode", rest_rot_mode())
                    if mode not in ("turns", "group"):
                        raise ValueError("mode: turns (по очереди) или group (вся группа)")
                    control["rest_rot"] = {"on": bool(body.get("on", True)), "mode": mode}
                    save_control()
                return self.send_json({"ok": True, "on": rest_rot_on(), "mode": rest_rot_mode()})
            if u.path == "/api/usb":                    # первая прошивка новых плат по USB
                return self.send_json({"ok": True, "job": usb_flash(body.get("ports") or [], self.client_address[0])})
            if u.path == "/api/ident":                  # найти плату: светодиод агента мигает secs секунд (0 — перестать)
                mac = str(body.get("mac", "")).lower()
                secs = max(0, min(120, int(body.get("secs", 20))))
                with lock:
                    a = agents.get(mac)
                    ip = a["ip"] if a else None
                if not ip:
                    raise ValueError("агент не известен или без адреса")
                send_line(f"IDENT s={secs}", ip)
                return self.send_json({"ok": True, "mac": mac, "secs": secs})
            if u.path == "/api/compose":                # составы: сохранить и/или пуск, стоп, тушение
                busy = session_active["s"] is not None or runner.brief()
                act = body.get("action", "save")
                if act in ("apply", "stop", "quench") and busy and not body.get("force"):
                    raise ValueError("идёт сессия или программа — сначала остановите её")
                if any(body.get(k) for k in ("wheels", "slot", "groups", "sub")):
                    compose_save(body)
                res = {"save": lambda: [], "apply": compose_apply, "stop": compose_stop, "quench": compose_quench}.get(act)
                if res is None:
                    raise ValueError("action: save, apply, stop или quench")
                return self.send_json({"ok": True, "applied": res(), "compose": compose_brief()})
            if u.path == "/api/session/start":
                return self.send_json({"ok": True, "id": session_start(body)})
            if u.path == "/api/program/start":
                busy = "идёт сессия — сначала остановите её" if session_active["s"] is not None else None
                return self.send_json({"ok": True, "id": runner.start(body.get("program"), busy)})
            if u.path == "/api/program/stop":            # {now} — сразу; иначе после опыта; {cancel} — снять просьбу
                return self.send_json({"ok": True, "did": runner.request_stop(bool(body.get("now")), bool(body.get("cancel")))})
            if u.path == "/api/program/resume":          # пауза: аккумуляторы заменены — дальше (или снять просьбу о паузе)
                return self.send_json({"ok": True, "did": runner.request_resume()})
            if u.path == "/api/program/pause":           # пауза по кнопке: после текущего опыта всё стоит, ждём «продолжить»
                x = runner.active
                if not x:
                    raise ValueError("программа не идёт")
                if (x.get("now") or {}).get("pause"):
                    raise ValueError("программа уже на паузе")
                runner.request_pause(str(body.get("why") or "по кнопке «пауза»")[:120])
                print(f"[prog] №{x['id']}: оператор просит паузу после опыта", flush=True)
                return self.send_json({"ok": True})
            if u.path == "/api/program/continue":        # остановленная или прерванная программа — с первого невыполненного опыта
                busy = "идёт сессия — сначала остановите её" if session_active["s"] is not None else None
                return self.send_json({"ok": True, "id": runner.continue_program(int(body.get("id") or 0), busy)})
            if u.path == "/api/program/again":           # тревога статики: {n} — повторить шаг, {auto} — повторять сам
                return self.send_json({"ok": True, **runner.request_again(body)})
            if u.path == "/api/session/stop":
                if session_active["s"] is None:
                    raise ValueError("сессия не идёт")
                session_active["stop"].set()
                return self.send_json({"ok": True})
            if (session_active["s"] is not None or runner.brief()) and not body.get("force"):
                raise ValueError("идёт сессия или программа — сначала остановите её (или force)")
            cid = apply_all(body.get("targets", "all"), body.get("cfg", {}), body.get("own"),
                            bool(body.get("reset")))
            return self.send_json({"ok": True, "cid": cid})
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"ok": False, "error": f"ошибка сервера: {e}"}, 500)


# ---------------- программа автоматического эксперимента ----------------
def _prog_targets(spec):
    now = time.time()
    with lock:
        if spec == "all":                              # все, кто на связи: отключённый агент сорвал бы каждый шаг
            return [m for m, a in agents.items() if (a["gen"] is not None or a.get("stel"))
                    and a["last"] and now - a["last"] < 10]
        return [m.lower() for m in spec if m.lower() in agents]


def _prog_confirmed(macs, cid, timeout):
    gens = [m for m in macs if not agents[m].get("stel")]
    if cid is None or not gens:
        return []
    return wait_all(gens, lambda a: (a["gen"] or {}).get("cid") == cid, timeout)


def _prog_seen(macs, t, timeout):
    """После молчания телеметрии: дождаться живого пакета от каждого генератора (их счётчики — итоги шага).
    Кто не отзывается — ему раз в 2 с повторяется настройка (повтор по входящим пакетам молчащего не будит).
    Возвращает тех, от кого пакета так и не было."""
    gens = [m for m in macs if m in agents and not agents[m].get("stel")]
    t_end, last_send = time.time() + timeout, 0.0
    while True:
        with lock:
            bad = [m for m in gens if not (agents[m]["last"] and agents[m]["last"] > t)]
            sends = ([(cfg_line(control["desired"][m]), agents[m]["ip"]) for m in bad
                      if m in control["desired"] and agents[m]["ip"]] if time.time() - last_send >= RESEND_S else [])
        if not bad or time.time() > t_end:
            return bad
        if sends:
            last_send = time.time()
            for line, ip in sends:
                send_line(line, ip)
        time.sleep(0.3)


def _prog_rest(macs):
    """Отдых между шагами: стоять и остывать (80 МГц, без светодиода). Общие настройки не запоминаются —
    иначе следующий «Пуск» считал бы на 80 МГц."""
    with lock:
        base = dict(control.get("common") or DEFAULT_CFG)
        own = {m: dict(o) for m, o in (control.get("own") or {}).items()}
    return apply_control(macs, {**base, **STANDBY_CFG, "mode": 0}, own, remember=False, label="отдых")


def _prog_done(pairs):
    """Шаг на агенте: кто из (mac, cid) ещё не сдал итоги (в tel done ≠ cid шага)."""
    with lock:
        return [m for m, cid in pairs if m in agents and (agents[m]["gen"] or {}).get("done") != cid]


def _agent_name(mac):
    """«№8» по реестру; неизвестный — хвост MAC."""
    a = agents.get(mac)
    return f"№{a['id']}" if a and a.get("id") else mac[-5:]


def _prog_temps(macs):
    with lock:
        return {m: (agents[m].get("values") or {}).get("temp") for m in macs if m in agents}


def _prog_static():
    with lock:
        return {m for m, a in agents.items() if a.get("stel")}


def _prog_baseline(minutes, stop):
    """Записать эталон статических и дождаться конца (или остановки программы)."""
    if not watch:
        raise RuntimeError(WATCH_ERR)
    watch.rec_start("all", minutes)
    while watch.rec and not stop.is_set():
        stop.wait(1)
    if watch.rec:
        watch.rec_stop()
    return watch.last_rec


runner = Runner({"apply": lambda t, c, o, f: apply_all(t, c, o, f), "confirmed": _prog_confirmed,
                 "targets": _prog_targets, "snapshot": lambda macs: snapshot_results(macs),
                 "verdict": lambda t0, t1: watch.verdict(t0, t1) if watch else None,
                 "metrics": lambda t0, t1: watch.metrics(t0, t1) if watch else {},
                 "field": field_step,
                 "coil": lambda field, src: coils.set_field(field, src),
                 "coil_off": lambda src: coils.all_off(src),
                 "motor": lambda spec, src: motors.apply_spec(spec, src),
                 "motor_off": lambda src: (motors.set({"stop": True}), print(f"[mot] {src}: стоп", flush=True)),
                 "static_macs": _prog_static, "baseline": _prog_baseline,
                 "base_cfg": lambda: dict(control.get("common") or DEFAULT_CFG),
                 "base_own": lambda: dict(control.get("own") or {}),
                 "rest": lambda macs: _prog_rest(macs), "temps": lambda macs: _prog_temps(macs),
                 "done": _prog_done,
                 "seen": _prog_seen, "rig": lambda: rig_brief(), "name": lambda mac: _agent_name(mac),
                 # опыт кончился — кто из генераторов молчал дольше этого, тот «замолчал» (обычная телеметрия раз в
                 # секунду; редкая — раз в 10 с; молчание телеметрии — проверять нечем)
                 "tel_gap": lambda: None if control.get("tele") == 2 else 25 if control.get("tele") == 1 else 6,
                 "log": lambda text: print(text, flush=True)}, PROG_DIR)


def watch_ticker():
    while True:
        try:
            watch.tick()
        except Exception as e:
            print(f"[static] {e}", flush=True)
        time.sleep(1)


def _run_agent(mac):
    """Снимок агента для журнала опытов (под замком сервера)."""
    with lock:
        a = agents.get(mac)
        if not a:
            return None
        return {"id": a["id"], "last": a["last"], "values": dict(a["values"]), "gen": a["gen"], "gstat": a["gstat"],
                "verify": dict(a["verify"]), "plog": {k: a["plog"][k] for k in ("n", "fail", "lost")},
                "run_lost": a["run_lost"], "fw": a["fw"], "quiet": quiet_now(a, time.time())}


def _run_context():
    pr = runner.active
    if pr and pr.get("now"):
        return f"программа №{pr['id']} «{pr['program']['name']}», шаг «{pr['now']['name']}»"
    s = session_active["s"]
    if s:
        return f"сессия №{s['id']} «{s['name']}»"
    return None


def run_ticker():
    while True:
        time.sleep(15)
        try:
            runlog.tick()
        except Exception as e:
            print(f"[runs] {e}", flush=True)


def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8")   # консоль Windows иначе не печатает кириллицу
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=8090)
    ap.add_argument("--udp", type=int, default=5005)
    ap.add_argument("--data", help="папка данных (по умолчанию data/)")
    ap.add_argument("--no-beacon", action="store_true", help="без маяка: настоящие агенты сервер не найдут")
    a = ap.parse_args()
    global watch, HTTP_PORT
    HTTP_PORT = a.http
    if a.data:
        set_data_dir(os.path.abspath(a.data))
        runner.dir = PROG_DIR
        os.makedirs(PROG_DIR, exist_ok=True)
        print(f"[srv] данные: {DATA}", flush=True)
    load_control()
    sessions_load()
    rig_load()
    runner.mark_interrupted()                          # «идёт» с прошлого запуска — прервана, можно продолжить
    try:                                               # документация в HTML — всегда как в markdown
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_docs
        build_docs.build(quiet=True)
    except Exception as e:
        print(f"[docs] не собрана: {e}", flush=True)
    try:                                               # платы на USB: первая прошивка новых
        import new_board
        usb["mod"], usb["job"] = new_board, new_board.Job(on_done=usb_done)
        spawn("usb", usb_ticker)
    except Exception as e:                             # noqa: BLE001
        usb["err"] = f"платы на USB не видны: {e}"
        print(f"[usb] {usb['err']}", flush=True)
    global runlog, ratings
    ratings = Ratings(DATA, log=lambda text: print(text, flush=True))
    runlog = RunLog(DATA, {"agent": _run_agent, "context": _run_context, "log": lambda text: print(text, flush=True),
                           "static": lambda t0, t1: watch.verdict(t0, t1) if watch else None})
    spawn("runs", run_ticker)
    spawn("rest", rest_ticker)
    spawn("battery", battery_ticker)
    if Watch:
        watch = Watch(DATA, put=put_locked, log=lambda text: print(text, flush=True))
        watch.known_fn = known_lines
        watch.busy_fn = busy_generators
        spawn("static", watch_ticker)
    else:
        print(f"[static] {WATCH_ERR}", flush=True)
    spawn("primes", prm_worker)
    spawn("udp", udp_receiver, a.udp)
    spawn("motors", motors.run)
    spawn("coils", coils.run)
    if os.path.exists(ADB):
        spawn("phone", phone_link)
    if a.no_beacon:
        print("[srv] без маяка", flush=True)
    else:
        spawn("beacon", beacon, a.udp)
    srv = ThreadingHTTPServer(("0.0.0.0", a.http), Handler)
    print(f"[http] установка: http://localhost:{a.http}/  документы: /docs/", flush=True)
    for ip in local_ipv4():
        print(f"[http]           http://{ip}:{a.http}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
