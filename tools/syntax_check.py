"""Проверка синтаксиса прошивок компилятором напрямую, без arduino-cli.

    python tools/syntax_check.py [скетч ...]       по умолчанию: prime_lab

Нужна, когда arduino-cli не может запустить компилятор (так было 04.10.2026:
«cannot execute cc1plus.exe: CreateProcess», хотя напрямую тот же компилятор
работает). Флаги и пути те же, что у arduino-cli для XIAO ESP32-S3; каждый
файл проверяется с -fsyntax-only, .ino — с Arduino.h в начале (как у сборщика),
но без автоматических прототипов: функцию, вызванную раньше определения,
нужно объявить явно.
"""
import os, subprocess, sys, tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
A15 = os.path.join(os.environ["LOCALAPPDATA"], "Arduino15", "packages", "esp32")
TOOLS = os.path.join(A15, "tools")
LIBS = os.path.join(TOOLS, "esp32s3-libs", "3.3.7")
CORE = os.path.join(A15, "hardware", "esp32", "3.3.7")
GXX = os.path.join(TOOLS, "esp-x32", "2511", "bin", "xtensa-esp32s3-elf-g++.exe")
FQBN_DEFS = ["-DF_CPU=240000000L", "-DARDUINO=10607", "-DARDUINO_XIAO_ESP32S3", "-DARDUINO_ARCH_ESP32",
             '-DARDUINO_BOARD="XIAO_ESP32S3"', '-DARDUINO_VARIANT="XIAO_ESP32S3"', "-DARDUINO_PARTITION_default_8MB",
             '-DARDUINO_HOST_OS="windows"', '-DARDUINO_FQBN="esp32:esp32:XIAO_ESP32S3:LoopCore=0,EventsCore=0"',
             "-DESP32=ESP32", "-DCORE_DEBUG_LEVEL=0", "-DARDUINO_RUNNING_CORE=0", "-DARDUINO_EVENT_RUNNING_CORE=0",
             "-DARDUINO_USB_MODE=1", "-DARDUINO_USB_CDC_ON_BOOT=1", "-DARDUINO_USB_MSC_ON_BOOT=0",
             "-DARDUINO_USB_DFU_ON_BOOT=0"]


def fs(p):
    return p.replace("\\", "/")


def args_for(sketch_dir, extra_inc):
    a = ["-c", "@" + fs(os.path.join(LIBS, "flags", "cpp_flags")), "-w", "-Os", "-Werror=return-type",
         "-x", "c++", "-fsyntax-only", *FQBN_DEFS, "@" + fs(os.path.join(LIBS, "flags", "defines")), "-I" + fs(sketch_dir),
         "-iprefix", fs(os.path.join(LIBS, "include")) + "/", "@" + fs(os.path.join(LIBS, "flags", "includes")),
         "-I" + fs(os.path.join(LIBS, "qio_qspi", "include")), "-I" + fs(os.path.join(CORE, "cores", "esp32")),
         "-I" + fs(os.path.join(CORE, "variants", "XIAO_ESP32S3"))]
    for lib in ("WiFi", "Network", "Wire"):
        a.append("-I" + fs(os.path.join(CORE, "libraries", lib, "src")))
    for inc in extra_inc:
        a.append("-I" + fs(inc))
    return a


def check(path, args, tmp):
    src = path
    if path.endswith(".ino"):                 # как у сборщика: Arduino.h в начале
        src = os.path.join(tmp, os.path.basename(path) + ".cpp")
        with open(path, encoding="utf-8") as f, open(src, "w", encoding="utf-8") as g:
            g.write('#include <Arduino.h>\n#line 1 "%s"\n' % fs(path) + f.read())
    rsp = os.path.join(tmp, "args.rsp")
    with open(rsp, "w", encoding="utf-8") as f:
        f.write("\n".join('"%s"' % x if " " in x else x for x in args + [fs(src)]))
    for attempt in range(12):                # компилятор иногда не запускается — повторить
        r = subprocess.run([GXX, "@" + rsp], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if "cannot execute" not in r.stderr:
            break
    errs = [l for l in (r.stdout + r.stderr).splitlines() if "error" in l or "Error" in l]
    return r.returncode, errs, (r.stdout + r.stderr)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sketches = sys.argv[1:] or ["prime_lab"]
    libs = [os.path.join(ROOT, "firmware", "libraries", d, "src") for d in ("agent_common", "agent_sensors")]
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for sk in sketches:
            d = os.path.join(ROOT, "firmware", sk)
            if not os.path.isdir(d):
                print(f"нет скетча {sk}")
                continue
            files += [(d, os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith((".cpp", ".ino"))]
        for lib in libs[1:]:
            files += [(lib, os.path.join(lib, f)) for f in sorted(os.listdir(lib)) if f.endswith(".cpp")]
        for d, p in files:
            code, errs, full = check(p, args_for(d, libs), tmp)
            rel = os.path.relpath(p, ROOT)
            if code == 0:
                print(f"✓ {rel}")
            else:
                bad += 1
                print(f"✗ {rel}")
                for l in (errs or full.splitlines())[:12]:
                    print("    " + l[:300])
    print("ошибок нет" if not bad else f"файлов с ошибками: {bad}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
