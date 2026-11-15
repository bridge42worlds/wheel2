"""Матрица шумов и длин (план блок 1а): по одному опыту phase_run на ячейку — источник шума × длина числа,
поле ∿ обеими катушками по очереди блоками ABAB, направление вращения чередуется между ячейками.

    python tools/noise_matrix.py                       проход 1: каждый источник один, 64 бит, 8 мин на ячейку
    python tools/noise_matrix.py --leave-one-out       проход 2: смесь всех минус один
    python tools/noise_matrix.py --bits 64 128 256 512 --src 128 1   проход 3: длины на выбранных источниках
    python tools/noise_matrix.py --min 8 --blk 2 --seg 60 --dry

Источники (маска config.h): 1 ГСЧ, 8 дрожание, 128 регистр ГСЧ, 2 CSI (пинг роутера 100/с), 4 АЦП, 16 эфир B (все
рассеивают: ss=1), 32 акселерометр, 64 магнитометр (только у плат с датчиками — у остальных вместо датчика ГСЧ).
Без хеша (raw=1) там, где источник даёт слова/байты целиком; с датчиками и CSI — через SHA-256 (raw=0).
Порядок ячеек — случайный (зерно пишется), журнал — data/series/noise_matrix_<дата>.json: ячейка → номер программы.
Разбор: python tools/phase_report.py <номера программ из журнала>.
"""
import argparse, json, os, random, subprocess, sys, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ap = argparse.ArgumentParser()
ap.add_argument("--src", type=int, nargs="*", default=None, help="источники; по умолчанию все одиночные")
ap.add_argument("--bits", type=int, nargs="*", default=[64])
ap.add_argument("--leave-one-out", action="store_true", help="смесь всех минус один вместо одиночных")
ap.add_argument("--min", type=float, default=8)
ap.add_argument("--blk", type=float, default=1)
ap.add_argument("--seg", type=int, default=30)
ap.add_argument("--seed", type=int, default=None)
ap.add_argument("--dry", action="store_true")
A = ap.parse_args()

SINGLE = [128, 1, 8, 2, 4, 16, 32, 64]
ALL = sum(SINGLE)
RAW_OK = {128, 1, 8, 16}                             # слова/байты целиком — без хеша; датчики и CSI — через SHA
if A.leave_one_out:
    cells = [(ALL ^ s, b) for s in SINGLE for b in A.bits]
else:
    cells = [(s, b) for s in (A.src or SINGLE) for b in A.bits]
seed = A.seed if A.seed is not None else int(time.time()) % 100000
random.Random(seed).shuffle(cells)
log_path = os.path.join(ROOT, "data", "series", f"noise_matrix_{time.strftime('%Y-%m-%d')}.json")
log = json.load(open(log_path, encoding="utf-8")) if os.path.exists(log_path) else []
print(f"ячеек {len(cells)} · зерно {seed} · ≈ {len(cells) * (A.min + 4):.0f} мин")
for i, (src, bits) in enumerate(cells):
    pair = "opposite" if i % 2 == 0 else "same"
    raw = 1 if src in RAW_OK else 0
    ss = src & 16 != 0
    args = [sys.executable, "-u", os.path.join(ROOT, "tools", "phase_run.py"), "--all", "--bits", str(bits), "--src", str(src),
            "--raw", str(raw), "--min", str(A.min), "--blk", str(A.blk), "--seg", str(A.seg), "--freqs", "rot", "--base", "0",
            "--pair", pair] + (["--ss"] if ss else []) + (["--dry"] if A.dry else [])
    print(f"\n[{i + 1}/{len(cells)}] src {src} · {bits} бит · raw {raw}{' · ss' if ss else ''} · {pair}", flush=True)
    t0 = time.time()
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = out.stdout + out.stderr
    for line in text.splitlines():
        if "SSID" not in line.upper():
            print("   " + line)
    pid = None
    for line in text.splitlines():
        if line.startswith("программа №"):
            pid = int(line.split("№")[1].split()[0].rstrip(":"))
    rec = {"t0": t0, "t1": time.time(), "src": src, "bits": bits, "raw": raw, "ss": ss, "pair": pair, "pid": pid, "seed": seed,
           "ok": out.returncode == 0 and pid is not None}
    log.append(rec)
    if not A.dry:
        json.dump(log, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not rec["ok"] and not A.dry:
        print("✗ ячейка не прошла — стоп матрицы (проверьте установку и перезапустите, пройденные ячейки в журнале)")
        break
print("\nразбор:", "python tools/phase_report.py " + " ".join(str(r["pid"]) for r in log if r.get("pid")))
