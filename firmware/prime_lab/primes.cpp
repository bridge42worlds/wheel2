#include "primes.h"
#include "esp_cpu.h"
#include "mbedtls/bignum.h"

static inline uint32_t cyc() { return esp_cpu_get_cycle_count(); }

// ---------- малые простые ----------
static const int PMAX = 20000;
static uint16_t s_pr[2300];          // нечётные простые 3..19997 (их 2261)
static int      s_npr = 0;
static int      s_ntd = 0;           // сколько из них меньше порога td

// Для длинных чисел делим не на каждое простое, а на произведение нескольких
// подряд (меньше 2^32), и уже остаток проверяем по каждому простому группы —
// так в разы быстрее. Остаток считаем сами по 32-битным словам:
// mbedtls_mpi_mod_int для этого не годится — она делит по полслова и даёт
// верный остаток только для делителей меньше 2^16 (на этом 03.10.2026
// терялось 84 % простых).
struct Group { uint32_t prod; uint16_t first, count; };
// Таблица в двух экземплярах: новая настройка строится в свободном, потом переключается номер активного.
// Отсев зовут два ядра (gen_task и задача группы в эстафете с двумя ядрами), и задачу группы при смене
// настройки никто не останавливает — по полуперестроенной таблице она могла ложно отсеять кандидата (25.10.2026).
struct Table { Group gr[1100]; int ngr; int ntd; };   // групп при td = 20000 — 1082 (по 2–3 простых в произведении)
static Table        s_tab[2];
static volatile int s_act = 0;       // номер активной таблицы

void primes_init() {
  static uint8_t comp[PMAX / 8 + 1];   // бит = составное
  memset(comp, 0, sizeof comp);
  for (int i = 2; i * i < PMAX; ++i)
    if (!(comp[i >> 3] & (1 << (i & 7))))
      for (int j = i * i; j < PMAX; j += i) comp[j >> 3] |= 1 << (j & 7);
  s_npr = 0;
  for (int i = 3; i < PMAX; ++i)
    if (!(comp[i >> 3] & (1 << (i & 7)))) s_pr[s_npr++] = i;
}

void primes_configure(const GenConfig& c) {
  Table& t = s_tab[1 - s_act];        // строим в свободной, читатели пока ходят в активную
  t.ntd = 0;
  while (t.ntd < s_npr && s_pr[t.ntd] < c.td) ++t.ntd;
  t.ngr = 0;
  for (int i = 0; i < t.ntd && t.ngr < (int)(sizeof t.gr / sizeof t.gr[0]);) {
    uint64_t prod = 1;
    int first = i;
    while (i < t.ntd && prod * s_pr[i] < (1ULL << 32)) prod *= s_pr[i++];
    t.gr[t.ngr++] = {(uint32_t)prod, (uint16_t)first, (uint16_t)(i - first)};
  }
  s_ntd = t.ntd;
  s_act = 1 - s_act;                  // переключение одним словом: читатель видит либо старую, либо новую таблицу целиком
}

// ---------- 32 бита: свой код ----------
static inline uint32_t mulmod32(uint32_t a, uint32_t b, uint32_t m) { return (uint64_t)a * b % m; }

static uint32_t powmod32(uint32_t a, uint32_t e, uint32_t m) {
  uint32_t r = 1;
  a %= m;
  while (e) {
    if (e & 1) r = mulmod32(r, a, m);
    a = mulmod32(a, a, m);
    e >>= 1;
  }
  return r;
}

// n - 1 = d * 2^s. true — «вероятно простое» по базе a.
static bool mr32(uint32_t n, uint32_t d, int s, uint32_t a) {
  uint32_t x = powmod32(a, d, n);
  if (x == 1 || x == n - 1) return true;
  for (int i = 1; i < s; ++i) {
    x = mulmod32(x, x, n);
    if (x == n - 1) return true;
    if (x == 1) return false;
  }
  return false;
}

// Точная проверка для любого 32-битного n > 61: базы 2, 7, 61 (Jaeschke, 1993).
static bool exact32(uint32_t n) {
  uint32_t d = n - 1;
  int s = 0;
  while (!(d & 1)) { d >>= 1; ++s; }
  return mr32(n, d, s, 2) && mr32(n, d, s, 7) && mr32(n, d, s, 61);
}

// ---------- длинные числа: mbedtls ----------
static mbedtls_mpi N, NM1, D, X, T, A, RR, TWO;
static bool s_mpi_ready = false;
static bool s_selftest = false;   // самопроверка: число как есть, без старшего бита, путь mbedtls

static void mpi_prepare() {
  if (s_mpi_ready) return;
  for (mbedtls_mpi* m : {&N, &NM1, &D, &X, &T, &A, &RR, &TWO}) mbedtls_mpi_init(m);
  mbedtls_mpi_lset(&TWO, 2);
  s_mpi_ready = true;
}

// 1 — «вероятно простое», 0 — составное, -1 — ошибка mbedtls.
// RR — кэш R^2 mod N для ускорителя, общий для всех баз одного кандидата.
static int mr_mpi(uint32_t base, size_t s) {
  if (mbedtls_mpi_lset(&A, base) || mbedtls_mpi_exp_mod(&X, &A, &D, &N, &RR)) return -1;
  if (mbedtls_mpi_cmp_int(&X, 1) == 0 || mbedtls_mpi_cmp_mpi(&X, &NM1) == 0) return 1;
  for (size_t i = 1; i < s; ++i) {
    if (mbedtls_mpi_exp_mod(&T, &X, &TWO, &N, &RR)) return -1;
    mbedtls_mpi_swap(&X, &T);
    if (mbedtls_mpi_cmp_mpi(&X, &NM1) == 0) return 1;
    if (mbedtls_mpi_cmp_int(&X, 1) == 0) return 0;
  }
  return 0;
}

// Остаток от деления числа (32-битные слова, старшее первым) на m < 2^32.
static uint32_t mod32(const uint32_t* w, int nw, uint32_t m) {
  uint64_t r = 0;
  for (int i = 0; i < nw; ++i) r = ((r << 32) | w[i]) % m;
  return (uint32_t)r;
}

static inline uint32_t be32(const uint8_t* b) {   // старший байт первым, как у mbedtls
  return (uint32_t)b[0] << 24 | (uint32_t)b[1] << 16 | (uint32_t)b[2] << 8 | b[3];
}

static inline bool native32(const GenConfig& c) { return c.bits == 32 && !s_selftest; }

static bool divisible(const uint8_t* b, const GenConfig& c) {
  const Table& t = s_tab[s_act];      // снимок активной таблицы на весь отсев одного кандидата
  if (native32(c)) {
    uint32_t n = be32(b);
    for (int i = 0; i < t.ntd; ++i)
      if (n % s_pr[i] == 0) return true;
    return false;
  }
  if (!t.ngr) return false;
  const int nw = c.bits / 32;
  uint32_t w[64];
  for (int i = 0; i < nw; ++i) w[i] = be32(b + 4 * i);
  for (int g = 0; g < t.ngr; ++g) {
    uint32_t r = mod32(w, nw, t.gr[g].prod);
    for (int k = 0; k < t.gr[g].count; ++k)
      if (r % s_pr[t.gr[g].first + k] == 0) return true;
  }
  return false;
}

bool sieve_candidate(uint8_t* b, const GenConfig& c, PrimeStats& st) {
  const int nbytes = c.bits / 8;
  if (!s_selftest) {               // длина ровно bits, число нечётное
    b[0] |= 0x80;
    b[nbytes - 1] |= 1;
  }
  ++st.cand;
  uint32_t c0 = cyc();
  bool div = divisible(b, c);
  st.td_cyc += cyc() - c0;
  if (div) ++st.td_rej;
  return !div;
}

int mr_bases(const uint8_t* b, const GenConfig& c, const uint8_t* idx, int n, bool stop_first,
             PrimeStats& st, int* passed_out) {
  ++st.mr_in;
  uint32_t c1 = cyc();
  int witness = -1, passed = 0;
  if (native32(c)) {
    uint32_t nn = be32(b);
    uint32_t d = nn - 1;
    int s = 0;
    while (!(d & 1)) { d >>= 1; ++s; }
    for (int k = 0; k < n; ++k) {
      int i = idx[k];
      uint32_t b0 = cyc();
      bool ok = mr32(nn, d, s, BASES[i]);
      st.bcyc[i] += cyc() - b0;
      ++st.bt[i];
      if (ok) {
        ++passed;
      } else {
        ++st.br[i];
        if (witness < 0) witness = i;
        if (stop_first) break;
      }
    }
  } else {
    mpi_prepare();
    if (mbedtls_mpi_read_binary(&N, b, c.bits / 8)) {
      ++st.err;
      return -2;
    }
    mbedtls_mpi_sub_int(&NM1, &N, 1);
    size_t s = mbedtls_mpi_lsb(&NM1);
    mbedtls_mpi_copy(&D, &NM1);
    mbedtls_mpi_shift_r(&D, s);
    mbedtls_mpi_free(&RR);              // новый модуль — кэш R^2 считается заново
    for (int k = 0; k < n; ++k) {
      int i = idx[k];
      uint32_t b0 = cyc();
      int r = mr_mpi(BASES[i], s);
      st.bcyc[i] += cyc() - b0;
      if (r < 0) {
        ++st.err;
        witness = -2;
        break;
      }
      ++st.bt[i];
      if (r == 1) {
        ++passed;
      } else {
        ++st.br[i];
        if (witness < 0) witness = i;
        if (stop_first) break;
      }
    }
  }
  st.mr_cyc += cyc() - c1;
  if (passed_out) *passed_out = passed;
  return witness;
}

void number_hex(const uint8_t* b, int nbytes, char* hex, size_t hexlen) {
  size_t k = 0;
  for (int i = 0; i < nbytes && k + 2 < hexlen; ++i, k += 2) snprintf(hex + k, 3, "%02X", b[i]);
  hex[k] = 0;
}

bool test_candidate(const uint8_t* bytes, const GenConfig& c, PrimeStats& st, char* hex, size_t hexlen,
                    uint8_t* prime_out) {
  uint8_t b[256];
  memcpy(b, bytes, c.bits / 8);
  if (!sieve_candidate(b, c, st)) return false;
  return test_sieved(b, c, st, hex, hexlen, prime_out);
}

bool test_sieved(const uint8_t* b, const GenConfig& c, PrimeStats& st, char* hex, size_t hexlen, uint8_t* prime_out) {
  uint8_t idx[MAX_BASES];
  for (int i = 0; i < c.nb; ++i) idx[i] = i;
  int passed = 0;
  int w = mr_bases(b, c, idx, c.nb, !c.all, st, &passed);
  if (w != -1) {
    if (w >= 0 && passed) ++st.dis;   // кандидат обманул хотя бы одну базу, но другая его поймала
    return false;
  }
  ++st.primes;
  if (native32(c) && !exact32(be32(b))) ++st.fp;
  if (hex) number_hex(b, c.bits / 8, hex, hexlen);
  if (prime_out) memcpy(prime_out, b, c.bits / 8);
  return true;
}

// ---------- самопроверка ----------
// Числа проверены на компьютере (Python, 12 баз + 20 случайных). Сильно
// псевдопростые «обманщики»: 3215031751 = 151*751*28351 проходит базы 2, 3, 5, 7
// и ловится 11; 3825123056546413051 (62 бита) проходит первые 11 баз, ловит 37.
// td > 0 — заодно проверяется отсев: простое он пропускать обязан, а 2^256-1
// (делится на 3) — отсеять.
static bool check(const char* hexnum, int bits, int nb, int td, bool mpi, bool expect, bool expect_td = false) {
  uint8_t b[256] = {0};
  mbedtls_mpi m;
  mbedtls_mpi_init(&m);
  mbedtls_mpi_read_string(&m, 16, hexnum);
  mbedtls_mpi_write_binary(&m, b, bits / 8);
  mbedtls_mpi_free(&m);
  GenConfig c;
  c.bits = bits;
  c.nb = nb;
  c.td = td;
  primes_configure(c);
  PrimeStats st;
  s_selftest = mpi;
  bool got = test_candidate(b, c, st, nullptr, 0);
  s_selftest = false;
  bool ok = got == expect && (st.td_rej == 1) == expect_td && st.err == 0;
  if (!ok)
    Serial.printf("SELFTEST FAIL %.16s.. bits=%d nb=%d td=%d mpi=%d got=%d td_rej=%llu err=%llu\n",
                  hexnum, bits, nb, td, mpi, got, st.td_rej, st.err);
  return ok;
}

static const char* OAKLEY2 =    // RFC 2409, группа 2: простое 1024 бита
  "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
  "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
  "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF";
static const char* MODP14 =     // RFC 3526, группа 14: простое 2048 бит
  "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
  "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
  "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
  "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
  "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
  "15728E5A8AACAA68FFFFFFFFFFFFFFFF";

bool primes_selftest() {
  bool ok = true;
  // 32 бита, свой код
  ok &= check("BFA17DC7", 32, 4, 0, false, true);      // 3215031751: 4 базы обмануты
  ok &= check("BFA17DC7", 32, 5, 0, false, false);     // база 11 ловит
  ok &= check("BFA17DC7", 32, 4, 1000, false, false, true);   // но делитель 151 отсеивает
  ok &= check("FFFFFFFB", 32, 12, 20000, false, true); // 2^32-5 — простое, отсев не трогает
  ok &= check("FFFFFFFF", 32, 1, 0, false, false);     // 2^32-1 — составное
  // те же числа через mbedtls
  ok &= check("BFA17DC7", 32, 4, 0, true, true);
  ok &= check("BFA17DC7", 32, 5, 0, true, false);
  ok &= check("BFA17DC7", 32, 4, 1000, true, false, true);
  ok &= check("351591274F9AF9FB", 64, 11, 0, true, true);    // 11 баз обмануты
  ok &= check("351591274F9AF9FB", 64, 12, 0, true, false);   // база 37 ловит
  ok &= check("FFFFFFFFFFFFFFC5", 64, 12, 20000, true, true); // 2^64-59 — простое
  ok &= check("FFFFFFFFFFFFFFC3", 64, 12, 0, true, false);    // 2^64-61 — составное
  ok &= check("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF43", 256, 8, 20000, true, true);  // 2^256-189
  ok &= check("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF45", 256, 8, 0, true, false);     // 2^256-187
  ok &= check("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", 256, 8, 1000, true, false, true); // 2^256-1
  // простое p = 1 mod 16 (s = 4): проверяет возведения в квадрат
  ok &= check("952349B832226707B626AD95642D6BD675E3944E8DCD531023A2258F93DE63D1", 256, 12, 20000, true, true);
  ok &= check(OAKLEY2, 1024, 4, 20000, true, true);
  ok &= check(MODP14, 2048, 2, 20000, true, true);
  return ok;
}
