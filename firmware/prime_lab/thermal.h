// Предел температуры кристалла. Температуру снаружи агента мы не знаем, поэтому
// следим за своей: ядро счёта работает только долю времени work, остальное
// время спит (простой ядра почти не греет). Долю раз в секунду подбирает
// регулятор на ядре связи так, чтобы кристалл держался на градус ниже предела.
#pragma once
#include <Arduino.h>

namespace thermal {

enum State { NORMAL = 0, LIMITING = 1, PAUSED = 2, RADIO_SLEEP = 3 };   // 3 — доля минимальна и радио спит

void  set_limit(float tmax_c);   // предел, °C
float limit();
void  update(float temp_c);      // ядро 0, раз в секунду: новая доля работы
float work();                    // доля работы ядра счёта, 0..1
int   state();                   // NORMAL, LIMITING (доля < 1), RADIO_SLEEP, PAUSED (авария)

// Ядро 1: после куска работы длительностью busy_us — поспать столько, чтобы
// работа занимала долю work. В аварийной паузе ждёт, пока кристалл остынет
// (возвращается раньше, если abort стал true).
void  pace(uint32_t busy_us, volatile bool* abort);

}  // namespace thermal
