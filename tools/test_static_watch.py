"""Проверка слежения за статическими акселерометрами (server/static_watch.py) на синтетике.

    python tools/test_static_watch.py

Датчик: 500 Гц (1000 Гц, среднее по 2), ±2 g, тяжесть по z, белый шум 70 мкg/√Гц.
Пакеты — как у прошивки ("acc": номер первого отсчёта, base64 int16 x y z).

1. Эталон 3 минуты — должен быть годен: шум гауссов, фон ровный, квантование мелкое.
2. Тот же шум ещё 5 минут — ни одной тревоги, вывод «продолжать».
3. Линия 37 Гц 400 мкg по z — «линия», вывод «проверить».
   Слабая линия 23 Гц 120 мкg: в одном окне не видна, за 3 минуты — видна.
4. Линия 50 Гц — известный источник (сеть): помечена, вывод «продолжать».
5. Толчок (затухающие 15 Гц, 20 мg) — «толчок», вывод «повторить».
6. Значения кратны 256 (грубый режим, как у LIS3DH) — «квантование», «повторить».
7. Шум с выбросами (эксцесс) — «форма шума», «проверить».
8. Пропуск отсчётов — «пропуск», «повторить».
9. Без эталона — вывод «нет эталона», толчки всё равно видны.
"""
import base64, math, os, re, shutil, sys, tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import static_watch as sw   # noqa: E402

rng = np.random.default_rng(11)
HZ, LSB = 500.0, 16384.0
SIG = 70e-6 * math.sqrt(HZ / 2)          # 70 мкg/√Гц в полосе до 250 Гц — около 1,1 мg
ok_all = True


def check(name, cond, detail=""):
    global ok_all
    ok_all &= bool(cond)
    print(f"   {'✓' if cond else '✗'} {name}{(' — ' + detail) if detail else ''}")


class Agent:
    """Шлёт пакеты как прошивка: по 140 отсчётов, время — по часам «сервера»."""

    def __init__(self, watch, mac):
        self.w, self.mac, self.i, self.t = watch, mac, 0, 1_000_000.0

    def send(self, seconds, line=None, knock_at=None, quant=None, spikes=0.0, drop=None):
        n = int(seconds * HZ)
        tt = (self.i + np.arange(n)) / HZ
        g = rng.normal(0, SIG, (n, 3))
        g[:, 2] += 1.0
        if line:
            f, a = line
            g[:, 2] += a * np.sin(2 * np.pi * f * tt)
        if knock_at is not None:
            k = int(knock_at * HZ)
            m = np.arange(n - k)
            g[k:, 0] += 0.02 * np.exp(-m / (0.15 * HZ)) * np.sin(2 * np.pi * 15 * m / HZ)
        if spikes:
            hit = rng.random((n, 3)) < spikes
            g += hit * rng.normal(0, 8 * SIG, (n, 3))
        raw = np.clip(np.round(g * LSB), -32767, 32767).astype(np.int64)
        if quant:
            raw = (raw // quant) * quant
        raw = raw.astype("<i2")
        for s in range(0, n, 140):
            part = raw[s:s + 140]
            i0 = self.i + s
            if drop and drop[0] <= s < drop[1]:
                continue
            self.t += len(part) / HZ
            p = {"i0": i0, "n": len(part), "hz": HZ, "avg": 2, "afs": 2, "lsb": LSB, "mi": i0 + len(part),
                 "mms": 1000, "ms": 1000, "t": 30.0, "b64": base64.b64encode(part.tobytes()).decode()}
            self.w.add(self.mac, p, self.t)
        self.i += n
        return self.t


tmp = tempfile.mkdtemp(prefix="sw_test_")
try:
    W = sw.Watch(tmp, log=lambda *a: None)
    W.known_fn = lambda: [{"f": 50.0, "name": "сеть 50 Гц", "harm": 5}]
    A = Agent(W, "68:ee:8f:00:00:01")
    A.send(30)                                            # разогрев: обычный разброс для толчков

    print("1. Эталон 3 минуты")
    W.rec = {"t0": A.t, "t1": A.t + 180, "minutes": 3, "macs": [A.mac], "busy": []}
    A.send(185)
    W.tick(A.t + 5)
    s = W.sensors[A.mac]
    b = s.base
    check("эталон записан", b is not None, f"окон {b['n'] if b else 0}")
    if b:
        for q in b["quality"]:
            print(f"      {'✓' if q['ok'] else '◐'} {q['text']}")
        check("шум гауссов, фон ровный, квантование мелкое", all(q["ok"] for q in b["quality"]))

    def verdict_of(dur, **kw):
        t0 = A.t
        A.send(dur, **kw)
        v = W.verdict(t0, A.t)
        sv = v["sensors"].get(A.mac, {})
        return v["verdict"], sv.get("reasons", []), sv

    print("2. Тот же шум 5 минут")
    v, rs, _ = verdict_of(300)
    finds = [f for w in list(s.windows)[-30:] for f in w["finds"]]
    check("ни одной тревоги", not finds, f"нашлось {len(finds)}: {finds[:2]}")
    check("вывод «продолжать»", v == "ok", v)

    print("3. Линия 37 Гц, 400 мкg по z")
    v, rs, _ = verdict_of(60, line=(37.0, 400e-6))
    check("вывод «проверить»", v == "check", v)
    check("найдена линия около 37 Гц", any(re.search(r"линия 3[67],\d+ Гц", r) for r in rs), "; ".join(rs)[:200])
    check("полоса отдельно не тревожит — линия из полос исключена", not any("полоса" in r for r in rs))

    print("   слабая линия 23 Гц, 120 мкg, 3 минуты")
    v, rs, _ = verdict_of(180, line=(23.0, 120e-6))
    one = [f for w in list(s.windows)[-18:] for f in w["finds"] if f["kind"] == "line"]
    check("в отдельных окнах почти не видна", len(one) <= 3, f"окон с линией {len(one)} из 18")
    near23 = any(re.search(r"линия (22,[5-9]|23,[0-4])\d* Гц", r) for r in rs)   # шаг частоты 0,49 Гц
    check("за отрезок — «проверить», линия около 23 Гц", v == "check" and near23, "; ".join(rs)[:200])

    print("4. Линия 50 Гц — известный источник")
    v, rs, sv = verdict_of(60, line=(50.0, 400e-6))
    check("помечена «сеть 50 Гц»", "сеть 50 Гц" in sv.get("known", []), str(sv.get("known")))
    check("вывод «продолжать»", v == "ok", v + " " + "; ".join(rs)[:150])

    print("5. Толчок")
    v, rs, _ = verdict_of(30, knock_at=12)
    check("вывод «повторить»", v == "repeat", v)
    check("причина — толчок", any("толчок" in r for r in rs), "; ".join(rs)[:200])

    print("6. Значения кратны 256")
    v, rs, _ = verdict_of(30, quant=256)
    check("вывод «повторить»", v == "repeat", v)
    check("причина — квантование, шаг 256", any("256" in r for r in rs), "; ".join(rs)[:200])

    print("7. Шум с выбросами")
    A.send(30)                                            # вернуться к обычному
    v, rs, _ = verdict_of(60, spikes=0.002)
    check("вывод «проверить» или «повторить»", v in ("check", "repeat"), v)
    check("причины — выбросы (помеха) и форма шума", any("выбросы" in r for r in rs) and any("форма шума" in r for r in rs),
          "; ".join(rs)[:200])

    print("8. Пропуск отсчётов")
    A.send(30)
    v, rs, _ = verdict_of(30, drop=(5000, 6000))
    check("вывод «повторить»", v == "repeat", v)
    check("причина — пропуск", any("пропуск" in r for r in rs), "; ".join(rs)[:200])

    print("9. Без эталона")
    B = Agent(W, "68:ee:8f:00:00:02")
    B.send(60)
    t0 = B.t
    B.send(30)
    v = W.verdict(t0, B.t, [B.mac])
    check("вывод «нет эталона»", v["verdict"] == "nobase", v["verdict"])
    t0 = B.t
    B.send(30, knock_at=10)
    v = W.verdict(t0, B.t, [B.mac])
    check("толчок виден и без эталона", v["verdict"] == "repeat", v["verdict"])

    print("10. Показатели для программы")
    m = W.metrics(A.t - 60, A.t)[A.mac]
    check("полосы, разброс, эксцесс", m and "p4_z" in m and "sd_z" in m and "kurt_x" in m,
          ", ".join(f"{k} {v:.1f}" for k, v in list(m.items())[:6]) if m else "нет")
    sp = W.spectrum(A.mac)
    check("спектр для графика с эталоном", sp["f"] and "lo" in sp["axes"][2], f"бинов {len(sp['f'])}")
finally:
    for fs in W.files.values():
        for h in fs:
            h.close()
    shutil.rmtree(tmp, ignore_errors=True)

print("\nвсё сходится" if ok_all else "\nЕСТЬ РАСХОЖДЕНИЯ")
sys.exit(0 if ok_all else 1)
