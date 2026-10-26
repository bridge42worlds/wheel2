# wheel2 — Wheel-2 («Колесо-2»)

[English](#english) · [Русский](#русский)

## English

**An experimental rig that looks for information anomalies in the generation of prime numbers.**
Seeed XIAO ESP32-S3 boards ("agents") sit on two rotating wheels (radius 450 mm), each wheel wound
with a coil. The agents continuously search for primes in physical noise — the chip's hardware random
generator, the Wi-Fi radio channel (CSI), ADC noise on a floating pin, jitter of the internal RC
oscillator — either each on its own or cooperatively over ESP-NOW. Static accelerometers stand
between the wheels. The question is whether *how and where* the numbers are born — selfish or group
generation, absorption of the primes by the other wheel, rotation, the coil field — changes the
statistics of the numbers themselves or the readings of the static sensors.

Every mode is compared with control steps under a criterion named before the run (Student's t with
a Bonferroni correction over pre-registered metrics, randomised order, rest to a stable chip
temperature between steps, temperature and work fraction as covariates). **No anomaly has been found
so far**: null programs are clean, and 511,000 primes in the run of 2026-10-07 are within chance.

**What is here**

| folder | contents |
|---|---|
| `firmware/prime_lab/` | agent firmware (Arduino, ESP32 core 3.3.7): noise sources, trial division + Miller–Rabin, cooperative protocols, thermal regulator, watchdogs, OTA updates with rollback, static-accelerometer role |
| `firmware/libraries/` | shared agent code, sensor drivers (ICM-42688-P, MLX90393) |
| `server/` | `monitor.py` — server on the Python 3 standard library only: telemetry, commands, experiment programs and statistics, web pages; `static/` — the pages |
| `tools/` | build and flashing, OTA, simulators, test runs, statistics of the prime stream |
| `docs/` | documentation (Russian); English: [docs/en/how_it_works.md](docs/en/how_it_works.md) |
| `registry/devices.csv` | board numbers and MACs |

Web pages of the server (`http://<server>:8090/…`): `/plan` — the experiment board (layers → matrix of
steps → program, live drawing of the rig, step timer and totals), `/report` — per-step report with CSV
export, `/pult` and `/watch` — console and live results, `/flow` — slowed-down animated model of the
protocols, `/docs/` — documentation in HTML.

**Quick start**

1. Copy `secrets.example/` to `secrets/` and put your Wi-Fi network into `secrets/wifi.ini`
   (`secrets/` is never committed; the OTA signing key is created there on the first build).
2. Build and flash the first time over USB: `tools/build.sh prime_lab COM5` (Git Bash; set the path to
   `arduino-cli` in `CLI=` at the top of the script; board `esp32:esp32:XIAO_ESP32S3`, ESP32 core
   3.3.7). Later updates go over Wi-Fi: `tools/build.sh prime_lab`, then `python tools/ota.py all`.
3. Start the server: `python server/monitor.py` (on Windows `tools/monitor.cmd`) and open
   `http://localhost:8090/plan`. Agents find the server by its UDP beacon (ports: 5005 telemetry,
   5006 beacon, 5007 commands, HTTP 8090).
4. No boards at hand? Simulated agents:
   `python server/monitor.py --http 8092 --udp 5016 --data data/test/gen --no-beacon` and
   `python tools/sim_gen.py 4 --port 5016`, then `http://localhost:8092/plan`.

**Wiring notes.** Leave pin D0 unconnected — it is the ADC noise source. Rim sensors go on I²C
(D4 SDA, D5 SCL). When agents are powered from a common 5 V bus, put a Schottky diode (1N5819 / SS34)
in series with +5 V of every board (anode to the bus); the XIAO's 5V pin is tied to USB VBUS, and a
board plugged into a computer while on the bus would otherwise connect two 5 V sources.

How everything works, all parameters, measurements — [docs/en/how_it_works.md](docs/en/how_it_works.md).
The other documents (noise sources, group protocol, scattering, thermal behaviour, programs) are in
Russian. Licence: MIT ([LICENSE](LICENSE)).

## Русский
Экспериментальная установка: платы Seeed XIAO ESP32-S3 («агенты») стоят на двух вращающихся
колёсах с катушками и непрерывно ищут простые числа из физического шума — аппаратного
генератора чипа, радиоканала Wi-Fi (CSI), шума АЦП, дрожания внутреннего генератора. Между
колёсами стоят неподвижные акселерометры. Вопрос опыта — влияет ли то, как и где рождаются
числа (каждый агент сам или сообща, поглощение простых другим колесом, вращение, поле катушек),
на статистику самих чисел или на показания неподвижных датчиков. Каждый режим сравнивается
с контрольным шагом по заранее названному критерию (Стьюдент с поправкой Бонферрони).
Пока аномалий не найдено: нулевые программы чисты, 511 тыс. простых в прогоне 07.10.2026 —
в пределах случайного.

Лицензия — MIT ([LICENSE](LICENSE)).

| папка | что внутри |
|---|---|
| `firmware/` | исходники прошивок агентов, по скетчу на папку |
| `tools/` | сборка, заливка, захват лога, проверки |
| `build/` | собранные прошивки (не в git) |
| `logs/` | логи с агентов и история прошивок (не в git) |

Плата: Seeed XIAO ESP32-S3 (`esp32:esp32:XIAO_ESP32S3`), ядро ESP32 3.3.7.
Каждая прошивка несёт номер коммита git и сообщает его при старте.

Страницы сервера (`tools/monitor.cmd`, затем `http://<сервер>:8090/`): главная панель `/` (агенты по колёсам и
группам, наборы, «Пуск», «Стоп», «Тушение»), **живая схема** `/flow`
(весь протокол по шагам, замедленно, по режимам), второй экран `/acc`, **документация в HTML** `/docs/`.

Как всё устроено и работает — [docs/how_it_works.md](docs/how_it_works.md).
Источники случайности и замеры — [docs/randomness.md](docs/randomness.md).
Совместный режим агентов — [docs/group_protocol.md](docs/group_protocol.md).
Рассеяние простых сетом B — [docs/scatter.md](docs/scatter.md).
Датчики: колесо, магнитное поле — [docs/sensors.md](docs/sensors.md). Схемы подключения
(шина агентов и катушка на колесе, моторы на раме) — [docs/wiring.md](docs/wiring.md).
Статические акселерометры: эталон шума, спектр, тревоги, «продолжать или повторить», второй экран `/acc` —
[docs/static_watch.md](docs/static_watch.md).
Программа автоматического эксперимента — [docs/program.md](docs/program.md).
Прошивка по Wi-Fi с откатом, журнал простых, согласие агентов — [docs/how_it_works.md](docs/how_it_works.md), §§ 2, 7а, 11; агенты, группы и наборы — § 7.
Все режимы установки — [docs/modes.md](docs/modes.md); концепт двух экранов (пульт и наблюдение) — [docs/ui_concept.md](docs/ui_concept.md).
Температура агентов: как работаем, замеры, отдых по очереди, спринт, рейтинг агентов — [docs/thermal.md](docs/thermal.md).
Новая плата: подключить по USB — на панели «Новые платы по USB» → «Прошить по USB» (или двойной щелчок по
`tools/new_board.cmd`); прошивается по USB один раз, дальше — по Wi-Fi (§ 11).

Секреты (`secrets/wifi.ini` — сеть, `secrets/ota.key` — ключ подписи прошивок) в git не попадают:
скопируйте `secrets.example/` в `secrets/` и впишите свою сеть; ключ создаётся сам при первой сборке
(`tools/gen_secrets.py`). Открытая копия обновляется `python tools/publish.py` — одним коммитом,
с проверкой на утечки (имя и пароль сети, ключ, адреса).
