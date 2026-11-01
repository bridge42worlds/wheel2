// Большие буферы — в PSRAM (у XIAO ESP32-S3 8 МБ, сборка с PSRAM=opi): внутренняя память остаётся Wi-Fi,
// задачам и загрузке прошивки по Wi-Fi. 28.10.2026: у агентов с датчиками обода (№6, №20) свободной внутренней
// было 36–43 КБ, загрузка прошивки съедала её до 9 КБ и обрывалась. Нет PSRAM — во внутренней, как раньше.
#pragma once
#include <Arduino.h>
#include "esp_heap_caps.h"

inline void* big_alloc(size_t n) {
  void* p = heap_caps_calloc(1, n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) p = calloc(1, n);
  if (!p) {
    Serial.printf("big_alloc: no memory for %u bytes\n", (unsigned)n);
    abort();
  }
  return p;
}
