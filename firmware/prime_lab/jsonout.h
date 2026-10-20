// Сборка JSON-пакета телеметрии в буфер фиксированного размера.
// Отдельным файлом: в .ino Arduino вставляет прототипы функций выше
// объявлений типов, и тип из самого .ino в прототипе не виден.
#pragma once
#include <Arduino.h>
#include <stdarg.h>

struct Out {
  char b[1460];        // NetworkUDP шлёт не больше 1460 байт одним пакетом
  int  n = 0;
  bool over = false;   // не влезло — пакет не отправляется
};

inline void add(Out& o, const char* fmt, ...) {
  if (o.over) return;
  va_list ap;
  va_start(ap, fmt);
  int k = vsnprintf(o.b + o.n, sizeof o.b - o.n, fmt, ap);
  va_end(ap);
  if (k < 0 || k >= (int)sizeof o.b - o.n) o.over = true;
  else o.n += k;
}

inline void add_arr(Out& o, const char* key, const uint64_t* v, int n, uint32_t div) {
  add(o, ",\"%s\":[", key);
  for (int i = 0; i < n; ++i) add(o, "%s%llu", i ? "," : "", (unsigned long long)(v[i] / div));
  add(o, "]");
}
