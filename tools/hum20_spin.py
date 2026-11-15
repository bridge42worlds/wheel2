"""Точечная проверка (10.11.2026): меандр 20 Гц на К1 против постоянного поля того же уровня — при вращении.
Во фристайле ⊓20 Гц держался на вращающихся колёсах (повторы +3,7/+3,7/+3,3/+8,9 и +11/+18/+1,5/+1,1), а при
стоящих — нет (8 пар, t −0,5). Здесь: навстречу, невесомость; 8 пар по 30 с (порядок чередуется), между — дом 10 с;
разность мер «⊓20 − постоянное» по каждой паре, t по всем 40 мерам.

    python -u tools/hum20_spin.py [--level 400] [--pairs 8]
"""
import argparse, math, os, statistics as S, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ap = argparse.ArgumentParser()
ap.add_argument("--level", type=int, default=400)
ap.add_argument("--pairs", type=int, default=8)
ap.add_argument("--sec", type=float, default=30.0)
A = ap.parse_args()
_argv, sys.argv = sys.argv, ["freestyle.py"]
import freestyle as F  # noqa: E402
sys.argv = _argv
wn, say, req = F.wn, F.say, F.req

HUM = {"field": {"k1": "-", "k2": "off", "level": A.level, "ac": {"f": 20, "shape": "sq"}}, "count": None}
DC = {"field": {"k1": "-", "k2": "off", "level": A.level, "ac": None}, "count": None}


def main():
    st = wn.Static()
    say(f"проверка ⊓20 Гц при вращении: уровень {A.level}, {A.pairs} пар по {A.sec:g} с")
    F.apply(F.HOME)
    req("/api/motors", {"mod": None})
    req("/api/motors", {"pair": "free"})
    for w, d in (("a", 1), ("b", -1)):
        req("/api/motors", {"wheel": w, "mode": "zerog", "dir": d, "flip": 0, "zg": 0})
    time.sleep(45)
    w = req("/api/motors").get("wheels") or {}
    say(f"навстречу, невесомость: обод {[round((w.get(k) or {}).get('meas') or 0, 1) for k in 'ab']} об/мин")
    D = []
    try:
        for r in range(A.pairs):
            seq = [("hum", HUM), ("dc", DC)]
            if r % 2:
                seq.reverse()
            v = {}
            for key, x in seq:
                F.apply(x)
                t0 = time.time()
                time.sleep(A.sec)
                v[key], _ = F.measure(st, t0, time.time())
                F.apply(F.HOME)
                time.sleep(10)
            if v["hum"] is not None and v["dc"] is not None:
                D.append(v["hum"] - v["dc"])
                d = D[-1]
                say(f"  пара {r + 1}: СКО г1 {d[F.J['СКО г1']]:+.0f} мкg · эксцесс г2 {d[F.J['эксцесс г2']]:+.2f} · "
                    f"СКО в {d[F.J['СКО в']]:+.0f} мкg")
    finally:
        F.apply(F.HOME)
        req("/api/motors", {"stop": True})
        say("моторы стоп, поле выкл")
    if len(D) < 4:
        say("✗ мало пар")
        return
    M = np.array(D)
    t = M.mean(axis=0) / (np.maximum(M.std(axis=0, ddof=1), 1e-9) / math.sqrt(len(M)))
    same = np.array([max((M[:, j] > 0).sum(), (M[:, j] < 0).sum()) for j in range(M.shape[1])])
    order = np.argsort(-np.abs(t))[:8]
    for j in order:
        say(f"  {F.NAMES[j]}: Δ {M[:, j].mean():+.3f}, t {t[j]:+.1f}, один знак {same[j]}/{len(M)}")
    hold = [j for j in order if abs(t[j]) >= 4 and same[j] >= len(M) - 1]
    say("итог: " + ("⊓20 Гц при вращении меняет статику: " + ", ".join(F.NAMES[j] for j in hold) if hold
                    else "⊓20 Гц против постоянного при вращении — разницы нет"))


if __name__ == "__main__":
    main()
