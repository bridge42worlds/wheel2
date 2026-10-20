"""Статистика потока простых из журнала (data/primes/<дата>/<mac>.txt).

    python tools/prime_stats.py [дата YYYY-MM-DD] [--mac 68:ee:…] [--cid N] [--recheck]

Строка журнала: время сервера, часы агента (мс), номер настройки, бит, откуда
(0 — каждый сам, 1 — сообща), число (hex), проверка сервером (1/0).

Что проверяется — то, чем поток простых обязан быть у честного генератора:
  * все числа простые (по журналу; --recheck — перепроверить здесь ещё раз);
  * нет повторов (одно простое дважды — почти невозможно для 256 бит);
  * остатки по модулю 3, 5, 7, 11 равновероятны среди допустимых (хи-квадрат);
  * биты числа (кроме старшего и младшего — они выставлены) — единицы в половине случаев;
  * второй сверху и второй снизу бит, последняя шестнадцатеричная цифра — равномерны;
  * промежутки между находками по часам агента: у пуассоновского потока разброс равен
    среднему (коэффициент вариации ≈ 1), промежутки не коррелируют между собой.
Печатает z и p; z больше ~4 — повод смотреть, что не так (или что так).
"""
import argparse, math, os, sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def chi2_p(x, df):
    """P(X > x) для хи-квадрат с df степенями свободы (приближение Уилсона — Хилферти)."""
    if df <= 0:
        return 1.0
    z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return 0.5 * math.erfc(z / math.sqrt(2))


def z_p(z):
    return math.erfc(abs(z) / math.sqrt(2))


def load(day, mac=None, cid=None):
    d = os.path.join(ROOT, "data", "primes", day)
    rows = []
    if not os.path.isdir(d):
        return rows
    for fn in sorted(os.listdir(d)):
        m = ":".join(fn[:-4][i:i + 2] for i in range(0, 12, 2))
        if mac and m != mac.lower():
            continue
        for ln in open(os.path.join(d, fn), encoding="utf-8"):
            f = ln.split()
            if len(f) < 7:
                continue
            if cid is not None and int(f[2]) != cid:
                continue
            rows.append({"mac": m, "rx": float(f[0]), "ms": int(f[1]), "cid": int(f[2]), "bits": int(f[3]),
                         "src": int(f[4]), "hex": f[5], "ok": f[6] == "1"})
    return rows


def global_dups():
    """Повторы по всем журналам за все дни: [(число hex, [(день, mac, настройка)…])]."""
    base = os.path.join(ROOT, "data", "primes")
    seen, dups, n = {}, {}, 0
    for day in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        for r in load(day):
            n += 1
            k = r["hex"].lower()
            where = (day, r["mac"], r["cid"])
            if k in seen:
                dups.setdefault(k, [seen[k]]).append(where)
            else:
                seen[k] = where
    return n, sorted(dups.items())


_BIT_EXPECT = {}


def bit_expect(bits):
    """Ожидаемая доля единиц в каждом разряде у простых ровно bits бит. Плотность простых ~ 1/ln x
    убывает внутри [2^(bits−1), 2^bits), поэтому у старших разрядов единица чуть реже половины:
    для 256 бит разряд 254 — 0,49952, 253 — 0,49976; дальше вниз разница тает вдвое на разряд. С
    ожиданием ровно ½ за ~20 ночей накопления разряд 254 стал бы ложным «систематическим» сигналом.
    Разряд k: число x = u·2^(bits−1), u ∈ [1, 2); бит k = 1 ⇔ floor(u·2^j) нечётен, j = bits−1−k."""
    if bits in _BIT_EXPECT:
        return _BIT_EXPECT[bits]
    a = (bits - 1) * math.log(2)
    w = lambda u: 1 / (a + math.log(u))                # плотность с точностью до множителя
    ex = [0.5] * bits
    ex[0] = ex[bits - 1] = 1.0                          # младший и старший выставлены
    for k in range(bits - 2, 0, -1):
        j = bits - 1 - k
        if j > 14:                                      # дальше разница < 1e-7 — ровно ½
            break
        h, one, tot = 2.0 ** -j, 0.0, 0.0
        for i in range(1 << j):                         # Симпсон на каждом отрезке; бит = 1 на нечётных
            u0 = 1 + i * h
            s = h / 6 * (w(u0) + 4 * w(u0 + h / 2) + w(u0 + h))
            tot += s
            if i & 1:
                one += s
        ex[k] = one / tot
    _BIT_EXPECT[bits] = ex
    return ex


def report(rows, recheck=False, m=None):
    """Строки отчёта; m — словарь, куда сложить сами числа (для сводок)."""
    m = {} if m is None else m
    out = []
    say = out.append
    n = len(rows)
    say(f"простых: {n}; агентов {len({r['mac'] for r in rows})}; настроек {sorted({r['cid'] for r in rows})}; "
        f"длины {sorted({r['bits'] for r in rows})} бит; сообща {sum(r['src'] == 1 for r in rows)}")
    bad = [r for r in rows if not r["ok"]]
    m.update(n=n, bad=len(bad))
    say(f"{'✗' if bad else '✓'} не прошли проверку сервера: {len(bad)}" + (f" — например {bad[0]['hex'][:24]}…" if bad else ""))
    if recheck:
        from monitor import is_probable_prime
        rb = sum(1 for r in rows if not is_probable_prime(int(r["hex"], 16)))
        say(f"{'✗' if rb else '✓'} перепроверка здесь: не простых {rb}")
    dup = n - len({r["hex"] for r in rows})
    m["dup"] = dup
    say(f"{'✗' if dup else '✓'} повторов: {dup}")
    nums = [int(r["hex"], 16) for r in rows]
    # остатки по модулю малых простых: равновероятны среди ненулевых
    for q in (3, 5, 7, 11):
        c = Counter(x % q for x in nums)
        if c.get(0):
            say(f"✗ делится на {q}: {c[0]} чисел")
        exp = n / (q - 1)
        chi = sum((c.get(k, 0) - exp) ** 2 / exp for k in range(1, q)) if exp else 0
        p = chi2_p(chi, q - 2)
        m.setdefault("mod", {})[q] = round(p, 4)
        say(f"{'✓' if p > 1e-3 else '✗'} остатки mod {q}: хи² {chi:.1f} при {q - 2} степенях, p = {p:.3f}")
    # биты: доля единиц в каждом разряде, кроме выставленных
    if nums:
        bits = rows[0]["bits"]
        same = all(r["bits"] == bits for r in rows)
        if same and bits <= 2048:
            ex = bit_expect(bits)                       # ожидание доли единиц по разрядам: старшие — чуть ниже ½
            ones = [sum((x >> k) & 1 for x in nums) for k in range(bits)]
            zs = [(ones[k] - n * ex[k]) / math.sqrt(n * ex[k] * (1 - ex[k])) for k in range(1, bits - 1)]
            worst = max(range(len(zs)), key=lambda i: abs(zs[i]))
            mean_z = sum(zs) / len(zs)
            zm = mean_z * math.sqrt(len(zs))            # общий перекос по всем разрядам — в сигмах
            ex_mean = sum(ex[1:bits - 1]) / (bits - 2)
            m.update(ones=round(sum(ones[1:bits - 1]) / (bits - 2) / n * 100, 3), ones_exp=round(ex_mean * 100, 4),
                     zm=round(zm, 2), worst_bit=[worst + 1, round(zs[worst], 2)])
            lvl = "✗" if abs(zs[worst]) >= 4.5 or abs(zm) >= 5 else "◐" if abs(zm) >= 3 else "✓"
            say(f"{lvl} биты 1…{bits - 2}: единиц в среднем "
                f"{sum(ones[1:bits - 1]) / (bits - 2) / n * 100:.2f} % (ожидание {ex_mean * 100:.3f} %), "
                f"худший разряд {worst + 1}: z {zs[worst]:.2f}; "
                f"средний z по разрядам {mean_z:.3f} (ожидание 0 ± {1 / math.sqrt(len(zs)):.3f}, общий перекос {zm:+.1f} σ)")
            # Доли единиц — свои у каждого агента (у плат свой перекос): ожидания ниже считаются по ним,
            # иначе смесь плат с разным перекосом сама даёт и «сцепление» разрядов, и «сходство» чисел.
            p = [o / n for o in ones]
            by_mac = defaultdict(list)
            for x, r in zip(nums, rows):
                by_mac[r["mac"]].append((r["rx"], r["ms"], x))
            pa = {}
            for mac, lst in by_mac.items():
                lst.sort(key=lambda t: (t[0], t[1]))
                pa[mac] = [sum((t[2] >> k) & 1 for t in lst) / len(lst) for k in range(bits)] if len(lst) >= 3 else p
            # соседние разряды: равны так часто, как велят доли единиц (сырой шум без хеша может их сцеплять)
            mk = ((1 << (bits - 3)) - 1) << 1
            eq = e_eq = v_eq = 0.0
            for mac, lst in by_mac.items():
                pp = pa[mac]
                qk = [pp[k] * pp[k + 1] + (1 - pp[k]) * (1 - pp[k + 1]) for k in range(1, bits - 2)]
                eq += sum(bits - 3 - bin((t[2] ^ (t[2] >> 1)) & mk).count("1") for t in lst)
                e_eq += len(lst) * sum(qk)
                v_eq += len(lst) * sum(q * (1 - q) for q in qk)
            z_eq = (eq - e_eq) / math.sqrt(v_eq) if v_eq else 0.0
            m.update(eq=round(100 * eq / (n * (bits - 3)), 3), eq_exp=round(100 * e_eq / (n * (bits - 3)), 3), z_eq=round(z_eq, 2))
            say(f"{'✓' if abs(z_eq) < 3 else '◐' if abs(z_eq) < 5 else '✗'} соседние разряды равны в "
                f"{100 * eq / (n * (bits - 3)):.2f} % пар (ожидание по долям единиц агента {100 * e_eq / (n * (bits - 3)):.2f} %), "
                f"z {z_eq:+.1f}")
            # память шума: соседние (по времени) простые одного агента различаются в стольких разрядах,
            # сколько велят его доли единиц (n/(n−1) — доли по той же выборке); если шум помнит прошлое — меньше
            mask = ((1 << (bits - 2)) - 1) << 1
            s_d = s_e = s_v = 0.0
            npair = 0
            for mac, lst in by_mac.items():
                na = len(lst)
                if na < 3:
                    continue
                pp = pa[mac]
                qd = [2 * pp[k] * (1 - pp[k]) * na / (na - 1) for k in range(1, bits - 1)]
                s_d += sum(bin((a[2] ^ b[2]) & mask).count("1") for a, b in zip(lst, lst[1:]))
                s_e += (na - 1) * sum(qd)
                s_v += (na - 1) * sum(q * (1 - q) for q in qd)
                npair += na - 1
            if npair >= 20 and s_v > 0:
                z_d = (s_d - s_e) / math.sqrt(s_v)
                m.update(ham=round(s_d / npair, 3), ham_exp=round(s_e / npair, 3), z_ham=round(z_d, 2))
                say(f"{'✓' if abs(z_d) < 3 else '◐' if abs(z_d) < 5 else '✗'} соседние простые агента различаются в "
                    f"{s_d / npair:.2f} разрядах из {bits - 2} (ожидание по его долям единиц {s_e / npair:.2f}), z {z_d:+.1f}")
            if len({r["mac"] for r in rows}) > 1:     # перекос бит по агентам: один шумный источник виден сразу
                parts, worst_a = [], 0.0
                for mac in sorted({r["mac"] for r in rows}):
                    xs = [x for x, r in zip(nums, rows) if r["mac"] == mac]
                    if len(xs) < 20:
                        continue
                    o = sum(bin(x >> 1).count("1") - 1 for x in xs)      # разряды 1…bits-2
                    za = (o - len(xs) * (bits - 2) / 2) / math.sqrt(len(xs) * (bits - 2) / 4)
                    worst_a = max(worst_a, abs(za))
                    m.setdefault("agents", {})[mac] = [round(100 * o / (len(xs) * (bits - 2)), 3), round(za, 2), len(xs)]
                    parts.append(f"…{mac[-5:]} {100 * o / (len(xs) * (bits - 2)):.2f} % ({za:+.1f} σ, {len(xs)})")
                if parts:
                    say(f"{'✓' if worst_a < 3 else '◐' if worst_a < 5 else '✗'} единиц по агентам: " + "; ".join(parts))
            hexd = Counter(r["hex"][-1] for r in rows)
            exp = n / 8
            chi = sum((hexd.get(h, 0) - exp) ** 2 / exp for h in "13579BDF")
            m["hex_p"] = round(chi2_p(chi, 7), 4)
            say(f"{'✓' if chi2_p(chi, 7) > 1e-3 else '✗'} последняя цифра hex (1,3,5,7,9,B,D,F): хи² {chi:.1f}, p = {chi2_p(chi, 7):.3f}")
    # промежутки по часам агента, по агенту и настройке
    for (mac, cid), grp in sorted(defaultdict(list, {k: [r for r in rows if (r["mac"], r["cid"]) == k]
                                                     for k in {(r["mac"], r["cid"]) for r in rows}}).items()):
        ms = sorted(r["ms"] for r in grp)
        gaps = [b - a for a, b in zip(ms, ms[1:]) if 0 <= b - a < 600000]
        if len(gaps) < 30:
            continue
        mu = sum(gaps) / len(gaps)
        sd = math.sqrt(sum((g - mu) ** 2 for g in gaps) / (len(gaps) - 1))
        cv = sd / mu if mu else 0
        # автокорреляция соседних промежутков
        a = [(g - mu) for g in gaps]
        r1 = sum(x * y for x, y in zip(a, a[1:])) / sum(x * x for x in a) if sum(x * x for x in a) else 0
        zr = r1 * math.sqrt(len(gaps))
        m.setdefault("gaps", []).append([mac, cid, len(gaps) + 1, round(cv, 3), round(r1, 4), round(zr, 2)])
        say(f"{'✓' if abs(zr) < 4 else '◐'} {mac} настройка {cid}: {len(gaps) + 1} простых, промежуток {mu:.0f} мс "
            f"(±{sd:.0f}), коэффициент вариации {cv:.2f} (у пуассона 1,0; у группы меньше — цикл ровный), "
            f"корреляция соседних промежутков {r1:.3f} (z {zr:.1f})")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("day", nargs="?", default=None)
    ap.add_argument("--mac")
    ap.add_argument("--cid", type=int)
    ap.add_argument("--recheck", action="store_true")
    a = ap.parse_args()
    import time
    day = a.day or time.strftime("%Y-%m-%d")
    rows = load(day, a.mac, a.cid)
    if not rows:
        print(f"нет журнала простых за {day} (data/primes/{day}/)")
        return
    for line in report(rows, a.recheck):
        print(line)


if __name__ == "__main__":
    main()
