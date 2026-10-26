#include "config.h"

static bool valid_bits(long b) {
  return b == 32 || b == 64 || b == 128 || b == 256 || b == 512 || b == 1024 || b == 2048;
}

// "1,2,3" — номера по порядку
static bool parse_ids(const char* val, uint8_t* ids, int& n) {
  n = 0;
  for (const char* p = val; *p;) {
    char* e = nullptr;
    long id = strtol(p, &e, 10);
    if (e == p || id < 1 || id > 255 || n >= MAX_GROUP) return false;
    ids[n++] = (uint8_t)id;
    p = e;
    if (*p == ',') ++p;
    else if (*p) return false;
  }
  return true;
}

// "68ee8f51a018,68ee8f51ae20" — MAC по порядку
static bool parse_macs(const char* val, uint8_t (*macs)[6], int& n) {
  n = 0;
  for (const char* p = val; *p;) {
    if (n >= MAX_GROUP) return false;
    for (int k = 0; k < 6; ++k) {
      char h[3] = {p[0], p[0] ? p[1] : 0, 0};
      char* e = nullptr;
      long b = strtol(h, &e, 16);
      if (!h[0] || !h[1] || *e) return false;
      macs[n][k] = (uint8_t)b;
      p += 2;
    }
    ++n;
    if (*p == ',') ++p;
    else if (*p) return false;
  }
  return true;
}

bool same_work(const GenConfig& a, const GenConfig& b) {
  return a.src == b.src && a.raw == b.raw && a.bits == b.bits && a.nb == b.nb && a.all == b.all && a.td == b.td &&
         a.mode == b.mode && a.ng == b.ng && memcmp(a.grp, b.grp, sizeof a.grp) == 0 &&
         a.nrate == b.nrate && a.has_gmac == b.has_gmac && memcmp(a.gmac, b.gmac, sizeof a.gmac) == 0 &&
         a.kmax == b.kmax && a.pre == b.pre && a.cpu == b.cpu && a.dc == b.dc &&
         a.gv == b.gv && a.rk == b.rk && a.pw == b.pw &&
         a.role == b.role && a.sm == b.sm && a.sf == b.sf && a.cf == b.cf && a.nbg == b.nbg &&
         a.sh == b.sh && a.bgr == b.bgr &&
         memcmp(a.bg, b.bg, sizeof a.bg) == 0 && a.has_bmac == b.has_bmac &&
         memcmp(a.bmac, b.bmac, sizeof a.bmac) == 0;
}

bool soft_differs(const GenConfig& a, const GenConfig& b) {
  return a.tmax != b.tmax || a.txp != b.txp || a.ping != b.ping || a.hb != b.hb || a.led != b.led || a.tp != b.tp ||
         a.tl != b.tl || a.tq != b.tq || a.rmm != b.rmm || a.afs != b.afs || a.avg != b.avg || a.pl != b.pl ||
         a.dur != b.dur || a.ro != b.ro;
}

bool parse_config(const char* line, const GenConfig& cur, GenConfig& out, char* err, size_t errlen) {
  if (strncmp(line, "CFG ", 4) != 0) {
    snprintf(err, errlen, "not a CFG line");
    return false;
  }
  long cid = -1, src = cur.src, bits = cur.bits, nb = cur.nb, all = cur.all, td = cur.td;
  long tmax = cur.tmax, mode = cur.mode, nrate = cur.nrate, run = cur.run;
  long kmax = cur.kmax, pre = cur.pre, cpu = cur.cpu, txp = cur.txp, ping = cur.ping, led = cur.led, tp = cur.tp;
  long role = cur.role, sm = cur.sm, sf = cur.sf, cf = cur.cf, sh = cur.sh, bgr = cur.bgr;
  long tl = cur.tl, tq = cur.tq, rmm = cur.rmm, afs = cur.afs, avg = cur.avg, pl = cur.pl, dc = cur.dc;
  long gv = cur.gv, rk = cur.rk, pw = cur.pw, raw = cur.raw, hb = cur.hb, dur = cur.dur, ro = cur.ro;
  int nbg = cur.nbg;
  uint8_t bg[MAX_GROUP];
  memcpy(bg, cur.bg, sizeof bg);
  bool bad_bg = false;
  uint8_t bmac[MAX_GROUP][6];
  memcpy(bmac, cur.bmac, sizeof bmac);
  bool has_bmac = cur.has_bmac;
  int nbm = has_bmac ? cur.nbg : 0;
  bool bad_bm = false;
  int ng = cur.ng;
  uint8_t grp[MAX_GROUP];
  memcpy(grp, cur.grp, sizeof grp);
  bool bad_grp = false;
  uint8_t gmac[MAX_GROUP][6];
  memcpy(gmac, cur.gmac, sizeof gmac);
  bool has_gmac = cur.has_gmac;
  int ngm = has_gmac ? cur.ng : 0;
  bool bad_gm = false;
  static char buf[1280];             // два списка по 16 MAC; зовёт только задача связи
  strlcpy(buf, line + 4, sizeof buf);
  char* save = nullptr;
  for (char* tok = strtok_r(buf, " \r\n", &save); tok; tok = strtok_r(nullptr, " \r\n", &save)) {
    char* eq = strchr(tok, '=');
    if (!eq) continue;
    *eq = 0;
    const char* val = eq + 1;
    long v = strtol(val, nullptr, 10);
    if      (!strcmp(tok, "cid"))  cid = (long)strtoul(val, nullptr, 10);
    else if (!strcmp(tok, "src"))  src = v;
    else if (!strcmp(tok, "bits")) bits = v;
    else if (!strcmp(tok, "nb"))   nb = v;
    else if (!strcmp(tok, "all"))  all = v;
    else if (!strcmp(tok, "td"))   td = v;
    else if (!strcmp(tok, "tmax")) tmax = v;
    else if (!strcmp(tok, "mode")) mode = v;
    else if (!strcmp(tok, "nr"))   nrate = v;
    else if (!strcmp(tok, "run"))  run = v;
    else if (!strcmp(tok, "k"))    kmax = v;
    else if (!strcmp(tok, "pre"))  pre = v;
    else if (!strcmp(tok, "cpu"))  cpu = v;
    else if (!strcmp(tok, "txp"))  txp = v;
    else if (!strcmp(tok, "ping")) ping = v;
    else if (!strcmp(tok, "hb"))   hb = v;
    else if (!strcmp(tok, "dur"))  dur = v;
    else if (!strcmp(tok, "ro"))   ro = v;
    else if (!strcmp(tok, "led"))  led = v;
    else if (!strcmp(tok, "tp"))   tp = v;
    else if (!strcmp(tok, "role")) role = v;
    else if (!strcmp(tok, "sm"))   sm = v;
    else if (!strcmp(tok, "sf"))   sf = v;
    else if (!strcmp(tok, "cf"))   cf = v;
    else if (!strcmp(tok, "sh"))   sh = v;
    else if (!strcmp(tok, "bgr"))  bgr = v;
    else if (!strcmp(tok, "tl"))   tl = v;
    else if (!strcmp(tok, "tq"))   tq = v;
    else if (!strcmp(tok, "rmm"))  rmm = v;
    else if (!strcmp(tok, "afs"))  afs = v;
    else if (!strcmp(tok, "avg"))  avg = v;
    else if (!strcmp(tok, "pl"))   pl = v;
    else if (!strcmp(tok, "dc"))   dc = v;
    else if (!strcmp(tok, "gv"))   gv = v;
    else if (!strcmp(tok, "rk"))   rk = v;
    else if (!strcmp(tok, "pw"))   pw = v;
    else if (!strcmp(tok, "raw"))  raw = v;
    else if (!strcmp(tok, "gm")) { has_gmac = true; bad_gm |= !parse_macs(val, gmac, ngm); }
    else if (!strcmp(tok, "grp")) { memset(grp, 0, sizeof grp); bad_grp |= !parse_ids(val, grp, ng); }
    else if (!strcmp(tok, "bm"))  { has_bmac = true; bad_bm |= !parse_macs(val, bmac, nbm); }
    else if (!strcmp(tok, "bg"))  { memset(bg, 0, sizeof bg); bad_bg |= !parse_ids(val, bg, nbg); }
  }
  if (cid <= 0)                            snprintf(err, errlen, "cid missing");
  else if (src <= 0 || (src & ~SRC_MASK))  snprintf(err, errlen, "bad src=%ld", src);
  else if (!valid_bits(bits))              snprintf(err, errlen, "bad bits=%ld", bits);
  else if (nb < 1 || nb > MAX_BASES)       snprintf(err, errlen, "bad nb=%ld", nb);
  else if (td < 0 || td > 20000)           snprintf(err, errlen, "bad td=%ld", td);
  else if (tmax < 40 || tmax > 75)         snprintf(err, errlen, "bad tmax=%ld", tmax);
  else if (mode != MODE_SOLO && mode != MODE_GROUP) snprintf(err, errlen, "bad mode=%ld", mode);
  else if (bad_grp)                        snprintf(err, errlen, "bad grp");
  else if (nrate < 0 || nrate > 2)         snprintf(err, errlen, "bad nr=%ld", nrate);
  else if (bad_gm || (has_gmac && ngm != ng)) snprintf(err, errlen, "bad gm");
  else if (mode == MODE_GROUP && ng < 1)   snprintf(err, errlen, "grp missing");
  else if (kmax < 4 || kmax > 64)          snprintf(err, errlen, "bad k=%ld", kmax);
  else if (cpu != 80 && cpu != 160 && cpu != 240) snprintf(err, errlen, "bad cpu=%ld", cpu);
  else if (txp < 2 || txp > 20)            snprintf(err, errlen, "bad txp=%ld", txp);
  else if (ping != 0 && (ping < 10 || ping > 200)) snprintf(err, errlen, "bad ping=%ld", ping);   // 0 — без пинга
  else if (hb < 1 || hb > 20)              snprintf(err, errlen, "bad hb=%ld", hb);
  else if (dur < 0 || dur > 7200)          snprintf(err, errlen, "bad dur=%ld", dur);
  else if (ro && !dur)                     snprintf(err, errlen, "ro needs dur");   // без срока агент не вернулся бы в эфир
  else if (tp < 500 || tp > 5000)          snprintf(err, errlen, "bad tp=%ld", tp);
  else if (role < ROLE_A || role > ROLE_M) snprintf(err, errlen, "bad role=%ld", role);
  else if (afs != 2 && afs != 4 && afs != 8 && afs != 16) snprintf(err, errlen, "bad afs=%ld", afs);
  else if (avg != 1 && avg != 2 && avg != 4 && avg != 8) snprintf(err, errlen, "bad avg=%ld", avg);
  else if (sm < SM_SILENT || sm > SM_SHAM) snprintf(err, errlen, "bad sm=%ld", sm);
  else if (sf < SF_HASH || sf > SF_PERM)   snprintf(err, errlen, "bad sf=%ld", sf);
  else if (cf < 0 || cf > 3)               snprintf(err, errlen, "bad cf=%ld", cf);   // 3 — только пинг, без CSI (опыт)
  else if (bgr < 0 || bgr > 200)           snprintf(err, errlen, "bad bgr=%ld", bgr);
  else if (tl < TL_NORMAL || tl > TL_QUIET) snprintf(err, errlen, "bad tl=%ld", tl);
  else if (tq < 0 || tq > 65535)           snprintf(err, errlen, "bad tq=%ld", tq);
  else if (rmm < 50 || rmm > 2000)         snprintf(err, errlen, "bad rmm=%ld", rmm);
  else if (pl < 0 || pl > 1)               snprintf(err, errlen, "bad pl=%ld", pl);
  else if (dc < 0 || dc > 1)               snprintf(err, errlen, "bad dc=%ld", dc);
  else if (gv < 0 || gv > 2)               snprintf(err, errlen, "bad gv=%ld", gv);
  else if (rk < 1 || rk > 16)              snprintf(err, errlen, "bad rk=%ld", rk);
  else if (pw < 0 || pw > 5000)            snprintf(err, errlen, "bad pw=%ld", pw);
  else if (raw < 0 || raw > 2)             snprintf(err, errlen, "bad raw=%ld", raw);
  else if (bad_bg)                         snprintf(err, errlen, "bad bg");
  else if (bad_bm || (has_bmac && nbm != nbg)) snprintf(err, errlen, "bad bm");
  else {
    out.cid  = (uint32_t)cid;
    out.src  = (uint8_t)src;
    out.bits = (uint16_t)bits;
    out.nb   = (uint8_t)nb;
    out.all  = all != 0;
    out.td   = (uint16_t)td;
    out.tmax = (uint8_t)tmax;
    out.mode = (uint8_t)mode;
    out.ng   = (uint8_t)ng;
    out.nrate = (uint8_t)nrate;
    out.run = run ? 1 : 0;
    out.kmax = (uint8_t)kmax;
    out.pre = pre ? 1 : 0;
    out.cpu = (uint16_t)cpu;
    out.txp = (uint8_t)txp;
    out.ping = (uint8_t)ping;
    out.hb = (uint8_t)hb;
    out.dur = (uint16_t)dur;
    out.ro = ro ? 1 : 0;
    out.led = led ? 1 : 0;
    out.tp = (uint16_t)tp;
    out.role = (uint8_t)role;
    out.sm = (uint8_t)sm;
    out.sf = (uint8_t)sf;
    out.cf = (uint8_t)cf;
    out.sh = sh ? 1 : 0;
    out.bgr = (uint8_t)bgr;
    out.tl = (uint8_t)tl;
    out.tq = (uint16_t)tq;
    out.rmm = (uint16_t)rmm;
    out.afs = (uint8_t)afs;
    out.avg = (uint8_t)avg;
    out.pl = (uint8_t)pl;
    out.dc = (uint8_t)dc;
    out.gv = (uint8_t)gv;
    out.rk = (uint8_t)rk;
    out.pw = (uint16_t)pw;
    out.raw = (uint8_t)raw;
    out.nbg = (uint8_t)nbg;
    memcpy(out.bg, bg, sizeof bg);
    memcpy(out.bmac, bmac, sizeof bmac);
    out.has_bmac = has_bmac;
    memcpy(out.gmac, gmac, sizeof gmac);
    out.has_gmac = has_gmac;
    memcpy(out.grp, grp, sizeof grp);
    err[0] = 0;
    return true;
  }
  return false;
}
