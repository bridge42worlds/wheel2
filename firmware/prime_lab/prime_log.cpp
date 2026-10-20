#include "prime_log.h"

namespace plog {

static const size_t RING = 16384;
struct Head { uint32_t ms; uint16_t nbytes; uint8_t src, pad; };   // 8 байт перед числом

static uint8_t           s_ring[RING];
static volatile uint32_t s_w = 0, s_r = 0;      // байтовые указатели, растут без остановки (степень двойки)
static volatile uint32_t s_recs = 0, s_total = 0, s_lost = 0;
static volatile bool     s_on = false;
static uint32_t          s_cid = 0;
static portMUX_TYPE      s_mux = portMUX_INITIALIZER_UNLOCKED;

static void put_bytes(const uint8_t* d, size_t n) {
  for (size_t i = 0; i < n; ++i) s_ring[(s_w + i) % RING] = d[i];
  s_w += n;
}

static void get_bytes(uint32_t from, uint8_t* d, size_t n) {
  for (size_t i = 0; i < n; ++i) d[i] = s_ring[(from + i) % RING];
}

void configure(bool on, uint32_t cid) {
  portENTER_CRITICAL(&s_mux);
  if (cid != s_cid || on != s_on) {
    s_w = s_r = 0;
    s_recs = s_total = s_lost = 0;
    s_cid = cid;
  }
  s_on = on;
  portEXIT_CRITICAL(&s_mux);
}

void push(const uint8_t* num, uint16_t nbytes, uint8_t src) {
  if (!s_on || nbytes == 0 || nbytes > 256) return;
  Head h = {millis(), nbytes, src, 0};
  portENTER_CRITICAL(&s_mux);
  ++s_total;
  if (RING - (s_w - s_r) < sizeof h + nbytes) {
    ++s_lost;
  } else {
    put_bytes((const uint8_t*)&h, sizeof h);
    put_bytes(num, nbytes);
    ++s_recs;
  }
  portEXIT_CRITICAL(&s_mux);
}

uint32_t pending() { return s_recs; }
uint32_t total() { return s_total; }
uint32_t lost() { return s_lost; }

int build(Out& o) {
  portENTER_CRITICAL(&s_mux);
  uint32_t r = s_r, w = s_w;            // между r и w писатель ничего не трогает: читатель один
  portEXIT_CRITICAL(&s_mux);
  int n = 0;
  static char hex[516];
  while (r < w) {
    Head h;
    get_bytes(r, (uint8_t*)&h, sizeof h);
    if (h.nbytes == 0 || h.nbytes > 256) break;            // порча кольца — остановиться
    if (sizeof o.b - o.n < (size_t)(2 * h.nbytes + 28)) break;
    uint8_t num[256];
    get_bytes(r + sizeof h, num, h.nbytes);
    for (int i = 0; i < h.nbytes; ++i) snprintf(hex + 2 * i, 3, "%02X", num[i]);
    add(o, "%s[%lu,\"%s\",%u]", n ? "," : "", (unsigned long)h.ms, hex, h.src);
    r += sizeof h + h.nbytes;
    ++n;
  }
  return n;
}

void commit(int n) {
  portENTER_CRITICAL(&s_mux);
  for (int i = 0; i < n && s_recs > 0; ++i) {
    Head h;
    get_bytes(s_r, (uint8_t*)&h, sizeof h);
    s_r += sizeof h + h.nbytes;
    --s_recs;
  }
  portEXIT_CRITICAL(&s_mux);
}

}  // namespace plog
