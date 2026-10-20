// rng_bench — влияет ли датчик температуры кристалла на скорость генератора случайных чисел.
//
// Повод: в telemetry_demo после первого вызова temperatureRead() темп генерации
// упал с 2.88 млн до 42 тыс. чисел в секунду и не восстановился. temperatureRead()
// в ядре Arduino включает датчик при первом вызове и не выключает никогда.
//
// Меряются раздельно две скорости за 1 секунду:
//   rng — вызовов esp_random() (аппаратный генератор),
//   cpu — проверок на простоту чисел из простой арифметической последовательности
//         (без генератора: чистая скорость процессора).
// Пять состояний датчика: не устанавливался → включён → выключен → снова включён →
// выключен и удалён. Wi-Fi подключён и не спит — как в рабочей прошивке.

#include <Arduino.h>
#include <WiFi.h>
#include <agent_common.h>
#include "driver/temperature_sensor.h"
#if __has_include("version.h")
#include "version.h"
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "rng_bench"
#endif

static inline uint32_t mulmod(uint32_t a, uint32_t b, uint32_t m) { return (uint64_t)a * b % m; }
static uint32_t powmod(uint32_t a, uint32_t e, uint32_t m) {
  uint32_t r = 1; a %= m;
  while (e) { if (e & 1) r = mulmod(r, a, m); a = mulmod(a, a, m); e >>= 1; }
  return r;
}
static bool mr_round(uint32_t n, uint32_t a) {
  uint32_t d = n - 1; int s = 0;
  while (!(d & 1)) { d >>= 1; ++s; }
  uint32_t x = powmod(a, d, n);
  if (x == 1 || x == n - 1) return true;
  for (int i = 1; i < s; ++i) { x = mulmod(x, x, n); if (x == n - 1) return true; }
  return false;
}
static bool is_prime32(uint32_t n) {
  static const uint8_t small[] = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61};
  if (n < 2) return false;
  for (uint8_t p : small) if (n % p == 0) return n == p;
  return mr_round(n, 2) && mr_round(n, 7) && mr_round(n, 61);
}

static uint32_t rng_rate() {          // вызовов esp_random() за 1 с
  volatile uint32_t sink = 0;
  uint32_t n = 0, t0 = millis();
  while (millis() - t0 < 1000) {
    for (int i = 0; i < 4096; ++i) sink ^= esp_random();
    n += 4096;
  }
  return n;
}

static volatile uint32_t g_sink;      // результат наружу, чтобы компилятор не выбросил счёт

static uint32_t cpu_rate() {          // проверок на простоту за 1 с, без генератора
  uint32_t n = 0, x = 0x9E3779B9, primes = 0, t0 = millis();
  while (millis() - t0 < 1000) {
    for (int i = 0; i < 4096; ++i) { x = x * 1664525u + 1013904223u; primes += is_prime32(x); }
    n += 4096;
  }
  g_sink = primes;
  return n;
}

static temperature_sensor_handle_t ts = nullptr;

static void phase(const char* name) {
  float c = NAN;
  if (ts) temperature_sensor_get_celsius(ts, &c);
  uint32_t r = rng_rate();
  uint32_t p = cpu_rate();
  Serial.printf("PHASE %-12s rng=%8lu/s cpu=%8lu/s temp=%.1f\n", name, (unsigned long)r, (unsigned long)p, c);
}

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);
  agent::identity_init();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d\n", FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id());

  phase("wifi_off");                                     // радио ещё не включалось
  WiFi.mode(WIFI_STA);
  WiFi.setMinSecurity(WIFI_AUTH_WPA_PSK);
  WiFi.begin(agent::wifi_ssid(), agent::wifi_pass());
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(100);
  WiFi.setSleep(false);
  Serial.printf("WIFI %s\n", WiFi.status() == WL_CONNECTED ? "ok" : "fail");
  delay(1000);

  phase("never");                                        // датчик не устанавливался
  temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(20, 100);
  temperature_sensor_install(&cfg, &ts);
  temperature_sensor_enable(ts);
  phase("enabled");
  temperature_sensor_disable(ts);
  phase("disabled");
  temperature_sensor_enable(ts);
  phase("enabled2");
  temperature_sensor_disable(ts);
  temperature_sensor_uninstall(ts);
  ts = nullptr;
  phase("uninstalled");
  Serial.println("DONE");
}

void loop() { delay(1000); }
