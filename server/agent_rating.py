"""Рейтинг агентов: история ошибок и надёжность (docs/thermal.md, § 5).

Сервер отдаёт сюда телеметрию (tel), сводку группы (grp) и итоги проверки простых.
Модуль замечает события — перезапуск и его причину, сторожей радио и Wi-Fi, простые,
не прошедшие проверку, сверку соседа, «молчание» в группе, потери кадров, тепловую
аварию, — пишет их в data/agents/events.jsonl и считает рейтинг: 100 минус веса
событий за сутки, вес тает вдвое за 6 ч. Одно и то же три раза за сутки — «систематически».

При перечитывании сырья на старте сервера (live=False) события не пишутся — они уже в
файле, — но счётчики запоминаются, чтобы первый живой пакет сравнивался с ними.
"""
import json, os, threading, time
from collections import deque

RST_NAMES = {1: "включение питания", 2: "кнопка RESET", 3: "программно", 4: "сбой прошивки", 5: "сторож прерываний",
             6: "сторож задач", 7: "сторож", 9: "просадка питания", 11: "по USB", 14: "сбой питания", 15: "зависание ядра"}
RST_BAD = {4, 5, 6, 7, 9, 14, 15}

# тип: (вес, текст, короткое имя для «систематически»)
EV = {
    "prime_fail":    (50, "простое не прошло проверку сервера", "ошибка счёта"),
    "split":         (30, "сосед нашёл свидетеля: его «простое» составное", "ошибка счёта"),
    "selftest":      (40, "самопроверка прошивки не прошла", "самопроверка"),
    "crash":         (25, "перезапуск: {why}", "сбои прошивки"),
    "radio_wd":      (20, "сторож перезапустил: радио не передавало", "радио"),
    "wifi_wd":       (15, "сторож перезапустил: не было Wi-Fi 90 с", "Wi-Fi"),
    "zombie_wd":     (15, "сторож перезапустил: Wi-Fi «есть», а пакеты не уходили и после трёх переподключений", "Wi-Fi"),
    "heap_wd":       (5, "самоочистка: мало свободной памяти — перезапуск между шагами", "память"),
    "wifi_zombie":   (8, "Wi-Fi «есть», а пакеты не уходили — сторож переподключал", "Wi-Fi"),
    "muted":         (3, "группа его не слышала (заметил №{by})", "связь в группе"),
    "tx_loss":       (5, "кадры от него теряются: {pct} % за минуту", "передатчик"),
    "ota_fail":      (10, "прошивка по Wi-Fi не загрузилась", "прошивка по Wi-Fi"),
    "thermal_pause": (2, "тепловая авария: счёт стоял", "перегрев"),
    "reboot":        (0, "перезапуск: {why}", None),          # штатный: питание, USB, прошивка по Wi-Fi
    "rest":          (0, "отдых по очереди: {t0} → {t1} °C за {s} с", None),
}
HALF_LIFE_S = 6 * 3600
WINDOW_S = 24 * 3600
SYSTEMATIC = 3
MUTE_EVERY_S = 300            # «молчал» — не чаще раза в 5 мин на пару (кто заметил, кого)
TXLOSS_EVERY_S = 600


class Ratings:
    def __init__(self, data_dir, log=None):
        self.path = os.path.join(data_dir, "agents", "events.jsonl")
        self.log = log or (lambda text: None)
        self.lock = threading.Lock()
        self.events = deque(maxlen=50000)
        self.prev = {}            # mac -> последние счётчики
        self.last_ev = {}         # (тип, mac, ключ) -> время последнего такого события
        now = time.time()
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        e = json.loads(ln)
                    except ValueError:
                        continue
                    if now - e.get("t", 0) <= 7 * 86400:
                        self.events.append(e)
        except OSError:
            pass

    # ---------- события ----------
    def add(self, t, mac, aid, typ, live, key=None, every=0, **data):
        if not live:
            return
        k = (typ, mac, key)
        if every and t - self.last_ev.get(k, 0) < every:
            return
        self.last_ev[k] = t
        w, text, _ = EV[typ]
        e = {"t": round(t, 1), "mac": mac, "id": aid, "type": typ, "w": w, "text": text.format(**data)}
        if data:
            e["data"] = data
        with self.lock:
            self.events.append(e)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if w:
            self.log(f"[rating] №{aid}: {e['text']}")

    def on_tel(self, t, mac, aid, p, live, rebooted):
        """Пакет tel генератора или статического; rebooted — сервер заметил новый запуск."""
        st = self.prev.setdefault(mac, {})
        rbt = p.get("rbt") if isinstance(p.get("rbt"), list) and len(p["rbt"]) >= 3 else None
        if rebooted:
            if rbt and rbt[1] == 1:
                self.add(t, mac, aid, "radio_wd", live)
            elif rbt and rbt[1] == 2:
                self.add(t, mac, aid, "wifi_wd", live)
            elif rbt and rbt[1] == 3:
                self.add(t, mac, aid, "zombie_wd", live)
            elif rbt and rbt[1] == 4:
                self.add(t, mac, aid, "heap_wd", live)
            elif rbt and rbt[0] in RST_BAD:
                self.add(t, mac, aid, "crash", live, why=RST_NAMES.get(rbt[0], f"код {rbt[0]}"))
            else:
                why = RST_NAMES.get(rbt[0], f"код {rbt[0]}") if rbt else "причина неизвестна (старая прошивка)"
                if rbt and rbt[0] == 3 and p.get("fw") != st.get("fw"):
                    why = f"прошивка по Wi-Fi → {p.get('fw')}"
                self.add(t, mac, aid, "reboot", live, why=why)
            st.pop("wrst", None)
            st.pop("st_bad", None)
        w = p.get("wrst")
        if isinstance(w, int):
            if st.get("wrst") is not None and w > st["wrst"]:
                self.add(t, mac, aid, "wifi_zombie", live, n=w - st["wrst"])
            st["wrst"] = w
        if p.get("st") == 0 and not st.get("st_bad"):
            st["st_bad"] = True
            self.add(t, mac, aid, "selftest", live)
        ts = p.get("ts")
        if ts == 2 and st.get("ts") != 2:
            self.add(t, mac, aid, "thermal_pause", live, temp=p.get("temp"))
        st["ts"] = ts
        ota = p.get("ota")
        if isinstance(ota, list) and ota and ota[0] == -1 and st.get("ota") != -1:
            self.add(t, mac, aid, "ota_fail", live)
        if isinstance(ota, list) and ota:
            st["ota"] = ota[0]
        st["fw"] = p.get("fw")

    def on_grp(self, t, mac, aid, gs, grp, id2mac, live, resting=lambda m: False):
        """Сводка группы от агента aid: кого он сейчас не слышит, сверка соседа, потери его кадров.
        resting(mac) — отдыхает по команде диспетчера: его молчание — не ошибка."""
        st = self.prev.setdefault(mac, {})
        grp = list(grp or [])
        mu = gs.get("mute") or [0, 0]
        if not isinstance(mu, list) or len(mu) < 3:   # старая прошивка: вторым шло «сколько», а не маска
            mu = [0, 0, 0]
        for i, mid in enumerate(grp):
            if (mu[1] >> i) & 1 and mid != aid:
                m2 = id2mac(mid)
                if m2 and not resting(m2) and not resting(mac):
                    self.add(t, m2, mid, "muted", live, key=aid, every=MUTE_EVERY_S, by=aid)
        # сверка: эстафета и каскад — простое приходит от предыдущего по кругу (кроме молчащих)
        sp = gs.get("split") or 0
        if gs.get("gv") and st.get("split") is not None and sp > st["split"] and aid in grp and len(grp) > 1:
            k = grp.index(aid)
            for d in range(1, len(grp)):
                j = (k - d) % len(grp)
                if not (mu[1] >> j) & 1:
                    m2 = id2mac(grp[j])
                    if m2:
                        self.add(t, m2, grp[j], "split", live, by=aid, n=sp - st["split"])
                    break
        st["split"] = sp
        # свои кадры адресно: доля неподтверждённых за минуту и дольше
        ok, fail = gs.get("ok_to"), gs.get("fail_to")
        if isinstance(ok, list) and isinstance(fail, list):
            so, sf = sum(ok), sum(fail)
            w0 = st.get("txw")
            if w0 is None or so < w0[1] or sf < w0[2]:
                st["txw"] = (t, so, sf)
            elif t - w0[0] >= 60:
                do, df = so - w0[1], sf - w0[2]
                if do + df >= 300 and df / (do + df) > 0.05:
                    self.add(t, mac, aid, "tx_loss", live, every=TXLOSS_EVERY_S, pct=round(100 * df / (do + df), 1))
                st["txw"] = (t, so, sf)

    def on_prime_fail(self, t, mac, aid, hexstr, live):
        self.add(t, mac, aid, "prime_fail", live, key=hexstr[-16:], hex=hexstr[:32])

    # ---------- рейтинг ----------
    def brief(self, now=None):
        """mac -> {score, cat, sys: [что систематически], recent: [последние события]}."""
        now = now or time.time()
        out = {}
        with self.lock:
            evs = [e for e in self.events if now - e["t"] <= WINDOW_S]
        by = {}
        for e in evs:
            by.setdefault(e["mac"], []).append(e)
        for mac, lst in by.items():
            pen = sum(e["w"] * 0.5 ** ((now - e["t"]) / HALF_LIFE_S) for e in lst)
            score = max(0, round(100 - pen))
            kinds = {}
            for e in lst:
                name = EV.get(e["type"], (0, "", None))[2]
                if name and e["w"]:
                    kinds[name] = kinds.get(name, 0) + 1
            sys_ = [k for k, n in kinds.items() if n >= SYSTEMATIC]
            out[mac] = {"score": score, "cat": "ok" if score >= 90 and not sys_ else "warn" if score >= 70 else "bad",
                        "sys": sys_, "n": sum(1 for e in lst if e["w"]),
                        "recent": [{"t": e["t"], "text": e["text"], "w": e["w"]} for e in lst[-6:]][::-1]}
        return out

    def history(self, mac=None, n=200):
        with self.lock:
            lst = [e for e in self.events if mac is None or e["mac"] == mac]
        return lst[-n:][::-1]
