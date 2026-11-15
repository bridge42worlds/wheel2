"""Нащупывание (пользователь 08.11 19:25): где у статики №8 «странный спектр» или меняется форма. Колёса крутятся как
есть (ШИМ и качание не трогаем), меняется одно из трёх: режим счёта, набор источников шума, поле. Ячейки по одному
циклу качания (20 с), через одну — «дом» (опора) и проба; оценка пробы — отклонение формы и спектра от последних
домашних ячеек (|z| по СКО, эксцессу, асимметрии, 8 полосам 1–250 Гц на осях в/г1/г2, тяжести). Выделилась (|z| ≥ Z1) —
повтор дважды; повторилась тем же знаком (|z| ≥ Z2) — центр поиска переносится туда, дальше щупаются её соседи.

    python tools/probe.py [--minutes 30] [--cell 20] [--z1 4] [--z2 3]
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.request

import numpy as np

URL = "http://127.0.0.1:8090"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--minutes", type=float, default=600)
ap.add_argument("--cell", type=float, default=5.0)
ap.add_argument("--settle", type=float, default=1.5)
ap.add_argument("--z1", type=float, default=4.0)
ap.add_argument("--z2", type=float, default=3.0)
ap.add_argument("--level", type=int, default=650)
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--no-swing", action="store_true", help="выключить качание оборотов (ячейки короче цикла 20 с)")
ap.add_argument("--rot-every", type=float, default=0, help="менять вращение раз в столько минут (0 — не менять)")
ap.add_argument("--pwm", default="670,657", help="ШИМ колёс при смене вращения, ‰")
A = ap.parse_args()
rnd = random.Random(A.seed if A.seed is not None else int(time.time()))
MAC8 = "68ee8f5032f4"
BASE = {"src": 157, "bits": 64, "nb": 8, "all": 0, "td": 1000, "tmax": 73, "cpu": 240, "hb": 10, "ping": 0, "cf": 0, "det": 0,
        "ss": 0, "pl": 0, "tl": 0, "sq": 0, "rw": 4, "xf": 4, "raw": 1, "mode": 0, "dc": 1, "sm": 1, "sh": 0, "rt": 0, "tc": 1}
GROUP = {"каскад": {"mode": 1, "gv": 2, "dc": 1, "rk": 1, "pw": 20}, "эстафета": {"mode": 1, "gv": 1, "dc": 1, "rk": 1, "pw": 0},
         "одно число": {"mode": 1, "gv": 0, "dc": 0}}
GENS = ["эгоистичный", "каскад", "эстафета", "одно число", "поглощение 1→2", "поглощение 2→1", "своё рассеяние", "стоят"]
SRCS = {"все пять": 157, "регистр": 128, "ГСЧ": 1, "код": 16, "дрожание": 8, "АЦП": 4, "все кроме: регистр": 29,
        "все кроме: ГСЧ": 156, "все кроме: код": 141, "все кроме: дрожание": 149, "все кроме: АЦП": 153}
FIELDS = ["К1+ К2− постоянное", "К1− К2+ постоянное", "выключено", "переменное 1,2 Гц", "переменное 20 Гц", "только К1", "только К2"]
HOME = ("эгоистичный", "все пять", "К1+ К2− постоянное")
# вращение (пользователь 08.11: «изменишь направление — эта штука может поменяться») — блоками: колесу надо встать и разогнаться
ROTS = [("навстречу", 1, -1), ("в одну сторону", 1, 1), ("навстречу наоборот", -1, 1), ("обе назад", -1, -1)]


def set_rotation(rot):
    name, da, db = rot
    pa, pb = (int(v) for v in A.pwm.split(","))
    req("/api/motors", {"mod": None})
    req("/api/motors", {"stop": True})
    time.sleep(12)                                       # выбег
    req("/api/motors", {"pair": "free"})
    req("/api/motors", {"wheel": "a", "mode": "pwm", "dir": da, "pwm": pa})
    req("/api/motors", {"wheel": "b", "mode": "pwm", "dir": db, "pwm": pb})
    time.sleep(25)                                       # разгон
    w = req("/api/motors").get("wheels") or {}
    say(f"вращение: {name} — обод {[round((w.get(k) or {}).get('meas') or 0, 1) for k in 'ab']} об/мин; «дом» снимается заново")
BANDS = [(1, 2), (2, 4), (4, 8), (8, 16), (16, 32), (32, 64), (64, 128), (128, 250)]
AX = ("в", "г1", "г2")
NAMES = ([f"СКО {a}" for a in AX] + [f"эксцесс {a}" for a in AX] + [f"асимметрия {a}" for a in AX]
         + [f"{lo}–{hi} Гц {a}" for a in AX for lo, hi in BANDS] + ["тяжесть |g|"])
FLOOR = np.array([5.0] * 3 + [0.15] * 3 + [0.05] * 3 + [0.4] * 24 + [10.0])


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def beep(n):
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ";".join(["[console]::beep(880,300); Start-Sleep -m 200"] * n)],
                       timeout=15, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def say(s):
    print(f"{time.strftime('%H:%M:%S')} {s}", flush=True)


fleet = [b for b in req("/api/fleet").get("agents", []) if not b.get("retired")]
W = {w: [b["mac"] for b in sorted(fleet, key=lambda b: b["id"]) if b.get("kind") in ("A", "B") and b.get("slot") != "coil"
         and str(b.get("wheel")) == w] for w in ("1", "2")}
ALL = W["1"] + W["2"]
state_now = {"gen": None, "src": None, "field": None}


def apply(st):
    gen, src, field = st
    if gen != state_now["gen"]:
        own = {m: {"role": 0, "ss": 0, "src": 157, "raw": 1, "bits": 64} for m in ALL}
        cfg = {**BASE, "run": 1}
        if gen == "стоят":
            req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
        elif gen in GROUP:
            req("/api/control", {"targets": ALL, "cfg": {**cfg, **GROUP[gen]}, "own": own})
        elif gen.startswith("поглощение"):
            for m in (W["2"] if gen.endswith("1→2") else W["1"]):
                own[m] = {"role": 1, "ba": [], "src": 1, "raw": 0, "ss": 0}
            req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
        elif gen == "своё рассеяние":
            own = {m: {"role": 0, "ss": 1, "src": 157, "raw": 1, "bits": 64} for m in ALL}
            req("/api/control", {"targets": ALL, "cfg": {**cfg, "ss": 1}, "own": own})
        else:
            req("/api/control", {"targets": ALL, "cfg": cfg, "own": own})
        state_now["gen"], state_now["src"] = gen, None
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": "INFO on"})
    if src != state_now["src"]:
        for m in ALL:
            req("/api/cmd", {"mac": m, "line": f"NS {SRCS[src]}"})
        state_now["src"] = src
    if field != state_now["field"]:
        k = {"К1+ К2− постоянное": ("+", "-", None), "К1− К2+ постоянное": ("-", "+", None), "выключено": ("off", "off", None),
             "переменное 1,2 Гц": ("+", "-", {"f": 1.2, "shape": "sin"}), "переменное 20 Гц": ("+", "-", {"f": 20, "shape": "sin"}),
             "только К1": ("+", "off", None), "только К2": ("off", "-", None)}[field]
        if k[0] == "off" and k[1] == "off":
            req("/api/coils", {"off": True, "src": "нащупывание"})
        else:
            for w, s_ in (("1", k[0]), ("2", k[1])):
                if s_ == "off":
                    req("/api/coils", {"wheel": int(w), "state": "off", "src": "нащупывание"})
                else:
                    req("/api/coils", {"wheel": int(w), "state": s_, "level": A.level, "ac": k[2], "src": "нащупывание"})
        state_now["field"] = field


def static_cell(a, b, frame):
    base = os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(b)), MAC8)
    sz = os.path.getsize(base + ".idx.jsonl")
    recs = []
    with open(base + ".idx.jsonl", "rb") as f:
        f.seek(max(0, sz - 2_000_000))
        if f.tell():
            f.readline()
        for ln in f:
            try:
                p = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if a - 1 <= p["rx"] <= b + 1:
                recs.append(p)
    ts, xs = [], []
    with open(base + ".bin", "rb") as fb:
        for p in recs:
            fb.seek(p["i0"] * 6)
            buf = fb.read(p["n"] * 6)
            if len(buf) < p["n"] * 6:
                continue
            k = np.arange(p["n"])
            ts.append(p["rx"] - (p["n"] - 1 - k) / (p.get("hz") or 500.0))
            xs.append(np.frombuffer(buf, dtype="<i2").reshape(-1, 3).astype(np.float64))
    if not ts:
        return None, frame
    t, x = np.concatenate(ts), np.concatenate(xs)
    m = (t >= a) & (t < b)
    t, x = t[m], x[m]
    if len(x) < 0.6 * (b - a) * 400:
        return None, frame
    if frame is None:
        g = x.mean(axis=0)
        gu = g / np.linalg.norm(g)
        e1 = np.array([1.0, 0, 0]) - gu[0] * gu
        e1 /= np.linalg.norm(e1)
        frame = (g, np.stack([gu, e1, np.cross(gu, e1)]))
    g0, basis = frame
    d = (x - g0) @ basis.T * 62.5
    c = d - d.mean(axis=0)
    sd = c.std(axis=0)
    kurt = (c ** 4).mean(axis=0) / np.maximum(sd ** 4, 1e-12) - 3
    skew = (c ** 3).mean(axis=0) / np.maximum(sd ** 3, 1e-12)
    fs = len(x) / max(t[-1] - t[0], 1e-6)
    nper = int(2 * fs)
    acc, n = 0, 0
    win = np.hanning(nper)
    for s in range(0, len(c) - nper + 1, nper // 2):
        acc = acc + np.abs(np.fft.rfft(c[s:s + nper] * win[:, None], axis=0)) ** 2
        n += 1
    fr = np.fft.rfftfreq(nper, 1 / fs)
    psd = acc / n
    bp = np.array([[10 * np.log10(psd[(fr >= lo) & (fr < hi), ax].sum() + 1e-9) for lo, hi in BANDS] for ax in range(3)])
    gabs = (np.linalg.norm(x.mean(axis=0)) - np.linalg.norm(g0)) * 62.5
    # толчок в ячейке: короткий выброс — ячейка не в счёт (пользователя интересуют форма и спектр, а не толчки)
    mag = np.linalg.norm(c, axis=1)
    knock = float(mag.max()) > max(12 * float(np.median(mag)), 4000.0)
    vec = np.concatenate([sd, kurt, skew, bp.ravel(), [gabs]])
    return {"vec": vec, "knock": knock, "peak": float(mag.max()), "psd": psd, "fr": fr}, frame


def neighbors(center):
    gen, src, field = center
    out = [(g, src, field) for g in GENS if g != gen] + [(gen, s, field) for s in SRCS if s != src] + \
          [(gen, src, f) for f in FIELDS if f != field]
    rnd.shuffle(out)
    return out


def guard_state(gs):
    """Сторожа исчерпания (пользователь: «работай, пока что-нибудь не сядет или не откажет»): причина остановки или None."""
    bat = {x.get("label"): x.get("soc") for x in (req("/api/battery").get("list") or []) if x.get("alive")}
    low = [k for k, v in bat.items() if v is not None and v <= 10]
    online = {b["mac"] for b in req("/api/fleet").get("agents", []) if b.get("online")}
    lost = [m for m in ALL if m not in online]
    c = req("/api/coils")
    seen = [((c.get("sense") or {}).get(w) or {}).get("babs") for w in ("1", "2")]
    w = req("/api/motors").get("wheels") or {}
    rpm = [round((w.get(k) or {}).get("meas") or 0, 1) for k in "ab"]
    gs["lost"] = gs.get("lost", 0) + 1 if len(lost) > 2 else 0
    field_should = state_now["field"] not in (None, "выключено")
    gs["nofield"] = gs.get("nofield", 0) + 1 if field_should and all((v or 0) < 200 for v in seen) else 0
    say(f"сторожа: катушки {bat} %, генераторов {len(ALL) - len(lost)}/{len(ALL)}, поле {[round(v or 0) for v in seen]} мкТл, "
        f"обод {rpm} об/мин")
    if low:
        return "сели катушки: " + ", ".join(low)
    if gs["lost"] >= 2:
        return f"агенты не отвечают: нет {len(lost)} из {len(ALL)}"
    if gs["nofield"] >= 2:
        return "поле пропало — магнитометры не видят"
    if all(v < 5 for v in rpm):
        beep(2)
        say("◐ обод показывает 0 у обоих колёс — колёса стоят?")
    return None


def main():
    say(f"нащупывание до {A.minutes:g} мин или до исчерпания: ячейки {A.cell:g} с, через одну — «дом» {' · '.join(HOME)}; "
        "проба отличается от центра одним параметром; оценка — |z| формы и спектра против домашних ячеек")
    if A.no_swing:
        req("/api/motors", {"mod": None})
        say("качание оборотов выключено: обороты ровные (ячейки короче цикла качания)")
    t0m = time.time()
    frame, home_ref = None, []
    log = {"t0": time.time(), "home": HOME, "args": vars(A), "cells": [], "hits": [], "moves": []}
    path = os.path.join(ROOT, "data", "series", time.strftime("probe_%Y-%m-%d_%H%M.json"))
    center = HOME
    cand, sweep = neighbors(center), []
    t_end = time.time() + A.minutes * 60
    pending = []                                       # [(состояние, показатель, знак, [z…], "hit"|"pull")]
    best, gs, t_guard, no_static = [], {}, time.time(), 0
    why_stop = None
    try:
        turn = "home"
        rot_i, t_rot = 0, time.time()
        while time.time() < t_end:
            if A.rot_every and time.time() - t_rot > A.rot_every * 60 and not pending and turn == "home":
                rot_i = (rot_i + 1) % len(ROTS)
                set_rotation(ROTS[rot_i])
                log.setdefault("rots", []).append({"t": time.time(), "rot": ROTS[rot_i][0]})
                home_ref, cand, sweep, center, t_rot = [], neighbors(HOME), [], HOME, time.time()
            if not A.no_swing:
                now = time.time()
                wait = (A.cell - ((now - t0m) % A.cell)) % A.cell
                if wait > 0.2:
                    time.sleep(wait)
            if turn == "home":
                st = HOME
            elif pending:
                st = pending[0][0]
            else:
                if not cand:                                         # обход соседей кончился
                    if sweep:
                        zz, st2, j2, sg = max(sweep, key=lambda q: q[0])
                        say(f"обход соседей «{' · '.join(center)}» кончился; сильнее всех «{' · '.join(st2)}» "
                            f"({NAMES[j2]} |z| {zz:.1f}) — проверяю, тянет ли туда")
                        pending.append((st2, j2, sg, [zz * sg], "pull"))
                        sweep = []
                        continue
                    cand = neighbors(center)
                st = cand.pop()
            ta = time.time()
            apply(st)
            time.sleep(max(0.0, A.cell - (time.time() - ta) - 0.05))
            tb = time.time()
            cell, frame = static_cell(ta + A.settle, tb, frame)
            rec = {"t0": round(ta, 2), "t1": round(tb, 2), "state": st, "turn": turn}
            if cell is None:
                no_static += 1
                rec["skip"] = "нет отсчётов"
                log["cells"].append(rec)
                if no_static >= 6:
                    why_stop = "статика №8 молчит шесть ячеек подряд"
                    break
                continue
            no_static = 0
            rec["vec"] = np.round(cell["vec"], 3).tolist()
            if cell["knock"]:
                rec["skip"] = f"толчок {cell['peak'] / 1e6:.3f} g"
                log["cells"].append(rec)
                continue                                              # та же очередь — переиграть
            name = " · ".join(st)
            if turn == "home":
                home_ref.append(cell["vec"])
                home_ref = home_ref[-20:]
            elif len(home_ref) >= 6:
                R = np.array(home_ref)
                med = np.median(R, axis=0)
                mad = np.maximum(1.4826 * np.median(np.abs(R - med), axis=0), FLOOR)
                z = (cell["vec"] - med) / mad
                rec["z"] = np.round(z, 2).tolist()
                j = int(np.argmax(np.abs(z)))
                if pending:
                    st_p, jj, sign, zs, kind = pending[0]
                    zs.append(float(z[jj]))
                    say(f"   повтор «{name}»: {NAMES[jj]} z {z[jj]:+.1f}")
                    if len(zs) == 3:
                        pending.pop(0)
                        need = A.z2 if kind == "hit" else 2.5
                        ok = sum(1 for q in zs[1:] if q * sign > 0 and abs(q) >= need)
                        if ok == 2:
                            if kind == "hit":
                                say(f"★ ПОВТОРИЛОСЬ: «{name}» — {NAMES[jj]} z {', '.join(f'{v:+.1f}' for v in zs)} → центр сюда, щупаю вокруг")
                                beep(3 if min(abs(v) for v in zs) >= 4 else 2)
                                log["hits"].append({"state": st_p, "metric": NAMES[jj], "z": zs, "t": ta})
                                best.append((abs(float(np.mean(zs))), st_p, NAMES[jj], zs))
                            else:
                                say(f"→ тянет туда: «{name}» — {NAMES[jj]} z {', '.join(f'{v:+.1f}' for v in zs)}; центр сюда")
                            log["moves"].append({"t": ta, "from": center, "to": st_p, "kind": kind, "metric": NAMES[jj], "z": zs})
                            center, cand, sweep = st_p, neighbors(st_p), []
                        else:
                            say(f"✓ не {'повторилось' if kind == 'hit' else 'тянет'} ({ok} из 2) — дальше от «{' · '.join(center)}»")
                elif abs(z[j]) >= A.z1:
                    say(f"◐ выделилась «{name}»: {NAMES[j]} z {z[j]:+.1f} — повторяю дважды")
                    pending.append((st, j, 1 if z[j] > 0 else -1, [float(z[j])], "hit"))
                else:
                    sweep.append((abs(float(z[j])), st, j, 1 if z[j] > 0 else -1))
            log["cells"].append(rec)
            turn = "test" if turn == "home" else "home"
            if time.time() - t_guard > 300:
                t_guard = time.time()
                why_stop = guard_state(gs)
                json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
                tested = [c for c in log["cells"] if c.get("turn") == "test" and c.get("z")]
                if tested:
                    mx = max(tested, key=lambda c: max(abs(v) for v in c["z"]))
                    jm = int(np.argmax(np.abs(mx["z"])))
                    say(f"сводка: проб {len(tested)}, сильнее всех «{' · '.join(mx['state'])}» — {NAMES[jm]} z {mx['z'][jm]:+.1f}; "
                        f"центр «{' · '.join(center)}»; повторилось {len(log['hits'])}, переездов {len(log['moves'])}")
                if why_stop:
                    break
    finally:
        log["stop"] = why_stop
        json.dump(log, open(path, "w", encoding="utf-8"), ensure_ascii=False, default=float)
        if why_stop:
            say(f"✗ ОСТАНОВКА: {why_stop} — выключаю счёт, поле и колёса")
            for m in ALL:
                req("/api/cmd", {"mac": m, "line": "NS off"})
            req("/api/control", {"targets": ALL, "cfg": {**BASE, "dc": 0, "run": 0}, "own": {m: {"role": 0, "ss": 0} for m in ALL}})
            req("/api/coils", {"off": True, "src": "нащупывание: конец"})
            req("/api/motors", {"mod": None})
            req("/api/motors", {"stop": True})
            beep(2)
        else:
            apply(HOME)
        say(f"нащупывание закончено; журнал {path}")
        if best:
            for zz, st, nm, zs in sorted(best, key=lambda q: -q[0])[:5]:
                say(f"  повторилось: «{' · '.join(st)}» — {nm} z {', '.join(f'{v:+.1f}' for v in zs)}")
        else:
            say("  ни одна проба не повторилась")
        for mv in log["moves"]:
            say(f"  переезд ({mv['kind']}): «{' · '.join(mv['from'])}» → «{' · '.join(mv['to'])}» по {mv['metric']}")


if __name__ == "__main__":
    main()
