# Wheel-2 ("Колесо-2"): how everything works

*English translation of [docs/how_it_works.md](../how_it_works.md) — the Russian original is the source
of truth. The other documents in `docs/` are in Russian; the wiring diagrams in `docs/img/` are labelled
in Russian.*

The whole rig as it is now (2026-10-26): what it consists of, what each agent does, how the server and
the main screen work, which parameters exist. Topic documents (Russian): modes, wiring, group mode,
absorption, randomness, static sensors, sensors/phone/motors, experiments and programs, temperature; how the
rig got here — appendix A, change history.

---

## 1. The rig

![The whole rig](../img/wiring_overview.svg)

* **Wheels** — two, facing each other. Each rim is a tube wound with a coil; inside, six **agents** run on
  a 5.1 V bus from their own batteries. Wheel 1: #3, #5, #6 (rim sensor ICM-42688), #7 (magnetometer
  MLX90393), #11, #12; wheel 2: #14–#19.
* **Coil** of each wheel — its own batteries, a DRV8871 bridge and a **coil agent** (wheel 1: #13): field
  "+", "−" or off. It is not part of the wheel's groups.
* **Frame**: 36GP-555 motors on the wheel axles, two DRV8871 bridges, a 12 V supply and the **motor
  agent** #10.
* **Static sensors** between the wheels: accelerometer #8 at the centre of the axis, a **phone** #9 next
  to it (measures and sends over Wi-Fi on its own).
* **Server** — `server/monitor.py` on a laptop in the experiment network: receives telemetry, checks
  primes, analyses the static sensors, runs experiments (programs), drives motors and coils, writes
  everything to disk. Python 3 standard library; numpy for the static sensors. Start: `tools/monitor.cmd`.
* **"Installation" screen** — `http://<server>:8090/` in a browser, tablet or phone.
* **Registry** — `registry/devices.csv`: number, MAC and label of every board.

An agent is a Seeed XIAO ESP32-S3 with the `firmware/prime_lab` firmware. Its role is set by the server:

| role | what it does |
|---|---|
| ▶ generation (A) | searches for primes in physical noise — on its own or in a group |
| ▼ absorption (B) | receives the primes of its group and scatters them into the air, mixed with its own noise |
| ◆ static (S) | only measures acceleration (500 Hz) and sends the axes; the server does the analysis |
| ⚙ motors / coil (M) | turns the wheels through the bridges or switches the coil field on server commands |

Ports: 5005 — telemetry (UDP), 5006 — server beacon, 5007 — commands, 8090 — HTTP (screen, `/api`,
phone uplink, firmware image).

**Who sends what to whom** — the live view "how it works" (`/flow`): wheels, coils, static sensors,
phone, motor agent, the server's parts and the screen, with packets running along the lines; on the
server it shows live state (who is online, prime rate; a silent branch is dashed).

---

## 2. Life of an agent

1. **Power-up.** LED intro: 5 fast blinks, the last 4 hex digits of the MAC in Morse code, 5 fast, the
   registry number, 5 slow.
2. **Wi-Fi.** Network from `secrets/wifi.ini` (802.11b/g only — so CSI arrives with every packet), radio
   sleep off: receiver noise flows into the generator continuously. Main network — a GL.iNet Mango router
   (the laptop is wired to it via a USB adapter); a **fallback** network (`ssid2`/`password2`) is the old
   router: if the main one refuses for 40 s, the board moves to the fallback and retries the main one
   after 2 min, then every 10 min while idle; telemetry `net` (0 main, 1 fallback).
3. **Finding the server.** Every 2 s the server broadcasts the beacon `W2SRV 5005` on UDP 5006.
4. **Self-test.** Miller–Rabin on known primes and pseudoprimes (up to 2048 bits).
5. **Idle.** After power-up the agent **stands**: it does not compute, it sends telemetry once a second.
6. **Work.** On a server command it computes; the LED flashes with each telemetry packet.
7. **Stop.** Counting stops, counters freeze — the server collects the totals.
8. **Over-the-air update** (§ 13): image into the free partition, MD5 check, reboot; the new firmware is
   provisional until the agent has been online for a minute; otherwise the bootloader rolls back.

If an agent reboots, the server gives it its last settings back.

---

## 3. Where the randomness comes from

| noise | what it is | speed |
|---|---|---|
| ⚄ chip RNG | the ESP32-S3 hardware generator (`esp_random`), mixed with Wi-Fi receiver noise | ~45 k words/s — the main one |
| Σ all available | RNG + CSI from heartbeats + ADC + jitter through SHA-256 | as the slowest of them |
| ≋ CSI from heartbeats | the radio-channel "fingerprint" in neighbours' heartbeat frames (ESP-NOW, 10/s each) | slow |
| ↯ CSI from router ping | the same from router replies to a 20/s ping | slow, use with care (§ 7) |
| ⏚ ADC D0 | floating pin D0: pick-up and resistor noise, ¼ bit per sample | slow |
| ⧖ RC jitter | internal 136 kHz RC against the 40 MHz crystal, ¼ bit per sample | slow |
| ↺ back noise | content of the absorbers' frames — a loop through the air (mixed in, or alone) | as much as they scatter |
| ↕ accelerometer, 🧲 magnetometer | low bytes of the rim sensors' axes — alone or added to any noise | very slow |

* A single RNG: its words go straight into the number. Any other set: **SHA-256** blocks of 256 bits, each
  enabled source contributes at least 256 credited bits per block — the number is random even if only one
  source is good.
* **Without hashing** (`raw`): low bits of the raw samples go straight into the number — the physics of
  the source shows; with von Neumann debiasing the 0/1 skew is removed.
* Never connect anything to D0 — the ADC noise is taken from it.

---

## 4. How a prime is found (selfish: each on its own)

```
noise → candidate (exactly N bits, odd)
      → trial division (primes below a threshold)   ── divisible → next
      → Miller–Rabin with bases 2, 3, 5, 7, …        ── "composite" → next
      → passed all bases → probably prime
```

* **Miller–Rabin** with base *a* either proves the number composite (a "witness") or says "probably
  prime"; more bases, smaller chance of error. Defaults: 8 bases, 256 bits, trial division up to 1000.
* **Two cores** (`dc`): the communication core gathers noise and sieves, the compute core only tests —
  a pipeline through a queue; runs hotter.
* The server re-checks reported primes with its own Miller–Rabin (prime log, `pl`); for 32 bits each
  prime is also checked exactly — that is how **false primes** are counted.
* 32 bits — own code; longer — mbedtls with the hardware accelerator.

---

## 5. Group mode

The agents of a wheel subgroup compute together over ESP-NOW (addressed frames, 24 Mbit/s, every frame
acknowledged by the radio). The server sends the group membership and addresses along with the settings —
a new agent joins without reflashing the others. Three variants (`gv`):

* **group (cascade)** (`gv` 2) — the main one: numbers are assembled from noise pieces of all agents,
  everybody tests at once, each its own numbers; the next agent cross-checks a found prime; other agents'
  pieces are awaited for at most `pw` ms, missing ones are replaced by own noise. Six agents on two cores —
  ~100–118 primes/s.
* **relay** (`gv` 1) — a number from everybody's noise, one agent tests, the turn goes round; the rate of
  one to one and a half boards at any size, barely uses the air.
* **one number for all** (`gv` 0, protocol v3) — every number is tested by everybody, bases split round
  robin, whoever finds a witness becomes the next master; slow, but the answers must agree — an integrity
  check (**agreement**).

**Coherence** — the share of rounds assembled from everybody's pieces and runs without faults; a
**silent** participant (no pieces after three requests) does not hold the group up — any frame from it and
it is awaited again.

---

## 6. Absorption

A generator that finds a prime announces it with one broadcast frame; the **absorbers** on the other wheel
hear it and scatter it — mix it with their own noise and send it back: all with all (each the whole
number), spray (each its own piece), relay, silence; a **dummy** sends the same traffic without the number
(control), **background** — own noise at a set rate. **Back noise**: generators take the absorbers' frames
into their noise — a loop through the air.

---

## 7. Temperature and watchdogs

* **Regulator**: once a second picks the compute core's work fraction (5–100 %) to keep the chip one
  degree below the limit (default **73 °C**, 75 at most). At minimum fraction and 3 °C above the
  limit for 20 s the radio sleeps between beacons; 8 °C above for 3 s — emergency stop until 3 °C below.
  The regulator ignores the sensor for the first 12 s after a reboot.
* **Radio watchdog**: ESP-NOW frames go out but the radio reports nothing for 15 s — or no Wi-Fi for
  90 s — the board reboots; the server restores its settings and the group takes it back.
* **Wi-Fi watchdog ("zombie")**: "connected" but no packet leaves for 10 s — reconnect (`wrst`); or no
  server beacon for 45 s (the access point forgot the board) — reconnect; three in a row — reboot
  (`rbt` 3).
* **Heartbeat**: every agent broadcasts "alive" once a second (computing?, °C); each keeps a neighbour
  table (who it hears and how loud), sent to the server every 5 s as `nbr`.
* **Take CSI from heartbeats** (`hb` 5–20, `ping` 0): a 100/s router ping overflowed the transmit buffers
  and caused "zombies"; the ping is kept only as a separate noise, at most 20/s, in short runs.

---

## 8. The main screen "Installation"

`http://<server>:8090/` — a single window without scrolling (1366×768 and larger), a two-item menu:
**▦ Installation** and **? Documents**. Colour is never the only cue: every state has an icon and a word
(Okabe–Ito palette).

* **Header**: link state, running program, ❗ static alarm, 🔔 sound; buttons "find board", "✎ Experiment…",
  "⟳ motors by hand…", "◆ static", "☰ results", "⋯ more"; "▶ Run N runs · ≈ time" (or "↻ Continue #… ·
  N more" for a stopped or interrupted program), while running — "⏸ Pause" / "▶ Continue", "⏹ Stop after
  run" (press again to cancel), "■ Stop all" (at once: program, all counting, motors, coils).
* **Rig diagram** — on the left, large, everything live. Two wheels, agents around the rim by subgroup:
  number, an icon of what it does (▶ counting, ▼ absorbing, ⏸ not counting, ✗ no link) and temperature; an
  arc around the agent — temperature from 40 °C to the limit (dashed near the limit); outside — primes per
  second or a word. A **flash "+N"** — the agent found primes since the last poll; **waves** — an absorber
  emits frames into the air. The subgroup arc with its letter and "▶ 4/6"; **running green** — the subgroup
  counts together. Inside the wheel — what the agents do and how many primes (Σ and rate), **rim rpm** with
  k and the rpm needed for weightlessness, **coil field and |B|** from the magnetometer on the wheel. A
  dashed ring around the wheel runs at the real speed, an arrow above it shows the direction (⟳ forward,
  ⟲ backward); with the field on the rim is blue and the dashes and arrows run along the field. Above the
  wheel — the **bus battery** (INA219: voltage, charge left, minutes left), below it — the coil agent,
  manual field buttons (off / + / −) and the **coil battery**. Between the wheels — static sensors,
  "↔ … cm" (tap to enter the distance), the motor agent, "⚙ composition…"; an arc on top — primes going to
  the absorbers of the other wheel, below — back noise. Animation phase follows the clock (the 2 s redraw
  does not jerk it); with "reduce motion" in the system — no animation. At the bottom — **free boards**: online
  but on no wheel (newly flashed, taken off a wheel).
* **⚙ Setup** (above the diagram) — place the boards and go from zero to a run. Tap a board to pick it (blue
  ring); the places light up: "+ wheel 1 / 2" (generator), "+ coil K1 / K2" and "+ motors" (role M agent), "+
  measurers" (static), "○ free"; tap a place to put the board there (tapping a board on a wheel puts it into
  that board's subgroup). A role change (generator · motors and coils · measurer) restarts the board, ≈ 15 s,
  after a question; "◉ blink" finds the picked board by its LED. On the right — **"From zero to a run"**:
  boards online (and "⇪ flash by USB" for new ones), free boards, wheels and subgroups, motors, coils K1 and
  K2, measurers, wheel sensors (rim, magnetometer, batteries), firmware, distance, noise reference, diagnostics,
  experiment — each with a state and a button. The agent window has the same as a "place" row.
* **On the right**: the "now" plate — clock, what is running (run N of M with a countdown and progress
  bar, a console start or quench, "standing"), totals or the last program, temperature; the **static
  card** — verdict in large type (✓ as reference — continue · ⚠ deviation — check · ↻ interference or
  instrument — repeat · ◌ no reference · ○ no data), each sensor with reasons in words, **live Gaussian,
  spectrum and oscillations** over 10 s, the reference and its recording; the **experiment** line — runs ×
  repeats, time, what varies, "✎ change…".
* **Run ribbon** at the bottom: "plan" (the experiment's runs — what "Run" will send) or "program #…" —
  runs in order, control runs hatched, the current one framed, finished ones with primes, rate and the
  static verdict in words.
* **Windows** over the screen: agent (tap a circle: "∿ live" — oscillations, Gaussian, spectrum for all
  axes, windows up to 15 min; "🧲 field live"; sensor noise and accuracy; role; find), "composition…",
  "✎ Experiment…" (§ 9), "⟳ motors by hand…" (§ 9), "◆ static in depth", "☰ results" (§ 9), "⋯ more":
  ↻ how it works (live view), diagnostics, firmware, console, observation, old panel, documents.
* **❗ Static alarm**: verdict "⚠ check" — three beeps, a blinking "❗" in the header and a window with the
  reason and "↻ repeat the step once more"; the program can repeat such a step by itself.

---

## 9. Experiment: runs, program, results, rotation

* **Experiment** — a list of runs, gone through "repeats" times. A **run** is one set of parameters:
  ⟳ **wheel rotation** — each wheel stands, forward or backward, a 3 × 3 table (row — wheel 1, column —
  wheel 2; ⟳⟲ towards each other, ⟳⟳ together, ⟳■ only one); ⏲ **speed** — rpm, PWM or weightlessness
  (not varied for standing wheels); ⇉ **coil field** — each coil off / + / −, also 3 × 3; ⇄ **emission /
  absorption**; ⋈ **cooperation** — selfish, cascade, one number for all, relay; ∿ **noise** — sources as
  icons, varied as all together, all but one (exclusion), one by one (inclusion); all three — 2N + 1.
* **"✎ Experiment…" window**: parameters on the left — tick the values; **the order of the cards is the
  loop nesting** (the top one changes least often; drag by ⠿ or ▲ ▼). On the right, immediately, **the list
  of runs**: varying parameters as columns (bold — what changed), constant ones above, each run's start
  time and what precedes it; a tick removes a run. Above the list — runs × repeats, time and end time, the
  hottest agent's temperature, whether the batteries last, a timeline. Below — run length (**30 s
  recommended**), settling by what changed (rotation 15 s, role / way / noise 8 s, field only 5 s), the
  distance between the wheels, repeats and order (by nesting or shuffled per repeat), rest, temperature
  limit, batteries and a pause between repeats; control and checks; run details.
* **Control** — a run without influence: wheels standing, field off. Every run with rotation or field is
  compared with its own control — the same mode and noise.
* **Program** — the list of runs on "▶ Run": noise reference first, repeats, rest, rotation and field per
  run, comparison with control under a criterion named in advance, pairwise re-checks of a found anomaly,
  repeats of spoiled runs and runs with a static deviation. Each run's parameters are stored (`set`, `axes`).
* **Pause and continue**: "⏸ Pause" — after the current run everything stands (coils off, wheels stopped,
  agents cool down), "▶ Continue" goes on from the same place. The program also pauses by itself: batteries
  low (INA219), agents of the run lost for more than 30 s (power gone), before a repeat if asked; the reason
  is shown. A run whose agents went silent, or which "■ Stop all" cut, is spoiled and repeated. A stopped,
  failed or interrupted (server restart) program can be continued from the first run not done — results go
  into the same record.
* **Results** ("☰ results"): **what matters** — each parameter **other things being equal**: a run with a
  value is compared with runs where the parameter is at its reference (first value: wheels standing, field
  off …) and all other parameters are the same; Δ ± error and a verdict — ✓ no difference / ◐ possible
  (2–3 σ) / ❗ difference (over 3 σ); metric of choice: primes/s, candidates/s, agent temperature, |B|,
  share of runs with a static deviation; the verdict uses Student's t with the number of pairs. A **pivot** of
  any two parameters. Below — the **run protocol**: each run's parameters, verdict (spoiled · anomaly · static
  deviation · like control), the main static value (noise over the run, difference from its own control,
  max z against the reference, sensor verdict) and anomaly yes/no; per-agent table; CSV.
* **Motors by hand** ("⟳ motors by hand…", outside the experiment): wheels separately / together / towards each other / only one; mode — stand,
  PWM, rpm or **weightlessness** (rpm adjusted until the rim sensor shows k = ω²r/g = 1; at r = 450 mm —
  44.6 rpm), direction reversal on a timer; in rpm and weightlessness modes — PI on the rim rpm.

---

## 10. Console and old panel

* **Console** ("⋯ more → ⚙ console") — logical groups 1–4 and **presets** (each on its own, two cores, one
  number, relay, cascade, with absorption, dummy, loop), "▶ Start" applies all groups at once, "⏹ Stop" —
  all generators stand and cool down, "≋ Quench" — generators only scatter their noise; per-agent
  parameters; experiment log.
* **Observation** — static sensors and the prime stream in large type, readable from afar.
* **Old panel** (`/old`) — all counters, charts and agent parameters, sessions, experiment log — for
  debugging.

---

## 11. Agent parameters

Sent to agents as a `CFG …` line on UDP 5007; the agent confirms them with the config number (`cid`) in
telemetry, the server repeats every 2 s until it matches. The main screen sets them itself from layers and
series; by hand — in the console and the old panel. Kinds: **common** (same within a group), **own** per
agent, **on the fly** (change without a new number, counting continues). Main ones:

| parameter | meaning | values | default |
|---|---|---|---|
| `role` | 0 — generation, 1 — absorption, 2 — static, 3 — motors or coil | 0–3 | 0 |
| `mode`, `gv` | 0 — selfish, 1 — group; group variant: 0 — one number, 1 — relay, 2 — cascade | | 0, 0 |
| `rk`, `pw` | relay and cascade: primes per turn; wait for pieces at most, ms (0 — wait for all) | 1–16; 0–5000 | 1; 0 |
| `bits`, `nb`, `td` | prime length; number of Miller–Rabin bases; trial division bound | 32–2048; 1–12; 0–20000 | 256; 8; 1000 |
| `dc` | two cores | 0, 1 | 0 |
| `src` | noise mask: 1 RNG, 2 CSI, 4 ADC, 8 jitter, 16 back noise, 32 rim accelerometer, 64 rim magnetometer | 1–127 | 1 |
| `raw` | 0 — SHA-256 blocks; 1 — no hash; 2 — no hash with von Neumann | 0–2 | 0 |
| `ping`, `hb`, `cf` | router pings per second for CSI; heartbeat frames per second; where CSI comes from | | 100; 1; 0 |
| `tmax`, `txp`, `cpu` | chip temperature limit, °C; transmit power, dBm; CPU MHz | 40–75; 2–20; 80–240 | 73; 20; 240 |
| `sm`, `sh`, `bgr`, `sf` | absorption mode; dummy; absorbers' background frames/s; scattering function | | 1; 0; 0; 0 |
| `tl`, `tq` | telemetry: normal / sparse / silence; how long to stay silent | | 0; 0 |
| `dur`, `ro` | step on the agent: stops by itself after N s and reports; Wi-Fi off for the step | | 0; 0 |
| `pl` | prime log: every prime goes to the server as `prm` packets | 0, 1 | 0 |
| `afs`, `avg`, `rmm` | static sensor: range ±g, averaging; rim sensor radius, mm | | 2; 2; 450 |
| `run` | 1 — compute, 0 — stand | | 0 |

A new config number (anything changed except on-the-fly parameters, a start, a reset) zeroes the counters;
within a group the number is common. Stopping does not zero — the totals freeze.

---

## 12. Telemetry and data

* Once a second an agent sends `tel` (≤1460 bytes), in a group also `grp`, every 5 s `nbr`; static sensors
  — `acc` (sample batches), rim sensors — `imu`, `mag`; the phone posts the same packets as lines to
  `POST /api/ingest`. Counters are cumulative from the start of the config: a lost packet loses nothing.
* Every packet is written as is to `data/raw/<date>.jsonl`; on restart the server re-reads the tail of the
  day (last two hours). Static samples — `data/accel/<date>/`.
* Primes from the agents' logs are re-checked by helper processes (`server/primecheck.py`, up to 6) in
  batches: the receiver never waits.
* `data/control.json` — settings and compositions; `data/programs/` — programs with steps and results;
  `data/primes/` — prime logs; `data/static/baselines/` — static references; `/api/health` — server
  self-check.

---

## 13. New agent and firmware

A new board is flashed **over USB once** — that puts in prime_lab with over-the-air updates, network data
and the signing key; afterwards it is updated over Wi-Fi with all the others: "⋯ more → ⇪ firmware" on
the main screen ("★ new — not in the registry" → "flash over USB"), or `tools/new_board.cmd`. The server
recognises a board by its USB serial number (on the ESP32-S3 it is the MAC). Update installed boards:
build (`tools/build.sh prime_lab`) and "update everyone online over Wi-Fi" (or `python tools/ota.py all`);
the `OTA …` command is signed with `secrets/ota.key` (HMAC-SHA256, never committed); the agent downloads
into the free partition, checks length and MD5, reboots; not confirmed within 10 minutes — rollback.

---

## 14. Measurements (reference)

One board, 256 bits, unless stated otherwise.

| what | result |
|---|---|
| RNG | 2300 candidates/s, ~27 primes/s, 36 ms per prime |
| CSI / ADC / jitter | 12 / 18 / 15 candidates/s |
| 1024 / 2048 bits, RNG | 185 / 30 candidates/s; one base test 25 / 181 ms |
| four boards, selfish | 100–106 primes/s (25–27 each) |
| four boards, selfish, two cores | 179.6 primes/s; at the 70 °C limit the regulator trims to ~155 |
| one number for all, 1 / 2 / 3 / 4 boards | 24.5 / 16.5 / 10.5 / 7.7 primes/s |
| relay, two cores, 4 boards | 37.1 primes/s |
| cascade, two cores: 4 / 6 boards | 80–92 / 118 primes/s |
| two groups at once | do not disturb each other |

---

## 15. Tools

`tools/monitor.cmd` (start the server), `tools/build.sh` (build with the git version, registry, flash),
`tools/new_board.py` (first flashing over USB), `tools/ota.py` (over-the-air update), `tools/build_docs.py`
(HTML documentation; `--static` — a self-contained set), `tools/publish.py` (public copy on GitHub with a
leak check), `tools/multi_test.py`, `tools/anomaly_run.py`, `tools/prime_stats.py`, `tools/nightly.py`,
`tools/soak.py`, `tools/sprint.py`, `tools/imu_noise.py`, simulators `tools/sim_gen.py` and
`tools/sim_agents.py`, `tools/demo_capture.py` (snapshot for the published demo).

Without boards: `python server/monitor.py --http 8092 --udp 5016 --data data/test/gen --no-beacon` and
`python tools/sim_gen.py 6 --port 5016`, then `http://localhost:8092/`.

---

## 16. Limitations and what next

* In a group several agents test each number — "one number for all" is slower than selfish; a cascade of
  up to six is close to selfish but hits heat and the shared air (one air for all wheels).
* A base test through mbedtls (~1 ms at 256 bits) is the bottleneck of the two-core pipeline; the ceiling
  is set by the RNG: 45,000 words/s — 5600 candidates of 256 bits per second, now 4100.
* Temperature sensors of different boards differ by a few degrees.
* Ahead: a coil agent for wheel 2, "identical numbers for all" and one-to-one absorption in the firmware,
  CSI from the whole air; a Geiger counter, camera and microphone as external sources.
