# -*- coding: utf-8 -*-
"""Ролики о «Колесо-2» для YouTube: разобрать видео, озвучить текст, собрать (docs: media/ не в git).

    python tools/video.py info   <ролик>          длительность, кадр, когда снято (для сверки с журналом сервера)
    python tools/video.py frames <ролик> [сек]    кадры раз в N с (по умолчанию 2) и на сменах сцены + листы 3×3
                                                  с отметкой времени: media/work/<имя>/sheet_NN.jpg
    python tools/video.py hear   <ролик> [turbo]  речь в ролике → текст с таймкодами (Whisper из C:\\CL\\va):
                                                  media/work/<имя>/speech.txt
    python tools/video.py voice  <ролик>          media/work/<имя>/script_ru.txt → озвучка по репликам (Vosk-TTS,
                                                  голос 4); проверка, что каждая реплика успевает до следующей
    python tools/video.py build  <ролик>          ролик с озвучкой (исходный звук тише и приглушается под голосом) +
                                                  субтитры ru/en (.srt) → media/out/<имя>_ru.mp4, <имя>.ru.srt, <имя>.en.srt

Текст — media/work/<имя>/script_ru.txt (и script_en.txt для английских субтитров, те же времена):
    0:00.0 | Это колесо номер один.
    0:06.5 | На ободе — восемь плат и-эс-пэ тридцать два эс три.
Строки с # — комментарии. Латиницу и цифры Vosk не читает: писать словами («и-эс-пэ», «сорок пять»);
цифры, оставшиеся в тексте, переводятся в слова сами (num2words), латиница — ошибка.
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA = os.path.join(ROOT, "media")
FFMPEG = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\tools\ffmpeg\bin\ffprobe.exe"
VA_PY = r"C:\CL\va\.venv\Scripts\python.exe"           # окружение озвучки: Vosk-TTS, Whisper, numpy, scipy
HELPER = os.path.join(ROOT, "tools", "video_voice.py")
SR = 48000
sys.stdout.reconfigure(encoding="utf-8")


def work_dir(clip):
    d = os.path.join(MEDIA, "work", os.path.splitext(os.path.basename(clip))[0])
    os.makedirs(d, exist_ok=True)
    return d


def ts(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def probe(clip):
    """Длительность, размер кадра, частота, есть ли звук, время съёмки (метаданные телефона)."""
    if os.path.exists(FFPROBE):
        r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", clip],
                           capture_output=True, text=True, encoding="utf-8")
        j = json.loads(r.stdout or "{}")
        v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
        a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        return {"dur": float(j.get("format", {}).get("duration") or 0), "w": v.get("width"), "h": v.get("height"),
                "fps": round(float(num) / float(den or 1), 2) if float(den or 1) else 0, "codec": v.get("codec_name"),
                "audio": bool(a), "created": (j.get("format", {}).get("tags") or {}).get("creation_time"),
                "rotate": (v.get("tags") or {}).get("rotate")}
    import cv2                                              # без ffprobe — что знает OpenCV
    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    return {"dur": n / fps if fps else 0, "w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(fps, 2), "codec": None, "audio": None, "created": None, "rotate": None}


def cmd_info(clip):
    p = probe(clip)
    print(f"{os.path.basename(clip)}: {ts(p['dur'])} ({p['dur']:.1f} с), {p['w']}×{p['h']} {p['fps']} к/с, "
          f"{p['codec'] or '?'}, звук {'есть' if p['audio'] else 'нет' if p['audio'] is not None else '?'}"
          f"{', снято ' + p['created'] if p['created'] else ''}{', поворот ' + str(p['rotate']) if p['rotate'] else ''}")


def cmd_frames(clip, every=2.0):
    """Кадры раз в every с и на сменах сцены (разница гистограмм) → листы 3×3 по 9 кадров с отметкой времени."""
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    d = os.path.join(work_dir(clip), "frames")
    os.makedirs(d, exist_ok=True)
    cap = cv2.VideoCapture(clip)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    picks, prev_hist, last_t, i = [], None, -1e9, 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        t = i / fps
        i += 1
        if i % max(1, int(fps / 5)):                         # смотреть 5 раз в секунду
            continue
        ok, img = cap.retrieve()
        if not ok:
            break
        small = cv2.resize(img, (160, 90))
        hist = cv2.calcHist([cv2.cvtColor(small, cv2.COLOR_BGR2HSV)], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        scene = prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > 0.35
        prev_hist = hist
        if t - last_t >= every or (scene and t - last_t >= 0.6):
            last_t = t
            h, w = img.shape[:2]
            img = cv2.resize(img, (960, int(960 * h / w)))
            p = os.path.join(d, f"f_{len(picks):04d}_{t:07.2f}.jpg")
            cv2.imwrite(p, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            picks.append((t, p, scene))
    font = None
    for f in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(f):
            font = ImageFont.truetype(f, 28)
            break
    sheets = []
    for k in range(0, len(picks), 9):
        group = picks[k:k + 9]
        tiles = [Image.open(p).convert("RGB") for _, p, _ in group]
        tw, th = 640, int(640 * tiles[0].height / tiles[0].width)
        sheet = Image.new("RGB", (tw * 3, th * 3), (20, 20, 20))
        for j, ((t, _, scene), im) in enumerate(zip(group, tiles)):
            im = im.resize((tw, th))
            dr = ImageDraw.Draw(im)
            label = ts(t) + (" *" if scene else "")
            dr.rectangle([0, 0, 150, 38], fill=(0, 0, 0))
            dr.text((8, 4), label, fill=(255, 255, 0), font=font)
            sheet.paste(im, ((j % 3) * tw, (j // 3) * th))
        p = os.path.join(work_dir(clip), f"sheet_{k // 9 + 1:02d}.jpg")
        sheet.save(p, quality=82)
        sheets.append(p)
    print(f"кадров {len(picks)} (смен сцены {sum(1 for x in picks if x[2])}), листов {len(sheets)}: {os.path.dirname(sheets[0]) if sheets else d}")
    for s in sheets:
        print("  ", s)


def read_script(path):
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#") or "|" not in ln:
            continue
        t, _, text = ln.partition("|")
        m = re.fullmatch(r"(?:(\d+):)?(\d+(?:[.,]\d+)?)", t.strip())
        if not m:
            raise SystemExit(f"{path}: время «{t.strip()}» — нужно м:сс.с")
        out.append((int(m.group(1) or 0) * 60 + float(m.group(2).replace(",", ".")), text.strip()))
    return sorted(out)


def helper(*args):
    r = subprocess.run([VA_PY, HELPER, *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise SystemExit("озвучка: " + (r.stderr.strip().splitlines() or ["ошибка"])[-1])
    return r.stdout


def cmd_hear(clip, model="small"):
    wd = work_dir(clip)
    wav = os.path.join(wd, "orig.wav")
    if not os.path.exists(FFMPEG):
        raise SystemExit(f"нет {FFMPEG}")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", clip, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    out = os.path.join(wd, "speech.txt")
    print(helper("hear", wav, out, "large-v3-turbo" if model == "turbo" else "small"), end="")


def cmd_voice(clip):
    wd = work_dir(clip)
    lines = read_script(os.path.join(wd, "script_ru.txt"))
    dur = probe(clip)["dur"]
    spec = [{"t": t, "text": x, "until": (lines[k + 1][0] if k + 1 < len(lines) else dur)} for k, (t, x) in enumerate(lines)]
    jp = os.path.join(wd, "voice.json")
    json.dump({"lines": spec, "dur": dur, "sr": SR, "out": os.path.join(wd, "narration.wav")}, open(jp, "w", encoding="utf-8"),
              ensure_ascii=False)
    print(helper("voice", jp), end="")


def srt(lines, dur, path, lens=None):
    def st(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for k, (t, x) in enumerate(lines):
            nxt = lines[k + 1][0] if k + 1 < len(lines) else dur
            end = min(nxt, t + (lens[k] + 0.4 if lens and k < len(lens) else max(2.0, len(x) / 14)))
            f.write(f"{k + 1}\n{st(t)} --> {st(max(end, t + 1.0))}\n{x}\n\n")


def cmd_build(clip):
    wd = work_dir(clip)
    name = os.path.splitext(os.path.basename(clip))[0]
    out = os.path.join(MEDIA, "out")
    os.makedirs(out, exist_ok=True)
    narr = os.path.join(wd, "narration.wav")
    if not os.path.exists(narr):
        raise SystemExit("нет озвучки — сначала voice")
    p = probe(clip)
    meta = json.load(open(os.path.join(wd, "voice_out.json"), encoding="utf-8"))
    ru = read_script(os.path.join(wd, "script_ru.txt"))
    srt(ru, p["dur"], os.path.join(out, f"{name}.ru.srt"), meta.get("lens"))
    en_path = os.path.join(wd, "script_en.txt")
    if os.path.exists(en_path):
        en = read_script(en_path)
        if [round(t, 1) for t, _ in en] != [round(t, 1) for t, _ in ru]:
            print("◐ у английских субтитров другие времена, чем у русского текста — беру свои")
        srt(en, p["dur"], os.path.join(out, f"{name}.en.srt"), meta.get("lens") if len(en) == len(ru) else None)
    mp4 = os.path.join(out, f"{name}_ru.mp4")
    if p["audio"]:                                          # исходный звук тише и приглушается под голосом
        fc = ("[1:a]aresample=48000,asplit=2[n1][n2];[0:a]aresample=48000,volume=0.35[bg];"
              "[bg][n1]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=400[duck];"
              "[duck][n2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
        cmd = [FFMPEG, "-v", "error", "-y", "-i", clip, "-i", narr, "-filter_complex", fc, "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd = [FFMPEG, "-v", "error", "-y", "-i", clip, "-i", narr, "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", mp4]
    subprocess.run(cmd, check=True)
    print(f"✓ {mp4}")
    for lang in ("ru", "en"):
        s = os.path.join(out, f"{name}.{lang}.srt")
        if os.path.exists(s):
            print(f"✓ {s}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    cmd, clip = sys.argv[1], sys.argv[2]
    if not os.path.exists(clip):
        alt = os.path.join(MEDIA, "raw", clip)
        if not os.path.exists(alt):
            raise SystemExit(f"нет файла {clip}")
        clip = alt
    rest = sys.argv[3:]
    {"info": lambda: cmd_info(clip), "frames": lambda: cmd_frames(clip, float(rest[0]) if rest else 2.0),
     "hear": lambda: cmd_hear(clip, rest[0] if rest else "small"), "voice": lambda: cmd_voice(clip),
     "build": lambda: cmd_build(clip)}.get(cmd, lambda: print(__doc__))()


if __name__ == "__main__":
    main()
