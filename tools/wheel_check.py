# -*- coding: utf-8 -*-
"""Проверка колеса перед опытом: шина, датчик обода, магнитометр, катушка. Статику и моторы не трогает.

    python tools/wheel_check.py [--wheel 1] [--listen 60] [--cycles 8] [--wait 10] [--no-coil] [--url http://127.0.0.1:8090]

Колесо должно стоять. Платы колеса берутся из реестра (/api/fleet: wheel = номер колеса; роль S — статика —
не берётся). По шагам:

1. Ждёт, пока все платы колеса выйдут на связь (до --wait минут).
2. Катушка выключена, --listen секунд слушает: у каждой платы — потери, перезагрузки, температура, память;
   датчик обода (ICM-42688) — частота, |a| ≈ 1 g, гироскоп ≈ 0, шум (tools/imu_noise.py через сервер);
   магнитометр (MLX90393) — поле Земли, шум, ошибки.
3. Катушка: --cycles включений «+» и «−» по очереди, 15 с вкл / 10 с выкл. Каждый раз — пошёл ли ток (INA219
   агента катушки, > 0,3 А), видит ли магнитометр поле (> 300 мкТл) и с тем ли знаком; после выключения ток и
   поле вернулись. Аккумулятор катушки под полем ниже 6,6 В — выключить и остановиться.
4. После катушки — шина ещё раз: не перезагружалась ли, не теряла ли пакеты.

Управляет только катушкой (POST /api/coils) и в конце всегда её выключает. Итог — строки ✓ / ◐ / ✗.
"""
import argparse, json, math, sys, time, urllib.error, urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ap = argparse.ArgumentParser()
ap.add_argument("--wheel", default="1")
ap.add_argument("--listen", type=float, default=60)
ap.add_argument("--cycles", type=int, default=8)
ap.add_argument("--wait", type=float, default=10)
ap.add_argument("--no-coil", action="store_true")
ap.add_argument("--url", default="http://127.0.0.1:8090")
A = ap.parse_args()
URL, W = A.url, str(A.wheel)
COIL_ON_S, COIL_OFF_S = 15.0, 10.0
I_ON_MA, B_ON_UT, V_MIN = 300, 300, 6.6
verdict = []


def get(p, t=15):
    return json.load(urllib.request.urlopen(URL + p, timeout=t))


def post(p, b):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def say(t):
    print(f"{time.strftime('%H:%M:%S')} {t}", flush=True)


def mark(ok, text, warn=False):
    s = "✓" if ok else ("◐" if warn else "✗")
    verdict.append(f"{s} {text}")
    say(f"{s} {text}")


def agents(ids):
    return {a["id"]: a for a in get("/api/dash")["agents"] if a["id"] in ids}


def coil_battery():
    return next((b for b in get("/api/battery").get("list", []) if b.get("kind") == "coil" and str(b.get("wheel")) == W), None)


# ---------- платы колеса ----------
fleet = [b for b in get("/api/fleet")["agents"] if str(b.get("wheel")) == W and not b.get("retired") and b.get("kind") != "S"]
bus = sorted(b["id"] for b in fleet if b.get("kind") == "A")
coil_ag = [b["id"] for b in fleet if b.get("slot") == "coil"]
mac = {b["id"]: b["mac"] for b in fleet}
ids = tuple(bus + ([] if A.no_coil else coil_ag))       # без катушки агента катушки не ждать
if not bus:
    sys.exit(f"в реестре нет плат колеса {W}")
say(f"колесо {W}: шина {' '.join('№%d' % i for i in bus)}" + (f" · агент катушки №{coil_ag[0]}" if coil_ag else " · агента катушки нет"))
if get("/api/dash").get("program"):
    sys.exit("идёт программа — проверку не запускаю")

t_end, last = time.time() + A.wait * 60, -1
while True:
    ag = agents(ids)
    on = sorted(i for i, a in ag.items() if a["status"] == "online")
    if len(on) != last:
        say(f"на связи {len(on)}/{len(ids)}: {' '.join('№%d' % i for i in on) or '—'}")
        last = len(on)
    if len(on) == len(ids) or time.time() > t_end:
        break
    time.sleep(3)
missing = [i for i in ids if i not in on]
mark(not missing, "все платы на связи" if not missing else f"нет на связи: {' '.join('№%d' % i for i in missing)}")
if not on:
    sys.exit(1)

# ---------- шина: слушаем при выключенной катушке ----------
if coil_ag:
    post("/api/coils", {"wheel": W, "state": "off"})
say(f"— шина и датчики, катушка выключена, слушаю {A.listen:.0f} с —")
a0 = agents(ids)
time.sleep(A.listen)
a1 = agents(ids)


def link(a0, a1, what):
    for i in ids:
        a, b = a0.get(i) or {}, a1.get(i) or {}
        v = b.get("values") or {}
        if b.get("status") != "online":
            mark(False, f"№{i}: нет связи ({what})")
            continue
        dp = (b.get("packets_total") or 0) - (a.get("packets_total") or 0)
        dl = (b.get("lost_total") or 0) - (a.get("lost_total") or 0)
        rb = (b.get("reboots") or 0) - (a.get("reboots") or 0)
        t = v.get("temp") or 0
        ok = dl == 0 and rb == 0 and dp > 0 and t < 70
        mark(ok, f"№{i} {what}: пакетов {dp}, потерь {dl}, перезагрузок {rb} · {t} °C · сигнал {v.get('rssi')} дБм"
                 f" · прошивка {b.get('fw')}", warn=rb == 0 and dp > 0 and t < 73)


link(a0, a1, "без поля")
S = get("/api/dash?metrics=w_am&window=30")

for w in (x for x in S.get("wheel") or [] if x["id"] in bus):
    i = w["id"]
    moving = abs(w.get("rpm_now") or 0) > 0.5
    mark(not w.get("err") and (w.get("hz") or 0) > 150, f"№{i} датчик обода: {w.get('hz')} Гц, ошибок {w.get('err')}"
         + (f" · крутится {w.get('rpm_now')} об/мин — шум и |a| не проверяю" if moving else ""))
    if moving:
        continue
    r = get(f"/api/sensor_noise?id={i}&min={max(1.0, A.listen / 60):.1f}", 150)
    if not r.get("ok"):
        mark(False, f"№{i} шум не посчитан: {r.get('error')}")
        continue
    txt = r["text"]
    am = next((ln for ln in txt.splitlines() if ln.strip().startswith("|a|")), "")
    sds = [float(ln.split()[2]) for ln in txt.splitlines() if ln.strip()[:2] in ("x ", "y ", "z ") and len(ln.split()) > 3]
    gb = [abs(float(ln.split(": смещение нуля")[1].split()[0])) for ln in txt.splitlines() if ": смещение нуля" in ln]
    amag = float(am.split()[1]) if am else 0
    mark(abs(amag - 1000) < 30, f"№{i} |a| = {amag:.1f} мg (тяжесть, ждём ≈ 1000)")
    mark(sds and max(sds) < 1.2, f"№{i} шум акселерометра {' / '.join(f'{x:.2f}' for x in sds)} мg (стол без толчков — около 0,7)",
         warn=sds and max(sds) < 3)
    mark(gb and max(gb) < 1.0, f"№{i} гироскоп стоит: смещение нуля до {max(gb or [0]):.2f} °/с")
    say(f"   картинка: {URL}{r.get('png')}")

mags = [x for x in S.get("field") or [] if x["id"] in bus]
for m in mags:
    i = m["id"]
    L = get(f"/api/field/live?mac={mac[i]}&sec={min(A.listen, 120):.0f}")
    if not L.get("ok"):
        mark(False, f"№{i} магнитометр: {L.get('error')}")
        continue
    st = L["stats"]
    b = st["|B|"]["mean"]
    sd = max(st[k]["sd"] for k in "xyz")
    mark(not L.get("err") and 20 < b < 80, f"№{i} магнитометр: поле Земли {b:.1f} мкТл, ошибок {L.get('err')}, усиление {L.get('gain')}")
    mark(sd < 2.5, f"№{i} шум магнитометра до {sd:.2f} мкТл по осям (обычно 1,1–1,8)", warn=sd < 5)
if not mags:
    mark(False, "магнитометра на колесе нет — поле катушки проверить нечем", warn=True)

# ---------- катушка ----------
res = []
if coil_ag and not A.no_coil:
    bat = coil_battery()
    v0 = (bat or {}).get("v_now")
    say(f"— катушка: {A.cycles} включений по {COIL_ON_S:.0f} с —")
    if bat is None or v0 is None:
        mark(False, "аккумулятор катушки не виден (INA219 агента катушки) — катушку не включаю")
    elif v0 < 7.0:
        mark(False, f"аккумулятор катушки {v0} В — зарядить; катушку не включаю")
    else:
        mark(True, f"аккумулятор катушки {v0} В, {bat.get('soc')} %, без поля {bat.get('i_now')} мА")
        c0 = agents(ids)
        try:
            for k in range(A.cycles):
                st = "+" if k % 2 == 0 else "-"
                post("/api/coils", {"wheel": W, "state": st})
                t0, t_br, t_i, t_f = time.time(), None, None, None
                ii, pp, vmin = [], [], 99.0
                while time.time() - t0 < COIL_ON_S:
                    time.sleep(0.25)
                    c = get("/api/coils")
                    b = coil_battery() or {}
                    i, v = b.get("i_now") or 0, b.get("v_now") or 0
                    f = (c.get("sense") or {}).get(W) or {}
                    br = ((c.get("agents") or {}).get(W) or {}).get("mot", {}).get("a") or [0]
                    dt = time.time() - t0
                    t_br = t_br if t_br is not None or br[0] == 0 else dt
                    t_i = t_i if t_i is not None or i <= I_ON_MA else dt
                    t_f = t_f if t_f is not None or (f.get("dabs") or 0) <= B_ON_UT else dt
                    if i > I_ON_MA:
                        ii.append(i); pp.append(f.get("proj") or 0); vmin = min(vmin, v)
                    if v and v < V_MIN:
                        mark(False, f"аккумулятор катушки просел до {v} В — выключаю")
                        raise RuntimeError("battery")
                P = sum(pp) / len(pp) if pp else 0
                r = {"st": st, "br": t_br, "i": t_i, "f": t_f, "I": sum(ii) / len(ii) if ii else 0, "P": P,
                     "sign": (P > 0) == (st == "+") if pp else None, "V": vmin if ii else None,
                     "frac": len(ii) / int(COIL_ON_S / 0.25)}
                post("/api/coils", {"wheel": W, "state": "off"})
                time.sleep(COIL_OFF_S)
                b = coil_battery() or {}
                f = ((get("/api/coils").get("sense") or {}).get(W) or {})
                r["i_off"], r["b_off"] = b.get("i_now") or 0, f.get("dabs") or 0
                res.append(r)
                fmt = lambda x: f"{x:.1f} с" if x is not None else "—"
                say(f"   {k + 1} «{st}»: мост {fmt(t_br)} · ток {fmt(t_i)} ({r['I']:.0f} мА, держался {100 * r['frac']:.0f} %)"
                    f" · поле {fmt(t_f)} (вдоль {P:+.0f} мкТл) · мин {r['V']} В · выкл: {r['i_off']:.0f} мА, {r['b_off']:.1f} мкТл")
        except RuntimeError:
            pass
        finally:
            post("/api/coils", {"wheel": W, "state": "off"})
            say("катушка выключена")
        if res:
            n_i = sum(r["i"] is not None for r in res)
            mark(all(r["br"] is not None for r in res), f"мост брал команду: {sum(r['br'] is not None for r in res)} из {len(res)}")
            mark(n_i == len(res) and all(r["frac"] > 0.7 for r in res),
                 f"ток шёл: {n_i} из {len(res)} включений" + ("" if n_i == len(res) else " — плохой контакт между DC-DC и катушкой: VM, OUT1/OUT2, клеммы"),
                 warn=n_i > 0)
            if mags and n_i:
                on_ = [r for r in res if r["i"] is not None]
                Ps = [abs(r["P"]) for r in on_]
                mark(all(r["f"] is not None for r in on_), f"магнитометр видел поле: {sum(r['f'] is not None for r in on_)} из {len(on_)},"
                     f" {min(Ps):.0f}–{max(Ps):.0f} мкТл (рабочее около 1200)")
                mark(all(r["sign"] for r in on_), "знак поля совпал с командой «+» / «−»" if all(r["sign"] for r in on_)
                     else "знак поля не совпал с командой — провода катушки наоборот или магнитометр перевёрнут")
            mark(all(r["i_off"] < 200 and r["b_off"] < 20 for r in res), "после выключения ток и поле вернулись к фону")
        c1 = agents(ids)
        link(c0, c1, "при катушке")

say("— итог —")
for v in verdict:
    print("  " + v)
bad = sum(v.startswith("✗") for v in verdict)
warn = sum(v.startswith("◐") for v in verdict)
print("  " + ("✓ колесо готово" if not bad and not warn else " · ".join(x for x in (f"✗ не так: {bad}" if bad else "",
                                                                      f"◐ посмотреть: {warn}" if warn else "") if x)))
