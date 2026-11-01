"""Катушка на выносливость: поле по кругу «+ → пауза → − → пауза», пока не сядут аккумуляторы катушки.

    python tools/coil_soak.py [--wheel 1] [--mag 20] [--on 10] [--gap 2] [--stop-v 6.5] [--hours 5]

Раз в секунду — строка в CSV (data/tests/coil_soak_<дата_время>.csv): что задано, напряжение и ток INA219
агента катушки (последний замер), израсходовано мА·ч (ток × время — своё, не сбрасывается при перезапуске
агента), поле магнитометра колеса (агент --mag, MLX90393: |B|, x, y, z — среднее за секунду, из сырого журнала
сервера). Раз в минуту — сводка в окно и в .log рядом с CSV.

Остановка (поле в конце выключается всегда):
  * напряжение пары при включённом поле ≤ --stop-v пять замеров подряд — при 6,5 В это 3,25 В на банку,
    банки не переразряжаются;
  * агент катушки молчит дольше 60 с (сели совсем, DC-DC не держит) или сервер не отвечает дольше 60 с;
  * прошло --hours; появился файл data/tests/coil_soak.stop; Ctrl+C в окне.
Окно не закрывать крестиком: поле может остаться включённым — тогда «выкл» на планшетке.
Сервер мониторинга должен работать (tools/monitor.cmd).
"""
import argparse, csv, json, os, statistics, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "http://127.0.0.1:8090"
STOP = os.path.join(ROOT, "data", "tests", "coil_soak.stop")


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=5))


def post(path, body):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=5))


def mac_of(aid):
    with open(os.path.join(ROOT, "registry", "devices.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["id"].strip() == str(aid):
                return r["mac"].strip().lower()
    raise SystemExit(f"№{aid} нет в реестре")


def last_mag(mac):
    """Последний пакет поля агента из хвоста сырого журнала сервера (за сегодня)."""
    raw = os.path.join(ROOT, "data", "raw", time.strftime("%Y-%m-%d") + ".jsonl")
    try:
        with open(raw, "rb") as f:
            f.seek(max(0, os.path.getsize(raw) - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return None
    key = f'\\"mac\\":\\"{mac}\\"'
    for line in reversed(lines):
        if key in line and '\\"type\\":\\"mag\\"' in line:
            o = json.loads(line)
            return {**json.loads(o["raw"]), "rx": o["rx"]}
    return None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel", default="1")
    ap.add_argument("--mag", type=int, default=20, help="номер агента с магнитометром")
    ap.add_argument("--on", type=float, default=10, help="поле включено, с")
    ap.add_argument("--gap", type=float, default=2, help="пауза между направлениями, с")
    ap.add_argument("--stop-v", type=float, default=6.5)
    ap.add_argument("--hours", type=float, default=5)
    ap.add_argument("--mah0", type=float, default=0, help="уже израсходовано из этой пары (продолжение прогона), мА·ч")
    a = ap.parse_args()
    mmac = mac_of(a.mag)
    os.makedirs(os.path.dirname(STOP), exist_ok=True)
    if os.path.exists(STOP):
        os.remove(STOP)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    path = os.path.join(ROOT, "data", "tests", f"coil_soak_{stamp}.csv")
    logf = open(path[:-4] + ".log", "a", encoding="utf-8")

    def say(text):
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    plan = [("+", a.on), ("off", a.gap), ("-", a.on), ("off", a.gap)]
    say(f"катушка К{a.wheel}: «+» {a.on:g} с → пауза {a.gap:g} с → «−» {a.on:g} с → пауза; стоп при ≤ {a.stop_v} В под "
        f"полем; магнитометр №{a.mag}; журнал {os.path.relpath(path, ROOT)}")
    t0 = time.time()
    mah, cycles, low_n, why = a.mah0, 0, 0, ""
    last_ok = time.time()
    minute, minute_t = [], time.time()
    first = {}
    k, phase_end, state, state_t, sent = -1, 0.0, "off", time.time(), True
    prev_t = None
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "elapsed_s", "state", "state_age_s", "v", "i_ma", "mah", "babs_ut", "bx_ut", "by_ut", "bz_ut",
                    "gain", "mag_age_s", "coil_temp_c"])
        try:
            while True:
                now = time.time()
                if now >= phase_end:                       # следующее направление
                    k = (k + 1) % len(plan)
                    state, dur = plan[k]
                    if state == "+":
                        cycles += 1
                    phase_end, state_t, sent = now + dur, now, False
                if not sent:                               # сервер перезапускается — повторять каждую секунду
                    try:
                        post("/api/coils", {"wheel": a.wheel, "state": state, "src": "проверка выносливости"})
                        sent = True
                    except Exception:                      # noqa: BLE001
                        if now - last_ok > 60:
                            why = "сервер не отвечает дольше 60 с"
                            break
                time.sleep(max(0.0, 1.0 - (time.time() - now)))
                now = time.time()
                try:
                    bats = get("/api/battery")["list"]
                    last_ok = now
                except Exception:                          # noqa: BLE001 — сервер перезапускается: подождать
                    if now - last_ok > 60:
                        why = "сервер не отвечает дольше 60 с"
                        break
                    continue
                b = next((r for r in bats if r.get("wheel") == a.wheel and r.get("kind") == "coil"), None)
                if not b or b.get("age", 999) > 60:
                    why = "агент катушки молчит дольше 60 с (сели аккумуляторы или нет питания)"
                    break
                v, i = b.get("v_now", b["v"]), b.get("i_now", b["i"])
                if prev_t is not None:
                    mah += max(0.0, i) * (now - prev_t) / 3600.0
                prev_t = now
                m = last_mag(mmac) or {}
                bx, by, bz = (m.get("b") or [None] * 3)[:3]
                mage = round(now - m["rx"], 1) if m.get("rx") else None
                sage = now - state_t
                w.writerow([round(now, 2), round(now - t0, 1), state, round(sage, 1), v, i, round(mah, 2), m.get("babs"),
                            bx, by, bz, m.get("gain"), mage, m.get("t")])
                f.flush()
                if state != "off" and sage >= 2:
                    minute.append((state, v, i, m.get("babs") if mage is not None and mage < 3 else None))
                    first.setdefault(state, m.get("babs"))
                    low_n = low_n + 1 if v <= a.stop_v else 0
                    if low_n >= 5:
                        why = f"аккумуляторы сели: {v:.2f} В под полем (порог {a.stop_v} В)"
                        break
                if now - minute_t >= 60 and minute:
                    on = [x for x in minute if x[0] != "off"]
                    bp = [x[3] for x in on if x[0] == "+" and x[3] is not None]
                    bm = [x[3] for x in on if x[0] == "-" and x[3] is not None]
                    say(f"{(now - t0) / 60:5.1f} мин · циклов {cycles} · под полем {statistics.mean(x[1] for x in on):.2f} В, "
                        f"{statistics.mean(x[2] for x in on):.0f} мА · израсходовано {mah:.0f} мА·ч · |B| «+» "
                        f"{statistics.mean(bp) if bp else float('nan'):.0f}, «−» {statistics.mean(bm) if bm else float('nan'):.0f} мкТл")
                    minute, minute_t = [], now
                if os.path.exists(STOP):
                    why = "остановлено файлом coil_soak.stop"
                    break
                if now - t0 > a.hours * 3600:
                    why = f"прошло {a.hours:g} ч"
                    break
        except KeyboardInterrupt:
            why = "Ctrl+C"
        except Exception as e:                             # noqa: BLE001 — причину — в журнал, поле — выключить
            why = f"ошибка скрипта: {e!r}"
        finally:
            for _ in range(3):
                try:
                    post("/api/coils", {"off": True, "src": "проверка выносливости: конец"})
                    break
                except Exception:                          # noqa: BLE001
                    time.sleep(2)
    say(f"СТОП: {why or 'ошибка'} · {(time.time() - t0) / 60:.1f} мин, циклов {cycles}, израсходовано {mah:.0f} мА·ч; "
        f"|B| в начале «+» {first.get('+')}, «−» {first.get('-')} мкТл; поле выключено")
    logf.close()


if __name__ == "__main__":
    main()
