"""Разбор журнала entropy_probe.

    python tools/probe_report.py logs/probe.log

Печатает строки FLAGS и PROBE таблицей, затем для каждого дампа слов
аппаратного генератора (DUMP <метка>) считает линейную сложность
(алгоритм Берлекэмпа — Мэсси над GF(2)):
  * по каждой из 32 позиций бита — последовательность из 4096 бит;
  * по общему потоку бит (первые 8192 бита подряд).
У случайной последовательности длины n линейная сложность ~ n/2.
Если она заметно меньше — в генераторе есть линейная структура
(регистр сдвига без подмешивания шума).
"""
import collections
import math
import statistics
import sys


def linear_complexity(bits):
    """Берлекэмп — Мэсси. bits — список 0/1. Многочлены хранятся как int."""
    n = len(bits)
    rev = 0                      # бит (n-1-j) = s_j: окно s_N, s_N-1, ... берётся сдвигом
    for j, b in enumerate(bits):
        if b:
            rev |= 1 << (n - 1 - j)
    c, b_poly, L, m = 1, 1, 0, -1
    for N in range(n):
        window = (rev >> (n - 1 - N)) & ((1 << (L + 1)) - 1)   # бит i = s_{N-i}
        if (c & window).bit_count() & 1:
            t = c
            c ^= b_poly << (N - m)
            if 2 * L <= N:
                L, m, b_poly = N + 1 - L, N, t
    return L


def analyze_csi(recs):
    """CSI: для самого частого отправителя смотрим, сколько шума в младших битах.

    Берём позиции (мнимая/действительная часть поднесущей), ненулевые почти во
    всех пакетах. По каждой позиции — ряд значений от пакета к пакету:
    доля единиц в младшем бите, совпадение младшего бита с прошлым пакетом
    (0.5 — без памяти) и мин-энтропия двух младших бит по самому частому значению.
    """
    labels = {0: "только пакеты для нас", 1: "все пакеты в эфире", 2: "ответы роутера на пинг 100 Гц"}
    for prom in (0, 1, 2):
        rs = [r for r in recs if r["prom"] == prom]
        if not rs:
            continue
        macs = collections.Counter(r["mac"] for r in rs)
        nfs = collections.Counter(r["nf"] for r in rs)
        rssi = [r["rssi"] for r in rs]
        mode = labels[prom]
        print(f"CSI ({mode}): записано {len(rs)}, отправителей {len(macs)}, "
              f"сигнал {min(rssi)}..{max(rssi)} дБм, noise_floor {dict(nfs)}")
        mac, _ = macs.most_common(1)[0]
        series = [r["vals"] for r in rs if r["mac"] == mac]
        if len(series) < 20:
            print(f"  от {mac} всего {len(series)} пакетов — мало для оценки")
            continue
        active = [j for j in range(4, 128)
                  if sum(1 for v in series if v[j] != 0) > 0.9 * len(series)]
        ones = eq = tot = pairs = 0
        h2, sds = [], []
        for j in active:
            col = [v[j] for v in series]
            ones += sum(c & 1 for c in col)
            tot += len(col)
            eq += sum(1 for a, b in zip(col, col[1:]) if ((a ^ b) & 1) == 0)
            pairs += len(col) - 1
            cnt = collections.Counter(c & 3 for c in col)
            p = max(cnt.values()) / len(col)
            pu = min(1.0, p + 2.576 * math.sqrt(p * (1 - p) / (len(col) - 1)))
            h2.append(-math.log2(pu))
            m = sum(col) / len(col)
            sds.append(math.sqrt(sum((c - m) ** 2 for c in col) / len(col)))
        if not active:
            print(f"  от {mac}: нет устойчиво ненулевых позиций")
            continue
        h = sum(h2) / len(h2)
        print(f"  от {mac}: пакетов {len(series)}, чисел в пакете {len(active)}, "
              f"разброс значения (медиана) {statistics.median(sds):.1f}; младший бит: единиц "
              f"{ones / tot:.3f}, совпадает с прошлым пакетом {eq / pairs:.3f}; "
              f"мин-энтропия двух младших бит {h:.2f} бит на число -> ~{len(active) * h:.0f} бит на пакет")


def main(path):
    dumps = collections.defaultdict(dict)
    csi = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            text = line.split(" ", 1)[1] if line[:1].isdigit() and " " in line else line
            # serial_capture.py ставит в начало метку времени; отрезаем её
            for key in ("FLAGS", "PROBE", "WIFI", "BOOT", "DUMP", "CSI"):
                k = text.find(key + " ")
                if k >= 0:
                    text = text[k:]
                    break
            if text.startswith("DUMP "):
                parts = text.split()
                words = parts[3:]
                # строка, оборванная сбросом платы, пропускается целиком
                if len(words) != 8 or any(len(w) != 8 for w in words):
                    continue
                try:
                    tag, idx = parts[1], int(parts[2])
                    values = [int(w, 16) for w in words]
                except ValueError:
                    continue
                for j, w in enumerate(values):
                    dumps[tag][idx + j] = w
            elif text.startswith("CSI "):
                parts = text.split()
                if len(parts) != 10 or len(parts[9]) != 256:
                    continue
                try:
                    raw = bytes.fromhex(parts[9])
                    csi.append({"prom": int(parts[1]), "mac": parts[4], "rssi": int(parts[5]),
                                "nf": int(parts[6]), "len": int(parts[7]),
                                "vals": [b - 256 if b > 127 else b for b in raw]})
                except ValueError:
                    continue
            elif text.startswith(("FLAGS", "PROBE", "WIFI", "BOOT")):
                print(text)

    print()
    print("Линейная сложность (случайная последовательность: около n/2)")
    for tag, d in dumps.items():
        words = [d[i] for i in sorted(d)]
        n = len(words)
        per_bit = [linear_complexity([(w >> k) & 1 for w in words]) for k in range(32)]
        stream = [(w >> k) & 1 for w in words for k in range(32)][:8192]
        lc_stream = linear_complexity(stream)
        print(f"  {tag:8s} слов {n}: по позициям бита мин {min(per_bit)} макс {max(per_bit)} "
              f"(ожидание {n // 2}); общий поток {lc_stream} из {len(stream)} "
              f"(ожидание {len(stream) // 2})")
    if csi:
        print()
        analyze_csi(csi)


if __name__ == "__main__":
    main(sys.argv[1])
