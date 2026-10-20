# -*- coding: utf-8 -*-
"""Проверка тайминга морзянки по логу агента.

    python check_morse.py logs/morse.log

Строит эталонную последовательность «вспышка — пауза» для сообщения из
строки BOOT и сверяет с ней каждое переключение светодиода из строк T.
"""
import sys, re

MORSE = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
    'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
    'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
    'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..',
}


def expected(msg, repeat_gap=14):
    """Список (вспышка, пауза_после) в единицах для одного прохода сообщения."""
    out = []
    words = msg.split(' ')
    for wi, word in enumerate(words):
        for li, ch in enumerate(word):
            code = MORSE[ch]
            for ei, el in enumerate(code):
                on = 3 if el == '-' else 1
                if ei < len(code) - 1:
                    gap = 1
                elif li < len(word) - 1:
                    gap = 3
                elif wi < len(words) - 1:
                    gap = 7
                else:
                    gap = repeat_gap
                out.append((on, gap, ch))
    return out


def main(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    msg, unit, rep = "HELLO WORLD", 250, 14
    for ln in lines:
        m = re.search(r'BOOT .*unit=(\d+) msg="([^"]+)"', ln)
        if m:
            unit, msg = int(m.group(1)), m.group(2)
        m = re.search(r'BOOT .*rep=(\d+)', ln)
        if m:
            rep = int(m.group(1))
    trans = []
    for ln in lines:
        m = re.search(r' T (\d+) ([01])$', ln)
        if m:
            trans.append((int(m.group(1)), int(m.group(2))))
    starts = [i for i, ln in enumerate(lines) if " START " in ln]
    if not trans:
        raise SystemExit("в логе нет переключений светодиода")

    ref = expected(msg, rep)
    # найти первую вспышку, с которой начинается полный проход сообщения
    first_on = None
    for s in starts:
        t_start = int(lines[s].split()[-1])
        for k, (t, st) in enumerate(trans):
            if st == 1 and t >= t_start:
                first_on = k
                break
        if first_on is not None and len(trans) - first_on >= 2 * len(ref):
            break
    if first_on is None:
        raise SystemExit("не найдено начало полного прохода сообщения (нет START)")

    seq = trans[first_on:first_on + 2 * len(ref) + 1]
    if len(seq) < 2 * len(ref) + 1:
        raise SystemExit(f"проход неполный: {len(seq)} переключений из {2*len(ref)+1}")

    print(f"сообщение: {msg}   единица: {unit} мс   вспышек в проходе: {len(ref)}\n")
    print(f"{'#':>3} {'буква':5} {'вспышка, мс':>12} {'ожид.':>6} {'пауза, мс':>10} {'ожид.':>6}")
    worst = 0
    bad = 0
    for i, (on_u, gap_u, ch) in enumerate(ref):
        t_on, s1 = seq[2 * i]
        t_off, s2 = seq[2 * i + 1]
        t_next, _ = seq[2 * i + 2]
        if (s1, s2) != (1, 0):
            raise SystemExit(f"нарушен порядок переключений на вспышке {i}")
        d_on, d_gap = t_off - t_on, t_next - t_off
        e_on, e_gap = on_u * unit, gap_u * unit
        err = max(abs(d_on - e_on), abs(d_gap - e_gap))
        worst = max(worst, err)
        mark = "" if err <= 5 else "  <<<"
        bad += err > 5
        print(f"{i:3d} {ch:5} {d_on:12d} {e_on:6d} {d_gap:10d} {e_gap:6d}{mark}")
    total = seq[-1][0] - seq[0][0]
    exp_total = sum(on + gap for on, gap, _ in ref) * unit
    print(f"\nдлительность прохода: {total} мс, ожидалось {exp_total} мс")
    print(f"наибольшее отклонение: {worst} мс;  вне допуска ±5 мс: {bad}")
    print("ИТОГ:", "ТАЙМИНГ ВЕРНЫЙ" if bad == 0 else "ЕСТЬ ОТКЛОНЕНИЯ")


if __name__ == "__main__":
    main(sys.argv[1])
