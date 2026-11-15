"""Простые по всем журналам (data/primes/*/<mac>.txt): что в самих числах — поверх prime_stats (который смотрит один день).

    python tools/prime_cross.py [--days 2026-11-03 2026-11-05 …] [--bits 64 256]

1. Повторы: одно и то же простое у одной платы, у разных плат, в разные дни (для 256 бит — невозможно; для 64 бит
   при N чисел ожидаемо N²/2 · ln2/2⁶³ совпадений — считаем и сравниваем).
2. Общие префиксы: сколько пар чисел совпадают в старших 24 битах против ожидания (день рождения) — «слипание»
   генератора в одной области.
3. Биты по разрядам у каждой платы и в целом — доля единиц, z по биномиальному; по дням — куда дрейфует.
4. Остатки по малым модулям (3…31) — хи-квадрат по платам и дням.
5. Промежутки по часам платы: доля «слишком коротких» (< 1/20 среднего) — пачки.
6. Соседство: у подряд найденных чисел одной платы — совпадение старших 8 бит чаще случайного?
"""
import argparse, glob, math, os, statistics as S
from collections import Counter, defaultdict

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--days", nargs="*", default=None)
ap.add_argument("--bits", nargs="*", type=int, default=None)
A = ap.parse_args()

days = A.days or sorted(os.path.basename(d) for d in glob.glob(os.path.join(ROOT, "data", "primes", "20*")))
seen = {}                                     # число → (день, mac)
dup_same, dup_other, dup_day = 0, 0, 0
by_bits = Counter()
ones = defaultdict(lambda: defaultdict(lambda: [0, 0]))     # (bits) → разряд → [единиц, всего]
ones_mac = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # (bits, mac) → разряд
ones_day = defaultdict(lambda: [0, 0])        # (bits, день) → все разряды кроме крайних
res = defaultdict(Counter)                    # (bits, m) → остаток → счёт
pre24 = defaultdict(Counter)                  # bits → старшие 24 бита → счёт
gaps_short = Counter(); gaps_n = Counter()
nb_same8 = Counter(); nb_n = Counter()
MODS = (3, 5, 7, 11, 13, 17, 19, 23, 29, 31)
for day in days:
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "primes", day, "*.txt"))):
        mac = os.path.basename(f)[:-4]
        prev_t, prev_x, tgaps = None, None, []
        for line in open(f, encoding="utf-8", errors="replace"):
            p = line.split()
            if len(p) < 7:
                continue
            try:
                t_ag, bits, hexs, ok = int(p[1]), int(p[3]), p[5], p[6]
            except ValueError:
                continue
            if ok != "1" or (A.bits and bits not in A.bits):
                continue
            x = int(hexs, 16)
            by_bits[bits] += 1
            key = (bits, x)
            if key in seen:
                d0, m0 = seen[key]
                if m0 == mac and d0 == day:
                    dup_same += 1
                elif m0 == mac:
                    dup_day += 1
                else:
                    dup_other += 1
            else:
                seen[key] = (day, mac)
            # биты
            n1 = bin(x).count("1")
            od = ones_day[(bits, day)]
            od[0] += n1 - 2; od[1] += bits - 2                 # крайние биты выставлены
            for i in range(0, bits, max(1, bits // 32)):      # до 32 разрядов на длину — выборочно, равномерно
                b = (x >> i) & 1
                o = ones[bits][i]; o[0] += b; o[1] += 1
                om = ones_mac[(bits, mac)][i]; om[0] += b; om[1] += 1
            for m in MODS:
                res[(bits, m)][x % m] += 1
            pre24[bits][x >> (bits - 24)] += 1
            if prev_t is not None and t_ag > prev_t:
                tgaps.append(t_ag - prev_t)
            if prev_x is not None:
                nb_n[bits] += 1
                if (prev_x >> (bits - 8)) == (x >> (bits - 8)):
                    nb_same8[bits] += 1
            prev_t, prev_x = t_ag, x
        if len(tgaps) > 100:
            mean = S.mean(tgaps)
            gaps_n[mac] += len(tgaps)
            gaps_short[mac] += sum(1 for g in tgaps if g < mean / 20)

print("дней", len(days), "·", ", ".join(f"{b} бит: {n}" for b, n in sorted(by_bits.items())))
print(f"\n1. повторы простых: у одной платы в один день {dup_same}, у одной платы в разные дни {dup_day}, у разных плат {dup_other}")
for b, n in sorted(by_bits.items()):
    exp = n * n / 2 * math.log(2) * b / 2 ** (b - 1)        # плотность простых около 2^b: 1/(b·ln2); пар N²/2
    print(f"   ожидаемо случайных совпадений для {b} бит при N={n}: {exp:.2g}")
print("\n2. общие старшие 24 бита (пары с одинаковым префиксом) против случая:")
for b, c in sorted(pre24.items()):
    n = sum(c.values()); pairs = sum(v * (v - 1) // 2 for v in c.values())
    exp = n * (n - 1) / 2 / 2 ** 23                           # старший бит выставлен: 2^23 корзин
    z = (pairs - exp) / math.sqrt(exp) if exp > 0 else 0
    top = c.most_common(1)[0]
    print(f"   {b} бит: пар {pairs} при ожидании {exp:.0f} (z {z:+.1f}); самый частый префикс {top[0]:06X} — {top[1]} раз")
print("\n3. доля единиц по разрядам (все платы), |z| > 3:")
for b, cols in sorted(ones.items()):
    bad = []
    for i, (k, n) in sorted(cols.items()):
        if n < 1000 or i in (0, b - 1):
            continue
        z = (k - n / 2) / math.sqrt(n / 4)
        if abs(z) > 3:
            bad.append(f"бит {i}: {k / n:.4f} (z {z:+.1f})")
    print(f"   {b} бит: {'; '.join(bad) if bad else 'ни одного'}")
print("   по платам, |z| > 4:")
for (b, mac), cols in sorted(ones_mac.items()):
    bad = [f"бит {i} z {(k - n / 2) / math.sqrt(n / 4):+.1f}" for i, (k, n) in sorted(cols.items())
           if n >= 1000 and i not in (0, b - 1) and abs((k - n / 2) / math.sqrt(n / 4)) > 4]
    if bad:
        print(f"   {b} бит {mac}: {'; '.join(bad)}")
print("   по дням (все разряды кроме крайних):")
for (b, day), (k, n) in sorted(ones_day.items()):
    if n:
        print(f"   {b} бит {day}: {k / n:.5f} (z {(k - n / 2) / math.sqrt(n / 4):+.1f}, бит {n})")
print("\n4. остатки по малым модулям, хи-квадрат (z = (χ² − df)/√(2df)):")
for (b, m), c in sorted(res.items()):
    n = sum(c.values()); e = n / (m - 1)
    chi = sum((c.get(r, 0) - e) ** 2 / e for r in range(1, m))
    df = m - 2
    z = (chi - df) / math.sqrt(2 * df)
    flag = " ◐" if abs(z) > 3 else ""
    if abs(z) > 2 or m in (3, 31):
        print(f"   {b} бит mod {m}: z {z:+.1f}{flag}")
print("\n5. пачки по часам платы (промежуток < 1/20 среднего), доля:")
for mac in sorted(gaps_n):
    print(f"   {mac}: {gaps_short[mac] / gaps_n[mac]:.4f} из {gaps_n[mac]}")
print("\n6. подряд найденные с одинаковыми старшими 8 битами (ожидание 1/128 — старший выставлен):")
for b in sorted(nb_n):
    n, k = nb_n[b], nb_same8[b]
    e = n / 128
    print(f"   {b} бит: {k} из {n} (ожидание {e:.0f}, z {(k - e) / math.sqrt(e):+.1f})")
