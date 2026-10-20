#!/usr/bin/env bash
# Сборка прошивки с номером версии из git и, если указан порт, заливка.
#   tools/build.sh <скетч> [порт]
# Номер версии вшивается в version.h и печатается агентом при старте.
# Каждая заливка записывается в logs/flash_history.csv.
set -euo pipefail
export PYTHONIOENCODING=utf-8   # иначе питон под Windows не может напечатать кириллицу
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="/c/Program Files/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe"
# LoopCore=0, EventsCore=0: setup() и обработчик событий Wi-Fi — на ядре 0,
# ядро 1 остаётся только для счёта (по умолчанию Arduino сажает их на ядро 1).
FQBN="${FQBN:-esp32:esp32:XIAO_ESP32S3:LoopCore=0,EventsCore=0}"
SKETCH="$1"; PORT="${2:-}"

if [ -n "${SKIP_BUILD:-}" ]; then
  # Только заливка готового образа: tools/new_board.py собирает раз на несколько плат.
  # Плата уже должна быть в реестре до той сборки — образ знает номера плат.
  VER="$(cat "$ROOT/build/$SKETCH/version.txt")"
  echo "заливка готового образа $SKETCH версии $VER"
else
# Плата на порту: прочитать её MAC и, если её нет в реестре, записать со
# следующим номером — до сборки, чтобы прошивка уже знала свой номер.
ESPTOOL="$(ls -d "$LOCALAPPDATA"/Arduino15/packages/esp32/tools/esptool_py/*/esptool.exe 2>/dev/null | tail -1)"
if [ -n "$PORT" ] && [ -n "$ESPTOOL" ]; then
  PMAC="$("$ESPTOOL" --port "$PORT" read-mac 2>/dev/null | grep -oiE 'MAC: *([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -1 | sed 's/MAC: *//I')"
  if [ -n "$PMAC" ]; then python "$ROOT/tools/register.py" "$PMAC"; fi
fi

# VER_SUFFIX — добавка к версии (проверочные сборки), EXTRA_FLAGS — флаги компилятора,
# например EXTRA_FLAGS=-DOTA_ROLLBACK_TEST VER_SUFFIX=-rbtest tools/build.sh prime_lab
VER="$(git -C "$ROOT" describe --always --dirty 2>/dev/null || echo nogit)${VER_SUFFIX:-}"
printf '#pragma once\n#define FW_VERSION "%s"\n#define FW_SKETCH "%s"\n' "$VER" "$SKETCH" \
  > "$ROOT/firmware/$SKETCH/version.h"
python "$ROOT/tools/gen_devices.py"
python "$ROOT/tools/gen_secrets.py"
echo "сборка $SKETCH версии $VER"
"$CLI" compile --fqbn "$FQBN" --libraries "$ROOT/firmware/libraries" \
  ${EXTRA_FLAGS:+--build-property "compiler.cpp.extra_flags=$EXTRA_FLAGS"} \
  --output-dir "$ROOT/build/$SKETCH" "$ROOT/firmware/$SKETCH" | tail -2
echo "$VER" > "$ROOT/build/$SKETCH/version.txt"      # версия рядом с образом
fi

if [ -n "$PORT" ]; then
  OUT="$("$CLI" upload -p "$PORT" --fqbn "$FQBN" --input-dir "$ROOT/build/$SKETCH" 2>&1)" || {
    echo "$OUT" | tail -8
    echo "заливка в $PORT не удалась: проверьте кабель; если не помогает — зажмите на плате BOOT, переподключите кабель, отпустите и повторите"
    exit 1
  }
  echo "$OUT" | grep -E "Connected|Hash of data verified|Hard resetting" | tail -3
  MAC="$(echo "$OUT" | grep -oiE 'MAC: *([0-9a-f]{2}:){5}[0-9a-f]{2}' | head -1 | sed 's/MAC: *//I')"
  mkdir -p "$ROOT/logs"
  [ -f "$ROOT/logs/flash_history.csv" ] || echo "time,port,mac,sketch,version" > "$ROOT/logs/flash_history.csv"
  echo "$(date '+%Y-%m-%d %H:%M:%S'),$PORT,${MAC:-?},$SKETCH,$VER" >> "$ROOT/logs/flash_history.csv"
  echo "залито: $PORT ${MAC:-} $SKETCH $VER"
fi
