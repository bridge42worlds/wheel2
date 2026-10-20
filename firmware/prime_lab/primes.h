// Проверка кандидата: отсев малыми делителями, затем Миллер — Рабин по первым
// nb базам из BASES. 32 бита — свой код на 64-битном умножении; длиннее —
// mbedtls_mpi с аппаратным ускорителем RSA чипа (CONFIG_MBEDTLS_HARDWARE_MPI=y).
#pragma once
#include <Arduino.h>
#include "config.h"

// Все счётчики — накопительно с момента применения настроек. Время — в тактах
// процессора (240 МГц); в микросекунды переводит телеметрия.
struct PrimeStats {
  uint64_t cand = 0;     // кандидатов
  uint64_t td_rej = 0;   // отсеяно делителями
  uint64_t mr_in = 0;    // дошло до Миллера — Рабина
  uint64_t primes = 0;   // прошло все nb баз
  uint64_t fp = 0;       // из них составные (точная проверка есть только для 32 бит)
  uint64_t dis = 0;      // режим «все базы»: одни базы сказали «простое», другие «составное»
  uint64_t err = 0;      // ошибки mbedtls (нехватка памяти)
  uint64_t bt[MAX_BASES] = {};    // проверено базой i
  uint64_t br[MAX_BASES] = {};    // база i сказала «составное»
  uint64_t bcyc[MAX_BASES] = {};  // такты на базу i
  uint64_t td_cyc = 0, mr_cyc = 0;
};

void primes_init();                         // таблица нечётных простых до 20000
void primes_configure(const GenConfig& c);  // группы делителей под порог td

// bytes — bits/8 случайных байт; старший и младший биты выставляются здесь
// (длина ровно bits, число нечётное). true — кандидат прошёл все nb баз;
// тогда в hex (если не nullptr) — его шестнадцатеричная запись.
bool test_candidate(const uint8_t* bytes, const GenConfig& c, PrimeStats& st, char* hex, size_t hexlen,
                    uint8_t* prime_out = nullptr);   // prime_out — само простое (длина bits/8), если нашлось
// То же для кандидата, уже прошедшего отсев (sieve_candidate на другом ядре): только Миллер — Рабин.
bool test_sieved(const uint8_t* b, const GenConfig& c, PrimeStats& st, char* hex, size_t hexlen,
                 uint8_t* prime_out = nullptr);

// Части проверки по отдельности — для совместного режима: мастер отсеивает,
// каждый агент проверяет своими базами.
// Отсев: выставляет старший бит и нечётность прямо в b; true — уцелел.
bool sieve_candidate(uint8_t* b, const GenConfig& c, PrimeStats& st);
// Миллер — Рабин базами idx[0..n-1] (номера в BASES); stop_first — обрыв на первой
// «составное». Возвращает номер базы-свидетеля, -1 — никто не нашёл, -2 — ошибка.
int  mr_bases(const uint8_t* b, const GenConfig& c, const uint8_t* idx, int n, bool stop_first,
              PrimeStats& st, int* passed);
void number_hex(const uint8_t* b, int nbytes, char* hex, size_t hexlen);

bool primes_selftest();                     // известные простые и псевдопростые
