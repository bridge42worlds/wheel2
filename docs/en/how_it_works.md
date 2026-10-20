# Wheel-2 («Колесо-2»): how it all works

*English translation of [docs/how_it_works.md](../how_it_works.md) (the Russian original is the
source of truth; if the two disagree, the Russian one is newer). Other documents in `docs/` are in
Russian: [randomness.md](../randomness.md) (noise sources, measurements),
[group_protocol.md](../group_protocol.md) (cooperative mode, results),
[scatter.md](../scatter.md) (scattering of primes by set B).*

This document describes the whole system: what each agent does, how the server works, which
parameters exist and what they change. Status as of 2026-10-04, with later additions up to
2026-10-19. Names of buttons and screens are given as they appear in the (Russian) user interface,
with a translation.

---

## 1. Parts

```
  agents (XIAO ESP32-S3 boards)            computer
 ┌──────────────────────────┐   Wi-Fi   ┌───────────────────────────────┐
 │ #1  core 0: comms         │ ────────▶ │ server/monitor.py             │
 │     core 1: compute       │  UDP 5005 │  · receives telemetry         │
 ├──────────────────────────┤           │  · writes raw packets to disk │
 │ #2  …                    │ ◀──────── │  · sends commands (UDP 5007)  │
 ├──────────────────────────┤  commands │  · runs sessions / programs   │
 │ #3  …                    │           │  · web pages: http://…:8090/  │
 └──────────────────────────┘           └───────────────────────────────┘
        ▲      ▲
        └──────┘  ESP-NOW between agents (cooperative mode)
```

* **Agent** — a Seeed XIAO ESP32-S3 board running `firmware/prime_lab`. Two cores: core 0 does
  communication, core 1 only computes.
* **Server** — one Python program without third-party libraries (`server/monitor.py`). Start it
  with `tools/monitor.cmd` (double click) or `python server/monitor.py`.
* **Web page** — `http://localhost:8090/` on this computer or `http://<computer address>:8090/`
  from a tablet on the same network.
* **Registry** — `registry/devices.csv`: number, MAC and label of every board.

---

## 2. Life of an agent

1. **Power-up.** LED greeting: 5 fast blinks, the last 4 characters of the MAC in Morse code,
   5 fast blinks, the board number from the registry, 5 slow blinks.
2. **Wi-Fi.** Connects to the router network from `secrets/wifi.ini` (802.11b/g only — that way
   CSI arrives for every packet); radio sleep is off: receiver noise flows into the generator
   continuously and the link has no delays.
3. **Finding the server.** Every 2 s the server broadcasts the beacon `W2SRV 5005` on UDP 5006;
   the agent learns the server address by itself.
4. **Self-test.** Miller–Rabin on known primes and on "liars" (up to 2048 bits). The result is
   reported in telemetry.
5. **Waiting.** After power-up the agent **stands**: it does not compute and sends telemetry once
   a second (temperature, signal, state). The LED does not blink.
6. **Work.** On a server command (usually the start of a session or program step) it starts
   computing; the LED flashes once per second, on every telemetry packet.
7. **Stop.** Computing stops and the counters freeze — the server collects the totals.
8. **Firmware update over Wi-Fi.** The agent gets a new build from the server without USB
   (§ 11): it downloads the image into the free flash partition, checks the MD5 and reboots. The new
   firmware is on probation until the agent has stayed online with the server for a minute; if it
   does not come online within 10 minutes or crashes, the bootloader rolls back to the previous one.

If an agent reboots, the server sends it its last settings again.

---

## 3. Where the randomness comes from

Sources are selected with check boxes; there are two groups.

| group | source | how it is taken | credited |
|---|---|---|---|
| internal — noise inside the chip | chip RNG + radio noise | `esp_random()`: hardware generator, Wi-Fi receiver noise is mixed into it; 44,800 words/s | 32 bits per word |
| | RC oscillator jitter | crystal clock cycles counted over 8 periods of the internal 136 kHz RC oscillator | ¼ bit per sample |
| external — noise from the environment | Wi-Fi CSI | channel measurements in the router's replies to a 100 Hz ping: 104 numbers per packet, ~97 packets/s (later: CSI from neighbours' heartbeat frames, see § 6) | 32 bits per packet |
| | ADC noise | pin D0 pulled to mid-supply by both pull resistors: pick-up and resistor noise | ¼ bit per sample |

* RNG alone — its words go into the number as they are.
* Any other set — **SHA-256** blocks of 256 bits; every selected source puts at least 256 credited
  bits into every block. The number is random even if all sources but one are bad. Sources are
  collected one after another, so their times add up: all four together give ~5 candidates/s versus
  ~12 for CSI alone and 2300 for the RNG alone.
* Nothing may be connected to pin D0 — the ADC noise is taken from it.
* The chip temperature sensor was measured as a source and rejected: 1 °C steps, two distinct
  values in 4096 reads.

---

## 4. How a prime is found (every agent on its own)

```
noise → candidate (exactly N bits, odd)
      → trial division (primes below a threshold)   ── divisible → next
      → Miller–Rabin with bases 2, 3, 5, 7, …        ── "composite" → next
      → passed all bases → probably prime
```

* **Miller–Rabin** with base *a* either proves that the number is composite (a "witness") or says
  "probably prime". The more bases, the smaller the chance of taking a composite for a prime.
* **Check mode:** "until the first failure" — the next base only checks those that passed the
  previous ones; "all bases" — every base checks every candidate (that is how a set of 8 agents works).
* For 32 bits every prime found is additionally checked exactly (bases 2, 7, 61) — this is how
  **false primes** are counted.
* The server re-checks the reported primes with its own Miller–Rabin (up to 256 bits; up to 512
  in cooperative mode).
* 32 bits — own code; longer — the mbedtls library with the hardware accelerator.

---

## 5. Cooperative mode

A group is the agents listed in "Кому" (to whom); the relay goes in order of their numbers (for
example 1 → 2 → 3). Each cycle has one master. Protocol version 3 (since 2026-10-03), details in
[group_protocol.md](../group_protocol.md).

1. **The number arrives together with the result of the previous cycle.** Everybody checks it at
   once with their own bases. Bases are dealt round-robin: with 8 bases and two agents #1 checks
   2, 5, 11, 17 and #2 checks 3, 7, 13, 19. Each answers the master: found a witness or not.
2. **Result.** Someone found one → composite, the finder becomes the master (if several found one,
   the choice is made by the hash of the number). Nobody found one → prime, the relay passes to the
   next agent in order. The same message carries the number for the next cycle.
3. **Candidate stock.** Numbers are taken from a stock of candidates that survived trial division;
   the stock travels to the new master together with the result.
4. **Refill during the check.** When the stock runs out, the result tells everybody "send pieces to
   the new master". Every agent immediately sends noise pieces from **its own** sources, collected in
   advance (with 256 bits and two agents — 24 pieces of 128 bits), and checks the number. Meanwhile
   the master glues the pieces from everybody in random order, runs trial division and stores the
   survivors.
5. **Without a stock** (start of a session, after a break) the master collects pieces on request,
   runs trial division and sends out the number itself.

Pieces are collected in advance only from the fast source (RNG); slow sources are collected on
request, after the check.

**Link** — ESP-NOW, addressed frames at 24 Mbit/s; the radio acknowledges every frame and retries
lost ones by itself. Frames are sent one at a time. If an answer takes twice as long as usual for
that agent, the master repeats the request.

**Group membership and the members' addresses are sent by the server** together with the
settings, from its registry. A new agent therefore joins a group without re-flashing the others.

**Coherence** — a series of consecutive cycles without a single repeat or break. For a group the
worst series among the members is taken.

Later group variants (`gv`, § 7a): "relay" — one number from everybody's noise, one agent checks
it, turns go round; "cascade" — numbers from everybody's noise, all agents check at once, each its
own numbers.

---

## 6. Temperature limit and watchdogs

We do not know the temperature outside the agent, so each one watches its own chip.

* Once a second a regulator chooses the **work fraction** of the compute core (5–100 %) so that the
  chip stays one degree below the limit. The default limit is **70 °C**, it cannot be set higher.
* Work fraction at its minimum and the chip 3 °C or more above the limit for 20 s in a row → the
  radio sleeps between router beacons.
* 8 °C above the limit for three seconds in a row → emergency: computing stops and the radio
  sleeps until the chip is 3 °C below the limit. For the first 12 s after a reboot the regulator does
  not trust the sensor: right after start it reads 5–9 °C high (2026-10-05: #6 after a reboot in the
  middle of a cascade — 78.7 → 74.7 → 70.7 °C within 12 s, 69.7 before it), and one such reading cost
  a 40 s emergency pause.

Details on heat — measurements, where it comes from, resting in turns, sprints — in
[thermal.md](../thermal.md).

**Radio watchdog.** ESP-NOW frames are being sent but the radio reports nothing about them for 15 s
in a row — or there is no Wi-Fi for 90 s in a row — the board reboots itself (on 2026-10-05 the radio
of #6 froze for good and only a reboot revived it). The agent tile shows the reason of the last
reboot: power, USB, software (OTA update), firmware crash (⚠), brown-out (⚠) or "⚠ by watchdog: the
radio did not transmit / no Wi-Fi for 90 s / Wi-Fi zombie not cured by reconnecting".

**Heartbeat.** Once a second (`hb`, 1–20 per second) every agent broadcasts an ESP-NOW frame "I am
alive" (whether it computes, chip °C, uptime) — in any mode, not only in a group. So the radio
watchdog sees radio reports in "every agent on its own" too, and every agent keeps a neighbour table:
whom it hears, at what signal level, when it last heard them, how many frames it received, whether
the neighbour computes and how hot it is. Every 5 s it goes to the server as an `nbr` packet
(`[number, signal dBm, seconds since the last frame, frames, computes, °C]`); the server keeps it in
`/api/state` (`nbr`) and `tools/nightly.py` prints a "who hears whom" matrix. A group member learns
its neighbours' addresses and signal from the heartbeat before the first protocol frame.

**Wi-Fi watchdog ("zombie").** The board believes it is connected but not a single packet leaves:
10 s in a row → reconnect (`wrst`); three reconnects in a row without a single packet sent → reboot
(`rbt` 3). In the experiment log for 2026-10-04…07 this happened only in modes with CSI (router ping
100 times a second): 1.3 times per hour with CSI and never in 27 hours without it. In telemetry
`udpe` is the error code of the last failed send (12 — no transmit buffers), `cmd` — commands
accepted and rejected (commands are accepted from any address a server beacon came from within the
last 10 s: a computer with several addresses sends beacons from each), `pend` — how many milliseconds
the compute core has not been accepting new settings, `heapmin` — the smallest free heap since
start. While the agent stands (`run` 0), CSI and the router ping are off.

The second form of the "zombie" (2026-10-07, #4 after 20 minutes with CSI): packets "leave" without
errors, but nothing reaches the server and nothing comes from it: the access point has already
forgotten the board (last disconnect — reason 8) and the board does not know it. #4 stayed silent for
23 minutes this way. The symptom is silence of the server beacon (it comes every 2 s): 45 s without a
beacon while Wi-Fi is "connected" → reconnect; three in a row without a single beacon in between →
reboot (`rbt` 3). In telemetry `bcn` = beacons received, seconds since the last one, reconnects due to
silence. When the server is off, boards reconnect every 45 s and reboot every ~2.5 min — harmless.

Measurement on 2026-10-07 (`tools/soak.py`, four boards, 20 min, CSI noise, two cores): with a ping
of 100/s the watchdog reconnected #3/#4/#5/#6 2/22/34/7 times, `udpe` 12 (ENOMEM — no transmit
buffers), smallest free heap 28–36 KB; in the second half of the run #4, #5 and #6 dropped out and
#4 never came back. With a ping of 20/s (only #3 stayed online) — 4 reconnects.

Analysis the same evening (three 15-minute runs, four boards, two cores): "router ping without CSI"
(`cf` 3, RNG) — a storm of radio-watchdog reboots (#4 nine times in three minutes, everybody out of
transmit buffers); "CSI without ping" (`ping` 0) — reconnects 0/1/1, no reboots, but only ~0.3 primes
per minute (CSI only from router beacons); "CSI from neighbours' heartbeat" (`ping` 0, `hb` 10) — not
a single reconnect, not a single unsent packet, heap flat at 59 KB, ~28 CSI measurements per second
per board, 3.5 candidates/s, ~2.5 primes per minute per board. Conclusion: what breaks things is not
CSI capture but the 100/s router ping — four boards produce 800 frames per second at b/g rates, and
the old router (WPA only, 18 ms response) cannot acknowledge them in time; the boards run out of
transmit buffers. Therefore **take CSI from the heartbeat (`hb` 5–20, `ping` 0)**; the router ping
remains a separate option — "field noise via Wi-Fi" — but at no more than 10–20/s and only in short
runs until the router is replaced.

Measurements: computing heats the chip by 4–8 °C; ESP-NOW at 1 Mbit/s heated noticeably more than at
24 Mbit/s. Sensors of different boards read differently (#2 consistently 3–5 °C above #1).

Two cores (`dc` = 1) heat more: within 5–6 minutes all boards reach the 70 °C limit, the regulator
cuts work on both cores (each core has its own sleep debt — a shared debt was updated by two tasks
racing each other, went negative and throttling disappeared; fixed on 2026-10-04), and the total
rate of four boards settles from 188 to ~155 primes/s. The regulator is linear: rate ≈ rate at 100 %
× work fraction (checked on a ten-minute run: 47.5 primes/s per unit fraction on #3, 49.7 on #5).
Board sensors disagree: #4 reads 1–2 °C lower under the same conditions and is hardly throttled.

**Self-restart on low memory.** If free heap drops below 30 KB while the agent stands (between
steps) and more than a minute has passed since start, it reboots to clean up (`rbt` 4); an OTA update
is refused when free heap is below 40 KB.

---

## 7. Agents, groups, presets

The main panel (`/`) has a section **«Агенты и группы»** (agents and groups). It shows what each
agent does and lets you change how it works:

* **Wheel** — where the agent physically sits (wheel 1, 2…). Set once, at installation; the agent
  table is split by wheels.
* **Group** — logical, how the agent works now: group 1–4, with *generation* (looks for primes) and
  *scattering* (broadcasts the primes of its group); "stands" — does not compute and cools down;
  "static" — only measures acceleration. Groups can combine agents from different wheels and be
  rearranged at any time.
* **Preset** of a group — how it works: each on its own, each on its own with two cores, all on one
  number, relay, cascade, relay with scattering, sham (control), loop through the air. Every preset
  has a line about what it does and what it yields.
* **▶ Пуск** (start) — apply all groups at once: each has its own settings number, groups work
  independently and do not disturb each other (two groups side by side: relay on #3 + #4 —
  30.5 primes/s, cascade on #5 + #6 — 42 at the same time). Agents not in a group stand and cool down.
* **⏹ Стоп** (stop) — all generators stand and cool down (80 MHz, LED off); static agents keep
  measuring; roles stay as composed.
* **≋ Тушение** (quench) — all generators only scatter their own noise: role "scattering", sham,
  background 50 frames/s — noise into noise, not a single prime (on four boards: each sends
  50 frames/s and hears ~143 from neighbours, the comms core is 10 % busy). «Пуск» restores the groups.
* A **new agent** appears in the table by itself, marked "new", and stands until it gets a wheel and
  a group.

Places and presets are saved immediately (`data/control.json`, section `compose`) and applied with
«Пуск». Every applied group is a separate entry in the experiment log. During a session or program
the compositions are not applied.

## 7a. Parameters

Set on the web page (generator control or group presets), sent to the agents as one line `CFG …`
on UDP 5007. The agent confirms them with the settings number in telemetry and with the values
themselves; until they match, the server repeats the command every 2 s.

Three kinds of parameters:

* **Common** — the same for all selected agents (no other way inside a group).
* **Per agent** — each agent may have its own value.
* **Built into** the firmware — fine mechanics, not changeable from the panel (see below).

| parameter | meaning | values | default | kind |
|---|---|---|---|---|
| `mode` | 0 — every agent on its own, 1 — cooperatively | 0, 1 | 0 | common |
| `gv` | group variant: 0 — all on one number (bases dealt out), 1 — relay (number from everybody's noise, one agent checks, turns go round), 2 — cascade (numbers from everybody's noise, all check at once) | 0, 1, 2 | 0 | common |
| `rk` | relay and cascade: primes per turn | 1–16 | 1 | common |
| `pw` | relay and cascade: wait for pieces at most this many ms, then fill the missing ones with own noise; 0 — wait for everybody, asking again | 0–5000 | 0 | common |
| `grp`, `gm` | numbers and MACs of the group members (filled in by the server) | | | common |
| `nr` | ESP-NOW rate: 1 — 24 Mbit/s, 2 — 6 Mbit/s, 0 — 1 Mbit/s (slow, heats) | 0, 1, 2 | 1 | common |
| `bits` | prime length | 32 … 2048 | 256 | common |
| `nb` | number of Miller–Rabin bases (the first of 2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37) | 1–12 | 8 | common |
| `all` | 1 — every candidate is checked by all bases; 0 — until the first "composite" (cooperatively — always until the first) | 0, 1 | 0 | common |
| `td` | trial division by primes below this number; 0 — none | 0, 100, 1000, 10 000, 20 000 | 1000 | common |
| `k` | all on one number: candidates per collection (relay and cascade take as many as fit in a frame) | 4–64 | 24 | common |
| `pre` | group: collect noise pieces in advance (RNG only) | 0, 1 | 1 | common |
| `tp` | telemetry period, ms | 500–5000 | 1000 | common |
| `src` | noise sources (mask: 1 — RNG, 2 — CSI, 4 — ADC, 8 — jitter, 16 — air of set B) | 1–31 | 1 | per agent |
| `raw` | how noise is taken: 0 — SHA-256 blocks (uniform numbers), 1 — no hash: low bits of the raw samples straight into the number, 2 — the same with von Neumann debiasing | 0, 1, 2 | 0 | per agent |
| `tmax` | chip temperature limit, °C | 40–70 | 70 | per agent |
| `txp` | transmitter power (Wi-Fi and ESP-NOW share one radio), dBm | 2–20 | 20 | per agent |
| `cpu` | CPU frequency, MHz | 80, 160, 240 | 240 | per agent |
| `ping` | router pings per second for CSI; 0 — no ping, CSI only from beacons and other frames | 0, 10–200 | 100 | per agent |
| `hb` | heartbeat: "I am alive" frames per second to everybody (ESP-NOW, no router involved); for neighbours each such frame is a CSI measurement, so CSI can be taken from the heartbeat without the router ping (`ping` 0) | 1–20 | 1 | on the fly |
| `dur` | step on the agent: after this many seconds of computing the agent stops by itself (run 0 with the same number — counters freeze), restores telemetry (tl 0) and the radio and hands in its totals: `done` = step number in telemetry; 0 — until stopped. Counting starts with a new computing setting; changing on-the-fly parameters does not reset it | 0–7200 | 0 | on the fly |
| `ro` | radio during the step (only with `dur`): 1 — switch Wi-Fi off entirely for the step if the step does not need the air (every agent on its own, no set B, noise not from the air, no ping); it switches off after two telemetry packets with the new number (so the server has seen the confirmation) and back on at the stop. While the radio is off, commands are not heard — the step ends by itself; afterwards `roff` in telemetry tells how many seconds the radio was silent. The chip RNG is weaker without the radio (no receiver noise) | 0/1 | 0 | on the fly |
| `led` | LED flash once a second while computing | 0, 1 | 1 | per agent |
| `role` | role: 0 — generation (set A), 1 — scattering (set B), 2 — static accelerometer (only measures; the agent stores the role and reboots) | 0, 1, 2 | 0 | per agent |
| `sf` | scattering function: 0 — hash, 1 — XOR, 2 — multiplication, 3 — bit permutation | 0–3 | 0 | per agent |
| `sm` | how set B scatters: 1 — each on its own, 2 — spread, 3 — relay, 0 — silence (4 — old "sham of each") | 0–4 | 1 | common |
| `sh` | sham: the same scattering traffic, but noise instead of the number | 0, 1 | 0 | common |
| `bgr` | set B background: frames of own noise per second regardless of primes | 0–200 | 0 | common |
| `tl`, `tq` | telemetry: 0 — normal, 1 — rare (10 times less often), 2 — silence; stay silent for `tq` seconds (0 — until cancelled). An agent silent on command is shown as online (◇ silent), not lost | 0–2; 0–65535 | 0; 0 | per agent |
| `rmm` | radius of the rotation sensor, mm — to compare with the measured one | 50–2000 | 450 | per agent |
| `afs` | static accelerometer: full scale, ±g | 2, 4, 8, 16 | 2 | per agent |
| `avg` | static accelerometer: average over this many 1000 Hz samples — 1000/avg Hz to the server | 1, 2, 4, 8 | 2 | per agent |
| `pl` | prime log: every prime found, with the agent's timestamp, goes to the server in `prm` packets; the server re-checks each and writes it to `data/primes/` | 0, 1 | 0 | common |
| `dc` | two cores (each on its own, relay, cascade): the comms core collects noise and runs trial division in its spare time, the compute core only runs Miller–Rabin — a pipeline through a queue. Noise is still taken by one core, there is one multiplier. Heats more; `dcq` in telemetry — how often the compute core waited for a candidate and the comms core waited for queue space | 0, 1 | 0 | common |
| `cf` | CSI: 0 — from any frames, 1 — only from router replies, 2 — only from set B frames; 3 — experiment: router ping runs but CSI is not enabled (with any noise source) — for the "zombie" analysis | 0–3 | 0 | common |
| `bg`, `bm` | numbers and MACs of the set B agents (filled in by the server) | | | common |
| `run` | 1 — compute, 0 — stand (set by sessions, programs and the apply button) | 0, 1 | 0 | |

**What resets the counters.** A new settings number: something changed other than the on-the-fly
parameters (`tmax`, `txp`, `ping`, `hb`, `led`, `tp`, `tl`, `tq`, `rmm`, `afs`, `avg`, `pl`, `dur`,
`ro`), or a start, or the "reset counters" button. In a group, and when there is a set B, there is one
number for everybody, so changing, say, the source of one member restarts counting for all selected
agents. On-the-fly parameters change without a new number: counting continues. A stop does not reset
either — the totals freeze.

**Agreement mode (`all` = 1 cooperatively).** Normally the Miller–Rabin bases are dealt out among
the agents round-robin. But a random composite number gives a witness to almost any base, so in every
cycle either everybody finds one or nobody: the master counts cycles in which not everybody found one
(`split`) — that is either an extremely rare pseudoprime to somebody's bases or a computation or
transmission error. With `all` = 1 every agent checks all bases in the same order, and the number of
the first witness base must be the same everywhere (`disagree` — it was not). This is an integrity
check: four boards compute the same thing, and their answers must always agree.

**Built into the firmware:** the temperature regulator (4 % per second per degree, radio sleep at
limit + 3 °C, pause at + 8 °C); timeouts of the group protocol (twice the usual + 20 ms, 3 retries,
master takeover after 1.5 s); noise credits (CSI — 32 bits per packet, ADC and jitter — ¼ bit per
sample); Wi-Fi 802.11b/g only without radio sleep; ADC pin D0; ports 5005–5007.

---

## 8. Sessions

A session is a stretch of work with its own statistics. Start it from the «Сессии» (sessions) panel:
name, duration in minutes (0 — until stopped); participants and parameters are taken from the
control form.

Steps (shown on the page with the time of each):

1. **everybody gathered** — all participants online (wait up to 30 s);
2. **settings applied** — everybody confirmed the settings while standing (up to 20 s);
3. **go** — common start, everybody confirmed computing (up to 15 s);
4. **work** — until the timer or the stop button;
5. **stop and totals** — everybody stopped, counters froze, the server collected the totals.

If a step fails, the session is marked failed with the reason, and agents already started are
stopped. The totals of every session — `data/sessions/<number>.json`; in the session history it can be
opened with charts for exactly its time.

For experiments with many modes, controls and statistics see the automatic program —
[program.md](../program.md) and § 12a below.

---

## 9. What the main panel shows

* **Agent tiles** — number, online or not, firmware, mode, temperature (◐ near the limit, ⚠ above),
  signal, packets.
* **Control and sessions** — on the left.
* **Totals** of the selected agent: candidates, trial division, primes, false primes, time per prime
  and per candidate by stage, server check, self-test, radio, temperature.
* **9 charts at once**: primes/s, candidates/s, time per prime (log scale), time per candidate by
  stage, trial-division shares, noise collection time by source, core load, temperature with limit
  and work fraction, signal.
* **Cooperatively**: membership and roles, cycles and primes, comparison with "each on its own",
  coherence, failures, link, cycle time by stage with the share of pure computing, three charts.
* **Miller–Rabin bases**: how many each base checked, how many it rejected, how long a check takes.
* **Time to find a prime** — distribution.
* **Experiment log** — every computing run is recorded automatically, wherever it came from (panel,
  session, program, `multi_test`, `soak`): parameters in words, duration, candidates and primes, yield
  versus theory, chip temperature range and minimum work fraction, packet loss and watchdog
  reconnects, group failures and agreement, scattering, prime log, server check; "open" — a per-agent
  table and charts from series stored in the entry itself (`data/runs/<date>.jsonl`, every 15 s).
  Days before the log existed are rebuilt by `tools/runs_from_raw.py`.
* **Live diagram** — `http://<server>:8090/flow`: the whole process on one screen, slowed down.
  Server and router at the top, set A and B agents as cards (what the comms core and the compute core
  do, stage noise → candidate → division → MR → ✓, bases, stock, place in set B), noise sources at the
  bottom; ESP-NOW frames fly with labels (NXT, RES, PCS, REQ, NUM, PRIME, SCAT, CHAIN, background),
  broadcasts spread as a wave, the master has an orange frame with ★. On the right — an event log in
  words; clicking an agent keeps only its events. At the bottom — the steps of the mode, typical times
  "model / boards now" and a timeline. Presets for every mode, slow-down from ×10 to ×40 000, pause and
  single-step. When agents compute, membership, modes and delays come from the server and the cards
  show live counters; otherwise — a demonstration based on measurements of 2026-10-03…04. It is a
  model of the protocol (discrete events), not a frame capture: numbers are tuned so that rates and
  cycles match the boards. Link to a mode: `/flow#group`, `#solo`, `#dc`, `#scatter`, `#pieces`,
  `#chain`, `#silence`, `#sham`, `#loop`, `#csi`, `#relay`, `#cascade`.
* **HTML documentation** — `http://<server>:8090/docs/`: the same documents with a table of
  contents; built by `tools/build_docs.py` (and by the server at start) into `docs/html/`.
  `python tools/build_docs.py --static` builds a self-contained set in `build/site/` (addresses and
  the network name removed).
* **ⓘ icons** next to every parameter and block — what it means, what it controls, which values,
  common or per agent, whether it changes on the fly.
* **Session history**, **noise sources** (by group), raw packets, files.

---

## 10. Telemetry and data

* Once a second the agent sends a `tel` packet (≤ 1460 bytes), in cooperative mode also a `grp`
  packet; commands are answered with `ack`. All counters are cumulative since the settings were
  applied: a lost packet loses nothing, the server computes rates over a 5 s window.
* Every packet is written as is to `data/raw/<date>.jsonl` before parsing. On restart the server
  re-reads the tail of today's file — the last two hours (that is how long series live in memory),
  finding the start of the tail by binary search on the receive time. UDP reception opens
  immediately: live packets are written to disk and queued, and enter memory after the tail, in
  order. Reported primes are checked with one base during the re-read (they were already fully
  checked live, the answer is in `data/primes`); the live check runs in a separate task so the
  receiver does not wait for it (full Miller–Rabin — 1.5 ms for a 256-bit number, 0.3 s for 2048 bits).
  Re-reading a whole day now takes 12 s instead of 165.
* `http://<server>:8090/api/health` — server self-check: whether its tasks are alive, age of the
  last packet, parse errors (the receiver survives a bad packet and counts it), prime-check queue,
  disk space.
* `data/control.json` — the last settings of every agent; `data/commands.jsonl` — command log;
  `data/sessions/` — sessions; `data/programs/` — programs.
* `data/primes/<date>/<mac>.txt` — prime log (`pl` = 1): server time, agent clock (ms), settings
  number, bits, origin (0 — on its own, 1 — cooperative), the number, server check. Statistics of the
  stream — `tools/prime_stats.py`.

The `data/` folder is not part of the repository.

---

## 11. Adding a new agent

A new board is flashed **over USB once** — that is how it gets prime_lab with over-the-air updates,
the network credentials and the signing key. After that it is updated over Wi-Fi together with all
the others.

1. Connect the board with a data cable (charge-only cables are not seen by Windows at all).
2. In the panel section **«Новые платы по USB»** (new boards on USB) it appears by itself: "★ new —
   not in the registry". Press «Прошить по USB» (flash over USB). Without the panel —
   `tools/new_board.cmd` (or `python tools/new_board.py`).
3. Done. The board is added to the registry with the next number, the firmware is built once
   (1–3 min) and flashed into every new board. About 10 s after flashing the board comes online —
   marked "new": give it a wheel and a group. The other boards need no re-flashing: they get group
   membership and addresses from the server.

How the server recognises a board without touching it: on the ESP32-S3 the USB serial number is the
MAC. If a board is not visible or flashing fails, hold BOOT on it, reconnect the cable and release:
in bootloader mode it is always visible. Manually, one by one: `tools/build.sh prime_lab COM<N>` — the
script reads the MAC, adds it to the registry, builds and flashes.

**Updating installed boards — over Wi-Fi.** Build (`tools/build.sh prime_lab` without a port) and
press «Прошить всех по Wi-Fi» (flash everybody over Wi-Fi) on the panel, or run
`python tools/ota.py all`. The server serves the image over HTTP (`/fw/prime_lab.bin`) and sends each
agent an `OTA …` command signed with the key `secrets/ota.key` (HMAC-SHA256; the key is created by
`tools/gen_secrets.py` during the build, never goes into git and is built into the firmware — without
it the firmware cannot be replaced over the network; the command number grows, an old command cannot
be replayed). The agent stops computing, downloads the image into the free partition (the board has
two 3.3 MB partitions), checks length and MD5 and reboots. Until the agent has been online with the
server for a minute, the firmware "awaits confirmation": if it crashes or does not come online within
10 minutes, the bootloader restores the previous one. A new image cannot be flashed over an
unconfirmed one. The first firmware with OTA has to be flashed over USB.

**Power.** On the XIAO the 5V pin is tied to USB VBUS. When boards are powered from a common 5 V bus,
put a Schottky diode (1N5819, SS34) in series with +5 V of every board (anode to the bus, cathode to
the board's 5V pin); ground goes straight, without a diode. Otherwise a board plugged into a computer
while on the bus connects two 5 V sources — that is how boards were burned. Set the bus to about
5.2 V so the board gets ~4.8–4.9 V after the diode.

---

## 12. Measurements (one board, 256 bits unless stated otherwise)

| what | result |
|---|---|
| RNG | 2300 candidates/s, ~27 primes/s, 36 ms per prime |
| CSI / ADC / jitter | 12 / 18 / 15 candidates/s |
| 1024 / 2048 bits, RNG | 185 / 30 candidates/s; one base check 25 / 181 ms |
| two boards on their own | 52.8 primes/s together |
| two boards cooperatively | 7.3 primes/s (7.2 times less), ~105 cycles/s, cycle 9.5–10 ms, synchronisation 68–70 % |
| cooperative coherence | a minute without a single failure (session #1: 6537 cycles in a row) |
| three boards on their own | 77.3 primes/s (25.8 each — like one) |
| three boards cooperatively, 24 Mbit/s | 4.6 primes/s, 64 cycles/s, cycle 17 ms, synchronisation 84 % |
| cooperatively, protocol v3 (2026-10-04): 1 / 2 / 3 / 4 boards | 24.5 / 16.5 / 10.5 / 7.7 primes/s; 330 / 216 / 161 / 127 cycles/s |
| four boards on their own | 100–106 primes/s (25–27 each) |
| four boards on their own, **two cores** (`dc` = 1) | **179.6 primes/s** (41–47 each, 1.78 times more); 4100 candidates/s per board; chip +4–5 °C within a minute, #5 hit 70 °C (work 80 %) |
| four boards, two cores, all bases on every candidate | 54.5 primes/s — limited by the check (accelerator) |
| four cooperatively after replacing #2 with #6 | 9.1 primes/s, 131 cycles/s, 3 breaks in 40 s (up to 27 with #2) |
| two groups of two at once | 16–17 primes/s each — they do not disturb each other |
| four cooperatively: 512 / 1024 bits | 3.2 / 0.57 primes/s; cycle 11 / 33 ms |
| four cooperatively, noise only CSI / ADC / jitter / all four | 0.50 / 0.82 / 0.65 / 0.31 primes/s; cycle 90–210 ms — waiting for noise |
| four cooperatively: K = 4 · no pieces in advance · 6 Mbit/s · 2 dBm · 80 MHz · no trial division | 5.2 · 6.2 · 5.1 · 7.5 · 5.9 · 1.2 primes/s (normally 7.7) |
| four, each on its own, all bases on every candidate | 45 primes/s (11 each) |

---

## 12a. Experiment board (`/plan`) and report (`/report`)

**`/plan` — the experiment board** («планшетка»). An experiment is defined by layers, not by
parameter keys:

1. **wheel role** — generation / absorption / stands;
2. **way** — generation "selfish" (every agent from its own noise) or "group" (noise from everybody,
   own numbers from the common noise — the cascade; "identical numbers for everybody" will come
   later); absorption "all with all" ("one to one" — later);
3. **noise** — chip RNG, all available, CSI from the heartbeat, CSI from the router ping, ADC D0,
   RC jitter, back-noise of the absorber mixed in / only it; CSI from the whole air, accelerometer,
   magnetometer — later; chip temperature — rejected (see § 3). The ⓘ button lists where each noise
   comes from;
4. **coil field** of wheels 1 and 2 — off / + / − (switching will come with DRV8871 bridges; for now
   the field state is recorded in the step);
5. **step** — duration, telemetry silence, step on the agent, Wi-Fi off, temperature limit, rest,
   repeats, random order, control step "everybody stands" (only static sensors measure), noise
   baseline before the start.

The selected options multiply into a **matrix** → a list of steps (controls can be marked by hand,
extra ones removed) → a program ([program.md](../program.md)). A step may carry **several groups**
(wheel 1 generates with its group, wheel 2 with its own, or one generates and the other absorbs),
each with its own settings number; agents outside the groups of a step stand.

**One main window, no scrolling** (checked at 1600×900 and 1366×768): at the top — link status,
the running program with a timer, and buttons; in the middle three columns — wheel 1 (sub-groups as
chips: state icon, number, °C with a bar up to the limit, А/М — accelerometer/magnetometer; layers
1–3), the rig (drawing: two wheels with agents on the rim grouped by sub-group, static sensors between
them, coil state, rotation speed and how much is needed for weightlessness at the top,
k = ω²r/g = 1, i.e. 44.6 rpm at r = 450 mm; "now": step timer, primes per step and per program,
temperature, static verdict; layer 4 — field; layer 5 summary line) and wheel 2; at the bottom — the
step ribbon: "plan" (what will be launched, ＋ step / ⇊ whole matrix) or "program #…" (running or
done). Everything detailed opens in pop-up windows: agent (click a chip or a circle in the drawing:
wheel, sub-group, temperature, link, memory, sensors, "find"), composition, steps (order, control,
remove), experiment (step, temperature, rest, repeats), diagnostics, firmware, noise origins; Esc
closes. The report opens in a separate window. States are always an icon plus a word, never colour
alone (Okabe–Ito palette): ▶ computes · ▼ absorbs · ⏸ stands · ◇ silent · ◌ without radio ·
✔ handed in totals · ✗ offline.

**Sub-groups on a wheel.** Every agent on a wheel has a sub-group letter (а–з; stored on the server,
`compose.sub`, empty = «а»). In a step every sub-group computes as its own group — 1, 2 … up to 16
agents (firmware limit `MAX_GROUP`), different sizes allowed; absorbers of the other wheel join the
group with the same letter, otherwise they are distributed round-robin. Buttons split a wheel evenly
into 1–4 groups by board number. Before a launch the ESP-NOW limit (20 peers per agent) and the set
limit (16) are checked.

**Find a board**: "find" on an agent → `POST /api/ident {mac, secs}` → the agent gets `IDENT s=20`
and its LED blinks 5 times a second for 20 s (on top of the telemetry flashes; `s=0` — stop). The
"◉ find board" mode makes a click on any agent do this. This is how you tell which physical board has
which number.

**Installation diagnostics**: once agents are on the wheels and split into sub-groups — "check the
installation": a one-step program in which every sub-group computes cooperatively (cascade from the
RNG; a single agent on its own) for 0.5–10 min, then a table per agent: state, link to the server
(signal, loss), which members of its group it hears via the heartbeat, primes/s versus the group
median, temperature and work fraction, memory, sensors, verdict ✓ / ◐ (remarks in words) / ✗
(faults).

**Temperature**: "heat up to" (tmax 40–70 °C, sent in the settings of every step; the agent's
regulator keeps 1 ° below) and rest before a step: none / fixed minutes / until the temperature is
stable / **until cooled down to T °C** (`rest: "below"`, `rest_below`), the last two with "at most N
min" (`rest_max_s`).

**Step on the agent** (firmware with `dur`/`ro`): the duration goes into every agent's settings
(`dur` = step + settling time); the agent stops computing by itself, restores telemetry and hands in
its totals (`done`); the server waits until "everybody handed in" (at most the deadline + a minute;
whoever did not — the step is spoiled, "did not hand in totals"); after `done` the server remembers
the agent as standing and does not resend it the settings (a resend would restart counting). "Wi-Fi
off during the step if the air is not needed" (`ro`) — the agent switches its radio off entirely for
a selfish step without absorption and without air noise; ESP-NOW is brought up again afterwards. The
chip RNG is weaker without the radio — no receiver noise is mixed in; keep that in mind when comparing
steps with and without radio.

**`/report` — the report**: the selected program step by step — layers in words, start, duration,
primes per step, primes/s in total and per agent with temperature, static verdict, scan (the largest
t of the step with its probability and the number of metrics), verdict (✓ / anomaly / spoiled); CSV
export; anomalies, confirmation runs and verdicts at the bottom.

## 12b. Two more screens: console and watch

`/pult` — control (agents by wheels, groups and modes, program "plan vs. actual", experiment log
with statics, telemetry of the whole rig); `/watch` — results in real time (spectra of the static
sensors against their baseline, a statistical guard over the primes, experiment timeline). Details in
[ui_concept.md](../ui_concept.md). The old panel (`/`) remains as the detailed view.

## 13. Tools

| file | what it does |
|---|---|
| `tools/build.sh <sketch> [port]` | build with the version from git, add the board to the registry, flash (the path to `arduino-cli` is set in the script — `CLI=`) |
| `tools/new_board.py [--all] [--list]`, `tools/new_board.cmd` | first flashing of new boards over USB: find them on USB (serial number = MAC), add to the registry, build once, flash each; the same as the panel button |
| `tools/monitor.cmd` | start the server |
| `tools/serial_capture.py` | board log over USB with timestamps, reset |
| `tools/count_boards.ps1` | which boards Windows sees: ports and MACs |
| `tools/register.py` | add a board to the registry |
| `tools/group_test.py` | comparison: on their own, then cooperatively |
| `tools/group_watch.py` | watch a group in 20 s windows |
| `tools/multi_test.py [seconds] [cases]` | run agents through compositions and modes with counter checks; report in `data/tests/` |
| `tools/ota.py [all \| numbers] [--force]` | flash agents over Wi-Fi with the latest build and wait for the new version |
| `tools/prime_stats.py [date]` | statistics of the prime log: checks, repeats, residues, bits (overall and per agent), coupling of neighbouring bits, gaps |
| `tools/anomaly_summary.py report.json …` | summary table of anomaly runs for the documentation |
| `tools/sprint.py [seconds] [modes]` | sprint: every mode from rest at 240 MHz, measured from the 15th second; rest until the temperature is stable between modes |
| `tools/rest_ab.py [minutes] [cascade\|relay] [off,on]` | resting in turns "without" and "with" |
| `tools/anomaly_run.py [min] [steps \| hash \| raw \| vn] [--stop] [--rest]` | step-by-step run with the prime log: trial division and yield versus theory, the prime stream, repeats across all logs — anomaly search |
| `tools/soak.py <minutes> [key=value …]` | long run of one setting with a log of temperature, work fraction and rate every 15 s |
| `tools/nightly.py [minutes per step] [--steps=…] [--only-compare]` | evening "workout": the same set of short runs, per board and step — primes/s, yield z, chip, work fraction, loss, reconnects; "who hears whom" matrix; rows accumulate in `data/nightly.jsonl` and are compared with the median of previous evenings — a board that degrades is visible before it spoils an experiment |
| `tools/runs_from_raw.py [date] [--force]` | rebuild the experiment log of a day from raw data |
| `tools/build_docs.py` | documentation in HTML: `docs/*.md` and `README.md` → `docs/html/`; `--static` — a set for showing outside the network in `build/site/` |
| `tools/sim_gen.py [agents] [--port 5016]` | simulated generator agents — test the pages without boards; with a separate server: `python server/monitor.py --http 8092 --udp 5016 --data data/test/gen --no-beacon` |
| `tools/gen_secrets.py` | Wi-Fi credentials and the OTA key (`secrets/`) → header for the firmware; called by the build |
| `tools/publish.py [--push]` | public copy: a snapshot of `main` as one commit on the `public` branch, after a leak check (network name and password, key, local addresses, e-mail) |
| `tools/gen_sweep.py` | sweep over sources and lengths |
| `tools/test_analysis.py`, `tools/test_static_watch.py` | checks of the wheel and static-sensor analysis on synthetic data |
| `tools/sim_agents.py` | simulated agents with sensors |
| `tools/syntax_check.py` | check the firmware with the compiler directly, without arduino-cli |
| `tools/make_gccwrap.py` | compiler wrappers with retry — if Windows refuses to start it |
| `tools/csi_watch.py` | CSI stream over time windows |
| `firmware/entropy_probe` + `tools/probe_report.py` | measurement of the noise sources |

---

## 14. Limitations and what next

* Cooperative is slower than individual: everybody checks one number, whereas individually each
  checks its own. Protocol version 2 was 7 times slower; version 3 removed collection from the cycle
  (stock, pieces in advance) and one transmission (the number travels with the result).
* A base check through mbedtls for 256 bits takes about 1 ms, and it is the bottleneck of the
  two-core pipeline: the compute core waits for the hardware multiplier half of the time (core load
  53 % with a full candidate queue). A reserve is software Montgomery arithmetic on both cores (no
  multiplier needed); the ceiling is still set by the RNG rate: 45,000 words/s — 5600 candidates of
  256 bits per second, now 4100.
* Temperature sensors of different boards differ by a few degrees.
* Not yet connected but usable as external sources: the ICM-42688 accelerometer and the MLX90393
  magnetometer on the rim, the camera and microphone of the Sense board, a Geiger counter.
* Coming next in the firmware: a control agent per wheel (coil polarity via DRV8871, current and
  voltage via INA219, bus power), sensors as noise sources, CSI from the whole air, one-to-one
  absorption, "identical numbers" group variant.
