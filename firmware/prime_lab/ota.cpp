#include "ota.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <Update.h>
#include <Preferences.h>
#include <esp_ota_ops.h>
#include "esp_timer.h"
#include "mbedtls/md.h"
#include <agent_common.h>

// Крючок ядра Arduino: новая прошивка не подтверждается при старте, а позже — когда
// агент минуту на связи с сервером (ota::tick); иначе загрузчик вернёт прежнюю.
extern "C" bool verifyRollbackLater() { return true; }

namespace ota {

static volatile int      s_state = 0;
static char              s_err[64] = "";
static char              s_url[160] = "", s_md5[33] = "";
static uint32_t          s_len = 0, s_t = 0;
static volatile uint32_t s_got = 0;
static TaskHandle_t      s_task = nullptr;
static uint32_t          s_healthy_s = 0;
static bool              s_done = false;        // подтверждение или откат уже решены

static void fail(const char* fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(s_err, sizeof s_err, fmt, ap);
  va_end(ap);
  s_state = -1;
  Serial.printf("OTA fail: %s\n", s_err);
}

static uint32_t nvs_last_t() {
  Preferences p;
  uint32_t t = 0;
  if (p.begin("ota", true)) {
    t = p.getULong("t", 0);
    p.end();
  }
  return t;
}

static void nvs_set_t(uint32_t t) {
  Preferences p;
  if (p.begin("ota", false)) {
    p.putULong("t", t);
    p.end();
  }
}

// Подпись команды: HMAC-SHA256(ключ, "url|md5|len|ver|t") в hex — должна совпасть с sig.
static bool signature_ok(const char* url, const char* md5, uint32_t len, const char* ver, uint32_t t, const char* sig) {
  const char* keyhex = agent::ota_key();
  const size_t kl = strlen(keyhex);
  if (kl != 64 || !sig || strlen(sig) != 64) return false;
  uint8_t key[32];
  for (size_t i = 0; i < 32; ++i) {
    char h[3] = {keyhex[2 * i], keyhex[2 * i + 1], 0};
    key[i] = (uint8_t)strtoul(h, nullptr, 16);
  }
  char msg[300];
  snprintf(msg, sizeof msg, "%s|%s|%lu|%s|%lu", url, md5, (unsigned long)len, ver, (unsigned long)t);
  uint8_t mac[32];
  if (mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), key, sizeof key,
                      (const uint8_t*)msg, strlen(msg), mac) != 0)
    return false;
  char hex[65];
  for (int i = 0; i < 32; ++i) snprintf(hex + 2 * i, 3, "%02x", mac[i]);
  int diff = 0;                                   // сравнение за постоянное время
  for (int i = 0; i < 64; ++i) diff |= hex[i] ^ (char)tolower(sig[i]);
  return diff == 0;
}

static void task(void*) {
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(15000);
  bool ok = false;
  if (!http.begin(s_url)) {
    fail("bad url");
  } else {
    const int code = http.GET();
    if (code != HTTP_CODE_OK) {
      fail("http %d", code);
    } else {
      const int len = http.getSize();
      if (len <= 0 || (uint32_t)len != s_len) {
        fail("size %d (want %lu)", len, (unsigned long)s_len);
      } else if (!Update.begin(len, U_FLASH)) {
        fail("begin: %s", Update.errorString());
      } else {
        Update.setMD5(s_md5);
        NetworkClient* c = http.getStreamPtr();
        static uint8_t buf[2048];
        uint32_t got = 0;
        int64_t t_last = esp_timer_get_time();
        while (got < (uint32_t)len) {        // читаем сами: обрыв связи не вешает задачу
          const int avail = c->available();
          if (avail <= 0) {
            if (!c->connected() || esp_timer_get_time() - t_last > 10000000) break;
            vTaskDelay(1);
            continue;
          }
          const int n = c->read(buf, min((int)sizeof buf, avail));
          if (n <= 0) continue;
          if (Update.write(buf, n) != (size_t)n) break;
          got += n;
          s_got = got;
          t_last = esp_timer_get_time();
        }
        if (got != (uint32_t)len) fail("short %lu/%d: %s", (unsigned long)got, len, Update.errorString());
        else if (!Update.end()) fail("end: %s", Update.errorString());
        else ok = true;
      }
    }
    http.end();
  }
  if (ok) {
    nvs_set_t(s_t);                      // эту команду (и более старые) больше не принимать
    s_state = 2;
    Serial.printf("OTA ok: %lu bytes written, restart\n", (unsigned long)s_got);
    vTaskDelay(pdMS_TO_TICKS(500));
    ESP.restart();
  }
  s_task = nullptr;
  vTaskDelete(nullptr);
}

const char* start(const char* url, const char* md5, uint32_t len, const char* ver, uint32_t t, const char* sig) {
  if (s_task) return "busy";
  if (WiFi.status() != WL_CONNECTED) return "no wifi";
  if (pending_verify()) return "unconfirmed";       // прошлый откат ещё возможен — поверх не пишем
  if (!url || strncmp(url, "http://", 7) != 0 || strlen(url) >= sizeof s_url) return "bad url";
  if (ESP.getFreeHeap() < 40000) return "low heap";   // загрузка не начнётся (07.10.2026: №3 при 54 КБ дошла до 2,5 КБ)
  if (!md5 || strlen(md5) != 32) return "bad md5";
  if (len < 100000 || len > 0x330000) return "bad len";
  if (!signature_ok(url, md5, len, ver ? ver : "", t, sig)) return "bad sig";
  if (t <= nvs_last_t()) return "stale";            // команда не новее прошлой принятой
  strlcpy(s_url, url, sizeof s_url);
  strlcpy(s_md5, md5, sizeof s_md5);
  for (char* p = s_md5; *p; ++p) *p = (char)tolower(*p);
  s_len = len;
  s_t = t;
  s_got = 0;
  s_err[0] = 0;
  s_state = 1;
  if (xTaskCreatePinnedToCore(task, "ota", 12288, nullptr, 1, &s_task, 0) != pdPASS) {
    s_task = nullptr;
    fail("no task");
    return "no task";
  }
  return nullptr;
}

int         state() { return s_state; }
const char* err() { return s_err; }
uint32_t    progress() { return s_got; }

bool pending_verify() {
  const esp_partition_t* run = esp_ota_get_running_partition();
  esp_ota_img_states_t st;
  return run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY;
}

const char* slot() {
  const esp_partition_t* run = esp_ota_get_running_partition();
  return run ? run->label : "?";
}

void tick(bool healthy, uint32_t uptime_ms) {
  if (s_done) return;
  if (!pending_verify()) {
    s_done = true;
    return;
  }
  s_healthy_s = healthy ? s_healthy_s + 1 : 0;
  if (s_healthy_s >= CONFIRM_S) {
    esp_ota_mark_app_valid_cancel_rollback();
    s_done = true;
    Serial.printf("OTA confirmed: %s is now the valid app\n", slot());
  } else if (uptime_ms > ROLLBACK_S * 1000) {
    Serial.println("OTA: no server contact, rolling back");
    delay(100);
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
}

}  // namespace ota
