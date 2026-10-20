// morse_hello — заставка при старте, затем «HELLO WORLD» азбукой Морзе без конца.
//
// Заставка (из agent_common): 5 быстрых вспышек, последние 4 знака MAC,
// 5 быстрых, номер устройства из реестра, 5 вспышек с увеличенным интервалом.
//
// Морзе: единица 250 мс; точка 1, тире 3, пауза в знаке 1, между знаками 3,
// между словами 7. Между повторами сообщения — тоже 7, чтобы поток выглядел
// сплошным: HELLO WORLD HELLO WORLD ...
//
// Каждое переключение светодиода печатается в Serial: "T <millis> <0|1>".
// Светодиод XIAO ESP32-S3 на GPIO21 горит при LOW (проверено глазами 03.10.2026).

#include <Arduino.h>
#include <agent_common.h>
#if __has_include("version.h")
#include "version.h"   // создаётся tools/build.sh из номера коммита git
#else
#define FW_VERSION "unversioned"
#define FW_SKETCH  "morse_hello"
#endif

const uint32_t UNIT_MS          = 250;
const char*    MESSAGE          = "HELLO WORLD";
const uint32_t REPEAT_GAP_UNITS = 7;

void setup() {
  agent::led_init(LED_BUILTIN, LOW);
  agent::serial_begin();
  delay(3000);                // время порту подняться после сброса, чтобы заставка попала в лог
  agent::identity_init();
  Serial.printf("BOOT %s ver=%s mac=%s id=%d label=%s rep=%lu unit=%lu msg=\"%s\"\n",
                FW_SKETCH, FW_VERSION, agent::mac_str(), agent::device_id(),
                agent::device_label(), REPEAT_GAP_UNITS, UNIT_MS, MESSAGE);
  agent::boot_signature();
}

void loop() {
  Serial.printf("START %lu\n", millis());
  agent::morse_text(MESSAGE, UNIT_MS);
  Serial.printf("END %lu\n", millis());
  agent::gap(REPEAT_GAP_UNITS, UNIT_MS);
}
