# Bill of materials

*English translation of [docs/spec.md](../spec.md) — the Russian original is the source of truth.*

What the rig is built from and what it costs. Prices are in Kuwaiti dinars (KWD; 1 KWD ≈ 3.25 USD):

* **no mark** — as paid in AliExpress orders, shipping mostly free;
* **≈** — an estimate from an AliExpress search (2026-10-26): not in the orders — still to buy, or bought separately;
* **—** — bought locally or our own, no price.

The links go to product pages; prices and options there change. Where everything sits in the rig — the
[wiring diagrams](wiring.md).

## Total

| section | bought, KWD | estimate, KWD | total, KWD |
|---|---|---|---|
| Agents and sensors | 96.88 | — | 96.88 |
| Wheels and coils | 29.64 | ≈ 32.01 | 61.65 |
| Wheel power | 29.62 | ≈ 32.26 | 61.88 |
| Frame and motors | 34.08 | ≈ 3.09 | 37.17 |
| Experiment network (Wi-Fi) | 13.81 | — | 13.81 |
| Mounting and heat | 4.95 | — | 4.95 |
| Tools and consumables | 32.92 | — | 32.92 |
| **rig without tools** | **208.98** | **≈ 67.36** | **276.34** (≈ 898 USD) |
| **everything including tools** | **241.90** | **≈ 67.36** | **309.26** (≈ 1,005 USD) |

The laptop (server) and the Samsung Galaxy A26 phone (static sensor №9) are our own and are not included.

Still to buy: 18650 cells and their boxes, the missing heatsinks, DRV8833 — spare coil bridges. The copper wire and
some of the heatsinks were bought separately, their price is unknown — the estimate uses AliExpress as a guide.

## Agents and sensors

Agent boards and sensors: generators and absorbers on the wheels, coil and motor agents, static sensors.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| Seeed XIAO ESP32-S3 — agent | pack of 10 | 22.19 and 23.58 | 2 | 45.77 | [AliExpress](https://www.aliexpress.com/item/1005006987905528.html) | 20 boards: 12 on the wheels, coil, motors, static, spares |
| ICM-42688 accelerometer-gyroscope (purple board) | rim sensor, static | 5.69 | 2 | 11.38 | [AliExpress](https://www.aliexpress.com/item/1005011642881650.html) |  |
| MLX90393 magnetometer | pack of 2 | 4.98 | 1 | 4.98 | [AliExpress](https://www.aliexpress.com/item/1005012414990689.html) | field in the wheel tube |
| ADXL355 precision accelerometer | static at the centre of the axis | 23.82 | 1 | 23.82 | [AliExpress](https://www.aliexpress.com/item/1005009311859684.html) | noise 25 µg/√Hz |
| INA219 current meter | pack of 5 | 1.79 | 1 | 1.79 | [AliExpress](https://www.aliexpress.com/item/1005004939456588.html) | battery charge: one each for the bus and the coil of a wheel |
| Seeed XIAO ESP32-C3 | pack of 10 | 4.57 | 2 | 9.14 | [AliExpress](https://www.aliexpress.com/item/1005008791937363.html) | not used in the rig — spares |

Section: bought **96.88 KWD**.

## Wheels and coils

The rim is a silicone tube on an aluminium disc, the coil is wound on the tube; the coil bridge, agent heatsinks.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| Aluminium disc 6061 Ø300 × 3 mm | wheel base | 10.38 | 2 | 20.76 | [AliExpress](https://www.aliexpress.com/item/1005008038945720.html) |  |
| Food-grade silicone tube, ID 19 × OD 23 mm | rim — agents inside | 6.56 | 1 | 6.56 | [AliExpress](https://www.aliexpress.com/item/1005010025288570.html) |  |
| U-shaped PVC clips 25 mm | 20 pcs | 2.32 | 1 | 2.32 | [AliExpress](https://www.aliexpress.com/item/1005009762220367.html) | hold the tube to the disc |
| Enamelled copper wire 0.67 mm | ~200 m for two wheels (≈0.6 kg) | ≈ 19.18 | 1 | ≈ 19.18 | [AliExpress](https://www.aliexpress.com/item/1005003650119052.html) | bought separately; guide — a 1 kg spool |
| DRV8833 bridge — spare for the coils | 2.7–10.8 V | ≈ 0.76 | 2 | ≈ 1.52 | [AliExpress](https://www.aliexpress.com/item/1005009044264044.html) | the coils run on DRV8871 from 5.1 V (outside the rating); these are in case of failure |
| Aluminium heatsink 10 × 10 × 10 mm | 2 per agent: 24 pcs (3 packs of 10) | ≈ 3.77 | 3 | ≈ 11.31 | [AliExpress](https://www.aliexpress.com/item/1005004079109702.html) | some are already on the agents — check how many there are |

Section: bought **29.64 KWD**, to buy or estimate **≈ 32.01 KWD**.

## Wheel power

Two 2×18650 packs per wheel (agent bus and coil), 5.1 V DC-DC, protection and wiring.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| 18650 cell, 3400 mAh (LiitoKala NCR18650B) | 8 in use + 2 spare | ≈ 2.71 | 10 | ≈ 27.10 | [AliExpress](https://www.aliexpress.com/item/1005010576713983.html) | to buy; the current cells are weak — about 0.7 Ah per pair ([measurement](coil_test.md)); not every seller ships lithium cells to Kuwait — check delivery |
| 2×18650 box with lid and switch | 2 per wheel | ≈ 1.29 | 4 | ≈ 5.16 | [AliExpress](https://www.aliexpress.com/item/4001207701699.html) | to buy |
| LiitoKala Lii-402 charger | for 4 cells | 3.96 | 1 | 3.96 | [AliExpress](https://www.aliexpress.com/item/1005008078706966.html) |  |
| LM2596 DC-DC with voltmeter | 3 pcs | 1.40 | 3 | 4.20 | [AliExpress](https://www.aliexpress.com/item/1005010587294093.html) | 5 in use: bus and coil of each wheel, motor agent |
| LM2596 DC-DC with indicator | 2 pcs | 1.24 | 2 | 2.48 | [AliExpress](https://www.aliexpress.com/item/1005006735114768.html) |  |
| 1N5819 Schottky diodes | 50 pcs | 0.86 | 1 | 0.86 | [AliExpress](https://www.aliexpress.com/item/1005001552094086.html) | stripe towards the agent's 5V |
| 1N4007 diodes | 50 pcs | 0.76 | 1 | 0.76 | [AliExpress](https://www.aliexpress.com/item/1005001552094086.html) | spares |
| Electrolytics 470 µF 25 V | 20 pcs | 0.85 | 1 | 0.85 | [AliExpress](https://www.aliexpress.com/item/1005005945721276.html) | C1–C3 on the bus, at the bridges |
| Electrolytics, assortment | 120 pcs | 1.02 | 1 | 1.02 | [AliExpress](https://www.aliexpress.com/item/1005006244064108.html) |  |
| Ceramic 104 (0.1 µF) 50 V | 100 pcs | 0.68 | 1 | 0.68 | [AliExpress](https://www.aliexpress.com/item/1005008165798085.html) | on the motor terminals |
| Ceramics, assortment | 300 pcs | 1.15 | 1 | 1.15 | [AliExpress](https://www.aliexpress.com/item/1005008244407175.html) |  |
| 18AWG fuse holder with a 3 A fuse | 2 pcs | 0.73 | 2 | 1.46 | [AliExpress](https://www.aliexpress.com/item/1005007369776673.html) |  |
| Wire 2 × 18 AWG (red-black) | 10 m | 3.22 | 1 | 3.22 | [AliExpress](https://www.aliexpress.com/item/1005009014635843.html) | buses |
| Silicone wire 20 AWG | 3 m | 0.70 | 1 | 0.70 | [AliExpress](https://www.aliexpress.com/item/1005009017260144.html) |  |
| Silicone wire 22 AWG | 3 m | 0.64 | 1 | 0.64 | [AliExpress](https://www.aliexpress.com/item/1005009017260144.html) |  |
| T-tap wire connectors | 20 pcs | 2.24 | 1 | 2.24 | [AliExpress](https://www.aliexpress.com/item/1005011893119817.html) | taps from the bus to the agents |
| H2 connectors, 2 pins | 50 pcs | 1.53 | 1 | 1.53 | [AliExpress](https://www.aliexpress.com/item/1005008802959407.html) |  |
| Compact terminal blocks | 5 pcs | 0.57 | 4 | 2.28 | [AliExpress](https://www.aliexpress.com/item/1005012488325489.html) |  |
| Heat shrink, assortment | 127 pcs | 0.89 | 1 | 0.89 | [AliExpress](https://www.aliexpress.com/item/1005010440409440.html) |  |
| DC volt-ammeter 100 V 10 A | 1 pc | 0.70 | 1 | 0.70 | [AliExpress](https://www.aliexpress.com/item/1005005631605113.html) |  |

Section: bought **29.62 KWD**, to buy or estimate **≈ 32.26 KWD**.

## Frame and motors

Motors on the wheel axles, bridges, the motor agent on the frame; 12 V supply.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| 36GP-555 gear motor, 12 V, 112 rpm | one per wheel | 14.23 | 2 | 28.46 | [AliExpress](https://www.aliexpress.com/item/1005012582421407.html) |  |
| DRV8871 bridge | pack of 5 | 3.86 | 1 | 3.86 | [AliExpress](https://www.aliexpress.com/item/1005012157077217.html) | motors — 2, wheel 1 coil — 1 (works from 5.1 V) |
| Flanged shaft support, 8 mm | 2 pcs | 0.88 | 2 | 1.76 | [AliExpress](https://www.aliexpress.com/item/1005011593372874.html) |  |
| 12 V 5 A (60 W) power supply | motors and motor agent | ≈ 3.09 | 1 | ≈ 3.09 | [AliExpress](https://www.aliexpress.com/item/1005004110696893.html) | not in the AliExpress orders — check which one is used |
| 12 mm plywood, stop bearings, couplings, fasteners | frame and stops | — | 1 | — | — | bought locally |

Section: bought **34.08 KWD**, to buy or estimate **≈ 3.09 KWD**.

## Experiment network (Wi-Fi)

A router of its own just for the agents; the laptop is connected to it by cable, internet over its own Wi-Fi.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| GL.iNet GL-MT300N-V2 router (Mango) | 2.4 GHz, micro-USB 5 V power | 8.60 | 1 | 8.60 | [AliExpress](https://www.aliexpress.com/item/1005005454366776.html) | settings — [wiring, "Experiment network"](wiring.md#experiment-network-wi-fi) |
| UGREEN USB 3.0 → RJ45 adapter (ASIX AX88179) | laptop ↔ router LAN | 3.30 | 1 | 3.30 | [AliExpress](https://www.aliexpress.com/item/1005007088906526.html) |  |
| Cat6 cable | 3 m | 1.91 | 1 | 1.91 | [AliExpress](https://www.aliexpress.com/item/1005009029087811.html) |  |
| Second router (home) | fallback network for the agents | — | 1 | — | — | our own |

Section: bought **13.81 KWD**.

## Mounting and heat

Fixing the heatsinks and boards, insulation under the winding.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| Double-sided thermal tape 10 mm | 5 m | 0.92 | 1 | 0.92 | [AliExpress](https://www.aliexpress.com/item/1005005721222386.html) | heatsinks onto the board |
| Kapton tape 20 mm | 33 m | 1.65 | 1 | 1.65 | [AliExpress](https://www.aliexpress.com/item/1005002708125522.html) | heatsink edges under the wire |
| AB glue for metal | 50 ml | 2.00 | 1 | 2.00 | [AliExpress](https://www.aliexpress.com/item/1005008690859660.html) |  |
| Double-sided nano tape 20 mm | 1 m | 0.38 | 1 | 0.38 | [AliExpress](https://www.aliexpress.com/item/1005010778627319.html) |  |

Section: bought **4.95 KWD**.

## Tools and consumables

For assembly — not included in the cost of the rig itself.

| item | option, quantity | price | pcs | sum | link | note |
|---|---|---|---|---|---|---|
| 60 W soldering iron with control (S806), 7-in-1 kit |  | 2.49 | 1 | 2.49 | [AliExpress](https://www.aliexpress.com/item/1005007111909400.html) |  |
| "Third hand" with magnifier, 4 holders |  | 2.17 | 1 | 2.17 | [AliExpress](https://www.aliexpress.com/item/1005009212103647.html) |  |
| Silicone soldering mat |  | 1.27 | 1 | 1.27 | [AliExpress](https://www.aliexpress.com/item/1005005241808257.html) |  |
| Magnifier glasses |  | 6.00 | 1 | 6.00 | [AliExpress](https://www.aliexpress.com/item/1005005669666176.html) |  |
| 7-in-1 screwdriver |  | 1.21 | 1 | 1.21 | [AliExpress](https://www.aliexpress.com/item/1005012615752371.html) |  |
| Stripper-crimper 10–26 AWG |  | 2.61 | 1 | 2.61 | [AliExpress](https://www.aliexpress.com/item/1005009138448458.html) |  |
| Knife set |  | 0.70 | 1 | 0.70 | [AliExpress](https://www.aliexpress.com/item/1005005733410013.html) |  |
| 4" clamps | 5 pcs | 5.10 | 1 | 5.10 | [AliExpress](https://www.aliexpress.com/item/1005007147995927.html) |  |
| 3" spring clamps | 6 pcs | 1.66 | 2 | 3.32 | [AliExpress](https://www.aliexpress.com/item/1005006441096614.html) |  |
| 2" clamps | 10 pcs | 1.46 | 1 | 1.46 | [AliExpress](https://www.aliexpress.com/item/1005008565541292.html) |  |
| Sn63/Pb37 rosin-core solder, 0.8 mm | 50 g | 1.37 | 1 | 1.37 | [AliExpress](https://www.aliexpress.com/item/1005009113499721.html) |  |
| Flux pen 951 |  | 1.53 | 1 | 1.53 | [AliExpress](https://www.aliexpress.com/item/1005009476418222.html) |  |
| Desoldering braid 2.5 mm | 1.5 m | 0.68 | 1 | 0.68 | [AliExpress](https://www.aliexpress.com/item/1005012817073895.html) |  |
| Solder paste | 15 g | 1.03 | 1 | 1.03 | [AliExpress](https://www.aliexpress.com/item/1005009377104599.html) |  |
| Stainless steel container | 300 ml | 1.34 | 1 | 1.34 | [AliExpress](https://www.aliexpress.com/item/1005010588950996.html) |  |
| Type-C stand |  | 0.64 | 1 | 0.64 | [AliExpress](https://www.aliexpress.com/item/1005012357688054.html) |  |

Section: bought **32.92 KWD**.
