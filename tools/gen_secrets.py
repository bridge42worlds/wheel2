# -*- coding: utf-8 -*-
"""Секреты для прошивки: данные Wi-Fi из secrets/wifi.ini и ключ подписи команд OTA.

    python tools/gen_secrets.py

Пишет firmware/libraries/agent_common/src/secrets_gen.h. Ни один из этих файлов
не попадает в git. Пароль и ключ никогда не печатаются — только признак, заданы ли.

Ключ OTA (secrets/ota.key, 32 случайных байта в hex) создаётся сам, если его нет.
Им сервер подписывает команду «прошить по Wi-Fi», а агент проверяет подпись: без
ключа прошивку в плату по сети не залить. Потеряли ключ — новый ключ попадёт в платы
только прошивкой по USB.
"""
import os, secrets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "secrets", "wifi.ini")
KEY = os.path.join(ROOT, "secrets", "ota.key")
DST = os.path.join(ROOT, "firmware", "libraries", "agent_common", "src", "secrets_gen.h")


def c_str(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ota_key():
    """Ключ из secrets/ota.key; нет — создать."""
    if os.path.exists(KEY):
        k = open(KEY, encoding="utf-8").read().strip().lower()
        if len(k) == 64 and all(c in "0123456789abcdef" for c in k):
            return k
    os.makedirs(os.path.dirname(KEY), exist_ok=True)
    k = secrets.token_hex(32)
    with open(KEY, "w", encoding="utf-8", newline="\n") as f:
        f.write(k + "\n")
    return k


def main():
    vals = {"ssid": "", "password": "", "server": "", "ssid2": "", "password2": ""}
    if os.path.exists(SRC):
        for ln in open(SRC, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            vals[k.strip().lower()] = v.strip()
    key = ota_key()
    with open(DST, "w", encoding="utf-8", newline="\n") as f:
        f.write("// создаётся tools/gen_secrets.py из secrets/wifi.ini и secrets/ota.key — не в git, вручную не править\n")
        f.write("#pragma once\n")
        f.write(f'#define WIFI_SSID "{c_str(vals["ssid"])}"\n')
        f.write(f'#define WIFI_PASS "{c_str(vals["password"])}"\n')
        f.write(f'#define WIFI_SSID2 "{c_str(vals["ssid2"])}"\n')       # запасная сеть: пусто — нет
        f.write(f'#define WIFI_PASS2 "{c_str(vals["password2"])}"\n')
        f.write(f'#define SERVER_HOST "{c_str(vals["server"])}"\n')
        f.write(f'#define OTA_KEY "{key}"\n')
    print(f"wifi: сеть {vals['ssid']!r}, пароль {'задан' if vals['password'] else 'НЕ задан'}, "
          f"запасная {repr(vals['ssid2']) + (', пароль задан' if vals['password2'] else ', пароль НЕ задан') if vals['ssid2'] else 'нет'}, "
          f"сервер {vals['server'] or 'по маяку'}; ключ OTA задан")


if __name__ == "__main__":
    main()
