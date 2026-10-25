# -*- coding: utf-8 -*-
"""Помощник tools/video.py — запускается в окружении озвучки C:\\CL\\va\\.venv (Vosk-TTS, Whisper, scipy).

    video_voice.py voice <voice.json>             озвучить реплики голосом Vosk 4 и сложить дорожку на всю длину ролика
    video_voice.py hear  <звук.wav> <текст.txt> [small|large-v3-turbo]   расшифровать речь с таймкодами
"""
import json, os, re, sys
sys.stdout.reconfigure(encoding="utf-8")
VA = r"C:\CL\va"
SPEAKER = 4                     # выбран пользователем 20.10.2026 (проба ru_vosk_4)
MAX_RATE = 1.15                 # быстрее — уже скороговорка; не влезает — сократить текст


def to_words(text):
    """Цифры — словами (Vosk их не читает): «45» → «сорок пять», «0,5» → «ноль целых пять десятых»."""
    from num2words import num2words
    def num(m):
        s = m.group(0).replace(",", ".")
        try:
            return num2words(float(s) if "." in s else int(s), lang="ru")
        except Exception:
            return s
    return re.sub(r"\d+(?:[.,]\d+)?", num, text)


def voice(spec_path):
    import numpy as np, soundfile as sf
    from scipy.signal import resample_poly
    os.environ["VOSK_MODEL_PATH"] = os.path.join(VA, "models", "vosk")
    from vosk_tts import Model, Synth
    spec = json.load(open(spec_path, encoding="utf-8"))
    sr = spec["sr"]
    syn = Synth(Model(model_name="vosk-model-tts-ru-0.9-multi"))
    track = np.zeros(int(spec["dur"] * sr) + sr, dtype=np.float32)
    lens, bad = [], 0
    for k, ln in enumerate(spec["lines"]):
        text = to_words(ln["text"])
        if re.search(r"[A-Za-z]", text):
            raise SystemExit(f"реплика {k + 1} ({ln['t']:.1f} с): латиница «{re.findall(r'[A-Za-z]+', text)[0]}» — написать по-русски")
        room = ln["until"] - ln["t"] - 0.15
        rate, wav = 1.0, None
        for _ in range(3):
            wav = np.asarray(syn.synth_audio(text, speaker_id=SPEAKER, speech_rate=rate))
            if wav.dtype.kind in "iu":
                wav = wav.astype(np.float32) / 32768.0
            d = len(wav) / 22050
            if d <= room or rate >= MAX_RATE:
                break
            rate = min(MAX_RATE, rate * d / room * 1.02)
        wav = resample_poly(wav.astype(np.float32), 320, 147).astype(np.float32)   # 22 050 → 48 000 Гц
        d = len(wav) / sr
        lens.append(round(d, 2))
        a = int(ln["t"] * sr)
        track[a:a + len(wav)] += wav[:max(0, len(track) - a)]
        ok = d <= room + 0.05
        bad += not ok
        print(f"{int(ln['t'] // 60)}:{ln['t'] % 60:04.1f}  {d:4.1f} с из {room:4.1f}  темп {rate:.2f}  "
              f"{'✓' if ok else '✗ не влезает — сократить на ~' + str(int((d - room) * 14)) + ' знаков'}  {ln['text'][:60]}")
    peak = float(np.max(np.abs(track))) or 1.0
    track *= min(1.0, 0.9 / peak)
    sf.write(spec["out"], track[:int(spec["dur"] * sr)], sr, subtype="PCM_16")
    json.dump({"lens": lens}, open(os.path.join(os.path.dirname(spec["out"]), "voice_out.json"), "w", encoding="utf-8"))
    print(f"{'✓ все реплики влезают' if not bad else f'✗ не влезают: {bad}'} · дорожка {spec['out']}")


def hear(wav, out, model):
    os.environ["HF_HOME"] = os.path.join(VA, "models", "hf")
    os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 4) - 2))
    segs, info = m.transcribe(wav, vad_filter=True, beam_size=5)
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for s in segs:
            line = f"{int(s.start // 60)}:{s.start % 60:04.1f}–{int(s.end // 60)}:{s.end % 60:04.1f} | {s.text.strip()}"
            f.write(line + "\n")
            print(line)
            n += 1
    print(f"речь: {n} фраз, язык {info.language} ({info.language_probability:.0%}) → {out}")


if __name__ == "__main__":
    if sys.argv[1] == "voice":
        voice(sys.argv[2])
    elif sys.argv[1] == "hear":
        hear(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "small")
