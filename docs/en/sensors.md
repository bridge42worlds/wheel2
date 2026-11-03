# Sensors, phone, motors and coils

*English translation of [docs/sensors.md](../sensors.md) — the Russian original is the source of truth. Labels
inside the diagrams are in Russian.*

The sensor code is shared, in the library `firmware/libraries/agent_sensors`. The wheel algorithm (rpm,
weightlessness, curve shape) runs in the firmware (`wheel.cpp`) and line for line in Python (`server/analysis.py`),
where it is tested on synthetic data (`python tools/test_analysis.py`); change it in both places. The static
accelerometers compute nothing themselves: they send their axes and the server computes —
[static_watch.md](static_watch.md). Wiring of the sensors, wheels, coils and motors — [wiring.md](wiring.md).

All sensors are on the agent's I²C: **D4 — SDA, D5 — SCL**, power 3V3 and GND. At start the firmware itself looks
for what is connected and reports in the telemetry the sensor's address, its ID (the ICM-42688 answers 0x47) and the
modes **read back from the registers**, not the ones it meant to write.

## Rim sensor: ICM-42688

Searched for at addresses 0x68 and 0x69. Settings: ±16 g (at 2 rev/s at the bottom 8.25 g), ±2000 °/s
(2 rev/s = 720 °/s), output 200 Hz, bandwidth 40 Hz, low-noise mode. It is read by its own low-priority task on the
radio core — the compute core and the group protocol are not delayed.

**What is computed per revolution** (`WheelTracker`):

| quantity | how |
|---|---|
| wheel axis | the direction of the smoothed gyro vector in the sensor axes: there is no need to know how the board lies in the tube |
| revolutions | the angle around the axis, ∫ω dt / (1 + gyro scale correction); a full revolution is 2π |
| k = ω²r/g | the magnitude of the mean acceleration over a revolution: gravity averages to zero over a revolution, the centripetal part remains. **k = 1 — weightlessness at the top point** |
| \|f\| at the top and bottom | the smallest and largest over a revolution; at k = 1 — about 0 and about 2 g |
| radius | r = k·g/ω² from the measurement; the radius from the settings ("rotation sensor radius", 450 mm) — for comparison and while there is no measurement |
| curve shape | k from the shape, gravity phase, gyro correction, second harmonic, mean along the axes — see "Calibration" below |
| wheel axes | "towards the centre", "along the motion", "along the axis"; along the axis should be about zero, the rest is runout and vibration |

Weightlessness at r = 450 mm is 44.6 rpm; the "neighbourhood" on the screen is k within ±5 % (43.5–45.7 rpm).

**Packets** — once per telemetry period, from one snapshot:

* `imu` — revolutions since last time (up to 6), the current speed, the wheel axes, 20 readings in the wheel axes
  (20 Hz);
* `imr` — the same 20 moments as raw sensor samples (acceleration and gyro), for recording;
* `imc` — the curve shape of the same revolutions: k from the shape, the constant part of |f|², the gravity phase, the
  remaining gyro error, the scale correction, the second harmonic, the mean acceleration along the sensor axes,
  whether the revolution was even. The server matches it with the `imu` revolutions by agent time.

## Rim sensor calibration

![Rim sensor calibration: why |f|² over a revolution is a sinusoid in the gravity angle, the shift of the gravity phase as a gyro scale error, the radius and zero offset from the slope of the mean acceleration against ω², the tachometer check with one factor, and what goes where](../img/wheel_calibration.svg)

The window «⊙ Калибровка колёс…» (wheel calibration) is in the ☰ menu (there is a button in «⟳ Моторы вручную…» too,
and «📏 задать…» (set…) in the agent window). For each wheel it has three parts and the agent radii. Firmware without
the `imc` packet — «◐ прошивка без формы кривой — обновить по воздуху» (firmware without curve shape — update over the
air).

**1. Curve shape and gyro scale.** |f| over a revolution is not a sinusoid (at k = 1 it is a "tick" down to zero at the
top), but its square is an exact sinusoid in the gravity angle φ in the sensor axes:
**|f|²/g² = 1 + k² + 2k·cos φ**. Over a revolution |f|² is decomposed by the gyro angle (weighted by the angle
covered, so acceleration within a revolution does not interfere):

* **k from the shape** — half the first harmonic; the constant part must be 1 + k² (accelerometer scale). k from the
  shape and k from the mean do not have to agree: the accelerometer zero offset enters them differently.
* **Second harmonic relative to the first** — about 0 for an even wheel, grows with runout, jerks, sensor offset: below
  0.05 — «✓ форма чистая» (clean shape), up to 0.2 — «◐ колесо бьёт» (wheel wobbles), above — «✗ форма грязная»
  (dirty shape).
* **Gravity phase.** Gravity makes exactly one turn per wheel revolution, so its shift from revolution to revolution,
  divided by 2π, is the gyro scale error. The tracker adjusts the scale by itself on **even revolutions**: speed equal
  to the previous ±0.5 %, gravity visible (k ≥ 0.05), second harmonic below 0.2. For an even revolution 0.2 of the
  error found is applied; the correction is no more than ±5 %. With the right correction the revolutions are correct
  and gravity averages to zero over a revolution (with a 1.5 % error about 15 mg would remain on average — the radius
  and zero offset could not be found). The window shows the correction and what remains — the median and spread over
  the last 30 even revolutions (at least 5 needed).

**2. Radius and zero offset — from the slope.** The mean acceleration over a revolution in the sensor axes is
m = (ω²r/g)·"towards the centre" + b. For each axis — a line against ω²: the slopes of the three axes form the vector
(r/g)·"towards the centre", its length × g is the radius; the intercepts are the zero offset b, mg; the scatter around
the line is also in mg. Revolutions of the last 30 min are taken — even ones, with the adjustment converged (remainder
below 0.2 %), at least 6, at 2–3 speeds spread at least ±15 % (for example 35 · 44.6 · 55 rpm). The window compares this
radius with the radius measured with a ruler.

The switch **«радиус для невесомости»** (radius for weightlessness): «медиана оборотов» (median of revolutions) — the
median of r = k·g/ω² of the last 5 revolutions (no older than 15 s; the zero offset enters the radius) or «по наклону —
без смещения нуля» (from the slope — without zero offset) — the radius from the slope, the mean over the wheel's rim
sensors. The motors take this radius in "weightlessness" mode; with no measurement — the radius from the settings.

**3. Tachometer** — an independent check: optical, on a reflective mark. The wheel runs at even revolutions, the
tachometer points at the mark, the reading goes into the "tachometer" field → «записать точку» (record a point). The
server pairs it with the mean rim rpm over the last 6 s — over whole revolutions, without the tachometer correction;
«✓ ровно — можно записывать» (steady — you can record) when the revolutions of the window differ by no more than 1 %
and the current speed is the same ±0.5 %. The correction is one factor through zero:
c = Σ tachometer·rim / Σ rim² (tachometer = c · rim) and the scatter of the points around it, %. Up to 40 points,
1–300 rpm; best 3 at different speeds. **«Применять: моторы и показ ×c»** (apply: motors and display ×c) — the wheel
rpm for the motors, the screen and the step record is multiplied by c, and the radius from the rim is divided by c²
(it is computed from the gyro ω: r = k·g/ω²), so weightlessness stays where k = 1. Without points "apply" cannot be
switched on; «очистить точки» (clear points) also switches off the correction.

**Agent radii — with a ruler**, from the axis to the chip of each agent of the wheel, 100–2000 mm («сохранить радиусы»
(save radii) in the calibration window). Each agent has its own k_i = ω²r_i/g at the current wheel rpm (with the
tachometer correction, if applied): in the calibration window, in the agent window («радиус … мм линейкой · k = …»),
in the hover popup and in the record of every step (`radius` and `k` — from the mean rpm of the run; with the
correction — also `rpm_tach`). k within ±1 % of 1 — «✓ невесомость наверху» (weightlessness at the top), otherwise
"short" or "over". Weightlessness exactly at the top applies to the agent whose radius the rpm is held by; for the
others — the "± from weightlessness" steps in the experiment ([modes.md](modes.md)).

Where it is stored: tachometer points and switches — `data/rim_cal.json`; agent radii (with change history) —
`data/rig.json`. API: `GET /api/rimcal` — the whole window; `POST /api/rimcal` — `{wheel, tach}` (a point),
`{wheel, del: i}`, `{wheel, clear}`, `{wheel, tach_on, fit_on}`; `POST /api/rig` — `{radius: {mac: mm}}`.

**Test on synthetic data** (`python tools/test_analysis.py`, wheel r = 450 mm):

| case | what was found |
|---|---|
| speeding up 30 → 60 rpm, instruments correct, sensor rotated at random | k at weightlessness 1.004 (true 1.002), radius 450.7 mm, \|f\| at the top 0.006 g, wheel axis — exact |
| steady 44.6 rpm | k from the mean 1.0006, from the shape 1.0004 (true 1.0010), second harmonic 0.0006 |
| gyro +1.5 %, zero offset (20, −15, 10) mg, steps 30 · 38 · 44.6 · 52 rpm, forward and backward | gyro correction +1.44 and +1.47 %, remainder 0; rpm at weightlessness — error 0.005 %; radius from the slope 450.6 and 450.1 mm; zero offset — within 1.3 mg on each axis |
| uneven wheel: the same plus 20 mg noise, vibration at 23 and 61 Hz of 50 mg each, speed ripple ±0.3 %, drift ±0.1 % | correction +1.51 %, rpm — error 0.22 %, radius 450.5 mm, zero offset — within 1.4 mg, scatter around the line 3.1 mg |

## Magnetic field: MLX90393

Searched for at addresses 0x0C–0x13 and 0x18–0x1B. 20 measurements a second. The coil current is not known in advance,
so **the gain is chosen automatically**: if a reading approaches the edge of the range — lower gain, too small — higher.
The field is always in microtesla from the sensitivity table of the current gain (from 0.15 µT per count with a range
of about ±5 mT to 0.75 µT with a range of about ±50 mT).

Inside the toroid winding the coil field is directed along the tube: at agent №20 in the wheel 1 tube — 1.20–1.26 mT at
a current of about 0.9 A ([coil_test.md](coil_test.md)). The Earth's field (about 50 µT) rotates in the agent's axes
once per revolution. In the gaps of the winding the field is weaker and turns; how non-uniform it is inside the tube and
how much leaks outside — the [toroid field](../toroid_field.html) calculator (Russian).

**Packet** `mag`: the mean over the period along the axes, the magnitude (mean, smallest, largest), the gain, 20
individual measurements.

## Static accelerometer (role S of the same firmware)

An agent in role S only measures and sends the x, y, z axes 500 times a second; the spectrum, the noise baseline,
alarms and the verdict "continue / repeat / check" are computed by the server — [static_watch.md](static_watch.md).

* **ICM-42688** motionless: 1000 Hz through the FIFO, averaged over 2.
* **ADXL355** (address 0x1D — ASEL to GND, or 0x53) — a precision accelerometer, noise per datasheet 25 µg/√Hz (the
  ICM-42688 has 65–70). If it is on the bus, role S measures with it: it is searched for before the ICM-42688,
  recognised by registers 0x00–0x02 = 0xAD, 0x1D, 0xED; bus 400 kHz. Range ±2 g; the sensor's output is directly
  1000/avg Hz with its own filter (a quarter of the rate; avg 2 → 500 Hz); the sensor FIFO holds 32 triples with an
  x-axis marker and is collected every 10 ms; the axes are 20-bit, shifted into int16 in the packet (16000 per g, step
  62.5 µg — below the noise). The registers are read back: FILTER 03, RANGE 01, POWER_CTL 00. The server recognises the
  sensor by counts per g (16000 / 8000 / 4000).
* Measured on the same table: the ADXL355's own noise is 15–20 µg/√Hz against 61–80 for the ICM-42688, the accuracy of
  the mean over 1 s — 0.014–0.017 mg against 0.06–0.10; a single sample is the same for both — it is set by the table's
  vibration.

## The phone (app `phone/`, version 1.1)

The Android phone №9 is one more static detector. The app «Колесо-2 датчики» (Wheel-2 sensors; Kotlin, no third-party
libraries) takes only what the rig needs:

* **uncalibrated accelerometer** (the system does not adjust the zero offset; on a Galaxy A26 — ~500 Hz, ±8 g, step
  0.0024 m/s² = 1/4096 g) — `acc` packets like a static agent's (int16, 4096 per g): on the server "live", the noise
  baseline and alarms work;
* **uncalibrated magnetometer**, µT — `mag` packets with their own rate (`hz`), multiplier (`div` = 100) and step
  (`step`): «🧲 поле вживую» (field live) on the main screen;
* **background** in `tel` (`ph`): mains 50 Hz in the field (Goertzel amplitude over 2 s — visible if the magnetometer is
  faster than 105 Hz), field oscillations over 2 s, Wi-Fi and cellular signal, battery (charge, temperature, whether
  charging).

Android does not give apps the raw radio spectrum — only the signal strength the radio modules hear. The phone has one
screen: link, accelerometer and magnetometer (values, spread over 1 s, range over 10 s as x / y / z bars), background.

**With the screen off.** A foreground service (type health — sensors faster than 200 Hz) with an icon in the
notification shade measures and sends: the CPU does not fall asleep (PARTIAL_WAKE_LOCK), Wi-Fi does not go into power
saving (WifiLock). A service killed by the system comes back up, and after a phone reboot it starts by itself (unless
«■ стоп» (stop) was pressed). At the bottom of the screen: «■ стоп / ▶ мерить» (stop / measure), «☾ экран гаснет сам /
☀ не гаснет / ◐ притушен» (the screen turns off by itself / stays on / dimmed — a lit screen means extra pickup) and,
until it is allowed, «🔋 без ограничений» (no restrictions): without it Android in deep sleep may cut a motionless phone
without a charger off the network. On Samsung also: Settings → Apps → «Колесо-2 датчики» → Battery → "Unrestricted".
Screen off for 40 s (checked over USB): accelerometer 500 Hz, packets every second, no losses.

**Link.** Packets as lines — to `POST /api/ingest` on the server's port 8090, in batches 4 times a second; parsing is
the same as for UDP. The phone first tries the USB cable (every 15 s the server runs `adb reverse tcp:8090` if adb is
present), otherwise Wi-Fi to the address from the server's beacon; if the address changes it takes the new one from the
beacon. Field measurements carry the number of the first one and the time by the sensor's clock (`i0`, `mt0`, `mt1`,
`ms`): what accumulated during a link break the server puts in its places, repeats are dropped; overflow of the field
ring buffer — `mlost` in `tel`. For the server the phone is a role S agent with an address of the form `02:…` (from
ANDROID_ID), `dev: "phone"`; agent settings are not sent to it. The version is in `fw` (`phone-1.1`); when building,
raise `versionName` and `BuildInfo.VERSION` together. Build and install:

    cd phone; .\gradlew.bat assembleDebug
    C:\Android\Sdk\platform-tools\adb.exe install -r app\build\outputs\apk\debug\app-debug.apk

## On the main screen

* **The wheel column** — from the rim sensor: the current rpm, how much is needed for weightlessness, «✓ невесомость
  наверху» / «◐ недобор, k …» / «◐ перебор, k …» (weightlessness at the top / short / over); in the picture — the rpm
  with the direction inside the wheel.
* **The agent window** with a sensor: «∿ вживую» (live; accelerometer: oscillations, Gaussian, spectrum), «🧲 поле
  вживую» (field live; MLX90393 or the phone: |B| and axes, spectrum, field steps), «шум и точность датчика» (sensor
  noise and accuracy), radius by ruler and k.
* **Static sensors** — the card under the picture: the verdict, live Gaussian, spectrum and oscillations of the selected
  sensor; in detail — the window «◆ статика подробно» ([static_watch.md](static_watch.md)).

## Testing without boards

`python tools/sim_agents.py [seconds] --port 5015` — a simulation in the same formats as the firmware: an agent with an
ICM-42688 on a wheel r = 450 mm (rpm in steps 35 → 40 → 44.6 → 50 → 55 → 50 → 44.6 → 40 of 30 s each, the gyro is off
by +1.2 %, the accelerometer has a zero offset of (12, −8, 15) mg — there is something to calibrate), an agent with an
MLX90393, three static accelerometers with knocks, a line and a fault on a schedule; `--gens N` — more generators,
`--mot` — the motor agent and coil agents. Run it with a separate server — the data and commands will not mix with the
real ones:

    python server/monitor.py --http 8091 --udp 5015 --data data/test/sim --no-beacon

## Wheel motors (role M)

Two brushed 36GP-555 motors (12 V, planetary gearbox 112 rpm, no encoder) turn the wheels; the motor agent №10 is a
XIAO board in role M on the fixed frame, two DRV8871 bridges: wheel 1 — D0, D1, wheel 2 — D2, D3 (wiring —
[wiring.md](wiring.md)). PWM 20 kHz, slow decay; speeding up and braking are smooth; the direction changes only through
a stop and a 1 s pause; watchdog — no MOT command for 5 s, the motors stop smoothly. The server (`server/motors.py`)
sends `MOT a=‰ b=‰ r=ramp w=watchdog` once a second.

The window «⟳ Моторы вручную…» (motors by hand): the wheels separately / together / towards each other / only 1 / only 2;
wheel mode — standing / PWM / rpm / **weightlessness** (the rpm at which k = ω²r/g = 1; the radius — from the rim
sensor, see "Calibration", with no measurement — from the settings); direction change by timer. In the "rpm" and
"weightlessness" modes PWM = feed-forward + PI on the rim rpm (with the tachometer correction, if applied).
«■ Всё стоп» (all stop) stops the motors too. To assign a board: agent window → «→ в моторы» (to motors). In an
experiment rotation is set for every run ([modes.md](modes.md), [program.md](program.md)); at the end of a program the
motors stop.

* The rpm feedback is switched on only when the rim shows at least 60 % of the target: while speeding up and braking
  before a direction change the integrator does not accumulate error (overshoot on synthetic data 1–6 %); when it hits
  the PWM limit it does not accumulate either.
* «✓ невесомость наверху» at the motors is by the measured k (per revolution; with the "from the slope" radius — by it),
  not by the radius from the settings.
* The direction change timer ("in N min") counts from the start of motion: set for a standing wheel, it gives the first
  change after N minutes of running.
* The motor and coil agents do not take part in telemetry "silence": the server sends commands only to those it hears,
  and a link break longer than 10 s would take them out of control.
* "Forward ▶" is **clockwise when looking at the shaft from the front**; for a mirrored motor swap its OUT1 / OUT2
  ([wiring.md](wiring.md)).

## Wheel coils (role M, place "coil")

A coil agent is the same firmware in role M, assigned to the "coil" place of its wheel (agent window → «катушка колеса
1 / 2», coil of wheel 1 / 2); wheel 1 — №13. A DRV8871 bridge from 5.1 V, D0 → IN1, D1 → IN2, the coil on OUT1 / OUT2 —
[wiring.md](wiring.md). The server (`server/coils.py`) sends `MOT a=±level b=0 r=5000 w=5` once a second: the sign is
the field direction, level 1000 is a constant current; a change of sign goes through a stop and a 1 s pause; no command
for 5 s — the agent removes the current. A wheel may have several coil agents — the command goes to each one.

The field is set by the experiment's run (parameter "coil field", [modes.md](modes.md)), by hand — the buttons "off / + /
−" under the wheel on the diagram; at rest, at the end of a program, on «■ Всё стоп» and after a server restart — off.
The coil agent does not belong to the wheel's groups, steps and subgroups: the server and the screen recognise it by its
place, even when it is silent.

**Field from the sensor.** The wheel magnetometer sends the mean field once a second. Seconds when the coil is certainly
off and the readings are steady give the background (the Earth, iron nearby), seconds with "+" — the direction of the
coil field at the sensor. Readings minus background, projected onto "+", are the field with a sign: one can see whether
there is a field, where it points and whether it matches the command; in the step record — `coil_sense`. If the sensor
is moved, the background and direction are relearned within a few seconds.

**Alternating field.** The same coil, but the current changes direction smoothly: ∿ sine or ⊓ soft square (a
rectangle whose edges are half-waves of a sine, 100 ms each), frequency 0.1–50 Hz. The command gets `fa=` — frequency
in mHz, `ka=` — shape (0 sine, 1 square), `ea=` — edge in ms and `qa=` — the phase by the server clock in thousandths of
a period (DC — `fa=0`). The agent drives an envelope: the amplitude rises and falls at rate `r`, and the server sets it
so that full amplitude is reached in 3 periods, 1 to 5 s; a change of kind (DC ↔ AC, another shape) goes through zero.
On AC the bridge is updated 1000 times a second (20 points per period at 50 Hz), the PWM is the same 20 kHz. "+" and
"−" are phase 0 and 180°: up to 5 Hz the agent smoothly adjusts its phase to the server clock, so two coils on "+" run in
one phase and "+ −" in antiphase, to within the network delay (a few ms); above 5 Hz each runs on its own. In the bridge
telemetry — `aca`: [frequency mHz, shape, edge ms, phase ‰, phase error against the server ‰].

Why it does no harm and what to keep in mind. The coil is about 1 mH at 5 Ω (an estimate): the time constant is ~0.2 ms,
up to ~100 Hz the current keeps up with the voltage, and at 1 A it stores ~0.5 mJ — the bridge holds that even on an
abrupt switch; what limits is current and winding heat. A sine of the same amplitude heats the winding and drains the
battery half as much as DC. The coil wires vibrate at the field frequency and at twice it (force ~ current²) — in runs
with AC the server counts these lines with their harmonics as known in the static spectrum
([static_watch.md](static_watch.md)). The wheel magnetometer measures a one-second average — it sees AC by the spread of
|B| within the second (`coil_sense.seen` = "~", `spread`); background and the "+" direction are not learned on AC. The
coil INA219 on AC shows the momentary current, not the amplitude. A firmware without the alternating field would not
understand the command and run DC — the server refuses it ("update over Wi‑Fi").

It is set by the experiment window layer **∿ род поля** (kind of field: DC, = 70 % · PWM, ∿ 0.5 / 1 / 5 / 17 Hz,
⊓ 1 Hz, your own frequency or level) or by hand — the "∿" button under the wheel: kind of field in a cycle, the sign — by
the "+ / −" buttons.

**Frequencies.** Not multiples of 10 Hz: the rim magnetometer measures 20 times a second — at 20 Hz (and 40) the field
stands still for it, at 10 Hz it hits the same two points of the period; 50 Hz is also the mains in the static spectrum.
So the set has 17 Hz, and the window warns about your own frequency if it is a multiple of 10.

**The "= 70 % · PWM" control.** At full DC level the bridge is simply open — no switching; on AC it switches 20,000 times
a second all the time. PWM edges can reach the high-impedance inputs of the agents (common ground, winding capacitance)
— a comparison "AC against DC" would mix the effect of the field and the effect of switching. DC at 70 % level: PWM is
there, the field does not change (and the power is the same as a full-amplitude sine) — with it one can see what comes
from the changing field and what from switching. In the step — `coil.level` (‰); without it — full level.

**What the field does to the agents' noise.** The rim magnetometer (source 🧲) sees the field directly: in the tube
~1.3–1.4 mT — about 9000 counts at the most sensitive gain (limit ±4.9 mT on x, y, the threshold for a coarser gain is
28,000), the gain does not switch, there is no clipping. The sensor noise in the low bits is the same — with the hash the
quality of numbers does not change, but without the hash the field directly changes the low bits (AC works as dither): a
difference "field on / off" in 🧲 is known physics, a control of the channel, not an anomaly. Pin D0 (source ⏚ ADC)
hardly sees the field: the induction in its loop even at 50 Hz and full field is of the order of 40 µV per 1 cm² against
an ADC step of ~0.8 mV; but it may see the 20 kHz PWM edges (~21,000 samples a second — folding to about 1 kHz), hence
the "= 70 % · PWM" control. We credit ¼ bit per ADC sample and 2 bits per magnetometer byte — interference does not
remove noise, what is credited stays honest.

**⟳ With rotation.** A sine of frequency k × wheel rotation (×1, ×2, your own multiplier 0.25–4): the coil turns with the
wheel, and the field becomes tied to the position in the room. The phase comes from the top mark: once per revolution
the rim agent finds the top point (smallest |f| — weightlessness is there too) and sends `top` [ms ago, period ms,
revolution number] — in the imu packet, and in silent runs (setting `rt`, the program sets it itself) as a separate short
packet once a second. The server puts the sine maximum at the top of the agent with the accelerometer; the field in the
tube is one for the whole rim, so the other agents have their own constant shift: the agent opposite passes the top at
the minimum (field of opposite sign), agents at the sides near zero. Up to 5 Hz the coil agent holds the phase by the
mark; with no marks (wheel stands, sensor silent) the field runs at the previous frequency without adjustment (on the
board «◐ метки верха нет»). Wheels standing — such a run is not put in the list.

**Does the alternating current radiate.** Hardly. The wavelength at 50 Hz is 6000 km, at 1 Hz 300,000 km, and the coil is
a metre: it is not an antenna but a "near field" that falls off as 1/r³ and does not go out. An estimate of the radiated
power of the single winding turn around the ring (1 A, area ~0.08 m²) at 50 Hz is about 10⁻²⁵ W, at 1 Hz 10⁻³² W. What
really goes out is the 20 kHz PWM edges along the wires (as with any motor driver), hence the "= 70 % · PWM" control.
