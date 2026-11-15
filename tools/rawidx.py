"""Чтение сырых отсчётов статики (data/accel/<день>/<mac>.bin + .idx.jsonl) — общее для всех инструментов.

09.11.2026: поле i0 в индексе — номер отсчёта у самого датчика: при перезапуске датчика он сбрасывается, при потере
пакетов перескакивает. Место отсчётов в файле .bin — сумма n всех предыдущих строк индекса (сервер дописывает .bin и
строку индекса одну за другой, обе с flush). Читать по i0 нельзя: 08.11 после перезапуска датчика 16:17 чтение по i0
попадало в отсчёты ~05:00 утра.
"""
import json
import os
import time

import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def day_base(mac, t=None):
    """Файлы дня (по местному времени t) для датчика mac (без двоеточий)."""
    return os.path.join(ROOT, "data", "accel", time.strftime("%Y-%m-%d", time.localtime(t or time.time())), mac.replace(":", ""))


class RawTail:
    """Индекс дня в памяти: (rx, место в файле, n, hz) по каждой строке; новые строки дочитываются по update()."""

    def __init__(self, mac, t=None, keep_s=None, base=None):
        self.mac = mac.replace(":", "")
        self.base = base or day_base(self.mac, t)
        self.pos, self.off, self.keep_s = 0, 0, keep_s
        self.rx, self.o, self.n, self.hz = [], [], [], []
        self.lsb = 16000.0
        self.update()

    def update(self):
        """Дочитать новые строки индекса. Возвращает число новых пакетов."""
        p_idx = self.base + ".idx.jsonl"
        if not os.path.exists(p_idx):
            return 0
        with open(p_idx, "rb") as f:
            f.seek(self.pos)
            chunk = f.read()
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return 0
        self.pos += cut + 1
        k = 0
        for ln in chunk[:cut].split(b"\n"):
            try:
                p = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            n = int(p.get("n") or 0)
            self.rx.append(float(p["rx"]))
            self.o.append(self.off)
            self.n.append(n)
            self.hz.append(float(p.get("hz") or 500.0) or 500.0)
            self.lsb = p.get("lsb") or self.lsb
            self.off += n
            k += 1
        if self.keep_s and self.rx and self.rx[-1] - self.rx[0] > 2 * self.keep_s:   # долгий хвост — держим последние keep_s
            cut_t = self.rx[-1] - self.keep_s
            i = next(j for j, v in enumerate(self.rx) if v >= cut_t)
            self.rx, self.o, self.n, self.hz = self.rx[i:], self.o[i:], self.n[i:], self.hz[i:]
        return k

    def window(self, a, b, pad=1.0):
        """Отсчёты со временем в [a, b): (t, x) — x int16 → float, оси датчика; время отсчёта — приход пакета минус хвост."""
        if not self.rx:
            return None, None
        rx = np.asarray(self.rx)
        sel = np.where((rx >= a - pad) & (rx <= b + pad))[0]
        if not len(sel):
            return None, None
        ts, xs = [], []
        with open(self.base + ".bin", "rb") as fb:
            for i in sel:
                fb.seek(self.o[i] * 6)
                buf = fb.read(self.n[i] * 6)
                if len(buf) < self.n[i] * 6:
                    continue
                k = np.arange(self.n[i])
                ts.append(self.rx[i] - (self.n[i] - 1 - k) / self.hz[i])
                xs.append(np.frombuffer(buf, dtype="<i2").reshape(-1, 3).astype(np.float64))
        if not ts:
            return None, None
        t, x = np.concatenate(ts), np.concatenate(xs)
        m = (t >= a) & (t < b)
        return t[m], x[m]


def load(mac, a, b):
    """Разовое чтение отрезка [a, b) (может переходить через полночь — читает оба дня)."""
    out_t, out_x = [], []
    for day_t in sorted({a, b}):
        rt = RawTail(mac, day_t)
        t, x = rt.window(a, b)
        if t is not None:
            out_t.append(t)
            out_x.append(x)
    if not out_t:
        return None, None
    t, x = np.concatenate(out_t), np.concatenate(out_x)
    o = np.argsort(t, kind="stable")
    return t[o], x[o]
