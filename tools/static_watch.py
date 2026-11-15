"""Сторож статики в реальном времени (08.11.2026). Главный прибор опытов — статический акселерометр №8: ищем, не
появилось ли в нём что-то при смене режима (поле, источник шума, молчание плат, вращение) — сильное отклонение,
спектральное, сдвиг. Сторож берёт каждое окно сырых отсчётов (10 с) и сравнивает с последними минутами того же
положения датчика: толчки, сдвиг положения, разброс, форму (эксцесс), мощность по полосам частот на осях в/г1/г2.
Смены режима — из журнала сервера (data/monitor_console.log): событие вскоре после смены помечается ею.

    python tools/static_watch.py                      вживую (одна строка на событие — для Monitor)
    python tools/static_watch.py --from T0 --until T1 проверка на записи (время — секунды эпохи)

Все окна пишутся в data/static_watch/<день>.jsonl (метрики, z, режим) — для разбора потом."""
import argparse
import glob
import json
import math
import os
import sys
import time
from collections import deque

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawidx  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--mac", default="68ee8f5032f4", help="датчик (имя файла сырья без двоеточий); №8 по умолчанию")
ap.add_argument("--win", type=float, default=10.0, help="окно, с")
ap.add_argument("--ref", type=int, default=30, help="окон в опоре (30 × 10 с = 5 мин)")
ap.add_argument("--z", type=float, default=6.0, help="разовое отклонение: |z| от опоры")
ap.add_argument("--z3", type=float, default=4.0, help="держится: |z| три окна подряд")
ap.add_argument("--shift", type=float, default=2000.0, help="сдвиг положения: среднее по оси, мкg")
ap.add_argument("--knock", type=float, default=8.0, help="толчок: пик больше стольких СКО опоры (и не меньше 3000 мкg)")
ap.add_argument("--from", dest="t_from", type=float, default=None)
ap.add_argument("--until", type=float, default=None)
ap.add_argument("--quiet-start", action="store_true", help="без строки о запуске")
ap.add_argument("--print-knocks", action="store_true", help="сообщать и толчки (по умолчанию только в журнал: пользователя интересуют форма и спектр, 08.11)")
A = ap.parse_args()

BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
NAMES = ([f"СКО {a}" for a in AX] + [f"эксцесс {a}" for a in AX] + [f"{lo}–{hi} Гц {a}" for a in AX for lo, hi in BANDS]
         + ["тяжесть |g|", "среднее в", "среднее г1", "среднее г2"])
FLOOR = [5.0] * 3 + [0.15] * 3 + [0.4] * (3 * len(BANDS)) + [10.0] * 4   # наименьший разброс опоры: мкg, эксцесс, дБ, мкg
UNIT = ["мкg"] * 3 + [""] * 3 + ["дБ"] * (3 * len(BANDS)) + ["мкg"] * 4
LOG_DIR = os.path.join(ROOT, "data", "static_watch")
os.makedirs(LOG_DIR, exist_ok=True)


def say(text):
    print(text, flush=True)


def fmt_t(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def ru(x, nd=1):
    return f"{x:.{nd}f}".replace(".", ",")


# ---------- сырьё: файл дня и индекс пакетов ----------
def day_files():
    dirs = sorted(glob.glob(os.path.join(ROOT, "data", "accel", "20??-??-??")))
    for d in reversed(dirs):
        b = os.path.join(d, A.mac)
        if os.path.exists(b + ".bin") and os.path.exists(b + ".idx.jsonl"):
            return b
    return None


class Raw:
    """Новые пакеты индекса и их отсчёты. 09.11: место в .bin — сумма n прежних строк индекса (tools/rawidx.py);
    поле i0 — номер отсчёта у датчика (сбрасывается при перезапуске, перескакивает при потерях) — по нему не читать."""

    def __init__(self, base, t_from=None):
        self.base = base
        self.rt = rawidx.RawTail(os.path.basename(base), base=base)
        self.t_from = time.time() - 360 if t_from is None else t_from      # вживую — последние 6 мин на опору
        self.i = next((k for k, v in enumerate(self.rt.rx) if v >= self.t_from), len(self.rt.rx))
        self.lsb = self.rt.lsb

    def read(self):
        self.rt.update()
        if self.i >= len(self.rt.rx):
            return None, None
        out_t, out_x = [], []
        with open(self.base + ".bin", "rb") as fb:
            for k in range(self.i, len(self.rt.rx)):
                o, n, hz, rx = self.rt.o[k], self.rt.n[k], self.rt.hz[k], self.rt.rx[k]
                fb.seek(o * 6)
                buf = fb.read(n * 6)
                if len(buf) < n * 6:
                    break                                   # отсчёты ещё не на диске — дочитаем в следующий раз
                kk = np.arange(n)
                out_t.append(rx - (n - 1 - kk) / hz)
                out_x.append(np.frombuffer(buf, dtype="<i2").reshape(-1, 3).astype(np.float64))
                self.i = k + 1
        self.lsb = self.rt.lsb
        if not out_t:
            return None, None
        return np.concatenate(out_t), np.concatenate(out_x)


# ---------- режим: журнал сервера ----------
class Modes:
    """Смены режима из data/monitor_console.log: строки [prog] (отрезки, источник, молчание, статика) и [coil], [mot].
    Время строки — когда сторож её увидел (вживую с точностью до опроса); на записи — режим не известен."""

    KEYS = ("[prog]", "[coil]", "[mot] качание", "[mot] колесо")

    def __init__(self, live):
        self.live, self.path = live, os.path.join(ROOT, "data", "monitor_console.log")
        self.pos = os.path.getsize(self.path) if live and os.path.exists(self.path) else 0
        self.last = None                               # (время, текст)

    def poll(self):
        if not self.live or not os.path.exists(self.path):
            return
        sz = os.path.getsize(self.path)
        if sz < self.pos:                              # сервер перезапущен — журнал начат заново
            self.pos = 0
        if sz == self.pos:
            return
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = sz
        for ln in chunk.decode("utf-8", "replace").splitlines():
            if any(k in ln for k in self.KEYS) and ("отрезок" in ln or "[coil]" in ln or "[mot]" in ln or "№" in ln):
                txt = ln.replace("[prog] ", "").replace("шаг «этапы 0 · A · B: эталон, набор, тишина» · ", "") \
                        .replace("шаг «этапы C · D · E: без статики, возврат, эталон» · ", "")
                self.last = (time.time(), txt.strip()[:120])

    def note(self, t):
        if self.last and 0 <= t - self.last[0] <= 180:
            return f" · через {t - self.last[0]:.0f} с после «{self.last[1]}»"
        return ""


# ---------- окно ----------
def metrics(t, x, frame, lsb):
    g0, basis = frame
    d = (x - g0) @ basis.T / lsb * 1e6                  # мкg по осям в, г1, г2
    mean = d.mean(axis=0)
    c = d - mean
    sd = c.std(axis=0)
    kurt = (c ** 4).mean(axis=0) / np.maximum(sd ** 4, 1e-12) - 3.0
    peak = np.abs(c).max(axis=0)
    # спектр: Уэлч, куски по 2 с с перекрытием 50 %
    fs = len(t) / max(t[-1] - t[0], 1e-6)
    nper = int(2 * fs)
    bp = np.zeros((3, len(BANDS)))
    if len(c) >= nper:
        hop = nper // 2
        win = np.hanning(nper)
        acc, m = 0, 0
        for s in range(0, len(c) - nper + 1, hop):
            seg = c[s:s + nper] * win[:, None]
            acc = acc + np.abs(np.fft.rfft(seg, axis=0)) ** 2
            m += 1
        psd = acc / m
        fr = np.fft.rfftfreq(nper, 1 / fs)
        for j, (lo, hi) in enumerate(BANDS):
            sel = (fr >= lo) & (fr < hi)
            bp[:, j] = 10 * np.log10(psd[sel].sum(axis=0) + 1e-9)
    gabs = (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) / lsb * 1e6   # тяжесть: модуль среднего против опоры кадра
    vec = np.concatenate([sd, kurt, bp.ravel(), [gabs], mean])
    return {"mean": mean, "sd": sd, "peak": peak, "vec": vec, "n": len(t), "fs": fs}


def make_frame(x):
    g = x.mean(axis=0)
    gu = g / np.linalg.norm(g)
    e1 = np.array([1.0, 0, 0]) - gu[0] * gu
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(gu, e1)
    return g, np.stack([gu, e1, e2])


def tilt_deg(g):
    return math.degrees(math.acos(min(1.0, abs(g[2]) / np.linalg.norm(g))))


# ---------- основной цикл ----------
def main():
    base = day_files()
    if not base:
        sys.exit("нет сырья статики")
    live = A.t_from is None
    raw = Raw(base, A.t_from)
    modes = Modes(live)
    buf_t, buf_x = np.empty(0), np.empty((0, 3))
    frame, ref = None, deque(maxlen=A.ref)
    streak = np.zeros(len(NAMES), dtype=int)
    shift_n, last_data, gap_said = 0, time.time(), False
    w_end = None
    last_knock, t_started = -1e9, time.time()
    recent, dev_run = deque(maxlen=12), 0
    logf = open(os.path.join(LOG_DIR, time.strftime("%Y-%m-%d") + ".jsonl"), "a", encoding="utf-8")
    if not A.quiet_start:
        say(f"{fmt_t(time.time())} сторож статики: {os.path.basename(base)} · окно {ru(A.win, 0)} с · опора {A.ref} окон · "
            f"толчок > {ru(A.knock, 0)} СКО · сдвиг > {ru(A.shift, 0)} мкg · отклонение |z| ≥ {ru(A.z, 0)} (или ≥ {ru(A.z3, 0)} трижды)")
    while True:
        modes.poll()
        nb = day_files()
        if nb and nb != base:                          # новый день — новый файл
            base, raw = nb, Raw(nb, None)
        t, x = raw.read()
        if t is not None:
            buf_t, buf_x = np.concatenate([buf_t, t]), np.concatenate([buf_x, x])
            last_data, gap_said = time.time(), False
        elif live and time.time() - last_data > 60 and not gap_said:
            say(f"{fmt_t(time.time())} ◐ №8: нет отсчётов {time.time() - last_data:.0f} с (статика не мерит по плану? датчик молчит?)"
                + modes.note(time.time()))
            gap_said = True
        if not len(buf_t):
            if A.until and not live:
                break
            time.sleep(1)
            continue
        if w_end is None:
            w_end = math.floor(buf_t[0] / A.win) * A.win + A.win
        # обработать все полные окна
        while len(buf_t) and buf_t[-1] >= w_end:
            sel = buf_t < w_end
            wt, wx = buf_t[sel], buf_x[sel]
            buf_t, buf_x = buf_t[~sel], buf_x[~sel]
            t0 = w_end - A.win
            w_end += A.win
            if A.until and t0 > A.until:
                return
            if len(wt) < 0.5 * A.win * 400:
                continue
            if frame is None:
                frame = make_frame(wx)
            m = metrics(wt, wx, frame, raw.lsb)
            rec = {"t0": round(t0, 2), "n": m["n"], "mean": np.round(m["mean"], 1).tolist(), "sd": np.round(m["sd"], 1).tolist(),
                   "peak": np.round(m["peak"], 0).tolist(), "vec": np.round(m["vec"], 3).tolist()}
            ev = []
            if len(ref) >= 12:
                R = np.array([r["vec"] for r in ref])
                med = np.median(R, axis=0)
                mad = np.maximum(1.4826 * np.median(np.abs(R - med), axis=0), FLOOR)
                z = (m["vec"] - med) / mad
                sd_ref = np.median(np.array([r["sd"] for r in ref]), axis=0)
                mean_ref = np.median(np.array([r["mean"] for r in ref]), axis=0)
                rec["z"] = np.round(z, 2).tolist()
                knock = [(m["peak"][i], i) for i in range(3) if m["peak"][i] > max(A.knock * sd_ref[i], 3000.0)]
                dmean = m["mean"] - mean_ref
                if knock:
                    pk, i = max(knock)
                    ev.append(f"толчок: пик {ru(pk / 1e6, 3)} g по {AX[i]} ({ru(pk / sd_ref[i], 0)} СКО)")
                if np.abs(dmean).max() > A.shift:
                    shift_n += 1
                    if shift_n == 2:                   # держится два окна — новое положение датчика
                        ang = math.degrees(math.atan2(math.hypot(dmean[1], dmean[2]), 1e6))
                        ev.append("сдвиг положения: " + ", ".join(f"{AX[i]} {dmean[i]:+.0f} мкg" for i in range(3))
                                  + f" (≈ {ru(ang, 2)}°)")
                        frame, ref, shift_n = make_frame(wx), deque(maxlen=A.ref), 0
                        streak[:] = 0
                else:
                    shift_n = 0
                if not knock and shift_n == 0 and len(ref) >= 12:
                    hit = np.abs(z) >= A.z
                    streak = np.where(np.abs(z) >= A.z3, streak + 1, 0)
                    held = streak == 3
                    js = sorted(set(np.where(hit)[0]) | set(np.where(held)[0]), key=lambda j: -abs(z[j]))
                    if js:
                        parts = []
                        for j in js[:4]:
                            unit = UNIT[j]
                            dv = m["vec"][j] - med[j]
                            parts.append(f"{NAMES[j]} {dv:+.1f}{(' ' + unit) if unit else ''} (z {z[j]:+.1f}"
                                         + (", держится 30 с" if held[j] and not hit[j] else "") + ")")
                        ev.append("отклонение: " + "; ".join(parts) + (f" · и ещё {len(js) - 4}" if len(js) > 4 else ""))
            if not ev and shift_n == 0:
                ref.append({"vec": m["vec"], "sd": m["sd"], "mean": m["mean"]})
            # новый уровень: отклонение (без толчка) держится 6 окон подряд — сообщить один раз и взять его опорой
            # (08.11: полоса 64–128 Гц г1 −6 дБ держалась, сторож повторял её каждые 10 с)
            dev_now = bool(ev) and not any(e.startswith("толчок") or e.startswith("сдвиг") for e in ev)
            recent.append({"vec": m["vec"], "sd": m["sd"], "mean": m["mean"]})
            dev_run = dev_run + 1 if dev_now else 0
            if dev_run >= 6 and dev_run % 6 == 0:          # держится — напомнить раз в минуту; опору НЕ менять
                ev = [f"держится {int(dev_run * A.win)} с: " + "; ".join(e.replace("отклонение: ", "") for e in ev)]
            elif dev_now and dev_run > 1:
                rec["ev_hidden"] = ev                        # то же отклонение подряд — в журнал, не в сообщение
                ev = []
            rec["ev"] = ev
            rec["mode"] = modes.last[1] if modes.last else None
            logf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logf.flush()
            knocked = any(e.startswith("толчок") for e in ev)
            if knocked:
                last_knock = t0
            shown = [e for e in ev if A.print_knocks or not e.startswith("толчок")]
            if shown and not knocked and t0 - last_knock <= 15 and all(e.startswith("отклонение") for e in shown)                     and "держится" not in " ".join(shown):
                shown = []                                   # хвост толчка — в журнал, не в сообщение
            if shown and (not live or t0 >= t_started):
                say(f"{fmt_t(t0)} ⚠ №8 " + " · ".join(shown) + modes.note(t0 + A.win))
        if not live and A.until and (not len(buf_t) or buf_t[0] > A.until):
            t2, _ = raw.read()
            if t2 is None:
                break
        if live:
            time.sleep(1)


if __name__ == "__main__":
    main()
