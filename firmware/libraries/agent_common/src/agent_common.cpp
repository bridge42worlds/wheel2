#include "agent_common.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/temperature_sensor.h"

#if __has_include("devices_gen.h")
#include "devices_gen.h"   // создаётся tools/gen_devices.py из registry/devices.csv
#else
struct DeviceEntry { uint8_t mac[6]; uint16_t id; const char* label; };
static const DeviceEntry DEVICES[] = {{{0, 0, 0, 0, 0, 0}, 0, "none"}};
static const int DEVICES_COUNT = 0;
#endif

#if __has_include("secrets_gen.h")
#include "secrets_gen.h"   // создаётся tools/gen_secrets.py из secrets/wifi.ini, не в git
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif
#ifndef SERVER_HOST
#define SERVER_HOST ""
#endif
#ifndef OTA_KEY
#define OTA_KEY ""
#endif

namespace agent {

void serial_begin(uint32_t baud) {
  Serial.setTxBufferSize(4096);   // по умолчанию 256 байт: залп в десятки строк обрезается
  Serial.begin(baud);             // размер задаётся только ДО begin
  Serial.setTxTimeoutMs(50);      // не 0 и не 1 — см. agent_common.h
}

const char* wifi_ssid()   { return WIFI_SSID; }
const char* wifi_pass()   { return WIFI_PASS; }
const char* server_host() { return SERVER_HOST; }
const char* ota_key()     { return OTA_KEY; }

// ---------- идентификация ----------
static uint8_t     g_mac[6];
static char        g_mac_str[18];
static char        g_tail4[5];
static int         g_id    = 0;
static const char* g_label = "unregistered";

void identity_init() {
  esp_read_mac(g_mac, ESP_MAC_WIFI_STA);   // заводской MAC из фьюзов, Wi-Fi не нужен
  snprintf(g_mac_str, sizeof g_mac_str, "%02x:%02x:%02x:%02x:%02x:%02x",
           g_mac[0], g_mac[1], g_mac[2], g_mac[3], g_mac[4], g_mac[5]);
  snprintf(g_tail4, sizeof g_tail4, "%02x%02x", g_mac[4], g_mac[5]);
  for (int i = 0; i < DEVICES_COUNT; ++i) {
    if (memcmp(DEVICES[i].mac, g_mac, 6) == 0) {
      g_id    = DEVICES[i].id;
      g_label = DEVICES[i].label;
      break;
    }
  }
}

const char* mac_str()      { return g_mac_str; }
const uint8_t* mac_bytes() { return g_mac; }
const char* mac_tail4()    { return g_tail4; }
int         device_id()    { return g_id; }
const char* device_label() { return g_label; }

bool device_mac(int id, uint8_t mac[6]) {
  for (int i = 0; i < DEVICES_COUNT; ++i) {
    if (DEVICES[i].id == id) {
      memcpy(mac, DEVICES[i].mac, 6);
      return true;
    }
  }
  return false;
}

// ---------- светодиод ----------
static uint8_t g_pin = LED_BUILTIN, g_on = LOW, g_off = HIGH;

void led_init(uint8_t pin, uint8_t on_level) {
  g_pin = pin;
  g_on  = on_level;
  g_off = (on_level == HIGH) ? LOW : HIGH;
  pinMode(g_pin, OUTPUT);
  digitalWrite(g_pin, g_off);
}

void led_set(bool on) {
  digitalWrite(g_pin, on ? g_on : g_off);
  Serial.printf("T %lu %d\n", millis(), on ? 1 : 0);
}

void led_quiet(bool on) { digitalWrite(g_pin, on ? g_on : g_off); }

void burst(int n, uint32_t on_ms, uint32_t off_ms) {
  for (int i = 0; i < n; ++i) {
    led_set(true);
    delay(on_ms);
    led_set(false);
    if (i < n - 1) delay(off_ms);
  }
}

void gap(uint32_t units, uint32_t unit_ms) { delay(units * unit_ms); }

// ---------- Морзе ----------
static const char* morse_code(char c) {
  switch (toupper((unsigned char)c)) {
    case 'A': return ".-";    case 'B': return "-...";  case 'C': return "-.-.";
    case 'D': return "-..";   case 'E': return ".";     case 'F': return "..-.";
    case 'G': return "--.";   case 'H': return "....";  case 'I': return "..";
    case 'J': return ".---";  case 'K': return "-.-";   case 'L': return ".-..";
    case 'M': return "--";    case 'N': return "-.";    case 'O': return "---";
    case 'P': return ".--.";  case 'Q': return "--.-";  case 'R': return ".-.";
    case 'S': return "...";   case 'T': return "-";     case 'U': return "..-";
    case 'V': return "...-";  case 'W': return ".--";   case 'X': return "-..-";
    case 'Y': return "-.--";  case 'Z': return "--..";
    case '0': return "-----"; case '1': return ".----"; case '2': return "..---";
    case '3': return "...--"; case '4': return "....-"; case '5': return ".....";
    case '6': return "-...."; case '7': return "--..."; case '8': return "---..";
    case '9': return "----.";
    case '?': return "..--..";
    default:  return "";
  }
}

static void morse_char(char c, uint32_t unit_ms) {
  const char* code = morse_code(c);
  for (const char* e = code; *e; ++e) {
    led_set(true);
    delay((*e == '-' ? 3 : 1) * unit_ms);
    led_set(false);
    if (*(e + 1)) delay(unit_ms);           // пауза между вспышками внутри знака
  }
}

void morse_text(const char* s, uint32_t unit_ms) {
  for (const char* p = s; *p; ++p) {
    if (*p == ' ') { delay(4 * unit_ms); continue; }   // 3 + 4 = 7 единиц между словами
    morse_char(*p, unit_ms);
    if (*(p + 1)) delay(3 * unit_ms);       // пауза между знаками
  }
}

// ---------- температура кристалла ----------
float chip_temp() {
  static temperature_sensor_handle_t ts = nullptr;
  static bool failed = false;
  if (!ts && !failed) {
    temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(20, 100);
    if (temperature_sensor_install(&cfg, &ts) != ESP_OK || temperature_sensor_enable(ts) != ESP_OK) {
      failed = true;
      ts = nullptr;
    }
  }
  float c = NAN;
  if (ts) temperature_sensor_get_celsius(ts, &c);
  return c;
}

// ---------- ядра ----------
CoreLoad core_load() {
  static uint32_t prev_idle[2] = {0, 0}, prev_total = 0;
  UBaseType_t cap = uxTaskGetNumberOfTasks() + 4;
  TaskStatus_t* st = (TaskStatus_t*)malloc(sizeof(TaskStatus_t) * cap);
  CoreLoad r = {-1, -1};
  if (!st) return r;
  uint32_t total = 0;
  UBaseType_t n = uxTaskGetSystemState(st, cap, &total);
  uint32_t idle[2] = {0, 0};
  for (UBaseType_t i = 0; i < n; ++i) {
    if (strncmp(st[i].pcTaskName, "IDLE", 4) == 0 && st[i].xCoreID >= 0 && st[i].xCoreID < 2)
      idle[st[i].xCoreID] = st[i].ulRunTimeCounter;
  }
  free(st);
  uint32_t dt = total - prev_total;
  if (prev_total && dt) {
    r.busy0 = 100.0f * (1.0f - (float)(idle[0] - prev_idle[0]) / dt);
    r.busy1 = 100.0f * (1.0f - (float)(idle[1] - prev_idle[1]) / dt);
  }
  prev_total = total;
  prev_idle[0] = idle[0];
  prev_idle[1] = idle[1];
  return r;
}

void task_report() {
  static char buf[2048];
  vTaskList(buf);
  Serial.println("TASKS имя            сост прио стек  номер ядро");
  for (char* line = strtok(buf, "\n"); line; line = strtok(nullptr, "\n"))
    Serial.printf("TASK %s\n", line);
  vTaskGetRunTimeStats(buf);
  for (char* line = strtok(buf, "\n"); line; line = strtok(nullptr, "\n"))
    Serial.printf("RUNTIME %s\n", line);
  Serial.println("TASKS end");
}

// ---------- заставка ----------
void boot_signature(const Timing& t) {
  char id_text[8];
  if (g_id > 0) snprintf(id_text, sizeof id_text, "%d", g_id);
  else          strcpy(id_text, "?");       // не зарегистрирована — мигает вопросом

  Serial.printf("SIG begin mac=%s id=%s\n", g_tail4, id_text);
  burst(5, t.burst_on, t.burst_off);
  gap(7, t.unit_ms);
  morse_text(g_tail4, t.unit_ms);
  gap(7, t.unit_ms);
  burst(5, t.burst_on, t.burst_off);
  gap(7, t.unit_ms);
  morse_text(id_text, t.unit_ms);
  gap(7, t.unit_ms);
  burst(5, t.burst_on, t.slow_off);
  gap(7, t.unit_ms);
  Serial.printf("SIG end %lu\n", millis());
}

}  // namespace agent
