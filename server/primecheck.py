# -*- coding: utf-8 -*-
"""Проверка простых Миллером — Рабином: сама проверка и процессы-помощники для сервера.

Сервер проверяет каждое число из журнала простых (пакеты prm) — ~1,5 мс на 256-битное. Двенадцать плат
дают ~400 простых/с, а потоки Python делят одно ядро: проверка не успевала, очередь росла (24.10.2026,
два колеса по 6 плат), и разобранный с опозданием журнал сбивал «последний пакет» — планшетка показывала
«нет связи» у работающих плат. Поэтому живые числа проверяют процессы-помощники (Pool): каждый —
python primecheck.py --worker, читает числа (hex) построчно и отвечает строками 1/0; их несколько — по ядрам.
"""
import os
import random
import subprocess
import sys
import threading

BASES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]
SMALL_PRIMES = [p for p in range(3, 1000) if all(p % q for q in range(2, int(p ** 0.5) + 1))]


def is_probable_prime(n, quick=False):
    """Миллер — Рабин: 12 известных баз и 8 случайных. quick — одна база (при перечитывании
    сырья на старте: эти простые уже проверены живьём, ответ записан в data/primes; полная
    проверка стоила 2/3 времени перечитывания)."""
    if n < 2 or n % 2 == 0:
        return n == 2
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    if quick:
        bases = [2]
    else:
        bases = BASES + [random.randrange(2, n - 1) for _ in range(8)] if n > 40 else BASES
    for a in bases:
        a %= n
        if a < 2:
            continue
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


class Pool:
    """Помощники проверки: check([числа]) → [True/False] в том же порядке, числа делятся между процессами.
    Помощник упал — исключение (сервер проверит пачку сам), при следующей пачке помощники запускаются заново.
    Сервер завершился — у помощников кончается ввод, они выходят сами."""

    CHUNK = 500        # чисел на помощника за раз: ответы (2 байта) не переполнят канал, пока мы ещё пишем

    def __init__(self, n=None, log=print):
        self.n = n or max(1, min(6, (os.cpu_count() or 2) - 2))
        self.log = log
        self.lock = threading.Lock()
        self.procs = []

    def _start(self):
        self._stop()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                       encoding="ascii", creationflags=flags) for _ in range(self.n)]
        self.log(f"[prm] помощники проверки простых: {self.n} процессов")

    def _stop(self):
        for p in self.procs:
            try:
                p.kill()
            except Exception:                         # noqa: BLE001
                pass
        self.procs = []

    def check(self, nums):
        with self.lock:
            if not self.procs or any(p.poll() is not None for p in self.procs):
                self._start()
            try:
                return self._check(nums)
            except Exception:
                self._stop()
                raise

    def _check(self, nums):
        res, k = [], len(self.procs)
        for at in range(0, len(nums), k * self.CHUNK):
            part = nums[at:at + k * self.CHUNK]
            shares = [part[i::k] for i in range(k)]
            for p, sh in zip(self.procs, shares):
                if sh:
                    p.stdin.write("".join(f"{n:x}\n" for n in sh))
                    p.stdin.flush()
            got = [[] for _ in range(k)]
            for i, (p, sh) in enumerate(zip(self.procs, shares)):
                for _ in sh:
                    line = p.stdout.readline()
                    if not line:
                        raise RuntimeError("помощник проверки простых завершился")
                    got[i].append(line.strip() == "1")
            res += [got[j % k][j // k] for j in range(len(part))]
        return res


def _worker():
    for line in sys.stdin:
        try:
            ok = is_probable_prime(int(line, 16))
        except ValueError:
            ok = False
        sys.stdout.write("1\n" if ok else "0\n")
        sys.stdout.flush()


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker()
