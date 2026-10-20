// wifi_test — проверка радиотракта и подключения к точке доступа.
//
// 1. Сканирует эфир и печатает все сети: имя, уровень сигнала, канал, защита.
//    По этому списку видно, работает ли антенна и видна ли наша сеть.
// 2. Если в secrets/wifi.ini задан пароль — подключается и печатает адрес,
//    шлюз, сигнал, канал. При отказе печатает код причины от Wi-Fi-стека.
// 3. Затем заставка (MAC и номер) и цикл: раз в ~4 с строка состояния,
//    светодиод морзянкой «OK» при связи или «NO» без неё.

#include <Arduino.h>
#include <WiFi.h>
#include <agent_common.h>
#if __has_include("version.h")
#include "version.h"
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "wifi_test"
#endif

const uint32_t UNIT_MS = 150;   // морзянка «OK/NO» побыстрее

static const char* status_name(wl_status_t s) {
  switch (s) {
    case WL_IDLE_STATUS:     return "idle";
    case WL_NO_SSID_AVAIL:   return "no_ssid";
    case WL_SCAN_COMPLETED:  return "scan_done";
    case WL_CONNECTED:       return "connected";
    case WL_CONNECT_FAILED:  return "connect_failed";
    case WL_CONNECTION_LOST: return "connection_lost";
    case WL_DISCONNECTED:    return "disconnected";
    default:                 return "?";
  }
}

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);
  agent::identity_init();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d label=%s\n",
                FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id(), agent::device_label());

  WiFi.onEvent([](WiFiEvent_t e, WiFiEventInfo_t info) {
    if (e == ARDUINO_EVENT_WIFI_STA_DISCONNECTED)
      Serial.printf("EVT disconnected reason=%d\n", info.wifi_sta_disconnected.reason);
    else if (e == ARDUINO_EVENT_WIFI_STA_CONNECTED)
      Serial.printf("EVT associated\n");
    else if (e == ARDUINO_EVENT_WIFI_STA_GOT_IP)
      Serial.printf("EVT got_ip\n");
  });

  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true);
  delay(200);

  uint32_t t0 = millis();
  int n = WiFi.scanNetworks();
  Serial.printf("SCAN n=%d t=%lu ms\n", n, millis() - t0);
  for (int i = 0; i < n; ++i) {
    Serial.printf("NET ssid=\"%s\" rssi=%d ch=%d auth=%d bssid=%s\n",
                  WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
                  (int)WiFi.encryptionType(i), WiFi.BSSIDstr(i).c_str());
  }
  WiFi.scanDelete();

  const char* ssid = agent::wifi_ssid();
  const char* pass = agent::wifi_pass();
  if (strlen(pass) == 0) {
    Serial.printf("WIFI skip: password not set for \"%s\"\n", ssid);
  } else {
    Serial.printf("WIFI connect ssid=\"%s\"\n", ssid);
    // Ядро по умолчанию требует не ниже WPA2 и молча отвергает точки на WPA.
    // Наша сеть работает на WPA (скан: auth=2), поэтому порог снижен ровно до него.
    WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
    t0 = millis();
    WiFi.begin(ssid, pass);
    wl_status_t st;
    while ((st = WiFi.status()) != WL_CONNECTED && millis() - t0 < 20000) delay(100);
    if (st == WL_CONNECTED) {
      // По умолчанию радио спит между маяками роутера: входящие пакеты ждут
      // пробуждения (пинг 30–330 мс), а ESP-NOW-рассылки теряются.
      WiFi.setSleep(false);
      Serial.printf("WIFI ok t=%lu ms ip=%s gw=%s mask=%s rssi=%d ch=%d bssid=%s\n",
                    millis() - t0, WiFi.localIP().toString().c_str(),
                    WiFi.gatewayIP().toString().c_str(), WiFi.subnetMask().toString().c_str(),
                    WiFi.RSSI(), WiFi.channel(), WiFi.BSSIDstr().c_str());
    } else {
      Serial.printf("WIFI fail status=%d (%s) after %lu ms\n", st, status_name(st), millis() - t0);
    }
  }
  agent::boot_signature();
}

void loop() {
  bool ok = WiFi.status() == WL_CONNECTED;
  Serial.printf("STAT wifi=%d status=%s rssi=%d ip=%s up=%lu\n",
                ok, status_name(WiFi.status()), ok ? WiFi.RSSI() : 0,
                ok ? WiFi.localIP().toString().c_str() : "-", millis() / 1000);
  agent::morse_text(ok ? "OK" : "NO", UNIT_MS);
  agent::gap(14, UNIT_MS);
}
