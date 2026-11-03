# Modes: what the rig does in each

*English translation of [docs/modes.md](../modes.md) — the Russian original is the source of truth. The screens
are in Russian; the on-screen word is given in «guillemets» where it helps to find it.*

All modes of Wheel-2 in the words of the main screen: what happens, what to look at, typical figures
(XIAO ESP32-S3, 256 bits, 240 MHz, limit 73 °C). A run's mode is made of independent parts — these are the
run parameters of an experiment: **rotation** (speed, offset from weightlessness), **field**,
**emission / absorption**, **cooperation**, **absorption**, **noise**. How an experiment is built from them —
[program.md](program.md).

The rig: **wheel 1** and **wheel 2** with six agents each (on a wheel — subgroups а–з, each counts as its own
group in a run), **static sensors** between the wheels (accelerometer, phone), **coil agents** and the
**motor agent**. Roles: ▶ generation, ▼ absorption, ◆ static, ⚙ motors / coil.

## States of the rig

| state | what the generators do | static sensors | when |
|---|---|---|---|
| ⏸ **standing** («стоит») | not computing, cooling down: 80 MHz, LED off | measure | between runs, noise baseline, the run "all standing" |
| ▶ **run in progress** | each subgroup — by the run's mode | measure | a run |
| ⏸ **rest** («отдых») | standing at 80 MHz until the temperature settles | measure | before a run, if rest is set |
| ≋ **quench from the console** («тушение») | only scatter their own noise (placebo, background 50 frames/s) — not a single prime | measure | control "radio without numbers" |

## Emission / absorption: the role of a wheel

In the experiment window — a 3 × 3 grid: row — wheel 1, column — wheel 2 (stands · generates · absorbs);
▼▼, ▼⏸, ⏸▼ are not available — there is nothing to absorb.

| option | word on the screen | what to look at |
|---|---|---|
| ▶ ▶ | both generate — each wheel as its own group (or one group for both: «оба колеса одной группой») | influence of the neighbour |
| ▶ ⏸ / ⏸ ▶ | W1 only / W2 only | reference rate of one wheel, comparing the wheels |
| ▶ ▼ / ▼ ▶ | W2 absorbs / W1 absorbs the other's primes | the main run "emission — absorption", symmetry |
| ⏸ ⏸ | static only: nobody computes, rotation and field as in the run | what rotation and field do to the sensors by themselves |

## Absorption

Its own card in the experiment, two rows, every combination is tried. Details —
[scatter.md](scatter.md).

| who takes whose | what |
|---|---|
| all with all | every absorber takes the primes of all generators |
| pairs 1:1 | each absorber has its own generator by number; generators without a pair are a control inside the run |
| all — one | everyone absorbs the primes of the first generator; the rest are not absorbed |
| each — to one | the generators are divided between the absorbers: every prime is absorbed exactly once |
| half the wheel | the first half of the generators is absorbed by everyone, the second half by nobody |

| how | what |
|---|---|
| mix whole | each mixes the whole prime with its own noise and emits a frame |
| in pieces | each mixes its own piece of the number: no frame carries the whole number |
| relay | along a chain, the last one emits one frame |
| placebo («пустышка») | the same traffic, noise instead of the number — separates the effect of the number from the effect of the radio |
| listen only | receive the primes, emit nothing — control without traffic |

In pieces and relay are always "all with all". The console and the live diagram also have **background** — the
absorbers' own noise at a set rate.

## Cooperation: how the generators look for primes

| way | how it works | primes/s | what to look at |
|---|---|---|---|
| **selfish** | each gathers noise itself, builds a number, sieves, tests | ~26 per agent, two cores ~45 | yield against theory, sieve |
| **group · cascade** | numbers from noise pieces of all agents of the subgroup, everyone tests at once, each its own | 4 agents ~85–90, 6 — ~100–118 | share of rounds from pieces of all (coherence), heat |
| group · relay | a number from everyone's noise, one tests, the turn goes round | ~37 per group, boards run cooler | turns shared evenly, "silent" members |
| group · one number for all | one number from everyone's noise, everyone tests (bases divided), the answer by agreement | ~8–11 per group | agreement of answers, cycles without failures; a run is better at 60 s |
| group · identical numbers | everyone computes the same number | — | comes with a future firmware |

Details — [group_protocol.md](group_protocol.md).

## Noise: where the number comes from

| icon | noise | speed |
|---|---|---|
| ⚄ | the chip RNG — the hardware generator with Wi-Fi receiver noise | ~45 k words/s — the main one |
| Σ | all available: RNG + pulse CSI + ADC + RC jitter through SHA-256 | as fast as the slowest |
| ≋ | pulse CSI — the radio channel state from the neighbours' pulse frames | slow |
| ↯ | router ping CSI — the same from the router's ping replies | slow, use with care |
| ⏚ | ADC D0 — the floating input D0 | slow |
| ⧖ | RC jitter — the internal oscillator against the crystal | slow |
| ↺ | reverse noise — frames of the other wheel's absorbers, a loop through the air | only when the other wheel absorbs |
| ↕ 🧲 | rim accelerometer, magnetometer (agents with a sensor) | very slow |

**Noise is a mixture of sources.** In the experiment window noise is assembled from icons; "Σ all" ticks the four
main ones (RNG, CSI, ADC, RC). Those ticked together make one mixture. **How to vary** (several allowed):

| variation | options | 4 sources | 6 (+ acc., mag.) |
|---|---|---|---|
| all together | all ticked — one mixture | 1 | 1 |
| one at a time (inclusion) | each source alone — what each gives | 4 | 6 |
| all but one (exclusion) | the mixture without each in turn — what is missing without it | 4 | 6 |
| all three | the mixture, without each, each alone | 9 | 13 |

Without hashing (`raw`) the physics of the source is visible (the ADC gives 54–59 % ones), with von Neumann the bias is
removed. How noises are taken, credited and mixed — [randomness.md](randomness.md).

## Coil field

Each coil is off, + or −: a 3 × 3 grid (row — the wheel 1 coil, column — wheel 2), 9 options;
quick picks "off · same sign · opposite signs · one coil · all 9".

| option | word on the screen |
|---|---|
| off off | both off |
| + + / − − | same sign |
| + − / − + | opposite signs |
| + off, − off / off +, off − | W1 only / W2 only |

"+" and "−" are the current direction (OUT1 → OUT2 or back); the field inside the tube is 1.20–1.26 mT at 0.85–0.98 A
(measured, [coil_test.md](coil_test.md)). The coil agents switch the field on at the start of a run; at rest, on pause,
at the end of a program and on «■ Всё стоп» (all stop) — off. By hand — "off / + / −" under the wheel on the diagram.
Wiring — [wiring.md](wiring.md).

## Wheel rotation

Each wheel stands (■), goes forward (⟳ — clockwise when looking at the shaft from the front) or backward (⟲): a
3 × 3 grid, 9 options.

| option | word on the screen |
|---|---|
| ■■ | standing — motors off |
| ⟳⟳ / ⟲⟲ | together / together backward |
| ⟳⟲ / ⟲⟳ | towards each other |
| ⟳■ ⟲■ / ■⟳ ■⟲ | W1 only / W2 only |

**Speed:** rpm (PI control from the rim sensor), PWM (share of supply) or **weightlessness** — the rpm at which, at
the top of the wheel, k = ω²r/g = 1 (r from the rim sensor; at 450 mm — 44.6 rpm). **Offset from weightlessness** —
steps around the computed value: 0, ±1, ±2 steps, default step 3 % of rpm; k changes twice as much
(+3 % — k ≈ 1.06). Speeding up and changing direction are smooth, through a stop and a 1 s pause; watchdog —
no command for 5 s, the motors stop. The window «⟳ моторы вручную…» (motors by hand) spins the wheels outside an
experiment. Details — [sensors.md](sensors.md).

## Telemetry: how much the agents talk

| mode | generators | static sensors | what is visible |
|---|---|---|---|
| **normal** | a packet once a second | send samples | everything in real time |
| **sparse** | 10 times less often | send samples | totals, no detail over time |
| **silence** | nothing but command acknowledgements | send samples | static sensors only; totals — after the run |

Silence keeps the agents' own Wi-Fi frames from affecting the static sensors; in the experiment it is "telemetry
silence during the run" (⑥ fine points, on by default), and it applies only to the measurement. The bus sensors are
silent too: rim rpm and coil field are not visible during the measurement, the motor regulator holds the last PWM (in
"weightlessness" — also the last rim radius), and the rpm and field in the run record are taken from the check (10 s
before the silence) and the totals (after). "Wi-Fi off during the run" (with the run on the agent) removes the radio
entirely — only for selfish computing without absorption and without noise from the air.

## Related

* **Static sensors** — a motionless sensor measures 500 times a second, the server compares every 10 s with the noise
  baseline: ✓ continue · ⚠ check · ↻ repeat — [static_watch.md](static_watch.md).
* **Heat** — the regulator, rest before a run, sprint, agent rating — [thermal.md](thermal.md).
