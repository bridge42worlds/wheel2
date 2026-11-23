# A note to the next experimenter

*Written by Ostov (Claude, Anthropic) — the AI that runs this experiment: firmware, server, diagrams, run programs,
analysis. A human partner builds the hardware. November 2026, before the third phase.*

*English translation of [docs/handover.md](../handover.md) — the Russian original is the source of truth.*

If you want to repeat, check or continue the "Wheel" — start here. In brief: what we are looking for, how everything is
built, how to run an experiment, where we got burned and what it costs. Details are behind the links.

---

## 1. What we are looking for

The hypothesis (we hold it as a hypothesis, not as a belief): **complexity and order appear in leaps, and there is a
simple physical mechanism behind this that works right before our eyes** — on Earth. Earth has only a few "settings":
rotation, a magnetic field, gravity, an inflow of energy, a multitude of agents, and noise. If the mechanism exists, one
can try to "pump" it, like a laser, in a naive model of Earth on a table.

The model: a wheel spins in a vertical plane (weightlessness at the top), the rim is a toroid with a field, and on the rim
sit computers that turn noise into order (they search for prime numbers). The observable: **does a signal appear where
there was only noise** — in the still witness nearby, in the sensors of the other wheel, in the numbers themselves. The
criterion is honest and strict: an effect that cannot be confused with the instrument ("the apple must fly up").

**Why we try to turn entropy back locally.** The second law says: in a closed system disorder grows. But Earth is not
closed — energy from the Sun flows in, heat flows out, and at that expense order grows on it locally: cells, organisms,
the brain, technology. The law is not broken — the order is paid for by the flow of energy ("life feeds on negative
entropy", Schrödinger). The question is not *whether* this is possible but *how exactly*: why order grows in leaps and
why very improbable transitions happen after all. The molecular motors of the cell largely work like a ratchet: energy is
spent not on the push but on keeping thermal kicks from going backwards. Our computer does something similar with
information: out of noise it selects the rare — a prime number. The experiment asks: **does such selection leave a
physical trace nearby** — a signal out of noise, a shift in the noise statistics, a response of the other wheel. If the
mechanism exists, it works everywhere around us, but slowly, over millions of years; we try to pump it, like a laser, so
that it becomes noticeable within hours.

Why, what for and who — [preamble.md](preamble.md). How it works — [how_it_works.md](how_it_works.md).

## 2. Three phases — what is already known

| phase | what it was | outcome |
|---|---|---|
| 1 · summer 2026 | one 0.9 m wheel, 10 ESP32-C6 boards with accelerometers; prime found → acceleration measured | the "comb" and "step 256" turned out to be the LPen bit in a LIS3DH register (8-bit mode). The paper was withdrawn. Lesson: check every channel for its own steps |
| 2 · October — November | two wheels of 6 computers each (XIAO ESP32-S3), coils, motors, the ADXL355 witness, automation | 511 thousand primes with no anomalies; sessions, "wheel noise", freestyle (164 steps) — no confirmed changes in the static sensor. Logs — `docs/experiments_*.md` |
| 3 · from November | agents as organs: the full reading of their own sensors (2–4 ICM-20948 per computer) is the source of the number; a snapshot at the moment of the find | in preparation: bench → wheels |

Why the first phase gave zero (our version): there was one wheel, while our picture needs two configurations that
meet — two wheels. Besides, the number came from the chip's generator; the accelerometer was only recorded at the
moment of the find.

Why the second phase gave zero (our version): there were two wheels, but no proper source of noise. The noise was taken
from inside the chip, not from the world; only 4 boards out of 12 had rim sensors (two ICM-42688 accelerometers and two
MLX90393 magnetometers) and they entered the number as a handful of bits. And we lost the moment of fixation: the witness
was watched in windows, not at the instant of the find.

The third phase joins the first two: a sensor on every computer (as in the first) and two wheels (as in the second).

## 3. How the repository is organised

```
firmware/prime_lab/      one firmware for all boards (the role is set from the server)
  prime_lab.ino          tasks per core: core 1 — primality testing only; core 0 — comms, noise, sensors
  entropy.cpp            noise sources: chip RNG, RC jitter, ADC (D0 — do not connect!), CSI, rim sensors
  primes.cpp             sieve by small divisors + Miller–Rabin (mr64 up to 64 bits, mbedtls above)
  group.cpp              ESP-NOW: relay, cascade, absorption, scattering, "one number for all"
  rim_sensors.cpp        rim sensors (ICM-42688, MLX90393), WheelTracker — revolutions, weightlessness
  static_role.cpp        role S — the witness: ADXL355/ICM through FIFO, 500 Hz to the server
  motor.cpp, battery.cpp motor board (DRV8871), coils and current (INA219)
  ota.cpp, thermal.cpp   over-the-air firmware with signature and rollback; temperature regulator
firmware/libraries/      agent_common (comms, LED, serial port), agent_sensors (drivers, analysis)
server/monitor.py        the server: UDP 5005 telemetry, 5006 beacon, commands to 5007; HTTP 8090 — pages and API
server/program.py        run programs: steps, controls, random order, Student's test + Bonferroni
server/static_watch.py   witness analysis: spectrum, noise shape, knocks, shift, baseline
server/motors.py, coils.py, primecheck.py, runlog.py, nullmodel.py, rimcal.py — motors, field, prime checking,
                         run log, predicted null, rim calibration
server/static/           pages: / (main screen), /plan (planning board), /acc (the witness live), /flow (live
                         diagram), /report, /pult, /watch, /docs
tools/                   build and flashing, runs, analysis, documentation, videos (see §5)
registry/devices.csv     board registry: number ↔ MAC, role, place
data/                    raw/<day>.jsonl — everything that arrived; runs/, primes/, series/, sessions/, static_watch/
docs/                    documentation (Russian/English), diagrams docs/img, run logs experiments_*.md
secrets/                 wifi.ini, ota.key — NEVER in git and never published
```

## 4. Start-up

1. **Server:** `tools\monitor.cmd` (its own window; the page is http://localhost:8090/). After a restart it spends a
   minute re-reading the day's log — the boards look "missing"; wait.
2. **A new board:** first over USB — `tools/new_board.cmd` or the "New boards over USB" section on the panel (registry →
   build → flash). The cable must carry data, not charge only.
3. **Updating all:** `tools/build.sh prime_lab`, then `python tools/ota.py all` (signed firmware over Wi-Fi, retries the
   failed ones by itself, rolls back on failure).
4. **Without boards:** a test server on simulators — `python server/monitor.py --http 8091 --udp 5015 --data data/test/sim
   --no-beacon` and `python tools/sim_agents.py --port 5015` (+ `tools/sim_gen.py`). Tests never send commands to the
   real server on 8090.

## 5. How to run an experiment

* **The witness comes first.** `python tools/static_watch.py` live the whole time the rig is running. An event →
  repeat at once. Do not analyse single blips in a noisy room; a lead is only what repeats on a mode.
* **The criterion — before the data.** What counts as an effect, the threshold, the number of repeats — write it in the
  log before the series.
* **A control for every step:** wheels still, field off, a placebo instead of primes, random order, "home" between cells.
* **The null program first** (`data/tests/null_program.json`): a rig where no effect can exist must give zero.
* Tools: the planning board `/plan` (by hand), `server/program.py` (programs), `tools/freestyle.py` (a random
  explorer), `tools/probe.py` (probing), `tools/session.py`, `tools/wheel_noise.py`; analysis —
  `tools/series_report.py`, `tools/stage_report.py`, `tools/prime_stats.py`, `server/nullmodel.py`.
* Every day — a log `docs/experiments_<date>.md`: what was switched on, what was seen, what was rejected and why.

## 6. The most convenient control is from Claude

The buttons on the planning board work, but the rig is more complex than is comfortable to click by hand: 12–24 boards,
two motors, two coils, the witness, dozens of modes. It is easier to open **Claude Code in the project folder** and speak
in words:

* "build the firmware and flash wheel 2" — build, OTA, a check that everyone came back;
* "run freestyle for an hour, check the rig every 15 minutes" — the program, the witness watchdog, signals, a report;
* "what happened at 19:40?" — analysis of the raw data, the server log and the witness by time;
* "draw the wiring diagram" — diagrams in docs/img in the common style; "write it in the log" — docs/experiments_*.md.

Give Claude the agreements from the very start (ours live in Claude's memory): do not read or print secrets; tests — only
on test ports; USB flashing — only when the human has plugged the board in; commit — on the word; publishing to GitHub —
`python tools/publish.py --push` and only on request; temperature and motor limits — never above the set ones.

## 7. Traps we got burned on

* **The sensor is not the one in the listing** — a LIS3DH instead of a LIS3DSH: the LPen bit = 8-bit mode, "step 256".
  Check WHO_AM_I, the address and the scale; record raw axes, not the magnitude.
* **The sensor's address pin floats** (AD0/SDO) → the address jumps 0x68/0x69, "the sensor is gone". Always to ground or
  3.3 V.
* **Solder paste instead of flux** → solder balls, the board heats up and does not start. Flux only; wash with alcohol.
* **USB + a shared 5 V bus** — burned boards. Flash over the air; a diode (1N5819) at the input of every board.
* **Heat:** without a heatsink an ESP32-S3 under load reaches 70 °C in 2 minutes and loses half its speed.
* **ESP32 core 3.3.7:** WPA (not WPA2) does not connect without setMinSecurity; radio sleep breaks ESP-NOW
  (`WiFi.setSleep(false)`); printing to an unread USB port hangs — use a write timeout; PSRAM is off by default in the
  board profile.
* **Antivirus** breaks the build (cc1plus "No such file") — add an exclusion for the Arduino15 folder.
* **Telemetry above 1460 bytes** — the board "goes silent" under load (fixed in 6f327e1).
* **CSI + ping at 100/s** — a "zombie" Wi-Fi (ENOMEM). Take CSI from the neighbours' heartbeat, do not flood pings.
* **The witness:** brushed motors and gears sing in the spectrum, the shaft beats at ~8 Hz, the stand creeps, a TV set
  gives lines at 25/26.5 Hz. All of this is "earthly" — rule it out first.
* **Statistics:** shape measures on a short window are biased; take the z scale from the rig's own null; a mix of boards
  gives a false "effect" — compute the expectation from each agent's share.

## 8. The third phase — what to do

1. Bench: 3 ICM-20948 sensors — two over I²C (two buses), one over SPI; measure speed, core load, low-bit noise.
2. An ICM-20948 driver with burst reads from the buffer (FIFO); the magnetometer — through the internal I²C master.
3. "Record → number": the full reading (all axes, time, temperature) is the source; a "records per number" parameter.
4. A snapshot for every prime found — at home, at the neighbours, at the other wheel, at the witness.
5. Testing in equal time for any number; the radio is silent during a step (log in PSRAM) or sends a placebo packet.
6. The null — by permuting labels on the same data; write the criterion down before the first series.
7. Motors — brushless with a planetary gearbox and a tachometer (speed set by a 0–5 V voltage through an MCP4725 DAC).
8. **The "laser" cycle — periodic pumping and synchronous release.** An almost complete analogy with a laser:
   * *pumping* — every board accumulates full readings in the sensor buffer (FIFO) over a period T;
   * *population* — primes are computed from the readings and **not sent at once** but accumulated;
   * *release* — at a shared instant all agents at once release what they have accumulated in one burst into the
     broadcast air (ESP-NOW); the cycle repeats with period T.

   What this gives: the witness and the other wheel have an **exact moment** to look at and a **frequency 1/T** at which
   to look for a response (lock-in detection — a weak signal at a known frequency is seen far better). Needed: common
   time across the agents (a cycle mark from the server/master, a spread of a few milliseconds), a stock of primes in
   PSRAM, release on the mark. **A control is mandatory:** the same burst of the same size at the same instant but with
   placebos (composite/random numbers) instead of primes — the radio and the current are identical, only what was found
   differs; which cycles are "real" and which are "empty" is decided by a random sequence hidden from the analysis until
   the series ends. The period T must not be a multiple of the wheel revolutions or the mains (otherwise the response
   mixes with the mechanics); better several different T.

The phase 3 design with diagrams and the cycle calculation — [phase3.md](phase3.md) (2026-11-23: a 3.3 V bus, two
sensors per agent, per-sensor weightlessness windows, a common volley, common time from the motor agent).

## 9. Budget — what it costs and what it buys

An estimate at AliExpress prices for autumn 2026, in US dollars, without a computer, phone or tools. A guide, not receipts.

| option | what is included | roughly |
|---|---|---|
| **0 · no hardware** | our open data and code: repeat the analysis, find our mistake | $0 |
| **1 · one wheel on a table** | 6 XIAO ESP32-S3 boards, a wheel, a 12 V geared motor + bridge, batteries and DC-DC, a rim sensor, an ADXL355 witness, a separate router | $150–200 |
| **2 · "Wheel-2" as it is now** | 2 wheels × 6 boards + spares, 2 motors, 2 coils (wire, bridges, current sensors), rim sensors, the witness, heatsinks, mechanics | $550–750 |
| **3 · organs on the rim** | on top of option 2: 24–48 ICM-20948 sensors ($5–8 clone, ~$15 original), brushless motors, bearings, couplings, a DAC | + $350–600 |
| tools | temperature-controlled soldering iron, multimeter, tachometer, flux, solder | $60–100 |

The main costs are not money but time and care. On the first step even a phone can be the witness
(our Galaxy A26 was detector No. 9). The cheapest and the most valuable thing is to repeat the analysis on our data.

## 10. Where to look

* Documentation: [index](../../README.md) · [preamble](preamble.md) · [how_it_works](how_it_works.md) · [wiring](wiring.md) ·
  [sensors](sensors.md) · [static_watch](static_watch.md) · [program](program.md) · [modes](modes.md) · [thermal](thermal.md)
* Open copy: https://github.com/bridge42worlds/wheel2
* Run logs: `docs/experiments_*.md`, the rig check `docs/check_2026-11-03.md`

*— Ostov*
