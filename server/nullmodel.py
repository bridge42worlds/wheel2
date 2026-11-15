# -*- coding: utf-8 -*-
"""Предсказанный ноль: чего ждать от счёта, если никакой аномалии нет, — без контрольных шагов.

Сравнение с контролем ловит разницу между режимами, но не говорит, верен ли сам счёт. Здесь ожидание
известно заранее — по теории или по замеренному сырью:

1. Выход простых и отсев делителями (z_yield, z_td). Доля простых среди нечётных чисел длины L — теорема
   о распределении простых (интеграл 1/ln x), доля чисел без делителей меньше td — произведение (1 − 1/p).
   Для равномерных кандидатов это точное ожидание. Перекос бит сырья на остатки по малым простым у чисел
   в 256 бит почти не влияет (вклад гаснет как |q + (1 − q)·ω|^L), поэтому ожидание то же и в режимах без
   хеша — сдвинуть его может только почти застывший источник.
2. Биты простых (z_pq, z_ps). В найденных простых (кроме старшего и младшего бита — их ставит отсев) доля
   единиц и совпадений соседних бит должна совпасть с предсказанием по счётчикам сырья (e: младший бит
   каждого отсчёта) и способу смешивания: SHA-256 и Тёплиц — 1/2; без хеша — XOR источников (лемма о
   накоплении); фон Нейман и XOR-складывание — по цепи Маркова с замеренными долей единиц и совпадений.
   Отбор простотой бит почти не трогает (то же рассуждение про остатки).
3. Проверка счёта (det). Кандидаты подряд, 2^(L−1) + 1 + 2·(det·2^32 + k): какие из них простые и сколько
   отсеют делители, известно точно. Расхождение — ошибка счёта (плата, питание, поле), не шум.

И эталон экстрактора Тёплица (raw = 4) — та же матрица, что в прошивке (entropy.cpp): самопроверка платы
сверяет с ним свой ответ (tz_selftest_vectors).
"""
import math
from functools import lru_cache

BASES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]       # как в прошивке (config.h)
M64 = (1 << 64) - 1
TZ_SEED = 0x243F6A8885A308D3        # зерно матрицы Тёплица: первые цифры π после запятой — ничего не спрятано
TZ_TEST_X = 0x0123456789ABCDEF      # вход самопроверки: биты splitmix64 с этим начальным словом
# сколько бит выдаёт отсчёт источника в число без хеша (entropy.cpp, raw_refill): у кого больше одного,
# счётчик сырья (младший бит) не говорит о связи соседних бит числа — она берётся нулевой
UNIT_BITS = {"rng": 32, "rrg": 32, "bc": 8, "mag": 2, "csi": 1, "adc": 1, "jit": 1, "acc": 1}


# ---------- экстрактор Тёплица ----------
def splitmix64(state):
    """Шаг splitmix64: (новое состояние, слово)."""
    state = (state + 0x9E3779B97F4A7C15) & M64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return state, z ^ (z >> 31)


def splitmix_bits(state, nbits):
    """nbits бит потока splitmix64 одним числом: бит i — бит (i % 64) слова i // 64."""
    out = 0
    for q in range((nbits + 63) // 64):
        state, w = splitmix64(state)
        out |= w << (64 * q)
    return out & ((1 << nbits) - 1)


def toeplitz(x, xf):
    """y = T·x над GF(2). x — m = xf·256 бит сырья (число: бит j — x_j, j-й взятый бит), y — 256 бит (бит i — y_i).
    T[i][j] = s[i − j + m − 1], s — зерно (splitmix64 от TZ_SEED): столбец j — окно зерна с бита m − 1 − j."""
    m = xf * 256
    s = splitmix_bits(TZ_SEED, m + 255)
    y, mask = 0, (1 << 256) - 1
    for j in range(m):
        if (x >> j) & 1:
            y ^= (s >> (m - 1 - j)) & mask
    return y


def tz_selftest_vectors():
    """Ответы самопроверки прошивки: {xf: 8 слов по 32 бита, младшее первым} для входа splitmix64(TZ_TEST_X)."""
    out = {}
    for xf in (4, 16):
        y = toeplitz(splitmix_bits(TZ_TEST_X, xf * 256), xf)
        out[xf] = [(y >> (32 * w)) & 0xFFFFFFFF for w in range(8)]
    return out


# ---------- выход простых и отсев ----------
def odd_primes_below(n):
    """Нечётные простые меньше n (таблица делителей платы — те же)."""
    if n <= 3:
        return []
    sieve = bytearray([1]) * n
    sieve[0:2] = b"\x00\x00"
    for p in range(2, int(n ** 0.5) + 1):
        if sieve[p]:
            sieve[p * p::p] = bytearray(len(range(p * p, n, p)))
    return [p for p in range(3, n) if sieve[p]]


@lru_cache(maxsize=64)
def prime_density(bits):
    """Доля простых среди нечётных чисел [2^(L−1), 2^L): 4·∫ 2^(t−L)/t dt по t от L−1 до L
    (замена x = 2^t в ∫ dx/ln x и деление на 2^(L−2) нечётных); Симпсон по 64 отрезкам."""
    L, n = bits, 64
    f = lambda t: 2.0 ** (t - L) / t
    h = 1.0 / n
    s = f(L - 1) + f(L) + sum((4 if i % 2 else 2) * f(L - 1 + i * h) for i in range(1, n))
    return 4 * s * h / 3


@lru_cache(maxsize=64)
def td_survive(td):
    """Доля нечётных чисел без делителей среди нечётных простых меньше td."""
    f = 1.0
    for p in odd_primes_below(td):
        f *= 1.0 - 1.0 / p
    return f


def z_binom(k, n, p):
    """Отклонение счёта k из n от ожидания n·p в стандартных отклонениях биномиального."""
    if n <= 0 or not 0 < p < 1:
        return None
    return (k - n * p) / math.sqrt(n * p * (1 - p))


def yield_z(cand, primes, tdr, bits, td):
    """z выхода простых и отсева делителями против теории для равномерных кандидатов: {"z_yield": …, "z_td": …}."""
    out = {}
    if not cand or not bits:
        return out
    z = z_binom(primes, cand, prime_density(bits))
    if z is not None:
        out["z_yield"] = z
    if td and tdr is not None:
        z = z_binom(tdr, cand, 1.0 - td_survive(td))
        if z is not None:
            out["z_td"] = z
    return out


# ---------- биты простых ----------
def prime_bits(hx):
    """Биты простого в порядке потока шума — байт за байтом, в байте от младшего бита (так их кладёт raw_fill):
    (свободных бит, единиц среди них, пар соседних свободных, совпало). Старший бит первого байта и младший
    последнего ставит отсев — они и пары с ними мимо."""
    b = bytes.fromhex(hx)
    nb = len(b)
    if nb < 2:
        return 0, 0, 0, 0
    L = 8 * nb
    s = int.from_bytes(b, "little")             # бит 8i + k — бит k байта i
    t1, t2 = 7, 8 * (nb - 1)                    # поставленные отсевом
    n = L - 2
    ones = bin(s).count("1") - ((s >> t1) & 1) - ((s >> t2) & 1)
    d = (s ^ (s >> 1)) & ((1 << (L - 1)) - 1)   # бит t — x_t ≠ x_(t+1)
    skip = {t1 - 1, t1, t2 - 1, t2}             # пары с поставленными битами
    diff = bin(d).count("1") - sum((d >> t) & 1 for t in skip)
    pairs = L - 1 - len(skip)
    return n, ones, pairs, pairs - diff


def _chain(q, s):
    """Цепь Маркова из двух состояний с долей единиц q и долей совпадений соседних s: (π0, π1, P)."""
    q = min(max(q, 1e-6), 1 - 1e-6)
    flow = (1 - s) / 2                          # π0·P(0→1) = π1·P(1→0) у любой стационарной цепи из двух
    a = min(max(flow / (1 - q), 0.0), 1.0)      # 0 → 1
    b = min(max(flow / q, 0.0), 1.0)            # 1 → 0
    return 1 - q, q, ((1 - a, a), (b, 1 - b))


def _sign_mean(q, s, k):
    """E[(−1)^(x_1 ⊕ … ⊕ x_k)] для k подряд бит цепи: π·D·(P·D)^(k−1)·1, D = diag(1, −1)."""
    p0, p1, P = _chain(q, s)
    v = (p0, -p1)
    for _ in range(k - 1):
        v = (v[0] * P[0][0] + v[1] * P[1][0], -(v[0] * P[0][1] + v[1] * P[1][1]))
    return v[0] + v[1]


def _vn(q, s):
    """Выход фон Неймана на цепи (пары подряд, 01 → 0, 10 → 1, 00 и 11 — мимо): (доля единиц, доля совпадений)."""
    _, _, P = _chain(q, s)
    # R[c][u] — первая принятая пара после состояния c начинается с u: R = P(c→u)P(u→¬u) + Σ_w P(c→w)P(w→w)R[w][u]
    R = [[0.0, 0.0], [0.0, 0.0]]
    for u in (0, 1):
        acc = [P[c][u] * P[u][1 - u] for c in (0, 1)]
        # R_c = acc_c + Σ_w P[c][w]·P[w][w]·R_w — система 2×2
        m00, m01 = 1 - P[0][0] * P[0][0], -P[0][1] * P[1][1]
        m10, m11 = -P[1][0] * P[0][0], 1 - P[1][1] * P[1][1]
        det = m00 * m11 - m01 * m10
        if abs(det) < 1e-12:
            return 0.5, 0.5
        R[0][u] = (acc[0] * m11 - m01 * acc[1]) / det
        R[1][u] = (m00 * acc[1] - m10 * acc[0]) / det
    # выход — цепь по y: после y = u цепь сырья стоит в ¬u
    t01, t10 = R[1][1] / max(R[1][0] + R[1][1], 1e-12), R[0][0] / max(R[0][0] + R[0][1], 1e-12)
    if t01 + t10 <= 0:
        return 0.5, 0.5
    q1 = t01 / (t01 + t10)
    return q1, 1 - (q1 * t10 + (1 - q1) * t01)


def predict_bits(raw, xf, srcs, stats, words=False):
    """Чего ждать от бит числа: (доля единиц, доля совпадений соседних, связь соседних r) по способу смешивания
    и счётчикам сырья. stats — {ключ источника: (доля единиц младшего бита, доля совпадений)}; words — «слова как
    есть» (один ГСЧ или регистр без хеша). Источник, отдающий больше бита на отсчёт (UNIT_BITS), — без связи."""
    if raw == 0 and not words or raw == 4:
        return 0.5, 0.5, 0.0
    per = []
    for k in srcs:
        q, s = stats.get(k, (0.5, 0.5))
        if UNIT_BITS.get(k, 1) > 1 or words:
            s = q * q + (1 - q) * (1 - q)       # отсчёт в несколько бит: соседние считаем независимыми
        if raw == 2:
            q, s = _vn(q, s)
        elif raw == 3:
            k1, k2 = _sign_mean(q, s, xf), _sign_mean(q, s, 2 * xf)
            q, s = (1 - k1) / 2, (1 + k2) / 2
        per.append((q, s))
    if not per:
        return 0.5, 0.5, 0.0
    e = math.prod(1 - 2 * q for q, _ in per)    # XOR независимых источников: средние знаки перемножаются
    c = math.prod(2 * s - 1 for _, s in per)
    q, s = (1 - e) / 2, (1 + c) / 2
    v = 2 * q * (1 - q)
    r = (s - (q * q + (1 - q) * (1 - q))) / v if v > 0 else 0.0
    return q, s, max(-0.9, min(0.9, r))


def bits_z(n, ones, pairs, same, q, s, r):
    """z доли единиц и совпадений соседних бит простых против предсказания: (z_pq, z_ps). Единицы связанных
    соседей разбросаны шире — множитель (1 + r)/(1 − r), как у цепи Маркова."""
    zq = zs = None
    if n > 0 and 0 < q < 1:
        zq = (ones - n * q) / math.sqrt(n * q * (1 - q) * (1 + r) / (1 - r))
    if pairs > 0 and 0 < s < 1:
        zs = (same - pairs * s) / math.sqrt(pairs * s * (1 - s))
    return zq, zs


# ---------- проверка счёта ----------
def det_value(bits, det, k):
    """Кандидат номер k проверки счёта: 2^(bits−1) + 1 + 2·(det·2^32 + k)."""
    return (1 << (bits - 1)) + 1 + 2 * ((det << 32) + k)


def det_index(n, bits):
    """Простое из журнала — кандидат проверки счёта? (det, k) или None."""
    v = n - (1 << (bits - 1)) - 1
    if v < 0 or v >= 1 << 64 or v & 1:
        return None
    v >>= 1
    return v >> 32, v & 0xFFFFFFFF


def _strong(n, bases):
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for a in bases:
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


@lru_cache(maxsize=256)
def det_expected(bits, det, td, nb, k0, k1):
    """Кандидаты k0 ≤ k < k1 проверки счёта: (номера простых по первым nb базам — как у платы, сколько отсеют
    делители меньше td). Решето по малым простым, на уцелевших — Миллер — Рабин теми же базами."""
    n = k1 - k0
    if n <= 0:
        return (), 0
    base = det_value(bits, det, k0)             # x = base + 2i, i = k − k0
    hit_td, hit = bytearray(n), bytearray(n)
    for p in odd_primes_below(max(td, 2000)):
        i = (-base * pow(2, -1, p)) % p          # base + 2i ≡ 0 (mod p)
        if i >= n:
            continue
        cnt = len(range(i, n, p))
        hit[i::p] = b"\x01" * cnt
        if p < td:
            hit_td[i::p] = b"\x01" * cnt
    bases = BASES[:nb]
    primes = tuple(k0 + i for i in range(n) if not hit[i] and _strong(base + 2 * i, bases))
    return primes, sum(hit_td)


def det_check(bits, det, td, nb, k0, k1, pr, tdr, logged=None, lost=0):
    """Сверка проверки счёта за окно k0…k1: det_dpr — простых у платы минус ожидание, det_dtd — то же для отсева
    (оба ровно 0 при верном счёте). logged — номера k простых из журнала: det_miss — ожидаемых нет в журнале
    сверх потерянных им (lost), det_extra — в журнале, но не простые по ожиданию."""
    primes, td_rej = det_expected(bits, det, td, nb, k0, k1)
    out = {"det_dpr": pr - len(primes), "det_n": len(primes)}
    if tdr is not None and td:
        out["det_dtd"] = tdr - td_rej
    if logged is not None:
        exp = set(primes)
        got = {k for k in logged if k0 <= k < k1}
        out["det_miss"] = max(0, len(exp - got) - max(lost, 0))
        out["det_extra"] = len(got - exp)
    return out


if __name__ == "__main__":
    for xf, words in tz_selftest_vectors().items():
        print(f"xf={xf}: " + ", ".join(f"0x{w:08X}" for w in words))
