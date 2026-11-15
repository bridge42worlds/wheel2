// Источники случайности и смешивание.
//
// Только SRC_RNG — слова esp_random() идут в кандидата как есть.
// Любой другой набор — блоки SHA-256 по 32 байта; в каждый блок каждый
// отмеченный источник кладёт не меньше 256 зачтённых бит. Так число
// остаётся случайным, даже если все источники, кроме одного, плохие.
// Остаток блока достаётся следующему кандидату. Датчики на ободе (SRC_ACC,
// SRC_MAG) вместе с другими источниками — добавка: кладут накопленное, не ждут.
#pragma once
#include <Arduino.h>
#include "config.h"

enum { SI_RNG = 0, SI_CSI = 1, SI_ADC = 2, SI_JIT = 3, SI_BC = 4, SI_ACC = 5, SI_MAG = 6, SI_RRG = 7, SI_N = 8 };

struct SourceStats {
  uint64_t units = 0;            // слов / пакетов / отсчётов
  uint64_t bits = 0;             // зачтённых бит энтропии
  uint64_t us = 0;               // время сбора, мкс, включая ожидание пакетов
  uint64_t ones = 0, total = 0;  // младший бит сырья: единиц из total
  uint64_t same = 0, pairs = 0;  // младший бит совпал с предыдущим (у CSI — с той же
                                 // позицией в прошлом пакете); 0.5 — без памяти
};

struct EntropyStats {
  SourceStats s[SI_N];
  uint64_t sha_blocks = 0;       // блоков смешивания SHA-256
};

// ---------- ядро 0 (связь) ----------
void     entropy_init();               // один раз из setup, до запуска задач
void     entropy_select(uint8_t mask); // включить или выключить CSI по маске источников
void     entropy_pump();               // часто: пинг роутера с заданной частотой, пока включён CSI
void     entropy_set_ping(uint8_t per_s);   // пингов в секунду (10..200)
// CSI: 0 — с любых кадров, 1 — только с ответов роутера, 2 — только с кадров
// агентов из списка macs (сет B).
void     entropy_set_csi_filter(uint8_t mode, const uint8_t (*macs)[6], int n);
// Эфир сета B: содержимое услышанного кадра рассеяния — в запас источника SRC_BC.
void     entropy_feed_bcast(const uint8_t* d, size_t n);
uint32_t bcast_lost();                 // байт эфира B не влезло в запас
// «Информация» (07.11): блок файла с сервера (код, музыка) по кругу вместо эфира B в источнике SRC_BC.
bool     entropy_info_put(uint32_t off, const uint8_t* d, size_t n);
void     entropy_info_clear();
void     entropy_info_on(bool on);
uint32_t entropy_info_len();
bool     entropy_info_active();
// Датчики на ободе (задача rim_sensors): младшие байты показаний — в запас источника SI_ACC / SI_MAG.
// Кладёт, только если шум с этого датчика сейчас выбран (entropy_wants); present — датчик запаян.
bool     entropy_wants(int si);
void     entropy_sensor_present(int si, bool present);
void     entropy_feed_sensor(int si, const uint8_t* d, size_t n);
uint32_t sensor_lost(int si);          // байт не влезло в запас
// Раздача шума колеса (09.11): поток датчиков своего колеса приходит по ESP-NOW (group.cpp, команда WS) —
// датчики считаются «есть», даже если у самой платы их нет: ГСЧ вместо них не подставляется, блок ждёт поток.
void     entropy_share_rx(bool on);
// Записи датчиков целиком (09.11): зачёт в 1/256 бита на байт — сколько бит засчитывать на байт запаса датчика
// (запись целиком: 6 бит на запись любой длины; младшие байты: 1 бит / 2 бита на байт, как прежде).
void     entropy_sensor_format(int si, uint16_t bpb256);
bool     entropy_hashed();              // шум сейчас идёт через SHA-256 (raw 0): записи датчиков можно класть целиком
uint32_t csi_received();               // пакетов CSI с запуска
uint32_t csi_dropped();                // выброшено: очередь полна, счёт не успевает
uint32_t ping_replies();               // ответов роутера на пинг
void     csi_detail(uint32_t out[4]);  // вызовов CSI всего, коротких, 802.11n, от роутера

// ---------- ядро 1 (счёт) ----------
void entropy_reset();                  // забыть остаток блока (новые настройки)
// Заполнить out n байтами (n кратно 4). false — ожидание CSI прервано флагом
// abort (пришли новые настройки). tick вызывается во время долгого ожидания.
// mask — noise_mask(): источники (SRC_*, биты 0–7), как брать шум (GenConfig::raw, биты 8–10): 0 — блоки
// SHA-256, 1 — без хеша, младшие биты сырья, 2 — без хеша с выравниванием фон Неймана, 3 — XOR каждых xf бит
// сырья, 4 — экстрактор Тёплица xf·256 → 256 бит; во сколько сжимать (GenConfig::xf) — биты 12–16.
bool entropy_fill(uint8_t* out, size_t n, uint32_t mask, EntropyStats& st,
                  volatile bool* abort, void (*tick)());
// Самопроверка экстрактора Тёплица против эталона на Python (server/nullmodel.py). true — сошлось.
bool entropy_selftest();
