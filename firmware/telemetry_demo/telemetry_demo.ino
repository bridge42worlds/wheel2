// telemetry_demo — агент генерирует данные и раз в секунду шлёт телеметрию на сервер.
//
// Ядро 1 (счёт): бесконечно берёт 32-битные числа из аппаратного генератора
// случайных чисел и проверяет на простоту (детерминированный Миллер–Рабин
// по базам 2, 7, 61 — точен для всех 32-битных чисел).
// Ядро 0 (связь): Wi-Fi, поиск сервера по маяку "W2SRV <порт>" на UDP 5006,
// раз в секунду пакет JSON на сервер, переподключение при обрыве.
//
// Метрики в пакете: chk — проверено чисел за секунду, pr — найдено простых,
// rssi — сигнал, temp — температура кристалла, heap — свободная память.
// Светодиод: заставка при старте, затем короткая вспышка на каждый отправленный пакет.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <agent_common.h>
#if __has_include("version.h")
#include "version.h"
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "telemetry_demo"
#endif

const uint16_t BEACON_PORT = 5006;

static volatile uint32_t g_checked = 0, g_primes = 0;
static volatile bool     g_sig_done = false;
static IPAddress         g_srv_ip;
static uint16_t          g_srv_port = 0;

// ---------- генерация: ядро 1 ----------
static inline uint32_t mulmod(uint32_t a, uint32_t b, uint32_t m) { return (uint64_t)a * b % m; }

static uint32_t powmod(uint32_t a, uint32_t e, uint32_t m) {
  uint32_t r = 1;
  a %= m;
  while (e) {
    if (e & 1) r = mulmod(r, a, m);
    a = mulmod(a, a, m);
    e >>= 1;
  }
  return r;
}

static bool mr_round(uint32_t n, uint32_t a) {
  uint32_t d = n - 1;
  int s = 0;
  while (!(d & 1)) { d >>= 1; ++s; }
  uint32_t x = powmod(a, d, n);
  if (x == 1 || x == n - 1) return true;
  for (int i = 1; i < s; ++i) {
    x = mulmod(x, x, n);
    if (x == n - 1) return true;
  }
  return false;
}

static bool is_prime32(uint32_t n) {
  static const uint8_t small[] = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61};
  if (n < 2) return false;
  for (uint8_t p : small) if (n % p == 0) return n == p;
  return mr_round(n, 2) && mr_round(n, 7) && mr_round(n, 61);
}

static void gen_task(void*) {
  for (;;) {
    for (int i = 0; i < 16384; ++i) {
      uint32_t n = esp_random();
      ++g_checked;
      if (is_prime32(n)) ++g_primes;
    }
    vTaskDelay(1);   // дать поработать служебной задаче ядра — иначе сработает сторожевой таймер
  }
}

// ---------- заставка: отдельная задача, чтобы не задерживать связь ----------
static void sig_task(void*) {
  agent::boot_signature();
  g_sig_done = true;
  vTaskDelete(nullptr);
}

// ---------- связь: ядро 0 ----------
static void comms_task(void*) {
  WiFiUDP udp, beacon;
  beacon.begin(BEACON_PORT);
  uint32_t next = millis() + 1000, seq = 0, last_chk = 0, last_pr = 0, last_try = 0;
  char buf[400];

  for (;;) {
    int n = beacon.parsePacket();
    if (n > 0) {
      char b[32] = {0};
      beacon.read(b, sizeof b - 1);
      if (strncmp(b, "W2SRV ", 6) == 0) {
        IPAddress ip = beacon.remoteIP();
        uint16_t port = atoi(b + 6);
        if (ip != g_srv_ip || port != g_srv_port)
          Serial.printf("SRV found %s:%u (beacon)\n", ip.toString().c_str(), port);
        g_srv_ip = ip;
        g_srv_port = port;
      }
    }

    uint32_t now = millis();
    if ((int32_t)(now - next) >= 0) {
      next += 1000;
      if ((int32_t)(now - next) > 2000) next = now + 1000;   // после долгой паузы не догонять
      uint32_t c = g_checked, p = g_primes;
      uint32_t dc = c - last_chk, dp = p - last_pr;
      last_chk = c;
      last_pr = p;
      agent::CoreLoad load = agent::core_load();
      static bool reported = false;
      if (!reported && now > 20000) {          // один раз: какие задачи на каком ядре
        agent::task_report();
        reported = true;
      }
      bool up = WiFi.status() == WL_CONNECTED;
      if (up && g_srv_port) {
        int len = snprintf(buf, sizeof buf,
          "{\"v\":1,\"type\":\"tel\",\"mac\":\"%s\",\"id\":%d,\"fw\":\"%s\",\"sk\":\"%s\","
          "\"seq\":%lu,\"up\":%lu,\"rssi\":%d,\"temp\":%.1f,\"heap\":%lu,\"chk\":%lu,\"pr\":%lu,"
          "\"cpu0\":%.1f,\"cpu1\":%.1f}",
          agent::mac_str(), agent::device_id(), FW_VERSION, FW_SKETCH,
          (unsigned long)seq, (unsigned long)(now / 1000), WiFi.RSSI(), agent::chip_temp(),
          (unsigned long)ESP.getFreeHeap(), (unsigned long)dc, (unsigned long)dp,
          load.busy0, load.busy1);
        udp.beginPacket(g_srv_ip, g_srv_port);
        udp.write((const uint8_t*)buf, len);
        udp.endPacket();
        ++seq;
        if (g_sig_done) { agent::led_quiet(true); vTaskDelay(20); agent::led_quiet(false); }
      }
      if (seq % 10 == 1 || !up || !g_srv_port)
        Serial.printf("TEL seq=%lu wifi=%d srv=%s chk=%lu pr=%lu\n", (unsigned long)seq, up,
                      g_srv_port ? g_srv_ip.toString().c_str() : "-", (unsigned long)dc, (unsigned long)dp);
    }

    if (WiFi.status() != WL_CONNECTED && millis() - last_try > 5000) {
      last_try = millis();
      Serial.println("WIFI reconnect");
      WiFi.reconnect();
    }
    vTaskDelay(10);
  }
}

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);
  agent::identity_init();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d label=%s\n",
                FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id(), agent::device_label());

  xTaskCreatePinnedToCore(sig_task, "sig", 4096, nullptr, 1, nullptr, 0);

  WiFi.mode(WIFI_STA);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);   // наша сеть работает на WPA
  WiFi.begin(agent::wifi_ssid(), agent::wifi_pass());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(100);
  WiFi.setSleep(false);                     // спящее радио задерживает пакеты и теряет ESP-NOW
  Serial.printf("WIFI %s ip=%s rssi=%d ch=%d t=%lu ms\n",
                WiFi.status() == WL_CONNECTED ? "ok" : "fail",
                WiFi.localIP().toString().c_str(), WiFi.RSSI(), WiFi.channel(), millis() - t0);

  if (strlen(agent::server_host())) {
    g_srv_ip.fromString(agent::server_host());
    g_srv_port = 5005;
    Serial.printf("SRV fixed %s:%u\n", g_srv_ip.toString().c_str(), g_srv_port);
  }

  xTaskCreatePinnedToCore(gen_task, "gen", 4096, nullptr, 1, nullptr, 1);     // ядро счёта
  xTaskCreatePinnedToCore(comms_task, "comms", 8192, nullptr, 2, nullptr, 0); // ядро связи
}

void loop() { vTaskDelete(nullptr); }
