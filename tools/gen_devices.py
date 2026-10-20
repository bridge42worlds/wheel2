# -*- coding: utf-8 -*-
"""Генерация таблицы устройств для прошивки из реестра.

    python tools/gen_devices.py

Читает registry/devices.csv и пишет
firmware/libraries/agent_common/src/devices_gen.h.
Сборка падает, если в реестре повторяется номер или MAC, MAC записан
неверно или метка содержит не-ASCII символы (метка попадает в прошивку).
"""
import csv, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "registry", "devices.csv")
DST = os.path.join(ROOT, "firmware", "libraries", "agent_common", "src", "devices_gen.h")
MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def main():
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
    ids, macs, errors = set(), set(), []
    for n, r in enumerate(rows, start=2):
        mac = r["mac"].strip().lower()
        try:
            did = int(r["id"])
        except ValueError:
            errors.append(f"строка {n}: номер не число: {r['id']!r}")
            continue
        if not MAC_RE.match(mac):
            errors.append(f"строка {n}: неверный MAC {mac!r}")
        if did <= 0:
            errors.append(f"строка {n}: номер должен быть больше нуля")
        if did in ids:
            errors.append(f"строка {n}: номер {did} уже занят")
        if mac in macs:
            errors.append(f"строка {n}: MAC {mac} уже записан")
        if not r["label"].isascii():
            errors.append(f"строка {n}: метка {r['label']!r} должна быть латиницей")
        ids.add(did)
        macs.add(mac)
    if errors:
        print("ОШИБКИ В РЕЕСТРЕ:\n  " + "\n  ".join(errors))
        sys.exit(1)

    out = [
        "// создаётся tools/gen_devices.py из registry/devices.csv — вручную не править",
        "#pragma once",
        "#include <stdint.h>",
        "struct DeviceEntry { uint8_t mac[6]; uint16_t id; const char* label; };",
        "static const DeviceEntry DEVICES[] = {",
    ]
    for r in rows:
        b = ", ".join("0x" + x for x in r["mac"].strip().lower().split(":"))
        out.append(f'  {{{{{b}}}, {int(r["id"])}, "{r["label"]}"}},')
    out += ["};", f"static const int DEVICES_COUNT = {len(rows)};", ""]
    open(DST, "w", encoding="utf-8", newline="\n").write("\n".join(out))
    print(f"реестр: {len(rows)} устройств -> devices_gen.h")


if __name__ == "__main__":
    main()
