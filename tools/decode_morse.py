# -*- coding: utf-8 -*-
"""Расшифровка вспышек из лога агента — только по длительностям света.

    python decode_morse.py logs/x.log [единица_мс]

Ничего не знает о том, что агент «хотел» передать: смотрит на строки
"T <millis> <0|1>" и классифицирует каждую вспышку по длительности.

Нотация:
    1 — точка, 2 — тире, 0 — граница знаков, пробел — граница слов,
    (*****)     — маркер из быстрых вспышек,
    (* * * * *) — маркер из вспышек с увеличенным интервалом.
"""
import sys, re

DECODE = {v: k for k, v in {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
    'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
    'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
    'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '?': '..--..'}.items()}


def flashes(path):
    """Список (длительность вспышки, пауза после или None), мс."""
    tr = [(int(m.group(1)), int(m.group(2)))
          for m in (re.search(r' T (\d+) ([01])$', ln) for ln in open(path, encoding="utf-8")) if m]
    out = []
    for k in range(len(tr) - 1):
        (t0, s0), (t1, s1) = tr[k], tr[k + 1]
        if s0 == 1 and s1 == 0:
            gap = tr[k + 2][0] - t1 if k + 2 < len(tr) else None
            out.append((t1 - t0, gap))
    return out


def decode(path, unit=250):
    stream, text, letter, burst = "", "", "", []

    def end_letter():
        nonlocal letter, text
        if letter:
            text += DECODE.get(letter, "?")
            letter = ""

    for on, gap in flashes(path):
        if on < 0.7 * unit:                           # вспышка маркера
            burst.append(gap)
            if gap is None or gap >= 3 * unit:        # маркер закончился
                inner = [g for g in burst[:-1] if g is not None]
                slow = bool(inner) and max(inner) >= 1.2 * unit
                n = len(burst)
                stream += ("(" + " ".join("*" * n) + ")") if slow else ("(" + "*" * n + ")")
                text += f"[{n} медл.]" if slow else f"[{n} быстр.]"
                burst = []
                if gap is not None:
                    stream += " " if gap < 10 * unit else "\n"
                    text += " " if gap < 10 * unit else "\n"
            continue
        el = "1" if on < 2 * unit else "2"
        stream += el
        letter += "." if el == "1" else "-"
        if gap is None:
            break
        u = gap / unit
        if u < 2:
            continue
        end_letter()
        if u < 5:
            stream += "0"
        elif u < 10:
            stream += " "
            text += " "
        else:
            stream += "\n"
            text += "\n"
    end_letter()
    return stream, text


if __name__ == "__main__":
    unit = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    s, t = decode(sys.argv[1], unit)
    print("ПОТОК (1 точка, 2 тире, 0 граница знаков, пробел граница слов, (*) маркеры):")
    print(s)
    print("ТЕКСТ:")
    print(t)
