"""Записать плату в реестр registry/devices.csv, если её там ещё нет.

    python tools/register.py 68:ee:8f:51:ae:20

Новая плата получает следующий свободный номер и метку bench-<номер>; метку,
роль и сет потом можно поправить руками. Сервер мониторинга сам перечитывает
реестр, когда файл меняется, а состав группы и адреса участников рассылает
агентам в настройке — старые платы перепрошивать не нужно.
"""
import csv
import datetime
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(ROOT, "registry", "devices.csv")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    mac = sys.argv[1].strip().lower()
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise SystemExit(f"реестр: не похоже на MAC: {mac}")
    with open(REG, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r["mac"].strip().lower() == mac:
            print(f"реестр: {mac} уже записана — №{r['id']} {r['label']}")
            return
    nid = max((int(r["id"]) for r in rows), default=0) + 1
    with open(REG, "a", encoding="utf-8", newline="") as f:
        f.write(f"{nid},{mac},ESP32-S3,,bench,bench-{nid},{datetime.date.today():%Y-%m-%d},"
                f"добавлена при первой прошивке\n")
    print(f"реестр: новая плата {mac} записана №{nid} (bench-{nid})")


if __name__ == "__main__":
    main()
