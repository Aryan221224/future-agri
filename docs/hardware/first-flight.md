# First flights: from bench to field, one assumption at a time

Each step proves something the next one relies on. If a step fails, fix it and repeat that
step; never skip ahead. Two people: the **pilot** (transmitter in hand, AUTO switch under a
finger, ready to take over) and the **operator** (console). Open ground, no people within
30 m, light wind (< 5 m/s), water only in the tank until step 6.

## 1. `check`: sensors, no outputs (props on or off)

    sudo systemctl stop fa-agent
    ~/fa-venv/bin/python -m fa_agent check --config /etc/fa-agent.yaml

Pass needs: bridge status with the pilot receiver OK, RTK **fixed** with a valid heading,
battery voltage and current, console reachable. Common fixes:

| Shows | Do |
|---|---|
| bridge: no status | Pi UART enabled? TX/RX crossed? Pico flashed (LED blinking)? common ground? |
| pilot frames bad | S-BUS polarity: the RP2040 inverts in firmware; no external inverter |
| rtk: float / no heading | NTRIP mount and login; antennas' sky view; `baseline_m` matches the measured separation |
| power: no monitor | I2C on (`raspi-config`), address 0x40, shunt wired in the negative lead |
| console: not reachable | `console_url`, the Wi-Fi/4G route, the device token |

## 2. `bench`: PROPS OFF

    ~/fa-venv/bin/python -m fa_agent bench --config /etc/fa-agent.yaml

The bench test is guided. It confirms, and writes into the profile:

- the AUTO switch;
- the CH5 value that selects GPS-SPEED;
- the direction of every stick axis, using Assistant2's RC monitor;
- the arming combination, which also confirms that the motors start and that they stop by themselves;
- the pump channel;
- the bridge fail-over: stopping the agent leads to hold, then return home;
- the hand-back to the pilot.

## 3. Hover hand-over (props on, empty tank)

1. The pilot takes off in GPS mode to 5 m and flips **AUTO** with no job running. The agent must
   hold the aircraft still (console: "airborne with no job: holding position").
2. The pilot flips AUTO off. The pilot has control at once.
3. Repeat three times. Then the pilot lands.

## 4. Calibration flight (props on, empty tank, open area ≥ 40 × 40 m)

Console → Aircraft → **Calibrate**. The agent takes off to the transit height. It then flies
3 s steps on each axis at two stick levels, returning to the start point between them, and
lands. The report lists the measured speed per stick and the response time for each axis. A
fit is applied only when it is consistent. Keep the written profile (`profile:` in the config).

## 5. Dry job (props on, empty tank)

A small job, 2–4 passes. Watch the cross-track error on the Live run page: after calibration
it should stay within about 0.5 m. Test **Pause**, **Resume** and **Abort** once each.

## 6. Water-only job

The same small job with water, to check that the pump is on only along the passes (relay and
channel) and that the litres booked match the tank.

## 7. Real job

Small field first. The first real batteries should be flown with the pilot watching every
pass.

## The same steps on an ArduPilot flight controller

1. Load `services/aircraft/fa_aircraft/params/t1_10l_quad.param`.
2. Run the usual ArduPilot calibrations (accelerometer, compass, radio, ESC) and a manual
   hover in LOITER.
3. Console → Aircraft → connect over USB, the SiK radio or Wi-Fi.
4. The preflight gate checks the safety parameters. Fly a dry job, then a water-only job.
