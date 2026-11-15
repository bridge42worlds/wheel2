"""«Информация» в шум (07.11): положить файл (код, музыка) в платы колёс — по кругу он становится источником SRC_BC
вместо эфира соседей. Чистая информация: --src 16 в phase_run; смесь с регистром ГСЧ байт в байт: --src 144.

    python tools/info_feed.py data/info/code.txt            положить первые 256 КБ файла во все платы колёс и пустышки
    python tools/info_feed.py data/info/bach.mp3 --kb 128    первые 128 КБ
    python tools/info_feed.py --off                         выключить информацию (платы снова слушают эфир)
    python tools/info_feed.py --check                       спросить у плат, сколько байт лежит

Кусок — 700 байт hex в одной строке UDP; каждый кусок шлётся дважды (потери UDP; запись по смещению — дубли безвредны).
"""
import argparse, json, os, sys, time, urllib.request

URL = "http://127.0.0.1:8090"
ap = argparse.ArgumentParser()
ap.add_argument("path", nargs="?")
ap.add_argument("--kb", type=int, default=256)
ap.add_argument("--off", action="store_true")
ap.add_argument("--check", action="store_true")
ap.add_argument("--ids", default=None, help="номера плат через запятую; по умолчанию все платы колёс и пустышки")
A = ap.parse_args()


def req(p, b=None):
    r = urllib.request.Request(URL + p, data=json.dumps(b).encode() if b is not None else None,
                               headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=20))
    except urllib.error.HTTPError as e:
        return json.load(e)


fleet = req("/api/fleet")["agents"]
if A.ids:
    ids = {int(x) for x in A.ids.split(",")}
    boards = [b for b in fleet if b["id"] in ids and b.get("online")]
else:
    boards = [b for b in fleet if not b.get("retired") and b.get("kind") in ("A", "B") and b.get("slot") != "coil"
              and str(b.get("wheel")) in ("1", "2", "3") and b.get("online")]
print(f"плат на связи: {len(boards)}: {' '.join('№' + str(b['id']) for b in boards)}")


def cmd(b, line):
    out = req("/api/cmd", {"mac": b["mac"], "line": line})
    if not out.get("ok"):
        print(f"  ✗ №{b['id']}: {out.get('error')}")


def acks():
    d = req("/api/dash")
    return {a["mac"]: (a.get("values") or {}, (d.get("control") or {}).get(a["mac"], {}).get("ack")) for a in d["agents"]}


if A.off:
    for b in boards:
        cmd(b, "INFO off")
    print("информация выключена — платы слушают эфир")
    sys.exit(0)
if A.check or not A.path:
    for b in boards:
        cmd(b, "INFOLEN")
    time.sleep(2)
    ak = acks()
    for b in boards:
        print(f"  №{b['id']}: {((ak.get(b['mac']) or (None, None))[1] or {}).get('err')}")
    sys.exit(0)

data = open(A.path, "rb").read()[:A.kb * 1024]
print(f"{os.path.basename(A.path)}: {len(data)} байт → кусками по 500")
for b in boards:
    cmd(b, "INFO clr")
time.sleep(0.5)
t0 = time.time()
for rep in range(2):                                   # дважды: потери UDP
    for off in range(0, len(data), 500):
        line = f"INFO {off} {data[off:off + 500].hex()}"
        for b in boards:
            cmd(b, line)
        if (off // 500) % 10 == 0:
            time.sleep(0.1)                            # не захлебнуть Wi-Fi и приёмник платы (буфер 1280 знаков)
    print(f"  проход {rep + 1} отправлен за {time.time() - t0:.0f} с")
time.sleep(1)
for b in boards:
    cmd(b, "INFOLEN")
time.sleep(2)
ak = acks()
bad = []
for b in boards:
    err = ((ak.get(b["mac"]) or (None, None))[1] or {}).get("err") or ""
    ok = f"len={len(data)}" in err
    print(f"  №{b['id']}: {err}{'' if ok else '  ← не совпало'}")
    if not ok:
        bad.append(b["id"])
for b in boards:
    cmd(b, "INFO on")
print("информация включена" + (f"; не полностью у: {bad}" if bad else " у всех"))
