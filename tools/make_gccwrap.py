"""Обёртки компилятора с повтором запуска — для сборки, когда Windows иногда
не даёт запустить программы компилятора («cannot execute cc1plus.exe:
CreateProcess», «Access is denied»; так было 04.10.2026).

    python tools/make_gccwrap.py        создать build/gccwrap/*.cmd

Каждая обёртка запускает настоящую программу из набора ESP32 и, если запуск
не удался, повторяет до 10 раз; настоящие ошибки компиляции не повторяются.
tools/build.sh переключается на обёртки сам, если обычная сборка упала так.
"""
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIN = os.path.join(os.environ["LOCALAPPDATA"], "Arduino15", "packages", "esp32", "tools", "esp-x32", "2511", "bin")
OUT = os.path.join(ROOT, "build", "gccwrap")
TOOLS = {"gcc": "xtensa-esp32s3-elf-gcc.exe", "gpp": "xtensa-esp32s3-elf-g++.exe",
         "ar": "xtensa-esp32s3-elf-gcc-ar.exe", "size": "xtensa-esp32s3-elf-size.exe"}

TEMPLATE = r"""@echo off
rem {name}: run the real tool, retry if Windows failed to start it
setlocal enabledelayedexpansion
set "REAL={real}"
set "ERRF=%TEMP%\gccwrap_%RANDOM%%RANDOM%.txt"
set RC=1
for /l %%i in (1,1,10) do (
  "!REAL!" %* 2> "!ERRF!"
  set RC=!errorlevel!
  findstr /c:"cannot execute" /c:"Access is denied" /c:"CreateProcess" "!ERRF!" >nul || goto done
)
:done
type "!ERRF!" 1>&2
del "!ERRF!" 2>nul
exit /b !RC!
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, exe in TOOLS.items():
        real = os.path.join(BIN, exe)
        if not os.path.exists(real):
            raise SystemExit(f"нет {real}")
        with open(os.path.join(OUT, name + ".cmd"), "w", encoding="ascii", newline="\r\n") as f:
            f.write(TEMPLATE.replace("{name}", name).replace("{real}", real))
    print("обёртки:", OUT)


if __name__ == "__main__":
    main()
