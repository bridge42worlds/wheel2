# -*- coding: utf-8 -*-
"""Захват лога с последовательного порта агента в файл.

    python serial_capture.py COM23 40 logs/x.log           просто слушать
    python serial_capture.py COM23 70 logs/x.log --reset   перезапустить плату и слушать с первой строки

Порт открывается без дёргания DTR/RTS, чтобы не сбросить плату и не
перевести её в режим загрузчика. При --reset даётся импульс RTS — штатный
перезапуск в рабочую прошивку. Порт ESP32-S3 при перезапуске пропадает
и появляется снова — захват переподключается сам.
Каждая строка пишется с меткой времени компьютера; служебные строки захвата
начинаются с '#'.
"""
import sys, time, os
import serial


def _open(port, baud):
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def capture(port, seconds, out_path, baud=115200, reset=False):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    t_end = time.time() + seconds
    ser, need_reset, n = None, reset, 0
    with open(out_path, "w", encoding="utf-8") as f:
        def note(text):
            f.write(f"{time.time():.3f} #{text}\n")
        buf = b""
        while time.time() < t_end:
            if ser is None:
                try:
                    ser = _open(port, baud)
                except (serial.SerialException, OSError):
                    time.sleep(0.2)
                    continue
                note("PORT open")
                if need_reset:
                    # Драйвер usbser.sys в Windows не отправляет изменение одной RTS,
                    # пока не тронуть DTR тем же запросом — поэтому повторно ставим DTR,
                    # как это делает esptool. RTS=1 при DTR=0 — сброс в рабочую прошивку.
                    ser.rts = True
                    ser.dtr = ser.dtr
                    time.sleep(0.2)
                    ser.rts = False
                    ser.dtr = ser.dtr
                    need_reset = False
                    note("RESET pulse")
            try:
                buf += ser.read(512)
            except (serial.SerialException, OSError):
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                note("PORT lost")
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                f.write(f"{time.time():.3f} {line.decode('utf-8', 'replace').rstrip()}\n")
                n += 1
        if ser:
            ser.close()
    return n


if __name__ == "__main__":
    port, seconds, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    n = capture(port, seconds, out, reset="--reset" in sys.argv)
    print(f"записано строк: {n} -> {out}")
