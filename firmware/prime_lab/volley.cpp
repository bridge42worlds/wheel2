#include "volley.h"
#include "group.h"
#include "p3_windows.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_random.h"
#include "mbedtls/sha256.h"

namespace vl {

static const uint16_t SLOT_US = 3000;           // слот участника в такте
static const int      MAX_OWN = 15;             // своих находок в кадре такта 1 (36 байт на находку)
static const int      MAX_TAB = 60;             // строк общей таблицы в кадре такта 2 (9 байт на строку)
static const int      MAX_TAKTS = 1 + 2 * 4;    // такт 1 и до четырёх пар
static const int64_t  GATE_US = 100000;         // окна датчиков закрываются за 100 мс до залпа
static const int      NOFF = 4;                 // меток в оценке разницы часов
// Такт излучения (настройка ve) — перед тактом 1: платы без роутера расходятся по каналам 1, 6, 11 (по номеру слота)
// и в один миг передают свои находки; друг друга в этот миг не слышат. Потом все возвращаются на общий канал.
// От начала залпа: 0 — уйти на свой канал, 4 мс — кадр, 6–10 мс — назад, 12 мс — такт 1.
static const uint32_t EMIT_US = 12000;
static const uint8_t  EMIT_CH[3] = {1, 6, 11};
static const int      K_EMIT = 255;             // «номер такта» в кадре излучения

#pragma pack(push, 1)
struct Sync {                                   // метка ведущего
  uint8_t  h[group::HDR_LEN];
  uint32_t vid, cyc;                            // номер настройки, номер залпа
  int64_t  t_tx, t0;                            // время ведущего: сейчас и начало залпа
  uint16_t slot_us;
  uint8_t  nslots, pairs, kind;                 // kind: 0 — настоящий залп, 1 — контрольный
  uint16_t srv_ms;                              // окно сервера после залпа
  uint32_t len_ms;                              // длина цикла (для пропущенной метки)
  uint16_t pre_us;                              // такт излучения перед тактом 1, мкс (0 — нет)
};
struct Vol {                                    // кадр участника
  uint8_t  h[group::HDR_LEN];
  uint32_t vid, cyc;
  uint8_t  takt, slot, kind, n;
  uint16_t rev_ms;                              // период оборота у участника — ведущему, чтобы считать накачку
  uint8_t  data[1];
};
struct E1 { uint8_t num[32]; uint8_t sensor, pad; uint16_t dt_ms; };   // такт 1: число, датчик, мс от начала накачки
struct E2 { uint8_t author; uint8_t dg[8]; };                          // такт 2: автор и начало числа
struct E3 { uint8_t ck[8]; uint8_t cnt; };                             // такт 3: сумма таблицы и число строк
#pragma pack(pop)
static const size_t VOL_HDR = sizeof(Vol) - 1;
#pragma pack(push, 1)
struct Rly {                                    // кусок команды от посредника участнику
  uint8_t  h[group::HDR_LEN];
  uint8_t  to;                                  // номер адресата
  uint16_t seq;                                 // номер команды у посредника
  uint8_t  idx, cnt;                            // кусок и сколько их
  uint16_t total;                               // длина строки
  uint8_t  data[1];
};
#pragma pack(pop)
static const size_t RLY_HDR = sizeof(Rly) - 1;
static const int    RLY_FRAG = 480;             // байт строки в кадре
static const int    RLY_TEXT = 1100;            // наибольшая строка команды
struct RlyMsg { uint8_t to; uint8_t left; uint16_t seq, len; char text[RLY_TEXT]; };

struct Cfg {
  bool     on = false, master = false, ve = false;
  uint8_t  vn = 5, vr = 1, vc = 0, ng = 0;
  uint16_t vj = 40, vs = 80;
  uint8_t  grp[MAX_GROUP] = {};
  uint32_t cid = 0;
  uint32_t vid = 0;                             // метка залпа в кадрах: состав участников (у ведущего и участника номера настроек разные)
  int      me = 0, slot = -1;
};

struct Plan {                                   // один залп в своих часах
  bool     ok = false;
  uint32_t cyc = 0;
  int64_t  t0 = 0;
  uint16_t slot_us = SLOT_US;
  uint8_t  nslots = 0, pairs = 1, kind = 0;
  uint16_t srv_ms = 80;
  uint32_t len_ms = 0;
  uint32_t pre_us = 0;                          // такт излучения перед тактом 1
  int      takts() const { return 1 + 2 * pairs; }
  int64_t  takt_us() const { return (int64_t)nslots * slot_us; }
  int64_t  dur() const { return pre_us + takts() * takt_us(); }
  int64_t  srv_end() const { return t0 + dur() + (int64_t)srv_ms * 1000; }
};

struct Stats {                                  // для телеметрии (под s_mux)
  uint8_t  on = 0, master = 0, lock = 0, kind = 0, lbal = 0;
  int8_t   slot = -1;
  uint32_t cyc = 0, syn = 0, miss = 0, skip = 0, vol = 0, bal = 0, tx = 0, own_drop = 0, tab_full = 0;
  uint32_t em = 0, em_late = 0, em_skip = 0;    // тактов излучения; кадр не ушёл за 10 мс; пропущено (плата на роутере)
  int32_t  off = 0;                             // разница часов с ведущим, мкс (младшие 31 бит)
  uint16_t own = 0, tab = 0;                    // последний залп: своих находок, строк таблицы
  uint8_t  heard[3] = {};                       // последний залп: от скольких участников слышали такты 1, 2, 3
  int32_t  late_mean = 0, late_max = 0;         // приход чужих кадров относительно начала их слота, мкс
  int8_t   rssi = 0;
  uint32_t len_ms = 0;
  // ведущий
  uint32_t m_cyc = 0, m_sync = 0, m_bal = 0;
  uint16_t m_rev = 0;
  uint8_t  m_heard[3] = {}, m_lbal = 0, m_kind = 0;
};

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static Cfg           s_new;                     // под s_mux
static volatile bool s_new_set = false;
static Cfg           s_c;                       // задача протокола
static Stats         s_st, s_pub;
static volatile bool s_active = false, s_quiet = false;

// ---- участник ----
static Plan     s_cur, s_next;
static int64_t  s_off[NOFF];
static int      s_noff = 0;
static int64_t  s_off_now = 0;
static int      s_miss = 0;
static bool     s_sent[MAX_TAKTS], s_final = false;
static E1       s_own[MAX_OWN];
static int      s_nown = 0;
static E2       s_tab[96];
static int      s_ntab = 0;
static uint8_t  s_ck[MAX_GROUP][8];
static uint16_t s_got[MAX_TAKTS];               // от каких слотов слышали такт
static int64_t  s_late_sum = 0;
static int32_t  s_late_max = 0;
static int      s_late_n = 0, s_rssi_sum = 0, s_rssi_n = 0;
static int64_t  s_pump_t0 = 0;                  // начало текущей накачки, свои часы
static uint8_t  s_em = 0, s_em_home = 0;        // такт излучения: шаг (0 — не начат … 3 — сделан, 4 — пропущен) и общий канал
// ---- ведущий ----
static int64_t  m_t0 = 0, m_sync_at[2] = {};
static uint32_t m_cyc = 0;
static uint8_t  m_kind = 0, m_pairs = 1, m_nslots = 0;
static uint32_t m_pre = 0;                      // такт излучения в текущем залпе, мкс
static uint32_t m_len_ms = 0;
static float    m_rev_ms = 810.0f;
static uint16_t m_got[MAX_TAKTS];
static uint8_t  m_ck[MAX_GROUP][8];
static bool     m_final = true;
static int64_t  m_boot_sync = 0;
// ---- посредник ----
static Report   m_rep;                          // собирается, пока идёт залп
static Report   s_rep;                          // отдан задаче связи
static volatile bool     s_rep_ready = false;
static volatile uint32_t s_rep_lost = 0;
static QueueHandle_t     s_rq = nullptr;        // команды от задачи связи (указатели на RlyMsg)
static RlyMsg   m_rq[MAX_GROUP];                // что передаём участникам: по одной команде на адресата, по три цикла
static uint16_t m_rseq = 0;
static int      m_rl_i = -1, m_rl_f = 0;        // передача идёт: какая команда, какой кусок
static int64_t  m_rl_next = 0;
// ---- участник: команда от посредника ----
static Info     s_info;                         // под s_mux
static struct { uint16_t seq = 0; uint8_t cnt = 0, mask = 0; uint16_t total = 0; char buf[RLY_TEXT + 1]; } s_ra;
static uint16_t s_rdone = 0;
static volatile uint8_t  s_rok = 0;
static char     s_rtext[RLY_TEXT + 1];
static volatile bool     s_rready = false;
static uint16_t s_rready_seq = 0;

static uint8_t pick_kind(const Cfg& c) { return c.vc == 2 ? (uint8_t)(esp_random() & 1) : c.vc; }

static void tab_add(uint8_t author, const uint8_t* dg) {
  for (int i = 0; i < s_ntab; ++i)
    if (s_tab[i].author == author && memcmp(s_tab[i].dg, dg, 8) == 0) return;
  if (s_ntab >= (int)(sizeof s_tab / sizeof s_tab[0])) {
    ++s_st.tab_full;
    return;
  }
  s_tab[s_ntab].author = author;
  memcpy(s_tab[s_ntab].dg, dg, 8);
  ++s_ntab;
}

static void tab_sum(uint8_t out[8]) {           // сумма таблицы: строки по порядку (автор, число) → SHA-256
  static E2 t[96];
  memcpy(t, s_tab, sizeof(E2) * s_ntab);
  for (int i = 1; i < s_ntab; ++i) {            // вставками: строк немного
    E2 x = t[i];
    int j = i - 1;
    while (j >= 0 && memcmp(&t[j], &x, sizeof(E2)) > 0) {
      t[j + 1] = t[j];
      --j;
    }
    t[j + 1] = x;
  }
  uint8_t h[32];
  mbedtls_sha256((const uint8_t*)t, sizeof(E2) * s_ntab, h, 0);
  memcpy(out, h, 8);
}

static void reset_all() {
  s_cur = Plan();
  s_next = Plan();
  s_noff = 0;
  s_miss = 0;
  s_final = true;
  s_ntab = s_nown = 0;
  s_pump_t0 = esp_timer_get_time();
  m_t0 = 0;
  m_final = true;
  m_boot_sync = 0;
  s_st = Stats();
  s_active = s_quiet = false;
  p3::gate(true);
}

// ---------- ведущий ----------
static void m_send_sync(int64_t now) {
  Sync m = {};
  m.vid = s_c.vid;
  m.cyc = m_cyc;
  m.t_tx = now;
  m.t0 = m_t0;
  m.slot_us = SLOT_US;
  m.nslots = m_nslots;
  m.pairs = m_pairs;
  m.kind = m_kind;
  m.srv_ms = s_c.vs;
  m.len_ms = m_len_ms;
  m.pre_us = (uint16_t)m_pre;
  group::vl_send(M_SYNC, (uint8_t*)&m, sizeof m);
  ++s_st.m_sync;
}

static void m_plan(int64_t from, int64_t now, bool first) {
  const int64_t dur = m_pre + (int64_t)(1 + 2 * m_pairs) * m_nslots * SLOT_US;   // кончившегося залпа
  m_nslots = s_c.ng;
  m_pairs = s_c.vr;
  m_pre = s_c.ve ? EMIT_US : 0;
  const int64_t pump = (int64_t)(s_c.vn * m_rev_ms * 1000.0f) + (s_c.vj ? (int64_t)(esp_random() % (s_c.vj * 1000u)) : 0);
  const int64_t t0 = first ? now + 1500000 : from + dur + (int64_t)s_c.vs * 1000 + pump;
  m_len_ms = first ? 0 : (uint32_t)((t0 - from) / 1000);
  m_t0 = t0;
  ++m_cyc;
  m_kind = pick_kind(s_c);
  m_final = false;
  memset(m_got, 0, sizeof m_got);
  m_rep.nf = 0;
  m_rep.st_mask = 0;
  m_sync_at[0] = now;                           // метка — дважды: сейчас и через 10 мс
  m_sync_at[1] = now + 10000;
  m_boot_sync = now + 500000;
}

// Команды участникам: забрать у задачи связи; после метки времени — по куску раз в 2 мс (очередь радио не забить).
static void m_relay_tick(int64_t now) {
  RlyMsg* p = nullptr;
  while (s_rq && xQueueReceive(s_rq, &p, 0) == pdTRUE) {
    int at = -1;
    for (int i = 0; i < MAX_GROUP; ++i)
      if (m_rq[i].left && m_rq[i].to == p->to) at = i;          // новая команда тому же адресату — вместо прежней
    for (int i = 0; i < MAX_GROUP && at < 0; ++i)
      if (!m_rq[i].left) at = i;
    if (at >= 0) {
      m_rq[at] = *p;
      if (++m_rseq == 0) m_rseq = 1;
      m_rq[at].seq = m_rseq;
      m_rq[at].left = 3;
    }
    free(p);
  }
  if (m_rl_i < 0 || now < m_rl_next) return;
  while (m_rl_i < MAX_GROUP && !m_rq[m_rl_i].left) { ++m_rl_i; m_rl_f = 0; }
  if (m_rl_i >= MAX_GROUP) { m_rl_i = -1; return; }
  RlyMsg& m = m_rq[m_rl_i];
  const int cnt = (m.len + RLY_FRAG - 1) / RLY_FRAG;
  static uint8_t buf[RLY_HDR + RLY_FRAG + 8];
  Rly& r = *(Rly*)buf;
  memset(buf, 0, RLY_HDR);
  r.to = m.to;
  r.seq = m.seq;
  r.idx = (uint8_t)m_rl_f;
  r.cnt = (uint8_t)cnt;
  r.total = m.len;
  const int o = m_rl_f * RLY_FRAG;
  const int n = m.len - o < RLY_FRAG ? m.len - o : RLY_FRAG;
  memcpy(r.data, m.text + o, n);
  group::vl_send(M_RLY, buf, RLY_HDR + n);
  m_rl_next = now + 2000;
  if (++m_rl_f >= cnt) { --m.left; ++m_rl_i; m_rl_f = 0; }
}

static void m_tick(int64_t now) {
  if (!m_t0) m_plan(0, now, true);
  for (int i = 0; i < 2; ++i) {
    int64_t& t = m_sync_at[i];
    if (t && now >= t) {
      t = 0;
      m_send_sync(now);
      if (i == 1 && m_rl_i < 0) { m_rl_i = 0; m_rl_f = 0; m_rl_next = now + 2000; }   // после второй метки — команды участникам
    }
  }
  m_relay_tick(now);
  if (m_cyc == 1 && now < m_t0 - 200000 && now >= m_boot_sync) {   // до первого залпа — метка каждые полсекунды
    m_boot_sync = now + 500000;
    m_send_sync(now);
  }
  const int64_t dur = m_pre + (int64_t)(1 + 2 * m_pairs) * m_nslots * SLOT_US;
  if (!m_final && now >= m_t0 + dur) {          // залп кончился: что слышал ведущий, и следующая метка
    m_final = true;
    const int last = 2 * m_pairs;               // последний такт 3
    int n3 = 0;
    bool same = true;
    int first = -1;
    for (int s = 0; s < m_nslots && s < MAX_GROUP; ++s) {
      if (!(m_got[last] >> s & 1)) continue;
      ++n3;
      if (first < 0) first = s;
      else if (memcmp(m_ck[s], m_ck[first], 8)) same = false;
    }
    const bool bal = n3 == m_nslots && same;
    s_st.m_heard[0] = (uint8_t)__builtin_popcount(m_got[0]);
    s_st.m_heard[1] = (uint8_t)__builtin_popcount(m_got[last - 1]);
    s_st.m_heard[2] = (uint8_t)n3;
    s_st.m_lbal = bal;
    if (bal) ++s_st.m_bal;
    s_st.m_kind = m_kind;
    s_st.m_rev = (uint16_t)m_rev_ms;
    s_st.m_cyc = m_cyc;
    m_rep.cid = s_c.cid;                        // отчёт серверу: состояния участников и находки такта 1
    m_rep.cyc = m_cyc;
    m_rep.kind = m_kind;
    m_rep.bal = bal;
    m_rep.nslots = m_nslots;
    memcpy(m_rep.heard, s_st.m_heard, 3);
    m_rep.rev_ms = (uint16_t)m_rev_ms;
    if (!s_rep_ready) {
      memcpy(&s_rep, &m_rep, sizeof s_rep);
      s_rep_ready = true;
    } else {
      ++s_rep_lost;
    }
    m_plan(m_t0, now, false);
  }
}

static void m_on_vol(const Vol& v, size_t len, int8_t rssi) {
  if (v.cyc != m_cyc || v.slot >= MAX_GROUP || v.takt >= MAX_TAKTS) return;
  const bool dup = m_got[v.takt] >> v.slot & 1;
  m_got[v.takt] |= (uint16_t)(1u << v.slot);
  const size_t body = len - VOL_HDR;
  if (v.takt == 0 && v.rev_ms >= 200 && v.rev_ms <= 5000) m_rev_ms = 0.8f * m_rev_ms + 0.2f * v.rev_ms;
  if (v.takt == 0 && !dup) {                    // находки участника — в отчёт серверу
    const int n = v.n * sizeof(E1) <= body ? v.n : 0;
    for (int i = 0; i < n && m_rep.nf < REP_FINDS; ++i) {
      const E1& e = ((const E1*)v.data)[i];
      RepFind& r = m_rep.f[m_rep.nf++];
      r.author = v.h[3];
      r.sensor = e.sensor;
      r.dt_ms = e.dt_ms;
      memcpy(r.num, e.num, 32);
    }
  }
  if (v.takt >= 2 && !(v.takt & 1) && body >= sizeof(E3)) {
    memcpy(m_ck[v.slot], v.data, 8);
    if (body >= sizeof(E3) + sizeof(Status)) {  // последний такт: состояние участника
      memcpy(&m_rep.st[v.slot], v.data + sizeof(E3), sizeof(Status));
      m_rep.st_id[v.slot] = v.h[3];
      m_rep.st_rssi[v.slot] = rssi;
      m_rep.st_mask |= (uint16_t)(1u << v.slot);
    }
  }
}

// ---------- участник ----------
static void a_begin(int64_t now) {              // залп начинается: свои находки за накачку, таблица с нуля
  s_cur = s_next;
  s_next.ok = false;
  s_final = false;
  memset(s_sent, 0, sizeof s_sent);
  memset(s_got, 0, sizeof s_got);
  s_ntab = 0;
  s_late_sum = 0;
  s_late_max = 0;
  s_late_n = s_rssi_sum = s_rssi_n = 0;
  s_em = 0;
  static p3::Find f[p3::NFIND];
  const int n = p3::finds(f, p3::NFIND);
  s_nown = 0;
  int have = 0;
  for (int i = 0; i < n; ++i)
    if (f[i].t_us >= s_pump_t0 && f[i].t_us < s_cur.t0) ++have;
  int skip = have > MAX_OWN ? have - MAX_OWN : 0;   // не влезло в кадр — уходят последние
  s_st.own_drop += skip;
  for (int i = 0; i < n; ++i) {
    if (f[i].t_us < s_pump_t0 || f[i].t_us >= s_cur.t0) continue;
    if (skip) {
      --skip;
      continue;
    }
    E1& e = s_own[s_nown++];
    memcpy(e.num, s_cur.kind ? f[i].sham : f[i].num, 32);
    e.sensor = f[i].sensor;
    e.pad = 0;
    const int64_t dt = (f[i].t_us - s_pump_t0) / 1000;
    e.dt_ms = dt > 65535 ? 65535 : (uint16_t)dt;
    tab_add((uint8_t)s_c.me, e.num);
  }
}

static void a_send(int k) {
  static uint8_t buf[600];
  Vol& v = *(Vol*)buf;
  memset(buf, 0, VOL_HDR);
  v.vid = s_c.vid;
  v.cyc = s_cur.cyc;
  v.takt = (uint8_t)k;
  v.slot = (uint8_t)s_c.slot;
  v.kind = s_cur.kind;
  v.rev_ms = p3::period_ms();
  size_t len = VOL_HDR;
  if (k == 0 || k == K_EMIT) {
    v.n = (uint8_t)s_nown;
    memcpy(v.data, s_own, sizeof(E1) * s_nown);
    len += sizeof(E1) * s_nown;
  } else if (k & 1) {
    const int n = s_ntab > MAX_TAB ? MAX_TAB : s_ntab;
    v.n = (uint8_t)n;
    memcpy(v.data, s_tab, sizeof(E2) * n);
    len += sizeof(E2) * n;
  } else {
    E3 e;
    tab_sum(e.ck);
    e.cnt = (uint8_t)s_ntab;
    memcpy(s_ck[s_c.slot], e.ck, 8);
    s_got[k] |= (uint16_t)(1u << s_c.slot);
    v.n = 1;
    memcpy(v.data, &e, sizeof e);
    len += sizeof e;
    if (k == 2 * s_cur.pairs) {                 // последний такт: своё состояние — посреднику
      Info in;
      portENTER_CRITICAL(&s_mux);
      in = s_info;
      portEXIT_CRITICAL(&s_mux);
      Status st = {};
      st.cid = in.cid;
      st.rseq = s_rdone;
      st.rok = s_rok;
      st.temp = in.temp;
      st.mv = in.mv;
      st.heap_k = in.heap_k;
      st.primes = in.primes;
      st.cands = in.cands;
      st.wins = in.wins;
      st.drop = in.drop;
      st.serr = in.serr;
      st.vol = s_st.vol;
      st.bal = s_st.bal;
      st.rssi = s_st.rssi;
      st.flags = in.flags;
      st.emit = (uint16_t)s_st.em;
      memcpy(v.data + sizeof e, &st, sizeof st);
      len += sizeof st;
    }
  }
  group::vl_send(M_VOL, buf, len);
  ++s_st.tx;
}

// Такт излучения: t — мкс от начала залпа. Только у платы без роутера: на связи с роутером канал менять нельзя.
static void a_emit(int64_t t) {
  if (s_em == 0) {
    portENTER_CRITICAL(&s_mux);
    const bool off = s_info.flags & 1;
    portEXIT_CRITICAL(&s_mux);
    uint8_t pri = 0;
    wifi_second_chan_t sec = WIFI_SECOND_CHAN_NONE;
    if (!off || esp_wifi_get_channel(&pri, &sec) != ESP_OK || !pri) {
      s_em = 4;
      ++s_st.em_skip;
      return;
    }
    s_em_home = pri;
    const uint8_t ch = EMIT_CH[s_c.slot % 3];
    if (ch != pri) esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    s_em = 1;
  } else if (s_em == 1 && t >= 4000) {
    a_send(K_EMIT);
    s_em = 2;
  } else if (s_em == 2 && t >= 6000) {
    const bool idle = group::vl_tx_idle();
    if (!idle && t < 10000) return;
    if (!idle) ++s_st.em_late;
    esp_wifi_set_channel(s_em_home, WIFI_SECOND_CHAN_NONE);
    s_em = 3;
    ++s_st.em;
  }
}

static void a_final() {
  if (s_em == 1 || s_em == 2) {                 // не должно быть: залп кончился, а плата на чужом канале
    esp_wifi_set_channel(s_em_home, WIFI_SECOND_CHAN_NONE);
    s_em = 4;
    ++s_st.em_late;
  }
  s_final = true;
  const int last = 2 * s_cur.pairs;
  bool bal = true;
  for (int s = 0; s < s_cur.nslots && s < MAX_GROUP; ++s) {
    if (!(s_got[last] >> s & 1)) { bal = false; break; }
    if (memcmp(s_ck[s], s_ck[s_c.slot], 8)) { bal = false; break; }
  }
  ++s_st.vol;
  if (bal) ++s_st.bal;
  s_st.lbal = bal;
  s_st.kind = s_cur.kind;
  s_st.own = (uint16_t)s_nown;
  s_st.tab = (uint16_t)s_ntab;
  s_st.heard[0] = (uint8_t)__builtin_popcount(s_got[0]);
  s_st.heard[1] = (uint8_t)__builtin_popcount(s_got[last - 1]);
  s_st.heard[2] = (uint8_t)(__builtin_popcount(s_got[last]) - 1);   // без себя
  s_st.late_mean = s_late_n ? (int32_t)(s_late_sum / s_late_n) : 0;
  s_st.late_max = s_late_max;
  s_st.rssi = s_rssi_n ? (int8_t)(s_rssi_sum / s_rssi_n) : 0;
  s_pump_t0 = s_cur.srv_end();                  // накачка следующего цикла — после окна сервера
}

static volatile int64_t s_sync_t = 0;           // когда пришла последняя метка ведущего (0 — не было)

static void a_on_sync(const Sync& m, int64_t t_rx) {
  if (m.nslots == 0 || m.nslots > MAX_GROUP || m.pairs < 1 || m.pairs > 4) return;
  s_sync_t = t_rx ? t_rx : 1;
  s_off[s_noff % NOFF] = m.t_tx - t_rx;         // чем меньше задержка кадра, тем больше разница: берём наибольшую
  ++s_noff;
  int64_t off = s_off[0];
  for (int i = 1; i < NOFF && i < s_noff; ++i)
    if (s_off[i] > off) off = s_off[i];
  s_off_now = off;
  ++s_st.syn;
  s_miss = 0;
  Plan p;
  p.ok = true;
  p.cyc = m.cyc;
  p.t0 = m.t0 - off;
  p.slot_us = m.slot_us;
  p.nslots = m.nslots;
  p.pairs = m.pairs;
  p.kind = m.kind;
  p.srv_ms = m.srv_ms;
  p.len_ms = m.len_ms;
  p.pre_us = m.pre_us > 50000 ? 0 : m.pre_us;
  if (s_cur.ok && p.cyc == s_cur.cyc) return;   // метка идущего залпа (повтор при запуске) — план уже взят
  s_next = p;
}

static void a_on_vol(const Vol& v, size_t len, int8_t rssi, int64_t t_rx) {
  if (!s_cur.ok || s_final || v.cyc != s_cur.cyc || v.slot >= s_cur.nslots || v.slot >= MAX_GROUP) return;
  if (v.takt >= s_cur.takts() || (int)v.slot == s_c.slot) return;
  const uint8_t from = v.h[3];                  // номер автора — в заголовке кадра
  s_got[v.takt] |= (uint16_t)(1u << v.slot);
  const int64_t late = t_rx - (s_cur.t0 + s_cur.pre_us + v.takt * s_cur.takt_us() + (int64_t)v.slot * s_cur.slot_us);
  s_late_sum += late;
  ++s_late_n;
  const int32_t al = (int32_t)(late < 0 ? -late : late);
  if (al > s_late_max) s_late_max = al;
  if (rssi) {
    s_rssi_sum += rssi;
    ++s_rssi_n;
  }
  const size_t body = len - VOL_HDR;
  if (v.takt == 0) {
    const int n = v.n * sizeof(E1) <= body ? v.n : 0;
    for (int i = 0; i < n; ++i) tab_add(from, ((const E1*)v.data)[i].num);
  } else if (v.takt & 1) {
    const int n = v.n * sizeof(E2) <= body ? v.n : 0;
    for (int i = 0; i < n; ++i) tab_add(((const E2*)v.data)[i].author, ((const E2*)v.data)[i].dg);
  } else if (body >= sizeof(E3)) {
    memcpy(s_ck[v.slot], v.data, 8);
  }
}

static void a_tick(int64_t now) {
  if (s_next.ok && now >= s_next.t0) {
    if (now < s_next.t0 + s_next.dur()) a_begin(now);
    else {                                      // метка пришла слишком поздно — залп пропущен
      s_next.ok = false;
      ++s_st.skip;
    }
  }
  if (s_cur.ok && !s_final) {
    const int64_t t = now - s_cur.t0;
    if (t >= s_cur.dur()) {
      a_final();
    } else if (t >= 0) {
      if (s_cur.pre_us && s_em < 3) a_emit(t);
      const int64_t tt = t - s_cur.pre_us;      // такты — после такта излучения
      if (tt >= 0) {
        const int k = (int)(tt / s_cur.takt_us());
        if (!s_sent[k] && tt >= k * s_cur.takt_us() + (int64_t)s_c.slot * s_cur.slot_us) {
          s_sent[k] = true;
          a_send(k);
        }
      }
    }
  }
  // Метка не пришла: залп — по прежней поправке и прежней длине цикла, не больше трёх раз подряд
  if (s_cur.ok && s_final && !s_next.ok && s_cur.len_ms && now > s_cur.srv_end() + 300000) {
    if (++s_miss <= 3) {
      s_next = s_cur;
      s_next.cyc = s_cur.cyc + 1;
      s_next.t0 = s_cur.t0 + (int64_t)s_cur.len_ms * 1000;
      ++s_st.miss;
    } else {
      s_cur.ok = false;                         // время потеряно: радио — как без цикла, ждём метку
    }
  }
  const bool lock = s_cur.ok || s_next.ok;
  bool srv = !lock;
  bool pump = !lock;
  if (s_cur.ok) {
    const int64_t t = now - s_cur.t0;
    srv = t >= s_cur.dur() && now < s_cur.srv_end();
    pump = now >= s_cur.srv_end() && (!s_next.ok || now < s_next.t0 - GATE_US);
  } else if (s_next.ok) {                       // первый залп впереди: до него — накачка
    pump = now < s_next.t0 - GATE_US;
  }
  s_active = lock;
  s_quiet = lock && !srv;
  p3::gate(pump);
  s_st.lock = lock;
  s_st.cyc = s_cur.cyc;
  s_st.off = (int32_t)(s_off_now & 0x7fffffff);
  s_st.len_ms = s_cur.len_ms;
}

// ---------- снаружи ----------
void configure(const GenConfig& c, int my_id) {
  Cfg n;
  n.on = c.vl && c.run && c.ng > 0;
  n.master = c.vm;
  n.ve = c.ve;
  n.vn = c.vn;
  n.vr = c.vr;
  n.vc = c.vc;
  n.vj = c.vj;
  n.vs = c.vs;
  n.ng = c.ng;
  memcpy(n.grp, c.grp, sizeof n.grp);
  n.cid = c.cid;
  n.vid = 2166136261u ^ c.ng;                   // FNV-1a по списку участников
  for (int i = 0; i < c.ng && i < MAX_GROUP; ++i) n.vid = (n.vid ^ c.grp[i]) * 16777619u;
  n.me = my_id;
  n.slot = -1;
  for (int i = 0; i < c.ng && i < MAX_GROUP; ++i)
    if (c.grp[i] == my_id) n.slot = i;
  portENTER_CRITICAL(&s_mux);
  s_new = n;
  s_new_set = true;
  portEXIT_CRITICAL(&s_mux);
}

void tick(int64_t now) {
  if (s_new_set) {
    Cfg n;
    portENTER_CRITICAL(&s_mux);
    n = s_new;
    s_new_set = false;
    portEXIT_CRITICAL(&s_mux);
    // те же участники и роль, новый номер настройки — цикл не рвём (сменились параметры «на ходу»)
    const bool same_set = n.on == s_c.on && n.master == s_c.master && n.ng == s_c.ng && n.slot == s_c.slot &&
                          memcmp(n.grp, s_c.grp, sizeof n.grp) == 0;
    const bool same = same_set && n.cid == s_c.cid;
    s_c = n;
    // Новый номер настройки при тех же участниках: цикл начинается заново, но «ведущий слышен» остаётся — иначе
    // участник без роутера уходил бы на роутер после каждой настройки, принятой через посредника.
    if (!same) { reset_all(); if (!same_set) s_sync_t = 0; }
    s_st.on = s_c.on;
    s_st.master = s_c.master;
    s_st.slot = (int8_t)s_c.slot;
  }
  if (!s_c.on) return;
  if (s_c.master) m_tick(now);
  if (s_c.slot >= 0) a_tick(now);
  static int64_t last = 0;
  if (now - last >= 50000) {
    last = now;
    portENTER_CRITICAL(&s_mux);
    s_pub = s_st;
    portEXIT_CRITICAL(&s_mux);
  }
}

static void a_on_rly(const Rly& r, size_t len) {
  if (r.to != s_c.me || !r.seq || r.seq == s_rdone) return;        // не мне или уже принято
  if (r.cnt < 1 || r.cnt > 3 || r.idx >= r.cnt || r.total > RLY_TEXT) return;
  if (r.seq != s_ra.seq) {
    s_ra.seq = r.seq;
    s_ra.cnt = r.cnt;
    s_ra.total = r.total;
    s_ra.mask = 0;
  }
  const size_t n = len - RLY_HDR, o = (size_t)r.idx * RLY_FRAG;
  if (n > (size_t)RLY_FRAG || o + n > (size_t)RLY_TEXT) return;
  memcpy(s_ra.buf + o, r.data, n);
  s_ra.mask |= (uint8_t)(1u << r.idx);
  if (s_ra.mask != (uint8_t)((1u << s_ra.cnt) - 1) || s_rready) return;
  s_ra.buf[s_ra.total] = 0;
  memcpy(s_rtext, s_ra.buf, s_ra.total + 1);
  s_rready_seq = r.seq;
  s_rdone = r.seq;
  s_rok = 0;
  s_rready = true;
}

void on_rx(uint8_t type, const uint8_t* d, size_t len, int8_t rssi, int64_t t_rx) {
  if (type == M_RLY) {                          // команда от посредника — и когда свой цикл ещё не идёт
    if (len > RLY_HDR && !s_c.master) a_on_rly(*(const Rly*)d, len);
    return;
  }
  if (!s_c.on) return;
  if (type == M_SYNC) {
    if (len < sizeof(Sync)) return;
    const Sync& m = *(const Sync*)d;
    if (m.vid != s_c.vid || s_c.slot < 0) return;
    a_on_sync(m, t_rx);
  } else if (type == M_VOL) {
    if (len < VOL_HDR) return;
    const Vol& v = *(const Vol*)d;
    if (v.vid != s_c.vid) return;
    if (s_c.master) m_on_vol(v, len, rssi);
    if (s_c.slot >= 0) a_on_vol(v, len, rssi, t_rx);
  }
}

bool active() { return s_active; }
bool quiet() { return s_quiet; }
bool air_ok() { return !s_quiet; }

void set_info(const Info& i) {
  portENTER_CRITICAL(&s_mux);
  s_info = i;
  portEXIT_CRITICAL(&s_mux);
}

const Report* report_take() { return s_rep_ready ? &s_rep : nullptr; }
void report_done() { s_rep_ready = false; }
uint32_t report_lost() { return s_rep_lost; }

bool relay(uint8_t to, const char* text) {
  const size_t n = strlen(text);
  if (!to || !n || n >= (size_t)RLY_TEXT) return false;
  if (!s_rq) s_rq = xQueueCreate(MAX_GROUP, sizeof(RlyMsg*));
  RlyMsg* m = (RlyMsg*)malloc(sizeof(RlyMsg));
  if (!s_rq || !m) { free(m); return false; }
  m->to = to;
  m->left = 0;
  m->seq = 0;
  m->len = (uint16_t)n;
  memcpy(m->text, text, n + 1);
  if (xQueueSend(s_rq, &m, 0) != pdTRUE) { free(m); return false; }
  return true;
}

bool relay_take(char* out, size_t cap, uint16_t& seq) {
  if (!s_rready) return false;
  strlcpy(out, s_rtext, cap);
  seq = s_rready_seq;
  s_rready = false;
  return true;
}

void relay_result(uint16_t seq, bool ok) {
  if (seq == s_rdone) s_rok = ok ? 1 : 2;
}

uint32_t sync_age_ms() {
  const int64_t t = s_sync_t;
  if (!t) return 0xffffffffu;
  const int64_t d = (esp_timer_get_time() - t) / 1000;
  return d < 0 ? 0 : d > 0x7fffffff ? 0x7fffffffu : (uint32_t)d;
}

void add_tel(Out& o) {
  Stats s;
  portENTER_CRITICAL(&s_mux);
  s = s_pub;
  portEXIT_CRITICAL(&s_mux);
  add(o, ",\"on\":%u,\"m\":%u,\"lock\":%u,\"slot\":%d,\"cyc\":%lu,\"off\":%ld,\"syn\":%lu,\"miss\":%lu,\"skip\":%lu,"
         "\"vol\":%lu,\"bal\":%lu,\"lbal\":%u,\"kind\":%u,\"own\":%u,\"tab\":%u,\"h\":[%u,%u,%u],\"late\":[%ld,%ld],"
         "\"rssi\":%d,\"len\":%lu,\"tx\":%lu,\"odrop\":%lu,\"tfull\":%lu,\"em\":[%lu,%lu,%lu]",
      s.on, s.master, s.lock, (int)s.slot, (unsigned long)s.cyc, (long)s.off, (unsigned long)s.syn, (unsigned long)s.miss,
      (unsigned long)s.skip, (unsigned long)s.vol, (unsigned long)s.bal, s.lbal, s.kind, s.own, s.tab, s.heard[0],
      s.heard[1], s.heard[2], (long)s.late_mean, (long)s.late_max, (int)s.rssi, (unsigned long)s.len_ms,
      (unsigned long)s.tx, (unsigned long)s.own_drop, (unsigned long)s.tab_full, (unsigned long)s.em,
      (unsigned long)s.em_late, (unsigned long)s.em_skip);
  if (s.master)
    add(o, ",\"mc\":%lu,\"ms\":%lu,\"mb\":%lu,\"mlb\":%u,\"mk\":%u,\"mrev\":%u,\"mh\":[%u,%u,%u]", (unsigned long)s.m_cyc,
        (unsigned long)s.m_sync, (unsigned long)s.m_bal, s.m_lbal, s.m_kind, s.m_rev, s.m_heard[0], s.m_heard[1],
        s.m_heard[2]);
}

}  // namespace vl
