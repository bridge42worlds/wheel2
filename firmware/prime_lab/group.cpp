#include "group.h"
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_now.h>
#include "esp_timer.h"
#include "esp_random.h"
#include "mbedtls/sha256.h"
#include "freertos/queue.h"
#include <agent_common.h>
#include "primes.h"
#include "entropy.h"
#include "prime_log.h"

namespace group {

// ---------- сообщения ESP-NOW ----------
// Третья версия протокола: число следующего цикла едет вместе с итогом, у
// мастера запас уцелевших кандидатов, кусочки шума набраны заранее. Метка "W4" —
// то же, но до 16 агентов (маска нашедших — 16 бит); "W5" — плюс эстафета и каскад
// (gv, сообщение TURN). Прошлые версии не принимаются.
enum : uint8_t { M_REQ = 1, M_PCS = 2, M_NUM = 3, M_RES = 4, M_NXT = 5, M_ASK = 6,
                 M_PRIME = 7, M_SCAT = 8, M_CHAIN = 9,    // рассеяние: простое A, кадр B, шаг эстафеты B
                 M_TURN = 10,                             // эстафета, каскад: ход, сверка, подтверждение
                 M_REST = 11,                             // эстафета, каскад: «отдыхаю» / «снова в деле»
                 M_ALIVE = 12 };                          // «пульс»: раз в секунду всем, в любом режиме

#pragma pack(push, 1)
struct Hdr { uint8_t m0, m1, type, from; uint32_t gs, cycle; uint8_t round; };
struct Req { Hdr h; uint8_t K; uint16_t pb; };
struct Pcs { Hdr h; uint8_t K; uint16_t pb; uint8_t data[1]; };
struct Num { Hdr h; uint16_t bits; uint8_t data[1]; };
struct Res { Hdr h; int8_t witness; uint8_t tests; uint32_t us; uint32_t tag; };
// Итог цикла h.cycle и начало следующего: nn = 1 — в data число следующего
// цикла, за ним rn чисел запаса (их получает только новый мастер); want = 1 —
// запас пуст: всем сразу слать новому мастеру кусочки; span — сколько цикл шёл
// у мастера, мкс (по нему прошлый мастер мерит, сколько идёт итог).
struct Nxt { Hdr h; uint8_t verdict, next; uint16_t finders; uint8_t want, nn, rn; uint16_t bits; uint32_t span; uint8_t data[1]; };
struct Ask { Hdr h; uint8_t what; };
// Рассеяние. h.cycle — номер простого у нашедшего его агента A.
struct Prm { Hdr h; uint16_t bits; uint8_t data[1]; };                 // простое от A, всем
struct Sct { Hdr h; uint8_t kind, sf, piece, pieces, ofrom; uint16_t len; uint8_t data[1]; };   // от B, в эфир
static const uint8_t K_BG = 5;       // вид кадра рассеяния: фон (шум агента B без простого)
struct Chn { Hdr h; uint8_t step, ofrom; uint16_t len; uint8_t data[1]; };   // эстафета B → следующему B
// Эстафета и каскад: ход h.cycle окончен. next — кому ход (0 — ход не передаётся);
// has_prime — в data найденное простое: получатель сверяет его одной другой базой;
// за ним rn чисел запаса новому проверяющему. Без простого и запаса — подтверждение:
// ход принят (h.cycle — уже новый ход). span — сколько шёл ход, мкс.
struct Trn { Hdr h; uint8_t next, primes, has_prime, rn; uint16_t bits; uint32_t span; uint8_t data[1]; };
struct Rst { Hdr h; uint8_t on, pad; uint16_t secs; };   // отдых по очереди: on 1 — ушёл, 0 — вернулся
struct Alv { Hdr h; uint8_t run; int8_t temp; uint32_t up_s; };   // пульс: считает ли, кристалл °C, с запуска
#pragma pack(pop)

static const size_t  PCS_HDR = sizeof(Pcs) - 1;
static const size_t  NUM_HDR = sizeof(Num) - 1;
static const size_t  NXT_HDR = sizeof(Nxt) - 1;
static const size_t  PRM_HDR = sizeof(Prm) - 1;
static const size_t  SCT_HDR = sizeof(Sct) - 1;
static const size_t  CHN_HDR = sizeof(Chn) - 1;
static const size_t  TRN_HDR = sizeof(Trn) - 1;
static const size_t  MSG_MAX = 600;            // наш предел длины сообщения
static const int     K_MAX = 64;               // кандидатов за один сбор
static const char    VER = '5';
static const uint8_t BCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

// ---------- события задачи протокола ----------
enum : uint8_t { EV_RX = 1, EV_JOB = 2, EV_CFG = 3, EV_PRIME = 4, EV_REST = 5 };   // EV_REST: len — секунд, 0 — назад
struct Event {
  uint8_t   kind;
  uint8_t   mac[6];
  int8_t    rssi;
  uint16_t  len;
  GenConfig cfg;                     // EV_CFG
  union {
    uint8_t   data[MSG_MAX];         // EV_RX
    JobResult res;                   // EV_JOB
  };
};

static QueueHandle_t     s_evq = nullptr, s_jobq = nullptr;
static uint8_t           s_job_buf[2048];      // кандидаты для отсева и проверки: до K × длина числа
static volatile uint32_t s_tx_fail = 0, s_rx_drop = 0, s_tx_cb = 0;
static volatile uint32_t s_ok_to[MAX_GROUP], s_fail_to[MAX_GROUP];   // по участникам, из задачи Wi-Fi
// Эстафета и каскад: участник молчит — кусочков нет после трёх просьб или ход не принял. Его кусочки
// сразу заменяет свой шум, ход идёт мимо него; любой кадр от него — снова ждём. Замечено 05.10.2026:
// у №6 замерло радио, проверяющий №4 без его кусочков бросал круг за кругом (3529 раз), счёт стоял.
static bool              s_silent[MAX_GROUP];
// Отдых по очереди (docs/thermal.md, § 4.3). Сервер шлёт самому горячему из урезанных «REST s=…»: агент
// доводит свой ход (эстафета), говорит группе «отдыхаю» — его сразу не ждут, как молчащего, — ставит
// 80 МГц и ничего не считает и не шлёт; через s секунд или по «BACK» — «снова в деле» и обратно в группу.
static bool              s_resting = false, s_rest_pending = false;
static int64_t           s_rest_until = 0;
static uint16_t          s_rest_cpu = 0;         // частота до отдыха

// ---------- состояние ----------
enum Phase : uint8_t {
  PH_OFF = 0, PH_WAIT = 1, PH_P_TEST = 4, PH_P_NXT = 5,
  PH_START = 9, PH_M_GATHER = 10, PH_M_SIEVE = 11, PH_M_TEST = 12, PH_M_FILL = 14,
  PH_R_WAIT = 20, PH_R_CHECK = 21,
};
// PH_WAIT    — участник ждёт число (мастер собирает его по запросу);
// PH_P_TEST  — проверяет число своими базами; PH_P_NXT — ответил, ждёт итог;
// PH_M_GATHER, PH_M_SIEVE — мастер без числа: собирает кусочки, отсеивает;
// PH_M_TEST  — проверка: ждёт ответы (запас при этом может пополняться);
// PH_M_FILL  — ответы есть, а числа на следующий цикл ещё нет: ждёт сбор.
// PH_R_WAIT  — эстафета: ход у другого, агент шлёт ему кусочки;
// PH_R_CHECK — эстафета, каскад: ход у агента — собирает, отсеивает, проверяет.

static GenConfig s_cfg;
static bool      s_on = false;
static int       s_me = 0, s_pos = -1, s_ng = 0;
static uint32_t  s_gs = 0;
static Phase     s_ph = PH_OFF;
static uint32_t  s_cycle = 0;
static uint8_t   s_round = 0;                   // круг сбора внутри цикла
static uint8_t   s_master = 0;
static int64_t   s_deadline = 0;               // мастер: ждём ответы (или старт), мкс; 0 — нет
static int       s_tries = 0;
static bool      s_clean = true;               // в этом цикле не было переспросов и разрывов
static bool      s_dirty_next = false;         // следующий цикл заранее не чистый
static int64_t   s_t0 = 0;                      // начало текущего цикла для этого агента
static int64_t   s_last_heard = 0;              // последнее сообщение мастера (участник)
static int64_t   s_last_end = 0;                // когда агент видел конец прошлого цикла
static int64_t   s_avg_cycle_us = 100000;
static int64_t   s_streak_t0 = 0;

// число цикла и запас
static uint8_t   s_K = 1;
static uint16_t  s_pb = 4, s_nbytes = 32;
static uint8_t   s_number[256];                 // число текущего цикла
static bool      s_num_ok = false;              // оно уже есть
static uint8_t   s_rsv[MSG_MAX];                // запас уцелевших кандидатов (у мастера)
static int       s_rsv_n = 0;

// сбор (мастер): кусочки по местам в группе, с номером цикла и круга
static uint8_t   s_pieces[MAX_GROUP][MSG_MAX];
static bool      s_pcs_ok[MAX_GROUP];
static uint32_t  s_pcs_cyc[MAX_GROUP];
static uint8_t   s_pcs_rnd[MAX_GROUP];
static bool      s_gathering = false, s_sieving = false;
static int64_t   s_g_t0 = 0, s_g_deadline = 0;
static int       s_g_tries = 0;
static uint32_t  s_g_cyc = 0;                   // цикл (ход), в котором просили кусочки этого круга

// проверка (мастер)
static bool      s_have_res[MAX_GROUP];
static int8_t    s_res_w[MAX_GROUP];
static uint32_t  s_res_us[MAX_GROUP];
static int64_t   t_num = 0;                     // число есть — проверка пошла
static uint8_t   s_next = 0, s_verdict = 0;
static uint16_t  s_finders = 0;                // кто нашёл свидетеля — по местам в группе
// Ответы, пришедшие раньше итога прошлого цикла: нас назначили мастером, а
// итог с числом до нас не дошёл. Сохраняем и просим итог повторить.
static bool      s_eres_ok[MAX_GROUP];
static uint32_t  s_eres_cyc[MAX_GROUP], s_eres_tag[MAX_GROUP], s_eres_us[MAX_GROUP];
static int8_t    s_eres_w[MAX_GROUP];
static int64_t   s_ask_t = 0;

// время цикла мастера по этапам: чего он ждал
enum { B_GATHER = 0, B_SIEVE = 1, B_TEST = 2 };
static int64_t   s_acc[3];
static int64_t   s_mark = 0;
static uint32_t  s_cp_us = 0, s_cs_us = 0;     // счёт на пути цикла: свои кусочки, отсев

// последний отправленный итог — для повтора; эстафета
static uint8_t   s_nxt_msg[MSG_MAX];
static uint16_t  s_nxt_len = 0;
static uint32_t  s_nxt_cycle = 0;
static uint8_t   s_nxt_to = 0;
static bool      s_hand_wait = false;          // ждём вестей от нового мастера
static int64_t   s_hand_t = 0, s_hand_deadline = 0;
static int       s_hand_tries = 0;
static uint32_t  s_hop_us = 0;                  // сколько идёт итог до нового мастера (среднее)

// адреса участников (узнаём из их сообщений)
static uint8_t   s_mac[MAX_GROUP][6];
static bool      s_mac_ok[MAX_GROUP];

// участник: последние отправленные PCS и RES — для повтора по переспросу
static uint8_t   s_last_pcs[MSG_MAX];
static uint16_t  s_last_pcs_len = 0;
static uint32_t  s_last_pcs_cycle = 0;
static uint8_t   s_last_pcs_round = 0, s_last_pcs_to = 0;
static Res       s_last_res;
static uint32_t  s_last_res_cycle = 0;

// кусочки шума заранее: наборы по K кусочков (в «все на одном числе» — один, в эстафете
// и каскаде — запас на свой ход) и просьбы, которые их ждут, по очереди
static const int PRE_MAX = 4;
static uint8_t   s_prep[PRE_MAX][MSG_MAX];
static int       s_pre_n = 0;
struct PReq { uint32_t cycle; uint8_t round, to; bool job; };   // job — заготовки не было, набирали по просьбе
static PReq      s_rq[MAX_GROUP];
static int       s_rq_n = 0;
static uint32_t  s_srv_cyc[MAX_GROUP];          // эстафета, каскад: кому какой круг уже отдан (по местам)
static uint8_t   s_srv_rnd[MAX_GROUP];
static bool      s_srv_ok[MAX_GROUP];

// задания ядру 1: одно в работе, по одному ждущему каждого вида
static bool      s_job_busy = false;
static int64_t   s_job_t0 = 0;                   // когда выдано текущее задание
static uint32_t  s_job_lost = 0;                 // итогов не дождались (очередь событий была полна)
static Job       s_job_cur;
static Job       s_pend[JOB_TYPES];
static bool      s_pend_ok[JOB_TYPES];
static uint32_t  s_last_pieces_us = 5000, s_last_test_us = 5000;
// Сколько обычно отвечает каждый агент (скользящее среднее, мкс): от начала
// круга сбора до его кусочков и от начала проверки до его ответа. Мастер ждёт
// по этой мерке, поэтому медленный агент — это задержка, а не потеря.
static uint32_t  s_lat_pcs[MAX_GROUP], s_lat_res[MAX_GROUP];

static void lat_update(uint32_t* a, int p, int64_t v) {
  a[p] = a[p] ? (uint32_t)((3 * (int64_t)a[p] + v) / 4) : (uint32_t)v;
}

static int64_t wait_us(const uint32_t* lat, uint32_t own_us) {
  uint32_t m = own_us;
  for (int i = 0; i < s_ng; ++i)
    if (i != s_pos && lat[i] > m) m = lat[i];
  return 2 * (int64_t)m + 20000;                // вдвое дольше обычного и ещё 20 мс
}

static Stats       s_stats, s_pub;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

static inline int64_t now_us() { return esp_timer_get_time(); }

static int pos_of(uint8_t id) {
  for (int i = 0; i < s_ng; ++i)
    if (s_cfg.grp[i] == id) return i;
  return -1;
}

static bool is_master_phase() { return s_ph >= PH_M_GATHER && s_ph <= PH_M_FILL; }
static size_t pcs_len() { return (size_t)s_K * s_pb; }
static bool pre_auto() { return s_cfg.pre && s_cfg.src == SRC_RNG; }   // заготовка — только у быстрого источника
static bool relay() { return s_cfg.gv != 0; }                         // эстафета или каскад
// Два ядра в эстафете и каскаде: шум кусочков набирает производитель на ядре связи,
// ядро счёта шум не трогает (шум берёт одно ядро — prime_lab.ino).
static bool prod_noise() { return relay() && s_cfg.dc; }
static int pre_cap() { return relay() && (pre_auto() || s_cfg.dc) ? PRE_MAX : 1; }

static uint32_t tag_of(const uint8_t* n) {      // метка числа в ответе: младшие 4 байта
  uint32_t t;
  memcpy(&t, n + s_nbytes - 4, 4);
  return t;
}

static int rsv_cap() {                          // сколько запаса уходит с итогом
  int c = (int)((MSG_MAX - NXT_HDR) / s_nbytes) - 1;
  return c < 0 ? 0 : c;
}

// ---------- отправка ----------
// Скорость ESP-NOW: настройка nr (0 — скорость по умолчанию, 1 Мбит/с;
// 1 — 802.11g 24 Мбит/с).
static void set_rate(const uint8_t* mac) {
  esp_now_rate_config_t rc = {};
  rc.phymode = s_cfg.nrate ? WIFI_PHY_MODE_11G : WIFI_PHY_MODE_11B;
  rc.rate = s_cfg.nrate == 1 ? WIFI_PHY_RATE_24M     // кадр 250 байт ~0.1 мс
          : s_cfg.nrate == 2 ? WIFI_PHY_RATE_6M      // ~0.4 мс, берёт слабее сигнал
                             : WIFI_PHY_RATE_1M_L;   // ~2 мс
  esp_now_set_peer_rate_config(mac, &rc);
}

// У ESP-NOW предел адресатов (20). При смене состава прежних — убрать, кроме
// широковещательного, иначе после нескольких перенастроек новые не добавятся.
static void clear_peers() {
  uint8_t list[20][6];
  int n = 0;
  esp_now_peer_info_t p;
  for (bool head = true; n < 20 && esp_now_fetch_peer(head, &p) == ESP_OK; head = false)
    if (memcmp(p.peer_addr, BCAST, 6)) memcpy(list[n++], p.peer_addr, 6);
  for (int i = 0; i < n; ++i) esp_now_del_peer(list[i]);
}

static void ensure_peer(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = 0;                     // канал роутера
  p.ifidx = WIFI_IF_STA;
  p.encrypt = false;
  if (esp_now_add_peer(&p) == ESP_OK) set_rate(mac);
}

// Кадры ESP-NOW уходят по одному: следующий — только после того, как радио
// отчиталось о предыдущем (так советует Espressif). Сотни кадров в секунду
// подряд забивали радио, и у агента пропадал обычный Wi-Fi (замер 03.10.2026).
struct TxFrame { uint8_t mac[6]; uint16_t len; int64_t t_ok; uint8_t data[MSG_MAX]; };   // t_ok — не раньше (0 — сразу)
static const int      TXQ = 32;      // итог — всем до 15 участникам, плюс рассеяние
static TxFrame        s_txq[TXQ];
static int            s_txq_head = 0, s_txq_n = 0;
static bool           s_tx_busy = false;
static int64_t        s_tx_t0 = 0;
static volatile bool  s_tx_done = false;   // отчёт радио пришёл (из задачи Wi-Fi)

static volatile bool  s_radio_off = false;   // Wi-Fi выключен на шаге (radio_pause): кадры не отправлять

static void tx_kick() {
  if (s_radio_off) {                         // радио выключено: очередь — в корзину, радио не трогать
    s_txq_n = 0;
    s_tx_busy = false;
    return;
  }
  if (s_tx_busy && (s_tx_done || now_us() - s_tx_t0 > 50000)) {
    if (!s_tx_done) ++s_stats.tx_stuck;      // отчёта нет 50 мс — считаем кадр ушедшим
    s_tx_busy = false;
  }
  while (!s_tx_busy && s_txq_n > 0) {
    TxFrame& f = s_txq[s_txq_head];
    if (f.t_ok && now_us() < f.t_ok) break;        // кадр рассеяния ждёт своего окна
    s_tx_done = false;
    esp_err_t e = esp_now_send(f.mac, f.data, f.len);
    s_txq_head = (s_txq_head + 1) % TXQ;
    --s_txq_n;
    if (e == ESP_OK) {
      s_tx_busy = true;
      s_tx_t0 = now_us();
    } else {
      ++s_tx_fail;
      ++s_stats.tx_err;
    }
  }
}

static void hdr(Hdr& h, uint8_t type) {
  h.m0 = 'W';
  h.m1 = VER;
  h.type = type;
  h.from = (uint8_t)s_me;
  h.gs = s_gs;
  h.cycle = s_cycle;
  h.round = s_round;
}

static void send_to(const uint8_t* mac, const void* msg, size_t len, int64_t t_ok = 0) {
  ++s_stats.tx;
  if (s_txq_n == TXQ) {
    ++s_stats.tx_drop;
    return;
  }
  TxFrame& f = s_txq[(s_txq_head + s_txq_n) % TXQ];
  memcpy(f.mac, mac, 6);
  f.len = (uint16_t)len;
  f.t_ok = t_ok;
  memcpy(f.data, msg, len);
  ++s_txq_n;
  if ((uint32_t)s_txq_n > s_stats.txq_max) s_stats.txq_max = s_txq_n;
  tx_kick();
}

static void send_all(const void* msg, size_t len) { send_to(BCAST, msg, len); }

static void send_pos(int p, const void* msg, size_t len) {   // адресно, если адрес известен
  if (s_ng <= 1) return;               // группа из одного: слушать некому
  if (p >= 0 && s_mac_ok[p]) send_to(s_mac[p], msg, len);
  else send_all(msg, len);
}

// Всем участникам — адресно: радио подтверждает каждый кадр и само повторяет
// потерянный. Широковещательные кадры без подтверждения терялись примерно раз
// в сотню циклов (замер 03.10.2026, 24 Мбит/с). skip — кому не слать.
static void send_members(const void* msg, size_t len, const bool* skip = nullptr) {
  if (s_ng <= 1) return;
  for (int i = 0; i < s_ng; ++i)
    if (i != s_pos && !(skip && skip[i])) send_pos(i, msg, len);
}

// ---------- пульс: «я жив» раз в секунду всем, в любом режиме ----------
// Без него радио молчало в «каждый сам» (сторож радио был слеп), а кто кого слышит и с каким
// сигналом было видно только в бою, внутри группы. Теперь у каждого агента есть таблица соседей:
// номер, адрес, сигнал, когда слышали, сколько кадров — уходит на сервер пакетом nbr.
static Neighbor   s_nb[NB_MAX];
static int        s_nb_n = 0;
static int64_t    s_alive_t = 0;
static volatile int8_t  s_alive_temp = 0;       // что писать в свой пульс (задаёт задача связи)
static volatile uint8_t s_alive_run = 0;
static volatile uint8_t s_alive_hz = 1;         // кадров пульса в секунду (настройка hb): соседям это ещё и CSI

static void alive_tick() {
  if (s_radio_off) return;
  const int64_t now = now_us();
  if (now - s_alive_t < 1000000 / (s_alive_hz ? s_alive_hz : 1)) return;
  s_alive_t = now;
  Alv m = {};
  hdr(m.h, M_ALIVE);
  m.run = s_alive_run;
  m.temp = s_alive_temp;
  m.up_s = (uint32_t)(now / 1000000);
  send_all(&m, sizeof m);
}

static void alive_note(const Alv& m, const uint8_t* mac, int8_t rssi) {
  const uint8_t id = m.h.from;
  if (!id || id == s_me) return;
  int k = -1;
  for (int i = 0; i < s_nb_n; ++i)
    if (s_nb[i].id == id) { k = i; break; }
  if (k < 0) {
    if (s_nb_n >= NB_MAX) return;
    k = s_nb_n++;
    s_nb[k] = Neighbor();
    s_nb[k].id = id;
  }
  portENTER_CRITICAL(&s_mux);
  Neighbor& n = s_nb[k];
  memcpy(n.mac, mac, 6);
  n.rssi = rssi ? (n.n ? (int8_t)((3 * n.rssi + rssi) / 4) : rssi) : n.rssi;
  n.last_us = now_us();
  ++n.n;
  n.run = m.run;
  n.temp = m.temp;
  portEXIT_CRITICAL(&s_mux);
  const int p = pos_of(id);                    // участник группы: его адрес и сигнал — сразу, до первого кадра протокола
  if (p >= 0 && p != s_pos) {
    if (rssi) s_stats.rssi_from[p] = s_stats.rssi_from[p] ? (int8_t)((3 * s_stats.rssi_from[p] + rssi) / 4) : rssi;
    if (!s_mac_ok[p] || memcmp(s_mac[p], mac, 6)) {
      memcpy(s_mac[p], mac, 6);
      s_mac_ok[p] = true;
      ensure_peer(mac);
      set_rate(mac);
    }
  }
}

// ---------- эстафета и каскад: состояние проверяющего ----------
static uint8_t    s_rr[4096];                   // запас уцелевших кандидатов
static int        s_rr_n = 0;
static int        s_rt_m = 0;                   // кандидатов в задании проверки у ядра 1 (в s_job_buf)
static int        s_turn_pr = 0;                // простых в этом ходе
static uint8_t    s_found[256];                 // последнее найденное простое — на сверку следующему
static uint8_t    s_aud[256];                   // чужое простое на сверку
static bool       s_aud_ok = false;
static int64_t    s_r_mr = 0, s_r_other = 0;    // за ход: проверка на ядре 1, прочий счёт ядра 1 (шум, отсев)
static uint32_t   s_r_pcs_us = 0, s_r_sv_us = 0; // за ход: свои кусочки и отсев на ядре 1
static PrimeStats s_sv;                         // отсев на ядре связи (два ядра)
static uint8_t    s_asm[4096];                  // кандидаты, сложенные на ядре связи (два ядра)
static int        s_asm_k = 0, s_asm_n = 0;     // отсеяно / всего: отсев по частям, задача не замирает
static volatile int      s_prod_len = 0;        // производителю: байт в наборе кусочков (0 — не нужно)
static volatile uint32_t s_prod_gs = 0;
struct ProdSet { uint32_t gs; uint16_t len; uint8_t data[MSG_MAX]; };
static QueueHandle_t     s_prodq = nullptr;     // наборы кусочков от производителя

static int rr_cap() { return min((int)(sizeof s_rr / s_nbytes), 128); }
static int rt_batch() { return s_nbytes <= 32 ? 8 : s_nbytes <= 64 ? 4 : 2; }   // кандидатов на одно задание проверки
static int trn_cap() {                          // сколько запаса уходит с ходом
  int c = (int)((MSG_MAX - TRN_HDR) / s_nbytes) - 1;
  return c < 0 ? 0 : c;
}

// ---------- задания ядру 1 ----------
static void assemble(uint8_t* out) {
  // Каждый кандидат — кусочки всех агентов в случайном порядке. Порядок — быстрым генератором
  // (xorshift128+), заправленным словами ГСЧ на каждый сбор: шум несут сами кусочки, а ГСЧ
  // отдаёт слово за ~21 мкс — по слову на перестановку съедало до трети ядра связи.
  uint64_t s0 = (uint64_t)esp_random() << 32 | esp_random();
  uint64_t s1 = (uint64_t)esp_random() << 32 | esp_random() | 1;
  auto next = [&s0, &s1]() -> uint32_t {
    uint64_t x = s0;
    const uint64_t y = s1;
    s0 = y;
    x ^= x << 23;
    s1 = x ^ y ^ (x >> 17) ^ (y >> 26);
    return (uint32_t)((s1 + y) >> 32);
  };
  for (int k = 0; k < s_K; ++k) {
    int perm[MAX_GROUP];
    for (int i = 0; i < s_ng; ++i) perm[i] = i;
    for (int i = s_ng - 1; i > 0; --i) {
      int j = next() % (i + 1);
      int t = perm[i];
      perm[i] = perm[j];
      perm[j] = t;
    }
    uint8_t* dst = out + k * s_nbytes;
    size_t off = 0;
    for (int i = 0; i < s_ng && off < s_nbytes; ++i) {
      size_t take = min((size_t)s_pb, (size_t)s_nbytes - off);
      memcpy(dst + off, s_pieces[perm[i]] + k * s_pb, take);
      off += take;
    }
  }
}

static bool job_valid(const Job& j) {
  if (j.gs != s_gs) return false;
  if (j.type == JOB_TEST) return j.cycle == s_cycle && s_num_ok && (s_ph == PH_M_TEST || s_ph == PH_P_TEST);
  if (j.type == JOB_SIEVE) return j.cycle == s_g_cyc && j.round == s_round && s_sieving;
  if (j.type == JOB_RTEST) return s_ph == PH_R_CHECK && s_rr_n > 0;
  if (j.type == JOB_AUDIT) return s_aud_ok;
  return true;                       // кусочки шума годятся всегда
}

static void dispatch(Job j) {
  if (j.type == JOB_SIEVE) {
    assemble(s_job_buf);
  } else if (j.type == JOB_TEST) {
    memcpy(s_job_buf, s_number, j.nbytes);
  } else if (j.type == JOB_RTEST) {  // кандидаты — из запаса прямо сейчас: сколько есть, не больше пакета
    const int m = min(s_rr_n, rt_batch());
    s_rr_n -= m;
    memcpy(s_job_buf, s_rr + (size_t)s_rr_n * s_nbytes, (size_t)m * s_nbytes);
    s_rt_m = m;
    j.count = (uint8_t)m;
  } else if (j.type == JOB_AUDIT) {
    memcpy(s_job_buf, s_aud, j.nbytes);
  }
  s_job_busy = true;
  s_job_t0 = now_us();
  s_job_cur = j;
  xQueueSend(s_jobq, &j, 0);
}

// Просьб о кусочках больше, чем готовых наборов: кто-то ждёт шум этого агента.
static bool pieces_urgent() { return s_rq_n > s_pre_n; }

// Ядро 1 свободно — следующее задание: сперва сверка и проверка (её ждут все), потом
// отсев, последним — кусочки шума. В эстафете и каскаде кусочки, которых уже ждут,
// идут раньше проверки запаса: без них не сложить следующие числа.
static void pump() {
  if (s_job_busy) return;
  static const uint8_t order[] = {JOB_AUDIT, JOB_TEST, JOB_SIEVE, JOB_RTEST, JOB_PIECES};
  static const uint8_t order_urgent[] = {JOB_AUDIT, JOB_TEST, JOB_SIEVE, JOB_PIECES, JOB_RTEST};
  const uint8_t* ord = relay() && pieces_urgent() ? order_urgent : order;
  for (int i = 0; i < (int)sizeof order; ++i) {
    const uint8_t t = ord[i];
    if (!s_pend_ok[t]) continue;
    s_pend_ok[t] = false;
    if (job_valid(s_pend[t])) {
      dispatch(s_pend[t]);
      return;
    }
  }
}

static void issue(const Job& j) {
  s_pend[j.type] = j;
  s_pend_ok[j.type] = true;
  pump();
}

static bool pieces_busy() {
  return s_pend_ok[JOB_PIECES] || (s_job_busy && s_job_cur.type == JOB_PIECES);
}

// Набрать кусочки шума. Не сразу, а когда ядро 1 свободно и ничего важнее не
// ждёт: проверку, выданную в том же событии, кусочки не задерживают.
static bool s_refill = false;

// Рассеяние (сет B): шум агента — блоки по s_bn_unit байт, набранные заранее.
static bool      s_b_on = false;               // агент — в сете B и работает
static bool      s_ann = false;                // агент — в сете A, сет B есть: объявлять простые
static int       s_bpos = -1, s_nbs = 0;       // место в сете B и его размер
static uint8_t   s_bmacs[MAX_GROUP][6];
static bool      s_bmac_ok[MAX_GROUP];
static uint32_t  s_ann_seq = 0;
static volatile uint32_t s_ann_drop = 0;       // из ядра 1: очередь событий занята
static uint32_t  s_ann_drop0 = 0, s_rx_drop0 = 0;  // их значения при применении настройки: счёт — с неё
static uint8_t   s_bn[1024];
static int       s_bn_units = 0, s_bn_unit = 32, s_bn_max = 4;

static int pieces_want() {           // сколько шума набрать, байт; 0 — не нужно
  if (s_b_on)                        // B: по два блока за раз — с медленным источником первый готов быстрее
    return s_bn_units < s_bn_max ? min(s_bn_max - s_bn_units, 2) * s_bn_unit : 0;
  return s_pre_n < pre_cap() ? (int)pcs_len() : 0;
}

static void refill() {
  if (prod_noise() && !s_b_on) return;            // шум кусочков набирает производитель на ядре связи
  if (pieces_want() > 0 && !pieces_busy()) s_refill = true;
}

static void refill_kick() {          // из цикла задачи, после события
  if (!s_refill) return;
  if (relay()) {                     // эстафета, каскад: запас впрок — когда ядру 1 нечего проверять
    if (s_pend_ok[JOB_SIEVE] || s_pend_ok[JOB_AUDIT]) return;
    if (!pieces_urgent() && (s_job_busy || s_pend_ok[JOB_RTEST])) return;
  } else if (s_job_busy || s_pend_ok[JOB_TEST] || s_pend_ok[JOB_SIEVE]) {
    return;
  }
  s_refill = false;
  const int n = pieces_want();
  if (n <= 0 || pieces_busy()) return;
  Job j = {};
  j.type = JOB_PIECES;
  j.gs = s_gs;
  j.nbytes = (uint16_t)n;
  issue(j);
}

static int my_bases(uint8_t* idx) {  // базы делятся по кругу: место в группе, место + N, …
  int n = 0;
  if (s_cfg.all) {                   // режим согласия: все базы у каждого, порядок один — ответы обязаны совпасть
    for (int i = 0; i < s_cfg.nb; ++i) idx[n++] = (uint8_t)i;
    return n;
  }
  for (int i = s_pos; i < s_cfg.nb; i += s_ng) idx[n++] = (uint8_t)i;
  return n;
}

// ---------- учёт серий без сбоев ----------
static void cycle_begin() {
  s_clean = !s_dirty_next;
  s_dirty_next = false;
  s_t0 = s_mark = now_us();
  s_acc[B_GATHER] = s_acc[B_SIEVE] = s_acc[B_TEST] = 0;
  s_cp_us = s_cs_us = 0;
  s_last_heard = s_t0;
}

static void cycle_end() {
  int64_t now = now_us();
  ++s_stats.cycles;
  if (s_last_end) s_avg_cycle_us = (s_avg_cycle_us * 7 + (now - s_last_end)) / 8;
  s_last_end = now;
  if (s_clean) {
    if (s_stats.streak == 0) s_streak_t0 = s_t0;
    ++s_stats.streak;
    s_stats.streak_ms = (uint32_t)((now - s_streak_t0) / 1000);
    s_stats.clean_ms += (uint64_t)((now - s_t0) / 1000);
    if (s_stats.streak > s_stats.streak_max) s_stats.streak_max = s_stats.streak;
    if (s_stats.streak_ms > s_stats.streak_ms_max) s_stats.streak_ms_max = s_stats.streak_ms;
  } else {
    s_stats.streak = 0;
    s_stats.streak_ms = 0;
  }
}

static void unclean() { s_clean = false; }

// Чего сейчас ждёт мастер: время до этой минуты — в этот этап.
static void account() {
  int64_t now = now_us();
  if (is_master_phase()) s_acc[s_ph == PH_M_TEST ? B_TEST : s_sieving ? B_SIEVE : B_GATHER] += now - s_mark;
  s_mark = now;
}

// ---------- кусочки шума ----------
static void gather_check();

static void take_pieces(int p, uint32_t cycle, uint8_t round, const uint8_t* data, bool job = false) {
  memcpy(s_pieces[p], data, pcs_len());
  s_pcs_ok[p] = true;
  s_pcs_cyc[p] = cycle;
  s_pcs_rnd[p] = round;
  // свои готовы — ждём остальных; эстафета и каскад с pw — срок уже задан от просьбы
  if (p == s_pos && s_gathering && cycle == s_g_cyc && round == s_round && !(relay() && s_cfg.pw))
    s_g_deadline = s_g_t0 + wait_us(s_lat_pcs, job ? s_last_pieces_us : 0);
  gather_check();
}

static bool rq_has(uint32_t cycle, uint8_t round, uint8_t to) {   // эта просьба уже ждёт шум
  for (int i = 0; i < s_rq_n; ++i)
    if (s_rq[i].cycle == cycle && s_rq[i].round == round && s_rq[i].to == to) return true;
  return false;
}

static int s_hold = 0;               // эстафета, каскад: столько наборов держать для своего круга (замена недостающих)

static void serve_pcs() {            // заготовки — тем, кто просил, по очереди; свой круг — первым
  while (s_pre_n > 0 && s_rq_n > 0) {
    int k = 0;
    if (relay())
      for (int i = 0; i < s_rq_n; ++i)
        if (s_rq[i].to == s_me) { k = i; break; }
    if (s_rq[k].to != s_me && s_pre_n <= s_hold) break;   // свой круг ждёт замену — соседям потом
    const PReq q = s_rq[k];
    --s_rq_n;
    memmove(s_rq + k, s_rq + k + 1, (size_t)(s_rq_n - k) * sizeof(PReq));
    const uint8_t* set = s_prep[--s_pre_n];
    if (q.to == s_me) {
      take_pieces(s_pos, q.cycle, q.round, set, q.job);
      continue;
    }
    Pcs* m = (Pcs*)s_last_pcs;
    hdr(m->h, M_PCS);
    m->h.cycle = q.cycle;
    m->h.round = q.round;
    m->K = s_K;
    m->pb = s_pb;
    memcpy(m->data, set, pcs_len());
    s_last_pcs_len = (uint16_t)(PCS_HDR + pcs_len());
    s_last_pcs_cycle = q.cycle;
    s_last_pcs_round = q.round;
    s_last_pcs_to = q.to;
    const int p = pos_of(q.to);
    send_pos(p, s_last_pcs, s_last_pcs_len);
    if (p >= 0) {
      s_srv_ok[p] = true;
      s_srv_cyc[p] = q.cycle;
      s_srv_rnd[p] = q.round;
    }
  }
  if (pre_auto() || s_rq_n > 0) refill();
}

// Просьба о кусочках для круга (cycle, round) мастера to: отдать сразу из
// заготовки, а нет её — набрать (ядро 1 сперва доделает проверку). Прежняя
// просьба того же агента (в «все на одном числе» — любая) больше не нужна.
static void want_pcs(uint32_t cycle, uint8_t round, uint8_t to) {
  for (int i = 0; i < s_rq_n;) {
    if (!relay() || s_rq[i].to == to) {
      --s_rq_n;
      memmove(s_rq + i, s_rq + i + 1, (size_t)(s_rq_n - i) * sizeof(PReq));
    } else {
      ++i;
    }
  }
  if (s_rq_n == MAX_GROUP) {         // очередь полна — старейшую просьбу вон
    --s_rq_n;
    memmove(s_rq, s_rq + 1, (size_t)s_rq_n * sizeof(PReq));
  }
  const bool have = s_pre_n > s_rq_n;
  s_rq[s_rq_n++] = {cycle, round, to, !have};
  if (have) ++s_stats.pre_hit;
  else ++s_stats.pre_miss;
  serve_pcs();
}

// ---------- мастер ----------
static void announce(const uint8_t* num, uint16_t nbytes);
static void b_pump();
static void start_test(bool num);
static void test_check();
static void finish();

// Просьба о кусочках круга (s_g_cyc, s_round). Эстафета и каскад: первая — всем одним
// широковещательным кадром (просят часто, а потерю покроет повтор или срок pw);
// повторы и «все на одном числе» — адресно каждому, кроме skip.
static void send_req(const bool* skip) {
  Req r;
  hdr(r.h, M_REQ);
  r.h.cycle = s_g_cyc;
  r.K = s_K;
  r.pb = s_pb;
  if (relay() && !skip) {
    if (s_ng > 1) send_all(&r, sizeof r);
    return;
  }
  send_members(&r, sizeof r, skip);
}

// Круг сбора (s_cycle, s_round). ask — попросить остальных запросом REQ;
// иначе их уже попросил итог прошлого цикла (want).
static void gather_round(bool ask) {
  account();
  s_gathering = true;
  s_sieving = false;
  s_g_t0 = now_us();
  s_g_deadline = 0;                  // таймер — когда свои кусочки готовы
  s_g_tries = 0;
  s_g_cyc = s_cycle;
  ++s_stats.gathers;
  if (relay() && s_cfg.pw) s_g_deadline = s_g_t0 + (int64_t)s_cfg.pw * 1000;   // дольше не ждать
  if (ask) send_req(nullptr);
  want_pcs(s_cycle, s_round, (uint8_t)s_me);
}

static void r_sieved(const uint8_t* buf, int n, uint32_t sv_us);

static void gather_check() {
  if (!s_gathering || s_sieving) return;
  for (int i = 0; i < s_ng; ++i)
    if (!s_pcs_ok[i] || s_pcs_cyc[i] != s_g_cyc || s_pcs_rnd[i] != s_round) return;
  account();
  s_sieving = true;
  s_g_deadline = 0;
  s_hold = 0;
  if (s_ph == PH_M_GATHER) s_ph = PH_M_SIEVE;
  // Эстафета, два ядра: у проверяющего узкое место — проверка на ядре счёта, поэтому складывает
  // и отсеивает ядро связи (по частям, в цикле задачи). Каскад: ядро связи занято шумом для
  // чисел всех, а ядро счёта простаивает до половины времени — отсев остаётся ему.
  if (s_cfg.gv == 1 && s_cfg.dc) {
    assemble(s_asm);
    s_asm_k = 0;
    s_asm_n = s_K;
    return;
  }
  Job j = {};
  j.type = JOB_SIEVE;
  j.gs = s_gs;
  j.cycle = s_g_cyc;
  j.round = s_round;
  j.nbytes = s_nbytes;
  j.count = s_K;
  issue(j);
}

static void pop_number(uint8_t* out) {
  --s_rsv_n;
  memcpy(out, s_rsv + s_rsv_n * s_nbytes, s_nbytes);
}

// Отсев готов: уцелевшие — в запас. Не уцелел никто — новый круг сбора.
static void sieve_done(const JobResult& r) {
  account();
  s_sieving = false;
  s_gathering = false;
  int n = r.value < 0 ? 0 : r.value;
  s_stats.cands += s_K;
  s_stats.survivors += n;
  if (s_ph == PH_M_SIEVE || s_ph == PH_M_FILL) s_cs_us += r.us;   // отсев задержал цикл
  for (int i = 0; i < n; ++i) {
    if (s_rsv_n < rsv_cap()) memcpy(s_rsv + s_rsv_n++ * s_nbytes, s_job_buf + i * s_nbytes, s_nbytes);
    else ++s_stats.rsv_drop;
  }
  if (n == 0) ++s_stats.regathers;
  if (s_rsv_n == 0) {
    ++s_round;
    gather_round(true);
    return;
  }
  if (s_ph == PH_M_SIEVE) {
    pop_number(s_number);
    start_test(true);
  } else if (s_ph == PH_M_FILL) {
    finish();
  }
}

static void send_num(const bool* skip) {
  static uint8_t buf[MSG_MAX];
  Num* m = (Num*)buf;
  hdr(m->h, M_NUM);
  m->bits = s_cfg.bits;
  memcpy(m->data, s_number, s_nbytes);
  send_members(buf, NUM_HDR + s_nbytes, skip);
}

// Число цикла есть (в s_number) — проверка. num — разослать его самим (цикл
// начат без итога); иначе число уже у всех с итогом прошлого цикла.
static void start_test(bool num) {
  account();
  s_ph = PH_M_TEST;
  s_tries = 0;
  t_num = now_us();
  s_num_ok = true;
  memset(s_have_res, 0, sizeof s_have_res);
  const uint32_t tag = tag_of(s_number);
  for (int i = 0; i < s_ng; ++i)               // ответы, пришедшие раньше итога
    if (i != s_pos && s_eres_ok[i] && s_eres_cyc[i] == s_cycle && s_eres_tag[i] == tag) {
      s_have_res[i] = true;
      s_res_w[i] = s_eres_w[i];
      s_res_us[i] = s_eres_us[i];
      s_eres_ok[i] = false;
    }
  if (num) send_num(s_have_res);
  Job j = {};
  j.type = JOB_TEST;
  j.gs = s_gs;
  j.cycle = s_cycle;
  j.round = s_round;
  j.nbytes = s_nbytes;
  j.nidx = (uint8_t)my_bases(j.idx);
  s_deadline = 0;                    // таймер — когда своя проверка готова
  if (j.nidx == 0) {                 // своих баз нет: сразу «не нашёл»
    s_have_res[s_pos] = true;
    s_res_w[s_pos] = -1;
    s_res_us[s_pos] = 0;
    s_deadline = t_num + wait_us(s_lat_res, 0);
  } else {
    issue(j);
  }
  if (s_rsv_n == 0 && !s_gathering) {          // запас пуст — пополнить, пока проверяем
    ++s_round;
    gather_round(true);
  }
  test_check();
}

// Мастер цикла: число уже в s_number, запас — в s_rsv. want — запас пуст, и
// остальные уже шлют кусочки (их попросил итог прошлого цикла).
static void master_cycle(uint32_t cycle, bool want) {
  s_cycle = cycle;
  s_round = 0;
  s_master = (uint8_t)s_me;
  cycle_begin();
  s_ph = PH_M_TEST;
  s_gathering = s_sieving = false;
  if (want) gather_round(false);
  start_test(false);
}

// Цикл без числа (начало, сбой, разрыв): собрать запас по запросу.
static void master_start(uint32_t cycle) {
  s_cycle = cycle;
  s_round = 0;
  s_master = (uint8_t)s_me;
  cycle_begin();
  s_gathering = s_sieving = false;
  s_num_ok = false;
  ++s_stats.starts;
  if (s_rsv_n > 0) {                 // запас ещё есть — число сразу
    pop_number(s_number);
    start_test(true);
    return;
  }
  s_ph = PH_M_GATHER;
  gather_round(true);
}

static void test_check() {
  if (s_ph != PH_M_TEST) return;
  for (int i = 0; i < s_ng; ++i)
    if (!s_have_res[i]) return;
  if (s_rsv_n > 0) {
    finish();
    return;
  }
  account();
  s_ph = PH_M_FILL;                  // ответы есть, а числа на следующий цикл ещё нет
  s_deadline = 0;
  ++s_stats.fill_waits;
  if (!s_gathering) {
    ++s_round;
    gather_round(true);
  }
}

static uint8_t pick_by_number(uint16_t mask, int nf) {
  uint8_t h[32];
  mbedtls_sha256(s_number, s_nbytes, h, 0);
  uint32_t seed = (uint32_t)h[0] << 24 | (uint32_t)h[1] << 16 | (uint32_t)h[2] << 8 | h[3];
  int k = seed % nf;
  for (int i = 0; i < s_ng; ++i)
    if (mask & (1 << i)) {
      if (k == 0) return s_cfg.grp[i];
      --k;
    }
  return s_cfg.grp[0];
}

static int64_t hand_wait_us() { return max((int64_t)50000, 4 * s_avg_cycle_us); }

static void participant_cycle(uint32_t cycle, uint8_t master, const uint8_t* num, bool want);

// Итог цикла и сразу начало следующего: число следующего цикла — из запаса.
static void finish() {
  const int64_t now = now_us();
  account();
  uint16_t mask = 0;
  int nf = 0;
  uint32_t tmax_us = 0;
  for (int i = 0; i < s_ng; ++i) {
    // −2 — ошибка счёта (mbedtls): число не признаётся простым, расхождение запишется как split
    if (s_res_w[i] >= 0 || s_res_w[i] == -2) { mask |= 1 << i; ++nf; }
    if (s_res_us[i] > tmax_us) tmax_us = s_res_us[i];
  }
  s_finders = mask;
  s_verdict = nf == 0 ? 1 : 0;
  if (s_ng > 1) {                    // согласие: ответы всех про одно число обязаны сходиться
    ++s_stats.agree;
    const bool split = nf != 0 && nf != s_ng;
    bool bad = split;
    if (s_cfg.all && !bad && nf)     // все нашли — у всех одна и та же первая база-свидетель?
      for (int i = 1; i < s_ng && !bad; ++i) bad = s_res_w[i] != s_res_w[0];
    if (bad) {
      if (split) ++s_stats.split; else ++s_stats.disagree;
      char hex[132];
      number_hex(s_number, min((int)s_nbytes, 64), hex, sizeof hex);
      int k = snprintf(s_stats.last_split, sizeof s_stats.last_split, "%s", hex);
      for (int i = 0; i < s_ng && k < (int)sizeof s_stats.last_split - 4; ++i)
        k += snprintf(s_stats.last_split + k, sizeof s_stats.last_split - k, "%s%d", i ? "," : " ", (int)s_res_w[i]);
      Serial.printf("GRP %s cycle=%lu %s\n", split ? "SPLIT" : "DISAGREE", (unsigned long)s_cycle, s_stats.last_split);
    }
  }
  if (nf == 0) {                     // простое: эстафета следующему по порядку
    s_next = s_cfg.grp[(pos_of(s_master) + 1) % s_ng];
    ++s_stats.primes_m;
    if (s_nbytes <= 64) number_hex(s_number, s_nbytes, s_stats.last_prime, sizeof s_stats.last_prime);
    if (s_ann) announce(s_number, s_nbytes);    // сету B — рассеивать
    if (s_cfg.pl) plog::push(s_number, s_nbytes, 1);   // журнал простых группы ведёт мастер
  } else if (nf == 1) {              // составное: мастер — нашедший
    s_next = s_cfg.grp[__builtin_ctz(mask)];
  } else {
    s_next = pick_by_number(mask, nf);
  }

  Nxt* n = (Nxt*)s_nxt_msg;
  hdr(n->h, M_NXT);
  n->verdict = s_verdict;
  n->next = s_next;
  n->finders = s_finders;
  n->bits = s_cfg.bits;
  n->span = (uint32_t)(now - s_t0);
  int nn = 0, rn = 0;
  if (s_rsv_n > 0) {
    nn = 1;
    pop_number(n->data);
    if (s_next != s_me) {            // остаток запаса — новому мастеру
      rn = s_rsv_n;
      memcpy(n->data + s_nbytes, s_rsv, (size_t)rn * s_nbytes);
    }
  }
  n->nn = (uint8_t)nn;
  n->rn = (uint8_t)rn;
  n->want = (uint8_t)(nn && s_rsv_n == 0);
  s_nxt_len = (uint16_t)(NXT_HDR + (size_t)(nn + rn) * s_nbytes);
  s_nxt_cycle = s_cycle;
  s_nxt_to = s_next;
  const int pn = pos_of(s_next);
  if (pn != s_pos) send_pos(pn, s_nxt_msg, s_nxt_len);   // новому мастеру — первым
  bool skip[MAX_GROUP] = {};
  if (pn >= 0) skip[pn] = true;
  n->rn = 0;                                             // остальным — без запаса
  send_members(s_nxt_msg, NXT_HDR + (size_t)nn * s_nbytes, skip);
  n->rn = (uint8_t)rn;

  const uint32_t hop = s_next != s_me ? s_hop_us : 0;
  ++s_stats.cycles_m;
  s_stats.t_gather += s_acc[B_GATHER];
  s_stats.t_sieve += s_acc[B_SIEVE];
  s_stats.t_test += s_acc[B_TEST];
  s_stats.t_hand += hop;
  s_stats.t_cycle += (now - s_t0) + hop;
  s_stats.c_pieces += s_cp_us;
  s_stats.c_sieve += s_cs_us;
  s_stats.c_test += tmax_us;
  cycle_end();

  const uint32_t next_cycle = s_cycle + 1;
  if (s_next == s_me) {
    if (nn) {
      memcpy(s_number, n->data, s_nbytes);
      master_cycle(next_cycle, n->want);
    } else {
      master_start(next_cycle);
    }
    return;
  }
  s_rsv_n = 0;                       // запас ушёл новому мастеру
  s_hand_wait = true;
  s_hand_t = now;
  s_hand_tries = 0;
  s_hand_deadline = now + hand_wait_us();
  participant_cycle(next_cycle, s_next, nn ? n->data : nullptr, n->want);
}

// ---------- участник ----------
static void send_res(int8_t w, uint8_t tests, uint32_t us) {
  hdr(s_last_res.h, M_RES);
  s_last_res.witness = w;
  s_last_res.tests = tests;
  s_last_res.us = us;
  s_last_res.tag = tag_of(s_number);
  s_last_res_cycle = s_cycle;
  send_pos(pos_of(s_master), &s_last_res, sizeof s_last_res);
  s_ph = PH_P_NXT;
}

static void start_ptest() {
  s_ph = PH_P_TEST;
  Job j = {};
  j.type = JOB_TEST;
  j.gs = s_gs;
  j.cycle = s_cycle;
  j.round = s_round;
  j.nbytes = s_nbytes;
  j.nidx = (uint8_t)my_bases(j.idx);
  if (j.nidx == 0) send_res(-1, 0, 0);
  else issue(j);
}

// Участник цикла с мастером master. num — число (пришло с итогом или NUM);
// нет его — мастер сначала соберёт. want — сразу отдать мастеру кусочки.
static void participant_cycle(uint32_t cycle, uint8_t master, const uint8_t* num, bool want) {
  s_cycle = cycle;
  s_round = 0;
  s_master = master;
  cycle_begin();
  s_gathering = s_sieving = false;
  s_deadline = 0;
  if (want) want_pcs(cycle, 0, master);      // кусочки — первыми: мастер отсеет, пока все проверяют
  if (num) {
    memcpy(s_number, num, s_nbytes);
    s_num_ok = true;
    start_ptest();
  } else {
    s_num_ok = false;
    s_ph = PH_WAIT;
  }
}

static int64_t watchdog_us() {
  int64_t w = max((int64_t)1500000, 6 * s_avg_cycle_us);
  w = max(w, 3 * (int64_t)s_last_pieces_us + 1000000);   // медленный источник: сбор шума идёт секунды, мастер ждёт всех
  return w + 500000LL * s_pos;
}

// Нас назначили мастером, а итог с числом не дошёл: попросить повторить.
// Итог хранит его автор; кто это — можем не знать, поэтому спрашиваем всех.
static void ask_nxt() {
  const int64_t now = now_us();
  if (s_ask_t && now - s_ask_t < 10000) return;   // не чаще раза в 10 мс
  s_ask_t = now;
  Ask a;
  hdr(a.h, M_ASK);                   // h.cycle — чей итог нужен
  a.what = M_NXT;
  send_members(&a, sizeof a);
  ++s_stats.asks_tx;
  unclean();
}

// Итог цикла n.h.cycle: стать мастером следующего или участником.
static void adopt_nxt(const Nxt& n, bool ours) {
  if (ours) cycle_end();
  const uint8_t* num = n.nn ? n.data : nullptr;
  if (n.next != s_me) {
    participant_cycle(n.h.cycle + 1, n.next, num, n.want);
    return;
  }
  if (!num) {
    master_start(n.h.cycle + 1);
    return;
  }
  memcpy(s_number, num, s_nbytes);
  s_rsv_n = 0;
  for (int i = 0; i < n.rn && s_rsv_n < rsv_cap(); ++i)
    memcpy(s_rsv + s_rsv_n++ * s_nbytes, num + (size_t)(i + 1) * s_nbytes, s_nbytes);
  master_cycle(n.h.cycle + 1, n.want);
}

// Запрос или число другого мастера, пока мастер мы: уступаем, если его цикл
// новее или тот же, но номер агента меньше.
static bool yield_to(const Hdr& h) {
  if (h.cycle > s_cycle || (h.cycle == s_cycle && h.from < s_me)) {
    ++s_stats.conflicts;
    s_dirty_next = true;
    return true;
  }
  ++s_stats.stale;
  return false;
}

// ---------- эстафета (gv = 1) и каскад (gv = 2) ----------
// Число — из кусочков всех, как в «все на одном числе», но проверяет его один:
// проверяющий держит запас уцелевших кандидатов, собирает кусочки кругами (просьба —
// всем одним кадром) и отдаёт ядру 1 пакеты по rt_batch() кандидатов: всеми базами,
// до первого простого. Ход кончается rk-м простым. Эстафета: ход и остаток запаса —
// следующему по порядку (он подтверждает приём), каскад: ход у всех сразу и остаётся,
// простое уходит следующему на сверку. Сверка — одной базой, которой проверяющий не
// проверял: нашла свидетеля — «простое» соседа составное (split).
static uint32_t s_aud_seq = 0;
static int      s_asm_s = 0;                    // уцелевших в сборке ядра связи

static int64_t r_hand_wait_us() { return max((int64_t)50000, 8 * (int64_t)s_hop_us); }

// Запас, ниже которого собирать ещё: уцелевших за круг и два про запас. Каскад — круг вперёд:
// ход не передаётся, запас не пропадает, а следующий круг успевает прийти, пока проверяется этот.
static int r_target() {
  const int per = max(1, s_stats.cands ? (int)((uint64_t)s_K * s_stats.survivors / s_stats.cands) : s_K / 6);
  return min(rr_cap(), (s_cfg.gv == 2 ? 2 * per : per) + 2);
}

static void rq_drop(uint8_t to) {    // просьбы этого агента больше не нужны
  for (int i = 0; i < s_rq_n;) {
    if (s_rq[i].to == to) {
      --s_rq_n;
      memmove(s_rq + i, s_rq + i + 1, (size_t)(s_rq_n - i) * sizeof(PReq));
    } else {
      ++i;
    }
  }
}

static void r_push_back(const uint8_t* b, int n) {   // кандидаты — в запас
  for (int i = 0; i < n; ++i) {
    if (s_rr_n < rr_cap()) memcpy(s_rr + (size_t)s_rr_n++ * s_nbytes, b + (size_t)i * s_nbytes, s_nbytes);
    else ++s_stats.rsv_drop;
  }
}

static void r_feed() {               // запасу — сбор, ядру 1 — проверку
  if (s_ph != PH_R_CHECK) return;
  if (!s_gathering && s_rr_n < r_target()) {
    ++s_round;
    gather_round(true);
  }
  if (s_ph != PH_R_CHECK) return;
  if (s_rr_n > 0 && !s_pend_ok[JOB_RTEST] && !(s_job_busy && s_job_cur.type == JOB_RTEST)) {
    Job j = {};
    j.type = JOB_RTEST;
    j.gs = s_gs;
    j.cycle = s_cycle;
    j.nbytes = s_nbytes;
    j.nidx = s_cfg.nb;
    for (int i = 0; i < s_cfg.nb; ++i) j.idx[i] = (uint8_t)i;
    issue(j);
  }
}

// Отсев готов (ядро 1 или ядро связи): уцелевшие — в запас.
static void r_sieved(const uint8_t* buf, int n, uint32_t sv_us) {
  s_sieving = false;
  s_gathering = false;
  s_stats.cands += s_K;
  s_stats.survivors += n;
  if (n == 0) ++s_stats.regathers;
  if (s_ph != PH_R_CHECK) return;
  r_push_back(buf, n);
  r_feed();
}

static void r_sieve_step() {         // два ядра: отсев на ядре связи — по 16 кандидатов за проход цикла задачи
  if (s_asm_n <= 0) return;
  if (s_asm_k == 0) s_asm_s = 0;
  const int end = min(s_asm_n, s_asm_k + 16);
  for (; s_asm_k < end; ++s_asm_k) {
    uint8_t* b = s_asm + (size_t)s_asm_k * s_nbytes;
    if (!sieve_candidate(b, s_cfg, s_sv)) continue;
    if (s_asm_s != s_asm_k) memmove(s_asm + (size_t)s_asm_s * s_nbytes, b, s_nbytes);
    ++s_asm_s;
  }
  if (s_asm_k < s_asm_n) return;
  s_asm_n = 0;
  r_sieved(s_asm, s_asm_s, 0);
}

static void r_audit_issue() {        // сверить s_aud одной базой, которой проверяющий не проверял
  if (s_cfg.nb >= MAX_BASES) {       // все базы уже у проверяющего — независимой сверки нет
    s_aud_ok = false;
    return;
  }
  Job j = {};
  j.type = JOB_AUDIT;
  j.gs = s_gs;
  j.cycle = ++s_aud_seq;
  j.nbytes = s_nbytes;
  j.count = 1;
  j.nidx = 1;
  j.idx[0] = (uint8_t)s_cfg.nb;
  issue(j);
}

static void r_audit_done(const JobResult& r) {
  if (r.cycle == s_aud_seq) s_aud_ok = false;
  if (s_ph == PH_R_CHECK) s_r_mr += r.us;
  if (r.value == -2) return;
  ++s_stats.agree;
  if (r.value < 0) return;
  ++s_stats.split;                   // свидетель нашёлся: «простое» соседа — составное
  char hex[132];
  number_hex(s_job_buf, min((int)s_nbytes, 64), hex, sizeof hex);
  snprintf(s_stats.last_split, sizeof s_stats.last_split, "%s %d", hex, (int)r.value);
  Serial.printf("GRP AUDIT SPLIT %s\n", s_stats.last_split);
}

static int next_alive(int from) {    // следующий по кругу после from, кроме молчащих; некому — сам
  for (int k = 1; k < s_ng; ++k) {
    const int q = (from + k) % s_ng;
    if (q != s_pos && !s_silent[q]) return q;
  }
  return s_pos;
}

static void mark_silent(int p) {
  if (p < 0 || p == s_pos || s_silent[p]) return;
  s_silent[p] = true;
  ++s_stats.mute;
  Serial.printf("GRP mute #%d (turn %lu)\n", s_cfg.grp[p], (unsigned long)s_cycle);
}

static void r_turn(uint32_t turn) {  // ход у агента
  s_ph = PH_R_CHECK;
  s_cycle = turn;
  s_master = (uint8_t)s_me;
  cycle_begin();
  s_turn_pr = 0;
  s_r_mr = s_r_other = 0;
  s_r_pcs_us = s_r_sv_us = 0;
  r_feed();
}

static void r_wait(uint32_t turn, uint8_t checker) {   // эстафета: ход у другого
  s_ph = PH_R_WAIT;
  s_cycle = turn;
  s_master = checker;
  cycle_begin();
  s_gathering = s_sieving = false;
  s_asm_n = 0;
  s_rr_n = 0;
  s_hold = 0;
  rq_drop((uint8_t)s_me);
  s_last_heard = now_us();
}

static void r_drop_turn() {          // уступили ход: свой запас и сбор — забыть
  s_hold = 0;
  s_rr_n = 0;
  s_gathering = s_sieving = false;
  s_asm_n = 0;
  rq_drop((uint8_t)s_me);
}

static void r_send_ack(int p) {      // ход принят — прежнему проверяющему
  Trn a = {};
  hdr(a.h, M_TURN);
  a.bits = s_cfg.bits;
  send_pos(p, &a, TRN_HDR);
}

// Ход окончен: учёт, простое — на сверку следующему; эстафета — ему же ход и запас.
static void r_pass() {
  const int64_t now = now_us();
  const int64_t span = now - s_t0;
  int64_t idle = span - s_r_mr - s_r_other;
  if (idle < 0) idle = 0;
  const bool hand = s_cfg.gv == 1 && s_ng > 1;
  const uint32_t hop = hand ? s_hop_us : 0;
  ++s_stats.cycles_m;
  s_stats.t_gather += idle;          // ядро счёта ждало числа
  s_stats.t_sieve += s_r_other;      // ядро счёта набирало шум и отсеивало
  s_stats.t_test += s_r_mr;
  s_stats.t_hand += hop;
  s_stats.t_cycle += span + hop;
  s_stats.c_pieces += s_r_pcs_us;
  s_stats.c_sieve += s_r_sv_us;
  s_stats.c_test += s_r_mr;
  cycle_end();
  const uint32_t turn = s_cycle;
  const int np = next_alive(s_pos);  // молчащих — мимо
  if (np == s_pos) {                 // один в группе (или остальные молчат): сверяет сам, ход — себе же
    memcpy(s_aud, s_found, s_nbytes);
    s_aud_ok = true;
    r_audit_issue();
    r_turn(turn + 1);
    return;
  }
  Trn* t = (Trn*)s_nxt_msg;
  hdr(t->h, M_TURN);
  t->primes = (uint8_t)s_turn_pr;
  t->has_prime = 1;
  t->bits = s_cfg.bits;
  t->span = (uint32_t)span;
  memcpy(t->data, s_found, s_nbytes);
  int rn = 0;
  if (hand) {                        // эстафета: ход и запас — следующему
    rn = min(s_rr_n, trn_cap());
    memcpy(t->data + s_nbytes, s_rr + (size_t)(s_rr_n - rn) * s_nbytes, (size_t)rn * s_nbytes);
    s_stats.rsv_drop += s_rr_n - rn;
    t->next = s_cfg.grp[np];
  } else {
    t->next = 0;                     // каскад: ход остаётся у нас
  }
  t->rn = (uint8_t)rn;
  s_nxt_len = (uint16_t)(TRN_HDR + (size_t)(1 + rn) * s_nbytes);
  s_nxt_cycle = turn;
  send_pos(np, s_nxt_msg, s_nxt_len);
  if (!hand) {
    r_turn(turn + 1);
    return;
  }
  s_nxt_to = s_cfg.grp[np];
  s_hand_wait = true;
  s_hand_t = now;
  s_hand_tries = 0;
  s_hand_deadline = now + r_hand_wait_us();
  r_wait(turn + 1, s_nxt_to);
}

static void r_prime(const uint8_t* pr) {
  ++s_stats.primes_m;
  ++s_turn_pr;
  memcpy(s_found, pr, s_nbytes);
  if (s_nbytes <= 64) number_hex(pr, s_nbytes, s_stats.last_prime, sizeof s_stats.last_prime);
  if (s_ann) announce(pr, s_nbytes);            // сету B — рассеивать
  if (s_cfg.pl) plog::push(pr, s_nbytes, 1);    // журнал простых: каждый проверяющий — свои
  if (s_turn_pr >= s_cfg.rk) r_pass();
}

static void r_test_done(const JobResult& r) {
  const int m = s_rt_m;
  s_rt_m = 0;
  if (s_ph != PH_R_CHECK) return;    // ход уже не наш — результат не нужен
  s_r_mr += r.us;
  const int tested = min((int)r.tests, m);
  r_push_back(s_job_buf + (size_t)tested * s_nbytes, m - tested);   // непроверенные — обратно в запас
  if (r.value >= 0 && r.value < m) {
    s_stats.found += (uint32_t)r.value;
    r_prime(s_job_buf + (size_t)r.value * s_nbytes);
  } else if (r.value == -1) {
    s_stats.found += (uint32_t)tested;
  }
  r_feed();
}

// Недостающие кусочки круга — своим шумом из запаса наборов: число всё равно случайное,
// просто в нём меньше чужого шума. Свои кусочки нужны в любом случае.
static void r_truncate(int64_t now) {
  auto have = [](int i) { return s_pcs_ok[i] && s_pcs_cyc[i] == s_g_cyc && s_pcs_rnd[i] == s_round; };
  if (!have(s_pos)) {
    s_g_deadline = now + 1000;
    return;
  }
  int miss = 0;
  for (int i = 0; i < s_ng; ++i)
    if (!have(i)) ++miss;
  if (miss > s_pre_n) {              // своих наборов на замену не хватает — копить, соседям не отдавать
    s_hold = miss;
    refill();
    s_g_deadline = now + 1000;
    return;
  }
  s_hold = 0;
  for (int i = 0; i < s_ng; ++i)
    if (!have(i)) {
      memcpy(s_pieces[i], s_prep[--s_pre_n], pcs_len());
      s_pcs_ok[i] = true;
      s_pcs_cyc[i] = s_g_cyc;
      s_pcs_rnd[i] = s_round;
      ++s_stats.trunc_pcs;
    }
  ++s_stats.truncs;
  refill();
  gather_check();
  serve_pcs();                       // соседям — то, что придержали
}

static void r_on_msg(int p, const Hdr& h, const uint8_t* d, size_t len, int64_t now) {
  switch (h.type) {
    case M_REQ: {                    // проверяющий просит кусочки своего круга
      if (len < sizeof(Req)) return;
      const Req& q = *(const Req*)d;
      if (q.K != s_K || q.pb != s_pb) return;
      if (s_cfg.gv == 1) {
        if (s_ph == PH_R_CHECK) {    // два проверяющих сразу: уступает тот, чей ход старше
          if (h.cycle > s_cycle || (h.cycle == s_cycle && h.from < s_me)) {
            ++s_stats.conflicts;
            s_dirty_next = true;
            r_drop_turn();
            r_wait(h.cycle, h.from);
          } else {
            ++s_stats.stale;
            return;
          }
        } else {
          if (h.cycle < s_cycle) { ++s_stats.stale; return; }
          if (h.cycle > s_cycle) {   // начался новый ход
            const bool skipped = s_ph == PH_R_WAIT && h.cycle > s_cycle + 1;
            if (s_ph == PH_R_WAIT) cycle_end();
            s_ph = PH_R_WAIT;
            s_cycle = h.cycle;
            cycle_begin();
            if (skipped) unclean();
          }
          s_master = h.from;
          s_last_heard = now;
        }
      }
      if (rq_has(h.cycle, h.round, h.from)) return;   // уже набираем
      if (s_srv_ok[p] && s_srv_cyc[p] == h.cycle && s_srv_rnd[p] == h.round) {   // повтор: наш набор не дошёл
        ++s_stats.asks_rx;
        unclean();
      }
      want_pcs(h.cycle, h.round, h.from);
      break;
    }
    case M_PCS: {                    // кусочки для нашего круга
      if (len < PCS_HDR) return;
      const Pcs& m = *(const Pcs*)d;
      if (m.K != s_K || m.pb != s_pb || len < PCS_HDR + pcs_len()) return;
      if (s_ph == PH_R_CHECK && s_gathering && !s_sieving && h.cycle == s_g_cyc && h.round == s_round &&
          !(s_pcs_ok[p] && s_pcs_cyc[p] == h.cycle && s_pcs_rnd[p] == h.round)) {
        lat_update(s_lat_pcs, p, now - s_g_t0);
        take_pieces(p, h.cycle, h.round, m.data);
        return;
      }
      ++s_stats.stale;
      break;
    }
    case M_TURN: {
      if (len < TRN_HDR) return;
      const Trn& t = *(const Trn*)d;
      if (t.bits != s_cfg.bits || len < TRN_HDR + (size_t)(t.has_prime + t.rn) * s_nbytes) return;
      if (t.has_prime) {             // простое соседа — сверить
        memcpy(s_aud, t.data, s_nbytes);
        s_aud_ok = true;
        r_audit_issue();
      }
      if (s_cfg.gv != 1) return;     // каскад: только сверка
      if (!t.has_prime && !t.rn) {   // подтверждение: наш ход принят
        if (s_hand_wait && h.from == s_nxt_to && h.cycle > s_nxt_cycle) {
          const int64_t rtt = now - s_hand_t;
          if (rtt > 0 && rtt < 1000000)
            s_hop_us = s_hop_us ? (3 * s_hop_us + (uint32_t)(rtt / 2)) / 4 : (uint32_t)(rtt / 2);
          s_hand_wait = false;
        }
        return;
      }
      if (t.next != s_me) return;
      const uint32_t turn = h.cycle + 1;
      if (s_ph == PH_R_CHECK && s_cycle >= turn) {   // повтор хода, который уже у нас: подтвердить ещё раз
        r_send_ack(p);
        ++s_stats.asks_rx;
        unclean();
        return;
      }
      if (turn < s_cycle) { ++s_stats.stale; return; }
      if (s_ph == PH_R_CHECK) {      // два проверяющих: их ход новее — принять его
        ++s_stats.conflicts;
        s_dirty_next = true;
        r_drop_turn();
      } else if (s_ph == PH_R_WAIT) {
        if (turn > s_cycle + 1) unclean();     // пропустили ходы
        cycle_end();
      }
      s_rr_n = 0;
      r_push_back(t.data + (size_t)t.has_prime * s_nbytes, t.rn);
      s_last_heard = now;
      r_turn(turn);
      r_send_ack(p);                 // подтверждение — уже с номером нового хода
      break;
    }
    default:
      ++s_stats.stale;
  }
}

static int64_t r_watchdog_us() {     // эстафета: сколько ждать вестей от проверяющего
  int64_t w = max((int64_t)1500000, 6 * s_avg_cycle_us);
  w = max(w, 3 * (int64_t)s_last_pieces_us + 1000000);   // медленный источник: круг сбора идёт секунды
  const int pc = pos_of(s_master);
  const int d = pc >= 0 ? (s_pos - pc - 1 + s_ng) % s_ng : s_pos;   // следующий по порядку — первым
  return w + 500000LL * d;
}

static void r_on_timer() {
  const int64_t now = now_us();
  if (s_hand_wait && now >= s_hand_deadline) {     // следующий не подтвердил ход: повторить, потом — мимо него
    Trn* t = (Trn*)s_nxt_msg;
    if (++s_hand_tries > 3) {
      ++s_stats.brk_hand;
      unclean();
      mark_silent(pos_of(s_nxt_to));
      const int np = next_alive(pos_of(s_nxt_to));
      if (np == s_pos) {                           // кроме нас ход взять некому
        s_hand_wait = false;
        r_push_back(t->data + s_nbytes, t->rn);
        r_turn(s_nxt_cycle + 1);
        return;
      }
      t->next = s_cfg.grp[np];
      s_nxt_to = t->next;
      s_master = s_nxt_to;
      s_hand_tries = 0;
      send_pos(np, s_nxt_msg, s_nxt_len);
    } else {
      send_pos(pos_of(s_nxt_to), s_nxt_msg, s_nxt_len);
      ++s_stats.asks_tx;
      unclean();
    }
    s_hand_deadline = now + r_hand_wait_us();
  }
  if (s_ph == PH_START) {                          // первый ход: эстафета — первый по порядку, каскад — все
    if (now >= s_deadline) {
      ++s_stats.starts;
      r_turn(s_cycle + 1);
    }
    return;
  }
  if (s_ph == PH_R_WAIT) {                         // эстафета: проверяющий давно молчит — ход себе
    if (!s_hand_wait && now - s_last_heard > r_watchdog_us()) {
      ++s_stats.resyncs;
      s_dirty_next = true;
      s_last_heard = now;
      r_turn(s_cycle + 1);
    }
    return;
  }
  if (s_ph != PH_R_CHECK || !s_gathering || s_sieving || !s_g_deadline || now < s_g_deadline) return;
  auto have = [](int i) { return s_pcs_ok[i] && s_pcs_cyc[i] == s_g_cyc && s_pcs_rnd[i] == s_round; };
  bool only_silent = false;                        // не хватает только молчащих (свои — не в счёт)
  for (int i = 0; i < s_ng; ++i) {
    if (i == s_pos || have(i)) continue;
    if (!s_silent[i]) { only_silent = false; break; }
    only_silent = true;
  }
  if (s_cfg.pw || only_silent) {                   // ждали сколько велено или ждём только молчащих —
    r_truncate(now);                               // собрать из тех, что есть (недостающие — свой шум)
    return;
  }
  if (++s_g_tries > 3) {                           // три просьбы без ответа: не приславших — в молчащие,
    ++s_stats.brk_pcs;                             // круг — из того, что есть (раньше круг бросался и
    unclean();                                     // начинался новый — с тем же молчащим, без конца)
    for (int i = 0; i < s_ng; ++i)
      if (!have(i)) mark_silent(i);
    r_truncate(now);
    return;
  }
  bool skip[MAX_GROUP];
  for (int i = 0; i < s_ng; ++i) {
    skip[i] = i == s_pos || (s_pcs_ok[i] && s_pcs_cyc[i] == s_g_cyc && s_pcs_rnd[i] == s_round);
    if (!skip[i]) ++s_stats.asks_tx;
  }
  send_req(skip);
  unclean();
  s_g_deadline = now + wait_us(s_lat_pcs, s_last_pieces_us);
}

// Наборы кусочков от производителя (два ядра) — в свой запас наборов.
static void r_drain_prod() {
  static ProdSet it;
  while (s_pre_n < PRE_MAX && xQueueReceive(s_prodq, &it, 0) == pdTRUE) {
    if (it.gs != s_gs || it.len != pcs_len()) continue;   // набор прошлой настройки
    memcpy(s_prep[s_pre_n++], it.data, pcs_len());
  }
  if (s_rq_n) serve_pcs();
}

// ---------- обработка сообщений ----------
static void on_msg(const uint8_t* mac, int8_t rssi, const uint8_t* d, size_t len) {
  const Hdr& h = *(const Hdr*)d;
  if (h.gs != s_gs) return;                         // другая настройка группы
  int p = pos_of(h.from);
  if (p < 0 || p == s_pos) return;
  ++s_stats.rx;
  if (h.type == M_REST) {                           // сосед ушёл отдыхать или вернулся
    if (len >= sizeof(Rst)) s_silent[p] = ((const Rst*)d)->on != 0;
    return;
  }
  if (s_resting) return;                            // отдыхаю: группу не слушаю, ничего не шлю
  s_silent[p] = false;                              // подал голос — снова ждём его кусочки и ход
  if (rssi) s_stats.rssi_from[p] = s_stats.rssi_from[p] ? (int8_t)((3 * s_stats.rssi_from[p] + rssi) / 4) : rssi;
  if (!s_mac_ok[p] || memcmp(s_mac[p], mac, 6)) {
    memcpy(s_mac[p], mac, 6);
    s_mac_ok[p] = true;
    ensure_peer(mac);
    set_rate(mac);
  }
  const int64_t now = now_us();
  if (relay()) {                                    // эстафета и каскад — свой разбор
    r_on_msg(p, h, d, len, now);
    return;
  }
  if (s_hand_wait && h.from == s_nxt_to && h.cycle > s_nxt_cycle && h.type != M_NXT)
    s_hand_wait = false;                            // новый мастер жив и начал цикл

  switch (h.type) {
    case M_NXT: {
      if (len < NXT_HDR) return;
      const Nxt& n = *(const Nxt*)d;
      if (n.nn > 1 || (n.nn && n.bits != s_cfg.bits) || len < NXT_HDR + (size_t)(n.nn + n.rn) * s_nbytes) return;
      if (s_hand_wait && h.from == s_nxt_to && h.cycle > s_nxt_cycle) {
        if (h.cycle == s_nxt_cycle + 1) {           // итог того, кому мы передали эстафету:
          int64_t rtt = now - s_hand_t - (int64_t)n.span;   // путь туда и обратно без его цикла
          if (rtt > 0 && rtt < 1000000) {
            uint32_t hop = (uint32_t)(rtt / 2);
            s_hop_us = s_hop_us ? (3 * s_hop_us + hop) / 4 : hop;
          }
        }
        s_hand_wait = false;
      }
      if (is_master_phase()) {
        // Итог цикла h.cycle открывает цикл h.cycle + 1. Не новее нашего — это
        // повтор итога, с которого начался наш цикл: не принимать (иначе цикл
        // начнётся заново, а запас удвоится). Новее — его ветка впереди, уступаем.
        if (h.cycle >= s_cycle) {
          ++s_stats.conflicts;
          s_dirty_next = true;
          adopt_nxt(n, false);
        } else {
          ++s_stats.stale;
        }
        return;
      }
      if (h.cycle < s_cycle || (h.cycle == s_cycle && h.from != s_master)) { ++s_stats.stale; return; }
      s_last_heard = now;
      bool ours = h.cycle == s_cycle;               // итог нашего цикла; иначе мы отстали
      bool skipped = !ours && s_cycle != 0;
      adopt_nxt(n, ours);
      if (skipped) unclean();
      break;
    }
    case M_NUM: {
      if (len < NUM_HDR) return;
      const Num& m = *(const Num*)d;
      if (m.bits != s_cfg.bits || len < NUM_HDR + s_nbytes) return;
      if (is_master_phase()) {
        if (yield_to(h)) participant_cycle(h.cycle, h.from, m.data, false);
        return;
      }
      if (h.cycle < s_cycle || (h.cycle == s_cycle && h.from != s_master)) { ++s_stats.stale; return; }
      s_last_heard = now;
      if (h.cycle > s_cycle) {                      // цикл начат без итога или итог не дошёл
        bool skipped = s_cycle != 0;
        participant_cycle(h.cycle, h.from, m.data, false);
        if (skipped) unclean();
        return;
      }
      if (s_last_res_cycle == s_cycle) {            // ответ уже был — повторить
        send_pos(p, &s_last_res, sizeof s_last_res);
        ++s_stats.asks_rx;
        unclean();
        return;
      }
      if (s_ph == PH_P_TEST) return;                 // уже проверяем
      memcpy(s_number, m.data, s_nbytes);
      s_num_ok = true;
      start_ptest();
      break;
    }
    case M_REQ: {
      if (len < sizeof(Req)) return;
      const Req& r = *(const Req*)d;
      if (r.K != s_K || r.pb != s_pb) return;
      if (is_master_phase()) {
        if (!yield_to(h)) return;
        participant_cycle(h.cycle, h.from, nullptr, false);
      } else {
        if (h.cycle < s_cycle || (h.cycle == s_cycle && h.from != s_master)) { ++s_stats.stale; return; }
        if (h.cycle > s_cycle) {                    // мастер начал цикл без числа
          bool skipped = s_cycle != 0;
          participant_cycle(h.cycle, h.from, nullptr, false);
          if (skipped) unclean();
        }
      }
      s_last_heard = now;
      if (s_last_pcs_len && s_last_pcs_cycle == h.cycle && s_last_pcs_round == h.round && s_last_pcs_to == h.from) {
        send_pos(p, s_last_pcs, s_last_pcs_len);     // мастер не получил — повторить
        ++s_stats.asks_rx;
        unclean();
        return;
      }
      if (rq_has(h.cycle, h.round, h.from)) return;  // уже набираем
      want_pcs(h.cycle, h.round, h.from);
      break;
    }
    case M_PCS: {
      if (len < PCS_HDR) return;
      const Pcs& m = *(const Pcs*)d;
      if (m.K != s_K || m.pb != s_pb || len < PCS_HDR + pcs_len()) return;
      if (is_master_phase() && h.cycle == s_cycle) {
        if (!s_gathering || s_sieving || h.round != s_round ||
            (s_pcs_ok[p] && s_pcs_cyc[p] == h.cycle && s_pcs_rnd[p] == h.round)) { ++s_stats.stale; return; }
        lat_update(s_lat_pcs, p, now - s_g_t0);
        take_pieces(p, h.cycle, h.round, m.data);
        return;
      }
      if (!is_master_phase() && h.cycle == s_cycle + 1 && h.round == 0) {   // раньше итога
        memcpy(s_pieces[p], m.data, pcs_len());
        s_pcs_ok[p] = true;
        s_pcs_cyc[p] = h.cycle;
        s_pcs_rnd[p] = h.round;
        ++s_stats.early;
        ask_nxt();
        return;
      }
      ++s_stats.stale;
      break;
    }
    case M_RES: {
      if (len < sizeof(Res)) return;
      const Res& r = *(const Res*)d;
      if (s_ph == PH_M_TEST && h.cycle == s_cycle) {
        if (r.tag != tag_of(s_number)) { ++s_stats.stale; return; }   // ответ про другое число
        if (!s_have_res[p]) {
          s_have_res[p] = true;
          s_res_w[p] = r.witness;
          s_res_us[p] = r.us;
          lat_update(s_lat_res, p, now - t_num);
        }
        test_check();
        return;
      }
      if (!is_master_phase() && h.cycle == s_cycle + 1) {   // раньше итога: сохранить
        s_eres_ok[p] = true;
        s_eres_cyc[p] = h.cycle;
        s_eres_tag[p] = r.tag;
        s_eres_w[p] = r.witness;
        s_eres_us[p] = r.us;
        ++s_stats.early;
        ask_nxt();
        return;
      }
      ++s_stats.stale;
      break;
    }
    case M_ASK: {
      if (len < sizeof(Ask)) return;
      const Ask& a = *(const Ask*)d;
      if (a.what != M_NXT || !s_nxt_len || s_nxt_cycle != h.cycle) return;
      ++s_stats.asks_rx;
      unclean();
      if (h.from == s_nxt_to) {                     // новому мастеру — целиком, с запасом
        send_pos(p, s_nxt_msg, s_nxt_len);
      } else {
        Nxt* n = (Nxt*)s_nxt_msg;
        uint8_t rn = n->rn;
        n->rn = 0;
        send_pos(p, s_nxt_msg, NXT_HDR + (size_t)n->nn * s_nbytes);
        n->rn = rn;
      }
      break;
    }
  }
}

// ---------- рассеяние: сет B ----------
// Сет A, найдя простое, объявляет его одним широковещательным кадром (PRIME).
// Агент B рассеивает его по режиму s_cfg.sm, смешивая со СВОИМ шумом (блоки из
// его источников, набранные заранее) своей функцией s_cfg.sf, и выпускает в эфир
// широковещательно (SCAT). Эстафета идёт адресно (CHAIN): B1 → B2 → …, последний
// выпускает. Пустышка: столько же кадров в те же моменты, но только шум.

// Работа, ждущая шума: простое (из PRIME) или шаг эстафеты (из CHAIN).
struct BWork { bool chain, waited; uint8_t from; uint32_t seq; uint16_t len; int64_t t; uint8_t data[256]; };
static const int BQ = 4;
static BWork     s_bq[BQ];
static int       s_bq_head = 0, s_bq_n = 0;
static uint16_t  s_perm[2048];                  // перестановка бит

static void be_to_words(const uint8_t* b, int len, uint32_t* w, int n) {   // младшие слова первыми
  for (int k = 0; k < n; ++k) {
    uint32_t v = 0;
    for (int t = 3; t >= 0; --t) {
      int i = len - 1 - (k * 4 + t);
      v = (v << 8) | (i >= 0 ? b[i] : 0);
    }
    w[k] = v;
  }
}

static void words_to_be(const uint32_t* w, uint8_t* b, int len) {
  for (int e = 0; e < len; ++e) b[len - 1 - e] = (uint8_t)(w[e >> 2] >> (8 * (e & 3)));
}

// Рассеять in (len байт) блоком шума noise функцией sf → out (len байт).
static void scatter_fn(uint8_t sf, const uint8_t* in, int len, const uint8_t* noise, uint8_t* out) {
  switch (sf) {
    case SF_XOR:                     // обратимо, только если знать шум — а он тут же забыт
      for (int i = 0; i < len; ++i) out[i] = in[i] ^ noise[i];
      break;
    case SF_MUL: {                   // число × нечётное из шума по модулю 2^(8·len)
      static uint32_t a[64], b[64], r[64];
      const int n = (len + 3) / 4;
      be_to_words(in, len, a, n);
      be_to_words(noise, len, b, n);
      b[0] |= 1;
      memset(r, 0, sizeof r);
      for (int i = 0; i < n; ++i) {
        uint64_t carry = 0;
        for (int j = 0; i + j < n; ++j) {
          uint64_t t = (uint64_t)a[i] * b[j] + r[i + j] + carry;
          r[i + j] = (uint32_t)t;
          carry = t >> 32;
        }
      }
      words_to_be(r, out, len);
      break;
    }
    case SF_PERM: {                  // перестановка бит; порядок задаёт шум (xorshift128+)
      uint64_t s0, s1;
      memcpy(&s0, noise, 8);
      memcpy(&s1, noise + 8, 8);
      if (!s0 && !s1) s1 = 1;
      const int nbits = len * 8;
      for (int i = 0; i < nbits; ++i) s_perm[i] = (uint16_t)i;
      for (int i = nbits - 1; i > 0; --i) {
        uint64_t x = s0, y = s1;
        s0 = y;
        x ^= x << 23;
        s1 = x ^ y ^ (x >> 17) ^ (y >> 26);
        const int j = (int)((s1 + y) % (uint64_t)(i + 1));
        const uint16_t t = s_perm[i];
        s_perm[i] = s_perm[j];
        s_perm[j] = t;
      }
      memset(out, 0, len);
      for (int k = 0; k < nbits; ++k) {
        const int src = s_perm[k];
        if (in[src >> 3] & (0x80 >> (src & 7))) out[k >> 3] |= (uint8_t)(0x80 >> (k & 7));
      }
      break;
    }
    default: {                       // SF_HASH: SHA-256(шум ‖ номер блока ‖ число), по 32 байта
      for (int off = 0, j = 0; off < len; off += 32, ++j) {
        uint8_t h[32], jb[2] = {(uint8_t)(j >> 8), (uint8_t)j};
        mbedtls_sha256_context cx;
        mbedtls_sha256_init(&cx);
        mbedtls_sha256_starts(&cx, 0);
        mbedtls_sha256_update(&cx, noise, 32);
        mbedtls_sha256_update(&cx, jb, 2);
        mbedtls_sha256_update(&cx, in, len);
        mbedtls_sha256_finish(&cx, h);
        mbedtls_sha256_free(&cx);
        memcpy(out + off, h, min(32, len - off));
      }
    }
  }
}

static void announce(const uint8_t* num, uint16_t nbytes) {   // A: простое — сету B
  static uint8_t buf[MSG_MAX];
  if (nbytes == 0 || PRM_HDR + nbytes > MSG_MAX) return;
  Prm* m = (Prm*)buf;
  hdr(m->h, M_PRIME);
  m->h.cycle = ++s_ann_seq;
  m->bits = (uint16_t)(nbytes * 8);
  memcpy(m->data, num, nbytes);
  send_to(BCAST, buf, PRM_HDR + nbytes);
  ++s_stats.ann;
}

// Агенты B получают объявление простого одновременно и отвечали бы одновременно:
// широковещательные кадры без подтверждения сталкивались (три B — около 15 % кадров
// терялось, замер 04.10.2026). Поэтому кадр на простое уходит в окно своего места
// в сете B: место p — через p мс (цикл задачи группы — 1 мс, окна не перекрываются).
static const int64_t B_SLOT_US = 1000;

static void b_send_scat(uint8_t kind, uint8_t piece, uint8_t pieces, const BWork& w, const uint8_t* data, int len,
                        bool count = true, int64_t t_ok = 0) {
  static uint8_t buf[MSG_MAX];
  Sct* m = (Sct*)buf;
  hdr(m->h, M_SCAT);
  m->h.cycle = w.seq;
  m->kind = kind;
  m->sf = s_cfg.sf;
  m->piece = piece;
  m->pieces = pieces;
  m->ofrom = w.from;
  m->len = (uint16_t)len;
  memcpy(m->data, data, len);
  send_to(BCAST, buf, SCT_HDR + len, t_ok);
  if (!count) return;
  ++s_stats.stx;
  s_stats.slat += (uint64_t)(max(now_us(), t_ok) - w.t);   // задержка — до своего окна
  ++s_stats.sn;
}

static void b_send_chain(uint8_t step, const BWork& w, const uint8_t* data, int len) {
  static uint8_t buf[MSG_MAX];
  Chn* m = (Chn*)buf;
  hdr(m->h, M_CHAIN);
  m->h.cycle = w.seq;
  m->step = step;
  m->ofrom = w.from;
  m->len = (uint16_t)len;
  memcpy(m->data, data, len);
  send_to(s_bmac_ok[step] ? s_bmacs[step] : BCAST, buf, CHN_HDR + len);   // адрес неизвестен — всем, примет только он
}

// Пустышка: тот же режим и тот же трафик, но вместо рассеянного числа — только шум.
static void b_fn(const uint8_t* in, int len, const uint8_t* noise, uint8_t* out, bool sham) {
  if (sham) memcpy(out, noise, len);
  else scatter_fn(s_cfg.sf, in, len, noise, out);
}

static void b_process(const BWork& w, const uint8_t* noise) {
  static uint8_t out[256];
  const bool sham = s_cfg.sh || s_cfg.sm == SM_SHAM;          // SM_SHAM — прежняя «пустышка каждого»
  const uint8_t sm = s_cfg.sm == SM_SHAM ? SM_EACH : s_cfg.sm;
  const uint8_t kind = sham ? SM_SHAM : sm;
  const int64_t slot = s_bpos > 0 ? now_us() + s_bpos * B_SLOT_US : 0;   // каждый сам и россыпь — в своё окно
  if (sm == SM_PIECES) {             // свой кусок числа
    const int pb = (w.len + s_nbs - 1) / s_nbs, off = s_bpos * pb, n = min(pb, (int)w.len - off);
    if (n <= 0) return;
    b_fn(w.data + off, n, noise, out, sham);
    b_send_scat(kind, (uint8_t)s_bpos, (uint8_t)s_nbs, w, out, n, true, slot);
    return;
  }
  b_fn(w.data, w.len, noise, out, sham);
  if (sm == SM_CHAIN) {
    if (!w.chain) ++s_stats.chs;     // начали эстафету (первый в сете B)
    if (s_bpos + 1 >= s_nbs) {       // последний — в эфир
      b_send_scat(kind, (uint8_t)s_bpos, (uint8_t)s_nbs, w, out, w.len);
      ++s_stats.chd;
    } else {
      b_send_chain((uint8_t)(s_bpos + 1), w, out, w.len);
      if (w.chain) ++s_stats.chf;
    }
    return;
  }
  b_send_scat(kind, 0, 1, w, out, w.len, true, slot);      // каждый сам: всё число
}

// Фон: агент B сам выпускает свой шум с частотой s_cfg.bgr кадров в секунду,
// независимо от простых. Так петля «сет A берёт шум только из эфира B»
// запускается: без фона в начале рассеивать нечего. Простые — в первую очередь.
static int64_t s_bg_next = 0;

static void b_background() {
  if (!s_cfg.bgr) return;
  const int64_t now = now_us(), period = 1000000 / s_cfg.bgr;
  if (now < s_bg_next) return;
  s_bg_next = now - s_bg_next > 3 * period ? now + period : s_bg_next + period;
  if (s_bq_n > 0 || s_bn_units == 0) {   // простые ждут шум или шума нет — фон пропускаем
    refill();
    return;
  }
  --s_bn_units;
  BWork w = {};
  w.seq = s_stats.sbg + 1;
  w.t = now;
  b_send_scat(K_BG, 0, 1, w, s_bn + s_bn_units * s_bn_unit, s_nbytes, false);   // фон — отдельный счёт
  ++s_stats.sbg;
  refill();
}

static void b_pump() {               // рассеять всё, на что есть шум
  while (s_bq_n > 0) {
    BWork& w = s_bq[s_bq_head];
    if (s_bn_units == 0) {           // шум ещё набирается
      if (!w.waited) {
        w.waited = true;
        ++s_stats.swait;
      }
      break;
    }
    --s_bn_units;
    b_process(w, s_bn + s_bn_units * s_bn_unit);
    s_bq_head = (s_bq_head + 1) % BQ;
    --s_bq_n;
  }
  refill();
}

static void b_push(bool chain, uint8_t from, uint32_t seq, const uint8_t* data, uint16_t len) {
  if (s_bq_n == BQ) {                // не успеваем — простое пропускаем
    ++s_stats.sdrop;
    return;
  }
  BWork& w = s_bq[(s_bq_head + s_bq_n) % BQ];
  w.chain = chain;
  w.waited = false;
  w.from = from;
  w.seq = seq;
  w.len = len;
  w.t = now_us();
  memcpy(w.data, data, len);
  ++s_bq_n;
  b_pump();
}

static void b_on_prime(const Hdr& h, const uint8_t* d, size_t len) {
  if (h.gs != s_gs || len < PRM_HDR) return;       // объявление другой настройки (другого состава)
  const Prm& m = *(const Prm*)d;
  const int nb = m.bits / 8;
  if (nb != s_nbytes || len < PRM_HDR + (size_t)nb) return;    // другая длина — не наша настройка
  ++s_stats.prx;
  if (s_cfg.sm == SM_SILENT || (s_cfg.sm == SM_CHAIN && s_bpos != 0)) return;   // эстафету начинает первый
  b_push(false, h.from, h.cycle, m.data, (uint16_t)nb);
}

static void b_on_chain(const Hdr& h, const uint8_t* d, size_t len) {
  if (h.gs != s_gs || len < CHN_HDR || s_cfg.sm != SM_CHAIN) return;
  const Chn& c = *(const Chn*)d;
  if ((int)c.step != s_bpos || c.len == 0 || c.len > s_bn_unit || len < CHN_HDR + (size_t)c.len) return;
  b_push(true, c.ofrom, h.cycle, c.data, c.len);
}

// Все принятые кадры: рассеяние — здесь, протокол группы A — в on_msg.
static void on_rx(const uint8_t* mac, int8_t rssi, const uint8_t* d, size_t len) {
  const Hdr& h = *(const Hdr*)d;
  if (h.type == M_ALIVE) {           // пульс соседа — в таблицу, в любом режиме и при любой настройке
    if (len >= sizeof(Alv)) alive_note(*(const Alv*)d, mac, rssi);
    return;
  }
  if (h.type == M_SCAT) {            // кадр рассеяния своей настройки: считаем; эфир B — в шум, если отмечен
    if (h.gs != s_gs) return;
    ++s_stats.heard;
    if ((s_cfg.src & SRC_BC) && len >= SCT_HDR) {
      const Sct& m = *(const Sct*)d;
      if (len >= SCT_HDR + (size_t)m.len) entropy_feed_bcast(m.data, m.len);
    }
    return;
  }
  if (h.type == M_PRIME) {
    if (s_b_on) b_on_prime(h, d, len);
    return;
  }
  if (h.type == M_CHAIN) {
    if (s_b_on) b_on_chain(h, d, len);
    return;
  }
  if (s_on) on_msg(mac, rssi, d, len);
}

// ---------- результаты ядра 1 ----------
static void on_job(const JobResult& r) {
  s_job_busy = false;
  if (r.gs != s_gs) {                // прошлая настройка
    pump();
    return;
  }
  if (!r.ok) {                       // ядро 1 ещё не приняло настройку — повторить
    if (s_job_cur.type == JOB_RTEST) {   // кандидаты задания — обратно в запас, возьмутся заново
      r_push_back(s_job_buf, s_rt_m);
      s_rt_m = 0;
    }
    if (!s_pend_ok[s_job_cur.type]) {
      s_pend[s_job_cur.type] = s_job_cur;
      s_pend_ok[s_job_cur.type] = true;
    }
    pump();
    return;
  }
  switch (r.type) {
    case JOB_PIECES:
      s_last_pieces_us = r.us;
      if (s_b_on) {                  // B: блоки шума — в запас, и рассеивать ждущее
        const int n = s_job_cur.nbytes / s_bn_unit;
        for (int i = 0; i < n && s_bn_units < s_bn_max; ++i, ++s_bn_units)
          memcpy(s_bn + s_bn_units * s_bn_unit, s_job_buf + i * s_bn_unit, s_bn_unit);
        b_pump();
        break;
      }
      if (s_job_cur.nbytes == pcs_len() && s_pre_n < PRE_MAX) memcpy(s_prep[s_pre_n++], s_job_buf, pcs_len());
      if (s_ph == PH_R_CHECK) {      // свой ход: шум на ядре счёта — время не на проверку
        s_r_other += r.us;
        s_r_pcs_us += r.us;
      }
      if (s_rq_n && s_rq[0].to == s_me && (s_ph == PH_M_GATHER || s_ph == PH_M_FILL)) s_cp_us += r.us;
      serve_pcs();
      break;
    case JOB_SIEVE:
      if (relay()) {                 // эстафета, каскад: отсев на ядре 1 (одно ядро)
        if (r.cycle == s_g_cyc && r.round == s_round && s_gathering && s_sieving) {
          if (s_ph == PH_R_CHECK) {
            s_r_other += r.us;
            s_r_sv_us += r.us;
          }
          r_sieved(s_job_buf, r.value < 0 ? 0 : r.value, r.us);
        }
        break;
      }
      if (r.cycle == s_cycle && r.round == s_round && s_gathering && s_sieving) sieve_done(r);
      break;
    case JOB_RTEST:
      r_test_done(r);
      break;
    case JOB_AUDIT:
      r_audit_done(r);
      break;
    case JOB_TEST:
      s_last_test_us = r.us;
      if (r.value >= 0) ++s_stats.found;
      if (r.cycle != s_cycle || memcmp(s_job_buf, s_number, s_nbytes) != 0) break;   // число сменилось
      if (s_ph == PH_M_TEST) {
        s_have_res[s_pos] = true;
        s_res_w[s_pos] = (int8_t)r.value;
        s_res_us[s_pos] = r.us;
        s_deadline = t_num + wait_us(s_lat_res, r.us);
        test_check();
      } else if (s_ph == PH_P_TEST) {
        send_res((int8_t)r.value, r.tests, r.us);
      }
      break;
  }
  pump();
}

// ---------- таймеры ----------
// Кто не ответил вовремя — тому повторить само исходное сообщение (запрос
// кусочков или число): если оно потерялось, участник начнёт работу; если
// потерялся его ответ — пришлёт ответ ещё раз.
static void on_timer() {
  const int64_t now = now_us();
  if (s_hand_wait && now >= s_hand_deadline) {     // новый мастер молчит — повторить ему итог
    if (++s_hand_tries > 3) {
      s_hand_wait = false;
      ++s_stats.brk_hand;
    } else {
      send_pos(pos_of(s_nxt_to), s_nxt_msg, s_nxt_len);
      ++s_stats.asks_tx;
      unclean();
      s_hand_deadline = now + hand_wait_us();
    }
  }
  if (s_ph == PH_START) {                           // первый по порядку начинает через паузу
    if (now >= s_deadline) master_start(s_cycle + 1);
    return;
  }
  if (s_ph >= PH_WAIT && s_ph <= PH_P_NXT) {        // участник: не слышим мастера — начинаем сами
    if (now - s_last_heard > watchdog_us()) {
      ++s_stats.resyncs;
      s_dirty_next = true;
      s_last_heard = now;
      master_start(s_cycle + 1);
    }
    return;
  }
  if (!is_master_phase()) return;
  if (s_gathering && !s_sieving && s_g_deadline && now >= s_g_deadline) {   // нет чьих-то кусочков
    if (++s_g_tries > 3) {                          // разрыв
      ++s_stats.brk_pcs;
      unclean();
      s_gathering = false;
      if (s_ph == PH_M_GATHER) {                    // числа нет: цикл заново
        s_dirty_next = true;
        master_start(s_cycle + 1);
      } else if (s_ph == PH_M_FILL) {               // итог без числа: новый мастер соберёт сам
        finish();
      }                                             // PH_M_TEST: соберём заново после ответов
      return;
    }
    bool skip[MAX_GROUP];
    for (int i = 0; i < s_ng; ++i) {
      skip[i] = s_pcs_ok[i] && s_pcs_cyc[i] == s_cycle && s_pcs_rnd[i] == s_round;
      if (!skip[i] && i != s_pos) ++s_stats.asks_tx;
    }
    send_req(skip);
    unclean();
    s_g_deadline = now + wait_us(s_lat_pcs, s_last_pieces_us);
  }
  if (s_ph == PH_M_TEST && s_deadline && now >= s_deadline) {   // нет чьих-то ответов
    if (++s_tries > 3) {                            // разрыв: цикл заново
      ++s_stats.brk_res;
      s_dirty_next = true;
      master_start(s_cycle + 1);
      return;
    }
    for (int i = 0; i < s_ng; ++i)
      if (i != s_pos && !s_have_res[i]) ++s_stats.asks_tx;
    send_num(s_have_res);
    unclean();
    s_deadline = now + wait_us(s_lat_res, s_last_test_us);
  }
}

// ---------- настройка ----------
static void reset_protocol() {
  s_cycle = 0;
  s_round = 0;
  s_master = 0;
  s_deadline = 0;
  s_tries = 0;
  s_clean = true;
  s_dirty_next = false;
  s_num_ok = false;
  s_rsv_n = 0;
  memset(s_pcs_ok, 0, sizeof s_pcs_ok);
  s_gathering = s_sieving = false;
  s_g_deadline = 0;
  memset(s_eres_ok, 0, sizeof s_eres_ok);
  s_ask_t = 0;
  s_nxt_len = 0;
  s_hand_wait = false;
  s_hop_us = 0;
  s_last_pcs_len = 0;
  s_last_pcs_cycle = s_last_res_cycle = 0;
  s_pre_n = 0;
  s_rq_n = 0;
  memset(s_srv_ok, 0, sizeof s_srv_ok);
  s_refill = false;
  memset(s_pend_ok, 0, sizeof s_pend_ok);
  s_last_end = 0;
  s_avg_cycle_us = 100000;
  memset(s_lat_pcs, 0, sizeof s_lat_pcs);
  memset(s_lat_res, 0, sizeof s_lat_res);
  s_g_cyc = 0;                       // эстафета и каскад
  s_hold = 0;
  s_rr_n = 0;
  s_rt_m = 0;
  s_turn_pr = 0;
  s_aud_ok = false;
  s_asm_n = s_asm_k = 0;
  s_sv = PrimeStats();
  s_r_mr = s_r_other = 0;
  s_r_pcs_us = s_r_sv_us = 0;
  memset(s_silent, 0, sizeof s_silent);
  s_resting = s_rest_pending = false;               // частоту ставит новая настройка
  s_rest_cpu = 0;
  if (s_prodq) xQueueReset(s_prodq);   // наборы прошлой настройки не нужны
}

static void apply_cfg(const GenConfig& c) {
  bool keep = keep_stats(c, s_cfg) && s_gs;          // счётчики группы не обнулять
  bool was_on = s_on;
  s_cfg = c;
  if (keep && c.run) return;                         // сменился только предел температуры
  s_prod_len = 0;                                    // производителю шума — стоять, пока не настроимся
  if (keep && !c.run) {                              // остановка сессии: итоги замирают
    s_on = false;
    s_b_on = s_ann = false;
    s_ph = PH_OFF;
    s_txq_n = 0;
    s_tx_busy = false;
    memset(s_pend_ok, 0, sizeof s_pend_ok);
    if (was_on) Serial.printf("GRP stop cid=%lu cycles=%lu\n", (unsigned long)c.cid, (unsigned long)s_stats.cycles);
    return;
  }
  s_ng = c.ng;
  s_pos = -1;
  if (c.mode == MODE_GROUP && c.role == ROLE_A) {
    if (c.has_gmac) {                                // своё место — по своему MAC в списке сервера
      for (int i = 0; i < c.ng; ++i)
        if (memcmp(c.gmac[i], agent::mac_bytes(), 6) == 0) { s_pos = i; break; }
    } else {                                         // без списка — по номеру из реестра прошивки
      s_pos = pos_of((uint8_t)agent::device_id());
    }
  }
  s_me = s_pos >= 0 ? c.grp[s_pos] : agent::device_id();   // номер в группе — от сервера
  s_on = c.role == ROLE_A && c.mode == MODE_GROUP && c.run && s_pos >= 0 && s_me > 0;
  s_nbs = c.nbg;                                     // сет B: своё место — так же, по MAC или номеру
  s_bpos = -1;
  if (c.role == ROLE_B)
    for (int i = 0; i < c.nbg; ++i)
      if (c.has_bmac ? memcmp(c.bmac[i], agent::mac_bytes(), 6) == 0 : c.bg[i] == agent::device_id()) {
        s_bpos = i;
        break;
      }
  s_b_on = c.role == ROLE_B && c.run && s_bpos >= 0;
  s_ann = c.role == ROLE_A && c.run && c.nbg > 0;
  s_gs = c.cid;
  s_stats = Stats();
  s_ann_drop0 = s_ann_drop;
  s_rx_drop0 = s_rx_drop;
  s_stats.espnow_ver = s_pub.espnow_ver;
  reset_protocol();
  clear_peers();
  memset(s_mac_ok, 0, sizeof s_mac_ok);
  for (int i = 0; i < c.ng && i < MAX_GROUP; ++i) {          // адреса участников — сразу:
    if (i == s_pos) continue;                                // из списка сервера, а без него —
    bool known = c.has_gmac;                                 // из реестра, вшитого в прошивку
    if (known) memcpy(s_mac[i], c.gmac[i], 6);
    else known = agent::device_mac(c.grp[i], s_mac[i]);
    if (known) {
      s_mac_ok[i] = true;
      ensure_peer(s_mac[i]);
      set_rate(s_mac[i]);
    }
  }
  if (s_on) {
    Serial.printf("GRP cid=%lu me=%d pos=%d/%d from=%s peers:", (unsigned long)c.cid, s_me, s_pos, s_ng,
                  c.has_gmac ? "server" : "firmware");
    for (int i = 0; i < s_ng; ++i)
      if (i != s_pos)
        Serial.printf(" %u=%s", c.grp[i], s_mac_ok[i] ? "ok" : "?");
    Serial.print("\n");
  }
  memset(s_bmac_ok, 0, sizeof s_bmac_ok);
  for (int i = 0; i < c.nbg && i < MAX_GROUP; ++i) {         // сет B: адреса для эстафеты
    if (i == s_bpos) continue;
    bool known = c.has_bmac;
    if (known) memcpy(s_bmacs[i], c.bmac[i], 6);
    else known = agent::device_mac(c.bg[i], s_bmacs[i]);
    if (known && c.role == ROLE_B) {
      s_bmac_ok[i] = true;
      ensure_peer(s_bmacs[i]);
      set_rate(s_bmacs[i]);
    }
  }
  s_tx_cb = 0;
  s_tx_fail = 0;
  for (int i = 0; i < MAX_GROUP; ++i) s_ok_to[i] = s_fail_to[i] = 0;
  s_txq_n = 0;                                       // кадры прошлой настройки не нужны
  s_tx_busy = false;
  set_rate(BCAST);
  s_nbytes = c.bits / 8;
  s_bn_unit = max(32, (int)s_nbytes);                // блок шума: на всё число (XOR, умножение) и не меньше 32
  s_bn_max = min(8, (int)(sizeof s_bn / s_bn_unit));
  s_bn_units = 0;
  s_bq_head = s_bq_n = 0;
  s_ann_seq = 0;
  s_bg_next = 0;
  if (s_b_on || s_ann)
    Serial.printf("SCAT cid=%lu set=%c pos=%d/%d sm=%u sf=%u\n", (unsigned long)c.cid, s_b_on ? 'B' : 'A',
                  s_bpos, s_nbs, c.sm, c.sf);
  if (s_b_on) refill();                              // шум на первые простые — заранее
  if (!s_on) {
    s_ph = PH_OFF;
    return;
  }
  s_pb = (uint16_t)(((s_nbytes + s_ng - 1) / s_ng + 3) / 4 * 4);   // кусочек: bits/N, кратно 4 байтам
  int k = (int)((MSG_MAX - PCS_HDR) / s_pb);
  s_K = (uint8_t)max(1, min(min(K_MAX, (int)c.kmax), k));
  if (c.gv) s_K = (uint8_t)max(1, min(K_MAX, k));    // эстафета, каскад: набор — сколько влезет в кадр (меньше кадров)
  if ((size_t)s_K * s_nbytes > sizeof s_job_buf) s_K = (uint8_t)(sizeof s_job_buf / s_nbytes);
  s_last_heard = now_us();
  s_prod_gs = s_gs;
  s_prod_len = prod_noise() ? (int)pcs_len() : 0;    // два ядра: шум кусочков — производителю
  if (pre_auto()) refill();                          // кусочки на первый сбор — заранее
  if (s_pos == 0 || c.gv == 2) {                     // первый по порядку начинает через паузу:
    s_ph = PH_START;                                 // остальные успеют принять настройку
    s_deadline = now_us() + 300000 + (c.gv == 2 ? 3000LL * s_pos : 0);   // каскад — все, вразбивку
  } else if (c.gv == 1) {                            // эстафета: ход у первого, ждём его просьб
    s_ph = PH_R_WAIT;
    s_master = c.grp[0];
  } else {
    s_ph = PH_WAIT;
  }
  if (c.gv) Serial.printf("GRP %s K=%u pb=%u pw=%u rk=%u dc=%u\n", c.gv == 1 ? "relay" : "cascade", s_K, s_pb,
                          c.pw, c.rk, c.dc);
}

// ---------- задача ----------
static void publish_stats() {
  s_stats.active = s_on;
  s_stats.set = s_cfg.role;
  s_stats.ann_drop = s_ann_drop - s_ann_drop0;
  s_stats.rx_drop = s_rx_drop - s_rx_drop0;
  s_stats.role = !s_on ? 0 : is_master_phase() || s_ph == PH_START || s_ph == PH_R_CHECK ? 2 : 1;
  s_stats.gv = s_cfg.gv;
  s_stats.rr = (uint16_t)s_rr_n;
  s_stats.sv_cand = s_sv.cand;
  s_stats.sv_tdr = s_sv.td_rej;
  s_stats.sv_tdcyc = s_sv.td_cyc;
  s_stats.master = s_master;
  s_stats.phase = s_ph;
  s_stats.cycle = s_cycle;
  s_stats.hop_us = s_hop_us;
  s_stats.tx_fail = s_tx_fail;
  s_stats.tx_cb = s_tx_cb;
  s_stats.job_lost = s_job_lost;
  s_stats.resting = s_resting ? 1 : s_rest_pending ? 2 : 0;
  s_stats.rest_left = s_resting && s_rest_until > now_us() ? (uint16_t)((s_rest_until - now_us()) / 1000000) : 0;
  s_stats.mute_mask = 0;
  for (int i = 0; i < s_ng && i < MAX_GROUP; ++i)
    if (s_silent[i]) s_stats.mute_mask |= (uint16_t)(1u << i);
  for (int i = 0; i < MAX_GROUP; ++i) {
    s_stats.ok_to[i] = s_ok_to[i];
    s_stats.fail_to[i] = s_fail_to[i];
  }
  portENTER_CRITICAL(&s_mux);
  s_pub = s_stats;
  portEXIT_CRITICAL(&s_mux);
}

// ---------- отдых по очереди ----------
static void rest_announce(uint8_t on, uint16_t secs) {
  Rst m = {};
  hdr(m.h, M_REST);
  m.on = on;
  m.secs = secs;
  send_members(&m, sizeof m);        // адресно каждому — радио подтвердит доставку
}

static void rest_enter() {
  const int64_t now = now_us();
  s_rest_pending = false;
  s_resting = true;
  ++s_stats.rests;
  s_gathering = s_sieving = false;   // свой сбор, запас и просьбы — бросить: вернёмся с чистого листа
  s_asm_n = s_asm_k = 0;
  s_hold = 0;
  s_rr_n = 0;
  s_rq_n = 0;
  s_hand_wait = false;
  memset(s_pend_ok, 0, sizeof s_pend_ok);
  rest_announce(1, (uint16_t)((s_rest_until - now) / 1000000));
  s_rest_cpu = (uint16_t)getCpuFrequencyMhz();
  if (s_rest_cpu > 80) setCpuFrequencyMhz(80);
  Serial.printf("GRP rest %lu s (cpu %u -> 80)\n", (unsigned long)((s_rest_until - now) / 1000000), s_rest_cpu);
}

static void rest_leave() {
  s_resting = false;
  // частота — из настройки, а не «какая была до отдыха»: за отдых могла прийти настройка с другой
  if (s_rest_cpu && s_cfg.cpu && s_cfg.cpu != getCpuFrequencyMhz()) setCpuFrequencyMhz(s_cfg.cpu);
  s_rest_cpu = 0;
  rest_announce(0, 0);
  s_last_heard = now_us();
  if (s_cfg.gv == 2) r_turn(s_cycle + 1);           // каскад: свои ходы — снова
  else r_wait(s_cycle, s_master);                    // эстафета: слушать проверяющего, ждать хода
  Serial.println("GRP rest done");
}

static void rest_cmd(uint16_t secs) {                // команда сервера: secs — сколько, 0 — назад
  if (!s_on || !relay()) return;                     // отдых — только эстафета и каскад
  if (secs) {
    s_rest_until = now_us() + (int64_t)secs * 1000000;
    if (!s_resting) s_rest_pending = true;
  } else {
    s_rest_pending = false;
    if (s_resting) rest_leave();
  }
}

static void rest_tick() {
  // эстафета: сначала довести свой ход и дождаться, что следующий его принял
  if (s_rest_pending && !(s_cfg.gv == 1 && s_ph == PH_R_CHECK) && !s_hand_wait) rest_enter();
  if (s_resting && now_us() >= s_rest_until) rest_leave();
}

static void task(void*) {
  static Event ev;
  for (;;) {
    if (xQueueReceive(s_evq, &ev, pdMS_TO_TICKS(1)) == pdTRUE) {   // 1 мс: очередь отправки не ждёт
      if (ev.kind == EV_CFG) apply_cfg(ev.cfg);
      else if (ev.kind == EV_RX) on_rx(ev.mac, ev.rssi, ev.data, ev.len);
      else if (ev.kind == EV_JOB) {
        if ((s_on || s_b_on) && !s_resting) on_job(ev.res);
        else s_job_busy = false;       // отдых: итог задания, начатого до него, не нужен
      } else if (ev.kind == EV_PRIME) {
        if (s_ann) announce(ev.data, ev.len);
      } else if (ev.kind == EV_REST) {
        rest_cmd(ev.len);
      }
    }
    alive_tick();                      // пульс — в любом режиме
    tx_kick();                         // и очередь отправки живёт всегда, не только в группе
    if (s_on || s_b_on || s_ann) {
      if (s_on && relay() && !s_resting) {
        r_drain_prod();                // два ядра: наборы кусочков от производителя
        r_sieve_step();                // два ядра: отсев на ядре связи — по частям
      }
      if (!s_resting) refill_kick();
      if (s_on && !s_resting) {
        if (relay()) r_on_timer();
        else on_timer();
      }
      if (s_on && relay()) rest_tick();
      if (s_b_on) b_background();
    }
    // Итог задания потерялся (очередь событий была полна, job_done не дождался места): не ждать
    // его вечно — иначе ядру 1 больше ничего не выдаётся, а новая настройка «не принимается».
    if (s_job_busy && now_us() - s_job_t0 > 5000000) {
      s_job_busy = false;
      ++s_job_lost;
      pump();
    }
    static int64_t last_pub = 0;       // снимок статистики — раз в 50 мс, а не каждый проход (копия ~750 Б без прерываний)
    const int64_t now = now_us();
    if (now - last_pub >= 50000) {
      last_pub = now;
      publish_stats();
    }
  }
}

static void on_recv(const esp_now_recv_info_t* info, const uint8_t* data, int len) {   // задача Wi-Fi
  static Event ev;
  if (len < (int)sizeof(Hdr) || len > (int)MSG_MAX || data[0] != 'W' || data[1] != VER) return;
  ev.kind = EV_RX;
  memcpy(ev.mac, info->src_addr, 6);
  ev.rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
  ev.len = (uint16_t)len;
  memcpy(ev.data, data, len);
  if (xQueueSend(s_evq, &ev, 0) != pdTRUE) ++s_rx_drop;
}

static void on_sent(const esp_now_send_info_t* info, esp_now_send_status_t st) {
  ++s_tx_cb;
  int p = -1;                                       // кому был кадр
  if (info && info->des_addr)
    for (int i = 0; i < s_ng && i < MAX_GROUP; ++i)
      if (s_mac_ok[i] && memcmp(s_mac[i], info->des_addr, 6) == 0) { p = i; break; }
  if (st != ESP_NOW_SEND_SUCCESS) {
    ++s_tx_fail;
    if (p >= 0) ++s_fail_to[p];
  } else if (p >= 0) {
    ++s_ok_to[p];
  }
  s_tx_done = true;
}

// ---------- снаружи ----------
uint8_t* job_data() { return s_job_buf; }

bool take_job(Job& j, TickType_t wait) { return xQueueReceive(s_jobq, &j, wait) == pdTRUE; }

void job_done(const JobResult& r) {
  static Event ev;                    // зовёт только ядро 1
  ev.kind = EV_JOB;
  ev.res = r;
  // Не ждать места вечно: пока ядро 1 стоит здесь, оно не принимает новую настройку (take_pending).
  // Потерянный итог задача группы спишет сама (5 с без итога — s_job_lost).
  if (xQueueSend(s_evq, &ev, pdMS_TO_TICKS(200)) != pdTRUE) ++s_job_lost;
}

void announce_prime(const uint8_t* num, uint16_t nbytes) {
  static Event ev;                    // зовёт только ядро 1
  if (nbytes == 0 || nbytes > MSG_MAX) return;
  ev.kind = EV_PRIME;
  ev.len = nbytes;
  memcpy(ev.data, num, nbytes);
  if (xQueueSend(s_evq, &ev, 0) != pdTRUE) ++s_ann_drop;
}

int prod_len(uint32_t* gs) {          // зовёт производитель (ядро связи, своя задача)
  if (gs) *gs = s_prod_gs;
  return s_resting ? 0 : s_prod_len;  // отдых — шум кусочков не набирать
}

bool prod_put(const uint8_t* b, int len, uint32_t gs, volatile bool* abort) {
  static ProdSet it;                  // зовёт только производитель
  if (!s_prodq || len <= 0 || len > (int)MSG_MAX) return false;
  it.gs = gs;
  it.len = (uint16_t)len;
  memcpy(it.data, b, len);
  while (xQueueSend(s_prodq, &it, pdMS_TO_TICKS(20)) != pdTRUE) {   // запас полон: ждать, пока возьмут
    if ((abort && *abort) || s_prod_len != len || s_prod_gs != gs) return false;
  }
  return true;
}

void rest(uint16_t secs) {             // зовёт задача связи (команда сервера)
  static Event ev;
  ev.kind = EV_REST;
  ev.len = secs;
  xQueueSend(s_evq, &ev, pdMS_TO_TICKS(50));
}

void configure(const GenConfig& c) {
  static Event ev;                    // зовёт только задача связи
  ev.kind = EV_CFG;
  ev.cfg = c;
  xQueueSend(s_evq, &ev, portMAX_DELAY);
}

void snapshot(Stats& s) {
  portENTER_CRITICAL(&s_mux);
  s = s_pub;
  portEXIT_CRITICAL(&s_mux);
}

void set_alive_rate(uint8_t hz) { s_alive_hz = hz < 1 ? 1 : hz > 20 ? 20 : hz; }

// Шаг без радио (prime_lab.ino, настройка ro): пока Wi-Fi выключен, ни пульса, ни кадров. После включения
// Wi-Fi драйвер поднят заново, и ESP-NOW с ним сброшен — поднять и вернуть адресатов текущей настройки.
void radio_pause(bool off) {
  s_radio_off = off;
  if (off) {
    s_txq_n = 0;
    s_tx_busy = false;
    return;
  }
  const esp_err_t e = esp_now_init();
  if (e == ESP_ERR_ESPNOW_EXIST) return;             // Wi-Fi только останавливали: ESP-NOW и адресаты на месте
  if (e != ESP_OK) {
    Serial.printf("ESPNOW re-init FAIL %d\n", (int)e);
    return;
  }
  esp_now_register_recv_cb(on_recv);
  esp_now_register_send_cb(on_sent);
  ensure_peer(BCAST);
  for (int i = 0; i < s_ng && i < MAX_GROUP; ++i)
    if (s_mac_ok[i]) { ensure_peer(s_mac[i]); set_rate(s_mac[i]); }
  for (int i = 0; i < s_nbs && i < MAX_GROUP; ++i)
    if (s_bmac_ok[i]) { ensure_peer(s_bmacs[i]); set_rate(s_bmacs[i]); }
  Serial.println("ESPNOW re-init after radio off");
}

void set_alive_info(float temp, bool run) {     // зовёт задача связи
  s_alive_temp = (int8_t)(isnan(temp) ? 0 : temp < -100 ? -100 : temp > 120 ? 120 : temp);
  s_alive_run = run ? 1 : 0;
}

int neighbors(Neighbor* out, int max) {          // зовёт задача связи
  portENTER_CRITICAL(&s_mux);
  const int n = s_nb_n < max ? s_nb_n : max;
  for (int i = 0; i < n; ++i) out[i] = s_nb[i];
  portEXIT_CRITICAL(&s_mux);
  return n;
}

void begin() {
  s_evq = xQueueCreate(20, sizeof(Event));   // каскад: кусочки приходят пачкой от всех сразу
  s_jobq = xQueueCreate(1, sizeof(Job));
  s_prodq = xQueueCreate(2, sizeof(ProdSet));
  if (esp_now_init() == ESP_OK) {
    esp_now_register_recv_cb(on_recv);
    esp_now_register_send_cb(on_sent);
    ensure_peer(BCAST);
    uint32_t v = 0;
    esp_now_get_version(&v);
    s_pub.espnow_ver = s_stats.espnow_ver = v;
    Serial.printf("ESPNOW ok ver=%lu\n", (unsigned long)v);
  } else {
    Serial.println("ESPNOW init FAIL");
  }
  xTaskCreatePinnedToCore(task, "group", 6144, nullptr, 3, nullptr, 0);   // ядро связи
}

}  // namespace group
