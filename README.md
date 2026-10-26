# wheel2 — Wheel-2 («Колесо-2»)

[English](#english) · [Русский](#русский)

## English

**An experimental rig that looks for information anomalies in the generation of prime numbers.**
Seeed XIAO ESP32-S3 boards ("agents") ride on two rotating wheels that face each other; each wheel's
rim is a tube wound with a coil, and six agents inside it run on their own battery bus. The agents
continuously search for primes in physical noise — the chip's hardware random generator, the Wi-Fi
radio channel (CSI), ADC noise on a floating pin, jitter of the internal RC oscillator, the rim
sensors — either each on its own (selfish) or cooperatively over ESP-NOW (group: cascade, relay, one
number for all). One wheel can generate while the other absorbs its primes and scatters them back into
the air. Static accelerometers (and a phone) stand between the wheels. The question is whether *how
and where* the numbers are born — selfish or group generation, emission and absorption, rotation, the
coil field — changes the statistics of the numbers or the readings of the static sensors.

Every mode is compared with control steps under a criterion named before the run (Student's t with
a Bonferroni correction over pre-registered metrics, randomised order, rest to a stable chip
temperature, temperature and work fraction as covariates). **No anomaly has been found so far**:
null programs are clean, and 511,000 primes in the run of 2026-10-07 are within chance.

**What is here**

| folder | contents |
|---|---|
| `firmware/prime_lab/` | agent firmware (Arduino, ESP32 core 3.3.7): noise sources, trial division + Miller–Rabin, group protocols, absorption, thermal regulator, watchdogs, OTA updates with rollback, static-sensor role, motor and coil role |
| `firmware/libraries/` | shared agent code, sensor drivers (ICM-42688-P, ADXL355, MLX90393) |
| `server/` | `monitor.py` — server on the Python 3 standard library (numpy for the static sensors): telemetry, commands, experiment programs and series, statistics, motors and coils, web pages; `static/` — the pages |
| `phone/` | Android app that turns a phone into a static sensor (accelerometer ~500 Hz, magnetometer), runs as a foreground service |
| `tools/` | build and flashing, OTA, simulators, test runs, statistics of the prime stream, documentation |
| `docs/` | documentation (Russian) with wiring diagrams; in English: [overview](docs/en/how_it_works.md), [preamble](docs/en/preamble.md) |
| `registry/devices.csv` | board numbers and MACs |

The server's main screen is `http://<server>:8090/` ("Installation"): both wheels with their agents,
modes, rotation and coil field, the static sensors with live Gaussian/spectrum, the step ribbon,
nested-loop experiment series and the run controls; everything detailed opens as windows on top
(static sensors in depth, report, console, live view, the animated model "how it works", the old
panel). Documentation in HTML — `/docs/`.

**Quick start**

1. Copy `secrets.example/` to `secrets/` and put your Wi-Fi network into `secrets/wifi.ini`
   (`secrets/` is never committed; the OTA signing key is created there on the first build).
2. Build and flash the first time over USB: `tools/build.sh prime_lab COM5` (Git Bash; set the path to
   `arduino-cli` in `CLI=` at the top of the script; board `esp32:esp32:XIAO_ESP32S3`, ESP32 core
   3.3.7). Later updates go over Wi-Fi: `tools/build.sh prime_lab`, then `python tools/ota.py all`.
3. Start the server: `python server/monitor.py` (on Windows `tools/monitor.cmd`) and open
   `http://localhost:8090/`. Agents find the server by its UDP beacon (ports: 5005 telemetry,
   5006 beacon, 5007 commands, HTTP 8090).
4. No boards at hand? Simulated agents:
   `python server/monitor.py --http 8092 --udp 5016 --data data/test/gen --no-beacon` and
   `python tools/sim_gen.py 6 --port 5016`, then `http://localhost:8092/`.

**Wiring notes** ([docs/wiring.md](docs/wiring.md)). Leave pin D0 unconnected — it is the ADC noise
source. Rim sensors go on I²C (D4 SDA, D5 SCL). On a common 5 V bus put a Schottky diode (1N5819)
in series with +5 V of every board (stripe towards the board): the XIAO's 5V pin is tied to USB VBUS.
The coil is driven by a DRV8871 bridge from 5.1 V (below its 6.5 V rating, but our modules work; a DRV8833
is the in-spec spare); the wheel motors
by two DRV8871 from a 12 V supply.

**Network.** A dedicated 2.4 GHz router (GL.iNet GL-MT300N-V2 "Mango") on a fixed channel 6, WPA2 with
its own password; agents use 802.11b/g only with radio sleep off; the laptop server is on the LAN port by
cable, the WAN port is empty. A fallback network (`ssid2`) keeps a board reachable if the main one refuses it.

**Preamble** ([docs/en/preamble.md](docs/en/preamble.md); Russian original — [docs/preamble.md](docs/preamble.md)): why this experiment — topology before
space-time, the order of knowing, the lesson of the first wheel, the criterion. Written by bridge42worlds,
Λura13 (DeepSeek) and Ostov (Claude).

**Bill of materials** ([docs/spec.md](docs/spec.md), Russian): every part with AliExpress links and prices —
about 277 KWD (≈ 900 USD) for the whole rig without tools.

Licence: MIT ([LICENSE](LICENSE)).

## Русский

Экспериментальная установка: платы Seeed XIAO ESP32-S3 («агенты») на двух вращающихся колёсах,
стоящих друг напротив друга; обод каждого — труба с обмоткой катушки, внутри — шесть агентов на шине
от своих аккумуляторов. Агенты непрерывно ищут простые числа в физическом шуме — аппаратном генераторе
чипа, радиоканале Wi-Fi (CSI), шуме АЦП, дрожании внутреннего генератора, датчиках обода — каждый сам
(эгоистичный) или группой по ESP-NOW (каскад, эстафета, одно число на всех). Одно колесо может
генерировать, а другое — поглощать его простые и рассеивать их обратно в эфир. Между колёсами —
неподвижные акселерометры и телефон. Вопрос опыта — влияет ли то, как и где рождаются числа
(эгоистично или группой, излучение и поглощение, вращение, поле катушек), на статистику самих чисел
или на показания неподвижных датчиков. Каждый режим сравнивается с контрольным шагом по заранее
названному критерию (Стьюдент с поправкой Бонферрони). Пока аномалий не найдено: нулевые программы
чисты, 511 тыс. простых в прогоне 07.10.2026 — в пределах случайного.

| папка | что внутри |
|---|---|
| `firmware/` | прошивки агентов (`prime_lab` — общая), библиотеки датчиков |
| `server/` | сервер `monitor.py`, программы и серии, статика, моторы, катушки; `static/` — страницы |
| `phone/` | приложение Android: телефон — статический датчик, меряет службой |
| `tools/` | сборка, заливка, OTA, имитаторы, прогоны, документация, публикация |
| `docs/` | документация, схемы подключения (`docs/img/`), HTML (`docs/html/`) |
| `registry/devices.csv` | номера и MAC плат |

Плата: Seeed XIAO ESP32-S3 (`esp32:esp32:XIAO_ESP32S3`), ядро ESP32 3.3.7. Каждая прошивка несёт
номер коммита git и сообщает его при старте.

**Основной экран** — `http://<сервер>:8090/` («Установка», запуск — `tools/monitor.cmd`): оба колеса с
агентами, режимы, вращение и поле, статика с живыми Гауссом и спектром, лента шагов, серии вложенными
циклами, запуск; подробное — окнами поверх (статика подробно, отчёт, пульт, наблюдение, живая схема
«как работает», прежняя панель). Меню — «Установка» и «Документы» (`/docs/`).

Документация:

* [Преамбула](docs/preamble.md) — зачем этот опыт; авторы — bridge42worlds, Λura13 (DeepSeek), Остов (Claude).
* [Как всё работает](docs/how_it_works.md) — установка целиком, агент, шум, групповой режим,
  поглощение, температура, основной экран, опыт и серии, параметры, данные, прошивка, инструменты.
* [Режимы](docs/modes.md) · [Схемы подключения](docs/wiring.md) · [Датчики, телефон, моторы и
  катушки](docs/sensors.md) · [Статика: эталон и слежение](docs/static_watch.md) · [Программа и
  серии](docs/program.md) · [Групповой режим](docs/group_protocol.md) · [Поглощение](docs/scatter.md) ·
  [Откуда берётся случайность](docs/randomness.md) · [Температура агентов](docs/thermal.md) ·
  [Спецификация и смета](docs/spec.md).
* Приложения: [А. История изменений](docs/history.md) · [Б. Концепт пульта и наблюдения](docs/ui_concept.md).

Новая плата: подключить по USB — на основном экране «⋯ ещё → ⇪ прошивка» → «прошить по USB» (или
двойной щелчок по `tools/new_board.cmd`); прошивается по USB один раз, дальше — по Wi-Fi.

Секреты (`secrets/wifi.ini` — сеть, `secrets/ota.key` — ключ подписи прошивок) в git не попадают:
скопируйте `secrets.example/` в `secrets/` и впишите свою сеть; ключ создаётся сам при первой сборке
(`tools/gen_secrets.py`). Открытая копия обновляется `python tools/publish.py` — одним коммитом, с
проверкой на утечки (имя и пароль сети, ключ, адреса).

Лицензия — MIT ([LICENSE](LICENSE)).
