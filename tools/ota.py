"""Прошить агентов по Wi-Fi последней сборкой и дождаться, пока они вернутся с новой версией.

    python tools/ota.py [all | 2 3 5 …] [--force] [--wait 300]

Кто минуту не начал загрузку (команда — один UDP-пакет, мог потеряться) или сообщил
об ошибке загрузки, тому команда повторяется — до двух раз.

Сервер должен работать (tools/monitor.cmd), образ — собран (tools/build.sh prime_lab).
Команда уходит через сервер (POST /api/ota): он подписывает её ключом secrets/ota.key.
Печатает, кто получил команду, потом раз в 2 с — состояние каждого: загрузка, перезапуск,
новая версия на связи, «ждёт подтверждения» → «подтверждена» (через минуту на связи).
"""
import argparse, json, sys, time, urllib.request

URL = "http://127.0.0.1:8090"


def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=10))


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        return json.load(e)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("who", nargs="*", default=["all"])
    ap.add_argument("--force", action="store_true", help="перепрошить и тех, у кого уже эта версия")
    ap.add_argument("--wait", type=float, default=300)
    a = ap.parse_args()
    d = get("/api/dash?window=10")
    fw = d.get("fw")
    if not fw:
        raise SystemExit("нет собранного образа: tools/build.sh prime_lab")
    by_id = {x["id"]: x["mac"] for x in d["agents"]}
    targets = "all" if a.who == ["all"] else [by_id[int(i)] for i in a.who if int(i) in by_id]
    print(f"сборка {fw['version']} ({fw['size']} байт, md5 {fw['md5'][:8]}…) → {a.who}", flush=True)
    r = post("/api/ota", {"targets": targets, "force": a.force})
    if not r.get("ok"):
        raise SystemExit(f"отказ: {r.get('error')}")
    names = {x["mac"]: f"№{x['id']}" for x in d["agents"]}
    print("команда ушла:", ", ".join(names.get(m, m) for m in r["sent"]) or "никому",
          "; пропущены:", {names.get(m, m): w for m, w in r["skipped"].items()} or "—", flush=True)
    if not r["sent"]:
        return
    # Перезапуск с новым образом виден по разделу: OTA пишет в свободный (app0 ↔ app1). Так «готово» не
    # наступает раньше времени, когда заливается сборка с тем же номером версии (--force).
    part0 = {x["mac"]: (x.get("ota") or [0, 0, ""])[2] for x in d["agents"]}
    ver0 = {x["mac"]: x["fw"] for x in d["agents"]}
    t0 = time.time()
    seen = {}
    sent_t = {m: t0 for m in r["sent"]}                # когда команда ушла этому агенту
    retries = {m: 0 for m in r["sent"]}
    while time.time() - t0 < a.wait:
        time.sleep(2)
        d = get("/api/dash?window=10")
        line = []
        done = 0
        for x in d["agents"]:
            if x["mac"] not in r["sent"]:
                continue
            o = x.get("ota") or [0, 0, ""]
            # Команда — один UDP-пакет: потерялась или загрузка оборвалась — повторить (до двух раз).
            # Признак: минуту ни загрузки, ни новой версии, или агент сообщил об ошибке загрузки.
            stuck = x["fw"] != fw["version"] and x["status"] == "online" and not o[1] and                 (o[0] == -1 or (o[0] == 0 and time.time() - sent_t[x["mac"]] > 60))
            if stuck and retries[x["mac"]] < 2:
                retries[x["mac"]] += 1
                sent_t[x["mac"]] = time.time()
                rr = post("/api/ota", {"targets": [x["mac"]], "force": a.force})
                print(f"  {time.time() - t0:4.0f} с: {names.get(x['mac'])} не начал — повтор команды "
                      f"№{retries[x['mac']]}{'' if rr.get('ok') else ': ' + str(rr.get('error'))}", flush=True)
            st = ("загрузка" if o[0] == 1 else "записана, перезапуск" if o[0] == 2 else "ОШИБКА загрузки" if o[0] == -1
                  else "ждёт подтверждения" if o[1] else "подтверждена" if x["fw"] == fw["version"] else "…")
            if x["status"] != "online":
                st = "перезапускается" if x["status"] in ("late", "offline") else st
            line.append(f"{names.get(x['mac'])}: {x['fw']} ({o[2] or '?'}) {st}")
            moved = (o[2] and part0.get(x["mac"]) and o[2] != part0[x["mac"]]) or x["fw"] != ver0.get(x["mac"])
            if x["fw"] == fw["version"] and x["status"] == "online" and not o[1] and moved:
                done += 1
        key = " | ".join(line)
        if key != seen.get("k"):
            print(f"  {time.time() - t0:4.0f} с: {key}", flush=True)
            seen["k"] = key
        if done == len(r["sent"]):
            print(f"готово: все {done} на версии {fw['version']} и подтверждены за {time.time() - t0:.0f} с")
            return
    print("время вышло; смотрите панель")


if __name__ == "__main__":
    main()
