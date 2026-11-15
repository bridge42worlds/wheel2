# -*- coding: utf-8 -*-
"""Большой ролик из сценария: кадры + голос Остова (Silero) + русский текст на экране + английские титры.
Запускать окружением озвучки:  C:\\CL\\va\\.venv\\Scripts\\python.exe tools/film.py <сценарий.txt> [voice|build|all] [--only N-M]

Сценарий — простой текст, такты через пустую строку:
    ## Фаза 1 · лето 2026 | Первое колесо        ← глава: метка | заголовок на плашке (смена главы — затемнение)
    :: phone spin1 12                            ← кадр: phone <видео> <с какой секунды>
                                                    pic <картинка> [still|pan]   (слева картинка, справа плашка)
                                                    full <картинка> [still|pan]  (на весь кадр, проезд сверху вниз)
                                                    … pan <y0> <y1> — проезд от y0 до y1 (пиксели картинки в кадре)
                                                    card                         (знак Остова крупно)
    Текст голоса. +Ударение плюсом, числа лучше словами.
    >> English subtitle.
    [] Текст на плашке (необязательно).
    @@ docs/wiring.md                            ← указатель на документацию внизу плашки (необязательно)
Кадр без строки «::» продолжает предыдущий (видео — дальше по времени). Строки с «#» и пустые — разделители.
Имена картинок и видео — ключи SOURCES или файлы из папки assets рядом со сценарием.
"""
import json, os, re, subprocess, sys
import numpy as np, soundfile as sf
from PIL import Image, ImageDraw, ImageFont
sys.stdout.reconfigure(encoding="utf-8")

FF = r"C:\tools\ffmpeg\bin\ffmpeg.exe"
VA = r"C:\CL\va"
W, H, SR, FPS = 1920, 1080, 48000, 30
LEAD, TAIL = 0.15, 0.30                     # тишина до и после реплики
TEMPO = 1.5                                 # темп голоса (ffmpeg atempo, высота тона не меняется)
AMB = 0.10                                  # родной звук видео под голосом
LINK = "github.com/bridge42worlds/wheel2"
SOURCES = {
    "rig": r"C:\wheel2\media\raw\rig_2026-11-15.mp4",
    "spin1": r"C:\server\1_ОТЧЁТ\media\wheel_spin.mp4",
    "spin2": r"C:\server\1_ОТЧЁТ\media\wheel_spin_2.mp4",
    "screen": r"C:\wheel2\media\work\phase3\film\rec\screen_all.mp4",
    "p1_full": r"C:\server\1_ОТЧЁТ\media\wheel_full.jpg",
    "p1_laptop": r"C:\server\1_ОТЧЁТ\media\wheel_laptop.jpg",
    "p1_node1": r"C:\server\1_ОТЧЁТ\media\node_inside_1.jpg",
    "p1_node2": r"C:\server\1_ОТЧЁТ\media\node_inside_2.jpg",
}
FIG1 = r"C:\server\1_ОТЧЁТ\figures"
SKY, ORANGE, INK, MUTED = (86, 180, 233), (230, 159, 0), (240, 244, 248), (160, 176, 192)
BG = (14, 19, 26)
F = lambda name, size: ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", name), size)


# ── сценарий ───────────────────────────────────────────────────────────────
def parse(path):
    beats, chap, vis = [], ("", ""), None
    block = []

    def flush():
        nonlocal vis
        if not block:
            return
        b = {"chap": chap, "ru": "", "en": "", "scr": "", "doc": "", "vis": None}
        for ln in block:
            if ln.startswith("::"):
                b["vis"] = ln[2:].split()
            elif ln.startswith(">>"):
                b["en"] = (b["en"] + " " + ln[2:].strip()).strip()
            elif ln.startswith("[]"):
                b["scr"] = ln[2:].strip()
            elif ln.startswith("@@"):
                b["doc"] = ln[2:].strip()
            else:
                b["ru"] = (b["ru"] + " " + ln.strip()).strip()
        if b["vis"] is None:
            b["vis"], b["cont"] = list(vis), True
        vis = b["vis"]
        if b["ru"]:
            beats.append(b)
        block.clear()

    for raw in open(path, encoding="utf-8"):
        ln = raw.rstrip("\n")
        if ln.startswith("## "):
            flush()
            lab, _, title = ln[3:].partition("|")
            chap = (lab.strip(), title.strip())
        elif not ln.strip() or ln.lstrip().startswith("# "):
            flush()
        else:
            block.append(ln.strip())
    flush()
    for k, b in enumerate(beats):
        b["id"] = f"{k + 1:03d}"
        b["new_chap"] = k == 0 or beats[k - 1]["chap"] != b["chap"]
        b["end_chap"] = k == len(beats) - 1 or beats[k + 1]["chap"] != b["chap"]
    return beats


def say_text(ru):
    """Для голоса: числа словами (num2words), без «[]»; «+» оставляем — это ударение для Silero."""
    from num2words import num2words
    def num(m):
        s = m.group(0).replace(" ", "").replace(",", ".")
        try:
            return num2words(float(s) if "." in s else int(s), lang="ru")
        except Exception:
            return s
    t = re.sub(r"\d+(?:[.,]\d+)?", num, ru.replace("—", ","))
    if re.search(r"[A-Za-z]", t):
        raise SystemExit(f"латиница в голосе: «{ru[:60]}» — написать по-русски")
    return t


# ── голос ──────────────────────────────────────────────────────────────────
def voice(beats, wd, speaker="aidar"):
    """Голос по тактам; кэш по содержанию (voice/<хеш текста>.wav) — вставка тактов не пересинтезирует остальное."""
    import hashlib
    lens_p = os.path.join(wd, "lens.json")
    lens = json.load(open(lens_p, encoding="utf-8")) if os.path.exists(lens_p) else {}
    if lens and isinstance(next(iter(lens.values())), dict):         # старый формат: ключ — номер такта
        new = {}
        for bid, v in lens.items():
            txt = v["key"].split("|", 1)[1]
            vk = hashlib.md5(txt.encode("utf-8")).hexdigest()[:12]
            a, z = os.path.join(wd, "voice", bid + ".wav"), os.path.join(wd, "voice", vk + ".wav")
            if os.path.exists(a) and not os.path.exists(z):
                os.replace(a, z)
            new[vk] = v["len"]
        lens = new
        json.dump(lens, open(lens_p, "w", encoding="utf-8"), indent=0)
    m = None
    for b in beats:
        txt = say_text(b["ru"])
        b["vkey"] = hashlib.md5(txt.encode("utf-8")).hexdigest()[:12]
        p = os.path.join(wd, "voice", b["vkey"] + ".wav")
        if b["vkey"] in lens and os.path.exists(p):
            continue
        if m is None:
            import torch
            m = torch.package.PackageImporter(os.path.join(VA, "models", "silero", "v4_ru.pt")).load_pickle("tts_models", "model")
        parts = [x for x in re.split(r"(?<=[.!?…])\s+", txt) if x.strip()]   # Silero: по предложению — ровнее
        wav = []
        for x in parts:
            a = m.apply_tts(text=x, speaker=speaker, sample_rate=SR, put_accent=True, put_yo=True).numpy()
            wav += [a.astype(np.float32), np.zeros(int(0.22 * SR), np.float32)]
        w = np.concatenate(wav[:-1])
        w *= 0.89 / (np.max(np.abs(w)) or 1)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        sf.write(p, w, SR, subtype="PCM_16")
        lens[b["vkey"]] = round(len(w) / SR, 2)
        print(f"  голос {b['id']} {lens[b['vkey']]:5.1f} с  {b['ru'][:60]}")
        json.dump(lens, open(lens_p, "w", encoding="utf-8"), indent=0)
    tag = "" if TEMPO == 1 else f"_x{round(TEMPO * 100)}"            # ускоренная копия рядом с исходной
    for b in beats:
        b["vfile"] = os.path.join(wd, "voice", b["vkey"] + tag + ".wav")
        if tag and not (b["vkey"] + tag in lens and os.path.exists(b["vfile"])):
            subprocess.run([FF, "-v", "error", "-y", "-i", os.path.join(wd, "voice", b["vkey"] + ".wav"),
                            "-af", f"atempo={TEMPO}", "-ar", str(SR), b["vfile"]], check=True)
            lens[b["vkey"] + tag] = round(sf.info(b["vfile"]).duration, 2)
            json.dump(lens, open(lens_p, "w", encoding="utf-8"), indent=0)
    return {b["id"]: lens[b["vkey"] + tag] for b in beats}


# ── рисунок плашек ─────────────────────────────────────────────────────────
def emblem(dr, x, y, s=1.0):
    p = [(0, 0), (-26, -18), (-44, 8), (24, -22), (30, 14), (6, 30), (52, -4)]
    e = [(0, 1), (1, 2), (0, 3), (0, 4), (0, 5), (4, 6)]
    P = [(x + a * s, y + b * s) for a, b in p]
    for i, j in e:
        dr.line([P[i], P[j]], fill=MUTED, width=max(2, int(3 * s)))
    for k, (a, b) in enumerate(P):
        r = (8 if k == 0 else 6) * s
        dr.ellipse([a - r, b - r, a + r, b + r], fill=ORANGE if k == 0 else SKY)


def wrap(dr, text, font, width):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if dr.textlength(t, font=font) <= width or not cur:
            cur = t
        else:
            out.append(cur)
            cur = w
    return out + ([cur] if cur else [])


def subtitles(dr, en):
    if not en:
        return
    fs = F("segoeuib.ttf", 38)
    rows = wrap(dr, en, fs, 1560)
    hh = 50 * len(rows) + 22
    yb = H - 34 - hh
    wmax = max(dr.textlength(r, font=fs) for r in rows)
    dr.rounded_rectangle([(W - wmax) / 2 - 22, yb, (W + wmax) / 2 + 22, yb + hh], 14, fill=(10, 14, 20, 200))
    for k, r in enumerate(rows):
        dr.text(((W - dr.textlength(r, font=fs)) / 2, yb + 10 + 50 * k), r, font=fs, fill=(255, 255, 255))


def panel(dr, b, x0, x1, y0=60, y1=860, title_size=96):
    dr.rounded_rectangle([x0 - 44, y0, x1, y1], 28, fill=(10, 15, 22, 178))
    emblem(dr, x0 + 34, y0 + 72, 1.0)
    dr.text((x0 + 104, y0 + 56), "ОСТОВ · ИИ-АВТОР ОПЫТА", font=F("seguisb.ttf", 26), fill=SKY)
    lab, title = b["chap"]
    y = y0 + 120
    for r in wrap(dr, lab, F("segoeui.ttf", 34), x1 - x0 - 40):
        dr.text((x0, y), r, font=F("segoeui.ttf", 34), fill=MUTED)
        y += 44
    ft = F("bahnschrift.ttf", title_size)
    for r in wrap(dr, title, ft, x1 - x0 - 40):
        dr.text((x0, y), r, font=ft, fill=INK)
        y += int(title_size * 1.12)
    y += 14
    dr.rectangle([x0, y, x0 + 110, y + 6], fill=ORANGE)
    y += 34
    if b["scr"]:
        fs = F("segoeuisl.ttf", 44)
        for r in wrap(dr, b["scr"], fs, x1 - x0 - 50):
            dr.text((x0, y), r, font=fs, fill=INK)
            y += 58
    if b["doc"]:
        dr.text((x0, y1 - 150), "документация", font=F("segoeui.ttf", 28), fill=MUTED)
        dr.text((x0, y1 - 112), b["doc"], font=F("seguisb.ttf", 34), fill=ORANGE)
        dr.text((x0, y1 - 66), LINK, font=F("segoeui.ttf", 30), fill=SKY)
    else:
        dr.text((x0, y1 - 104), "код, схемы и журналы открыты", font=F("segoeui.ttf", 28), fill=MUTED)
        dr.text((x0, y1 - 66), LINK, font=F("seguisb.ttf", 34), fill=SKY)


def corner(dr, b):
    """Для кадров «на весь экран»: метка главы слева сверху и ссылка справа сверху."""
    lab, title = b["chap"]
    t = f"{lab} · {title}" if title else lab
    f = F("seguisb.ttf", 28)
    dr.rounded_rectangle([24, 22, 24 + 80 + dr.textlength(t, font=f) + 24, 84], 16, fill=(10, 15, 22, 200))
    emblem(dr, 62, 53, 0.55)
    dr.text((104, 34), t, font=f, fill=INK)
    s = (b["doc"] + " · " if b["doc"] else "") + LINK
    f2 = F("seguisb.ttf", 28)
    wl = dr.textlength(s, font=f2)
    dr.rounded_rectangle([W - 24 - wl - 44, 22, W - 24, 84], 16, fill=(10, 15, 22, 200))
    dr.text((W - 24 - wl - 22, 34), s, font=f2, fill=ORANGE if b["doc"] else SKY)


def overlay(b, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    kind = b["vis"][0]
    if kind == "phone":
        panel(dr, b, 790, W - 70)
    elif kind == "pic":
        panel(dr, b, 1340, W - 50, title_size=64)
    elif kind in ("full", "screen"):
        corner(dr, b)
    elif kind == "rain":
        if b["scr"]:
            ft = F("bahnschrift.ttf", 86)
            rows = wrap(dr, b["scr"], ft, 1500)
            hh = 104 * len(rows) + 150
            y0 = (H - hh) // 2 - 60
            wmax = max(dr.textlength(r, font=ft) for r in rows)
            dr.rounded_rectangle([(W - wmax) / 2 - 60, y0, (W + wmax) / 2 + 60, y0 + hh], 30, fill=(4, 8, 10, 205))
            emblem(dr, W / 2 - 8, y0 + 62, 1.2)
            for k, r in enumerate(rows):
                dr.text(((W - dr.textlength(r, font=ft)) / 2, y0 + 120 + 104 * k), r, font=ft, fill=INK)
    elif kind == "card":
        emblem(dr, 470, 470, 5.2)
        lab, title = b["chap"]
        dr.text((860, 300), lab, font=F("segoeui.ttf", 44), fill=MUTED)
        y = 360
        for r in wrap(dr, title, F("bahnschrift.ttf", 110), 960):
            dr.text((860, y), r, font=F("bahnschrift.ttf", 110), fill=INK)
            y += 124
        dr.rectangle([860, y + 16, 990, y + 23], fill=ORANGE)
        y += 60
        for r in wrap(dr, b["scr"], F("segoeuisl.ttf", 46), 960):
            dr.text((860, y), r, font=F("segoeuisl.ttf", 46), fill=INK)
            y += 60
        dr.text((860, 880), LINK, font=F("seguisb.ttf", 34), fill=SKY)
    subtitles(dr, b["en"])
    im.save(path)


# ── кадры ──────────────────────────────────────────────────────────────────
def src_path(name, wd):
    if name in SOURCES:
        return SOURCES[name]
    for d in (os.path.join(wd, "assets"), FIG1):
        for ext in ("", ".png", ".jpg"):
            p = os.path.join(d, name + ext)
            if os.path.exists(p):
                return p
    raise SystemExit(f"нет картинки/видео «{name}»")


_DUR = {}


def vdur(src):
    if src not in _DUR:
        r = subprocess.run([FF.replace("ffmpeg.exe", "ffprobe.exe"), "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", src], capture_output=True, text=True)
        _DUR[src] = float(r.stdout.strip() or 0)
    return _DUR[src]


_PRIMES = None


def rain(n, seed=1):
    """Кадры «цифрового дождя»: столбцы цифр падают, хвост гаснет; в части столбцов идут простые числа — оранжевым."""
    global _PRIMES
    rng = np.random.default_rng(seed)
    if _PRIMES is None:
        sv = np.ones(3_000_000, bool)
        sv[:2] = False
        for i in range(2, 1733):
            if sv[i]:
                sv[i * i::i] = False
        _PRIMES = np.flatnonzero(sv[1_000_000:]) + 1_000_000
    fnt = F("consola.ttf", 26)
    cw, rh = 24, 30
    levels = 14

    def glyph(ch, col):
        g = Image.new("RGBA", (cw, rh), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((3, 0), ch, font=fnt, fill=col)
        return g
    green = [glyph(c, (0, int(60 + 140 * (1 - k / levels)), int(45 + 100 * (1 - k / levels)), int(255 * (1 - k / levels) ** 1.3)))
             for k in range(levels) for c in "0123456789"]
    head = [glyph(c, (225, 255, 240, 255)) for c in "0123456789"]
    orange = [glyph(c, (230, 159, 0, int(255 * (1 - k / levels) ** 1.1))) for k in range(levels) for c in "0123456789"]
    cols = W // cw
    y = rng.uniform(-H, H, cols)
    v = rng.uniform(5, 14, cols)
    L = rng.integers(8, levels, cols)
    seq = [list(str(int(rng.choice(_PRIMES))) * 6) for _ in range(cols)]
    is_p = rng.random(cols) < 0.18
    base = Image.new("RGB", (W, H), (6, 10, 13))
    for f in range(n):
        im = base.copy()
        for c in range(cols):
            hy = y[c]
            for k in range(int(L[c])):
                yy = int(hy - k * rh)
                if yy < -rh or yy > H:
                    continue
                ch = int(seq[c][(int(hy // rh) - k) % len(seq[c])])
                if k == 0:
                    g = head[ch]
                else:
                    g = (orange if is_p[c] else green)[min(levels - 1, k) * 10 + ch]
                im.paste(g, (c * cw, yy), g)
            y[c] += v[c]
            if y[c] - L[c] * rh > H:
                y[c] = rng.uniform(-200, 0)
                v[c] = rng.uniform(5, 14)
                is_p[c] = rng.random() < 0.18
                seq[c] = list(str(int(rng.choice(_PRIMES))) * 6)
        yield im.tobytes()


def seg_key(b, d, tv):
    import hashlib
    k = [b["vis"], b["chap"], b["scr"], b["doc"], b["en"], b["new_chap"], b["end_chap"], round(d, 3), round(tv, 2)]
    return hashlib.md5(json.dumps(k, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def render(b, d, wd, t_video, out):
    ov = os.path.join(wd, "ov", b["id"] + ".png")
    os.makedirs(os.path.dirname(ov), exist_ok=True)
    overlay(b, ov)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    kind = b["vis"][0]
    fade = []
    if b["new_chap"]:
        fade.append("fade=in:st=0:d=0.45")
    if b["end_chap"]:
        fade.append(f"fade=out:st={max(0, d - 0.5):.2f}:d=0.5")
    tail = ("," + ",".join(fade)) if fade else ""
    common = ["-c:v", "libx264", "-crf", "19", "-preset", "faster", "-pix_fmt", "yuv420p", "-r", str(FPS), "-an"]
    if kind == "phone":
        src = src_path(b["vis"][1], wd)
        fc = ("[0:v]split[a][b];[a]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,boxblur=26:2,"
              "eq=brightness=-0.27:saturation=0.6[bg];[b]scale=-2:1000[fg];[bg][fg]overlay=150:40[v1];"
              f"[v1][1:v]overlay=0:0{tail}[v]")
        cmd = [FF, "-v", "error", "-y", "-ss", f"{t_video:.2f}", "-t", f"{d:.3f}", "-i", src,
               "-loop", "1", "-t", f"{d:.3f}", "-i", ov, "-filter_complex", fc, "-map", "[v]"] + common + [out]
    elif kind == "card":
        bgp = os.path.join(wd, "ov", "bg_card.png")
        if not os.path.exists(bgp):
            g = Image.new("RGB", (W, H), BG)
            ImageDraw.Draw(g).ellipse([140, 140, 800, 800], fill=(18, 26, 36))
            g.save(bgp)
        fc = f"[0:v][1:v]overlay=0:0{tail}[v]"
        cmd = [FF, "-v", "error", "-y", "-loop", "1", "-t", f"{d:.3f}", "-i", bgp, "-loop", "1", "-t", f"{d:.3f}",
               "-i", ov, "-filter_complex", fc, "-map", "[v]"] + common + [out]
    elif kind == "screen":                                       # запись экрана — целиком под верхней строкой
        src = src_path(b["vis"][1], wd)
        fc = (f"color=c=0x0e131a:s={W}x{H}:d={d:.3f}[bg];[0:v]scale=1742:980,setsar=1[im];"
              f"[bg][im]overlay=89:100:shortest=1[v1];[v1][1:v]overlay=0:0{tail}[v]")
        cmd = [FF, "-v", "error", "-y", "-ss", f"{t_video:.2f}", "-t", f"{d:.3f}", "-i", src,
               "-loop", "1", "-t", f"{d:.3f}", "-i", ov, "-filter_complex", fc, "-map", "[v]"] + common + [out]
    elif kind == "rain":                                         # цифровой дождь из простых чисел
        n = int(round(d * FPS))
        fc = f"[0:v][1:v]overlay=0:0{tail}[v]"
        cmd = [FF, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
               "-i", "-", "-loop", "1", "-t", f"{d:.3f}", "-i", ov, "-filter_complex", fc, "-map", "[v]",
               "-frames:v", str(n)] + common + [out]
        pr = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for fr in rain(n, seed=int(b["id"])):
            pr.stdin.write(fr)
        pr.stdin.close()
        if pr.wait():
            raise SystemExit("дождь: ffmpeg")
        return out
    else:                                                        # pic / full
        img = src_path(b["vis"][1], wd)
        move = b["vis"][2] if len(b["vis"]) > 2 else "still"
        bx, by, bw, bh = (60, 60, 1240, 960) if kind == "pic" else (0, 100, W, H - 100)
        im = Image.open(img).convert("RGB")
        iw, ih = im.size
        if move == "pan":                                        # по ширине, едем сверху вниз
            sw = bw if kind == "pic" else min(1600, bw)
            sh = round(ih * sw / iw)
            ph = min(sh, bh)
            travel = int(max(0, min(sh - ph, 150 * max(0.0, d - 1.6))))      # не быстрее ~150 пикс/с — успеть прочесть
            y0 = 0
            if len(b["vis"]) > 4:                                # заданный проезд: от y0 до y1
                y0 = max(0, min(sh - ph, int(b["vis"][3])))
                travel = max(0, min(sh - ph, int(b["vis"][4])) - y0)
            y_expr = f"'{y0}+{travel}*min(1,max(0,(t-0.4)/{max(0.1, d - 0.8):.2f}))'"
            fc = (f"color=c=0x0e131a:s={W}x{H}:d={d:.3f}[bg];[0:v]scale={sw}:{sh},crop={sw}:{ph}:0:{y_expr}[im];"
                  f"[bg][im]overlay={bx + (bw - sw) // 2}:{by + (bh - ph) // 2}:shortest=1[v1];[v1][1:v]overlay=0:0{tail}[v]")
        else:                                                    # целиком в рамку
            k = min(bw / iw, bh / ih)
            sw, sh = int(iw * k) // 2 * 2, int(ih * k) // 2 * 2
            fc = (f"color=c=0x0e131a:s={W}x{H}:d={d:.3f}[bg];[0:v]scale={sw}:{sh}[im];"
                  f"[bg][im]overlay={bx + (bw - sw) // 2}:{by + (bh - sh) // 2}:shortest=1[v1];[v1][1:v]overlay=0:0{tail}[v]")
        cmd = [FF, "-v", "error", "-y", "-loop", "1", "-t", f"{d:.3f}", "-i", img, "-loop", "1", "-t", f"{d:.3f}",
               "-i", ov, "-filter_complex", fc, "-map", "[v]"] + common + [out]
    subprocess.run(cmd, check=True)
    return out


def ambient(src, t0, d):
    p = subprocess.run([FF, "-v", "error", "-ss", f"{t0:.2f}", "-t", f"{d:.3f}", "-i", src, "-vn", "-ac", "1", "-ar", str(SR),
                        "-f", "f32le", "-"], capture_output=True)
    a = np.frombuffer(p.stdout, np.float32)
    out = np.zeros(int(d * SR), np.float32)
    out[:min(len(a), len(out))] = a[:len(out)]
    return out


def build(beats, lens, wd, name):
    t_video, timeline, en, ru = {}, 0.0, [], []
    segs, voice_track, amb_track = [], [], []
    for b in beats:
        d = round(LEAD + lens[b["id"]] + TAIL + (0.25 if b["end_chap"] else 0), 3)
        kind, srcname = b["vis"][0], (b["vis"][1] if len(b["vis"]) > 1 else "")
        tv = 0.0
        if kind in ("phone", "screen"):
            if b.get("cont"):
                tv = t_video.get(srcname, 0.0)
            else:
                tv = float(b["vis"][2]) if len(b["vis"]) > 2 else 0.0
            tv = max(0.0, min(tv, vdur(src_path(srcname, wd)) - d - 0.1))    # видео не должно кончиться раньше такта
            t_video[srcname] = tv + d
        seg = os.path.join(wd, "seg", seg_key(b, d, tv) + ".mp4")
        if not os.path.exists(seg):
            render(b, d, wd, tv, seg)
            print(f"  кадр {b['id']} {kind:5} {d:5.1f} с")
        segs.append(seg)
        v, _ = sf.read(b["vfile"], dtype="float32")
        vt = np.zeros(int(d * SR), np.float32)
        i = int(LEAD * SR)
        vt[i:i + len(v)] = v[:len(vt) - i]
        voice_track.append(vt)
        if kind == "phone":
            a = ambient(src_path(srcname, wd), tv, d)
            a *= AMB / (np.percentile(np.abs(a), 99) or 1)
            r = min(len(a) // 4, int(0.25 * SR))
            if r:
                a[:r] *= np.linspace(0, 1, r)
                a[-r:] *= np.linspace(1, 0, r)
            amb_track.append(a)
        else:
            amb_track.append(np.zeros(int(d * SR), np.float32))
        s = timeline + LEAD
        en.append((s, s + lens[b["id"]] + 0.3, b["en"]))
        ru.append((s, s + lens[b["id"]] + 0.3, b["ru"].replace("+", "")))
        timeline += d
    mix = np.concatenate(voice_track) + np.concatenate(amb_track)
    mix *= 0.9 / (np.max(np.abs(mix)) or 1)
    wav = os.path.join(wd, "mix.wav")
    sf.write(wav, mix, SR, subtype="PCM_16")
    lst = os.path.join(wd, "segs.txt")
    open(lst, "w", encoding="utf-8").write("".join(f"file '{s}'\n" for s in segs))
    out_dir = r"C:\wheel2\media\out"
    final = os.path.join(out_dir, name + ".mp4")
    subprocess.run([FF, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", wav, "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", final], check=True)

    def st(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    for lang, rows in (("en", en), ("ru", ru)):
        with open(os.path.join(out_dir, f"{name}.{lang}.srt"), "w", encoding="utf-8", newline="\n") as f:
            for k, (a, z, x) in enumerate(r for r in rows if r[2]):
                f.write(f"{k + 1}\n{st(a)} --> {st(z)}\n{x}\n\n")
    print(f"готово: {final} · {int(timeline // 60)}:{timeline % 60:04.1f} · тактов {len(beats)}")


if __name__ == "__main__":
    script = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "all"
    wd = os.path.dirname(script)
    beats = parse(script)
    if "--only" in sys.argv:
        a, _, z = sys.argv[sys.argv.index("--only") + 1].partition("-")
        beats = beats[int(a) - 1:int(z or a)]
    name = os.path.splitext(os.path.basename(script))[0]
    print(f"тактов {len(beats)}, глав {len({b['chap'] for b in beats})}")
    lens = voice(beats, wd)
    if mode in ("build", "all"):
        build(beats, lens, wd, name + ("_part" if "--only" in sys.argv else ""))
