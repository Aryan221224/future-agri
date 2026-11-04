# K3-A Pro autonomy kit: let the software fly the drone

This kit adds a companion computer, a centimetre RTK pair and a small S-BUS bridge board
between the pilot's RC receiver and the JIYI K3-A Pro. The K3-A Pro keeps stabilising and
holding the aircraft in **GPS-SPEED mode**; `fa-agent` flies it like a pilot would, by
writing the stick channels, steered by its own RTK position. The pilot's transmitter keeps
an AUTO switch: off = the pilot flies, exactly as today.

Design and safety rules: [spec](../superpowers/specs/2026-11-04-drone-control-design.md).
No hardware added? See [stock K3-A Pro loop](#without-the-kit-stock-k3-a-pro) at the end.

## Parts (about USD 600–900)

| # | Part | Example | Notes |
|---|---|---|---|
| 1 | Companion computer | Raspberry Pi 5 (4 GB) + 32 GB card, or Jetson Orin Nano | Pi OS 64-bit / Ubuntu |
| 2 | S-BUS bridge MCU | Raspberry Pi Pico (RP2040) | firmware: `firmware/sbus_bridge` |
| 3 | Fail-over switch | 74LVC1G157 2:1 mux (SOT-23 or breakout) + 10 kΩ pull-down | pilot is the default input |
| 4 | RTK moving base pair | 2 × u-blox ZED-F9P (simpleRTK2B or SparkFun) + 2 multiband antennas | position + heading |
| 5 | RTK corrections | NTRIP account over 4G, **or** a third F9P as base + 433/868/915 MHz radio | |
| 6 | Official JIYI RTK module | on the K3-A Pro's RTK port | its own hold becomes cm-level |
| 7 | Height radar | JIYI radar on the EXT port | terrain-following spray height |
| 8 | Power monitor | INA228 breakout **without** on-board shunt + 100 A / 50 mV shunt (0.5 mΩ) | 12S up to 50.4 V |
| 9 | Pump cut relay | 30 A automotive relay, **normally closed**, + transistor driver (or a high-side MOSFET module) | cuts the 24 V pump line |
| 10 | 5 V supply | 12S-capable UBEC, 5 V 5 A (input 14–60 V) | for the Pi |
| 11 | 4G modem | USB LTE dongle or HAT | console link + NTRIP |
| 12 | Camera (closed loop) | global-shutter USB/CSI camera, nadir | weed counts |
| 13 | Optional radio | SiK 433/915 MHz pair | not needed with Wi-Fi/4G |
| 14 | Transmitter | your JIYI-compatible transmitter with a free 2-position switch on CH8 | receiver must output ≥ 8 S-BUS channels |

## Wiring

```
                         ┌─────────────── 74LVC1G157 (3.3 V) ───────────────┐
pilot RX S-BUS ──┬───────┤ A (S=0: default, pull-down)                        │
                 │       │                                    Y ──────────────┼──▶ K3-A Pro RC port (signal)
                 │  ┌────┤ B (S=1)                                            │
                 │  │    │ S ◀── 10 kΩ to GND                                 │
                 │  │    └──────────────────────────────────────────────────────┘
                 │  │           ▲
                 ▼  │           │
            GP1 RX  GP0 TX     GP2 SELECT          Pico (RP2040)  GP4 TX ──▶ Pi GPIO15 (RXD)
                                                                   GP5 RX ◀── Pi GPIO14 (TXD)
K3-A Pro RC port +5 V / GND ──▶ pilot receiver (as before); Pico GND = K3 GND = Pi GND (common ground!)

ZED-F9P front (moving base) ──USB──▶ Pi      UART2 ──RTCM 460800──▶ ZED-F9P rear (rover) ──USB──▶ Pi
INA228: shunt in the battery negative lead, VBUS to battery + through a 1 A fuse, SDA/SCL ──▶ Pi GPIO2/3
Pump relay coil ◀── transistor ◀── Pi GPIO17;  relay NC contact in series with the pump's 24 V supply
12S ──▶ UBEC 5 V 5 A ──▶ Pi 5 (USB-C or 5 V pins);  Pico from the Pi's 3.3/5 V or its own USB
```

Why this way:
* **The pilot is the default in hardware.** The mux's select line has a pull-down. It goes high
  only while the bridge is sending companion, hold or return-home frames. A bridge reset,
  crash or power loss hands the K3-A Pro straight back to the pilot's receiver.
* **The pump relay is normally closed**, energised to cut. With the agent off the drone sprays
  exactly as JIYI does today. In autonomous flight the agent cuts the pump everywhere except
  on the pass. Set `relay: {active_high: false}` in the agent config for this wiring.
* **Antennas** at least 0.5 m apart. On the 1.3 m wheelbase put them on the front and rear arm
  tips. Measure the separation (`baseline_m`) and the angle to the nose (`heading_offset_deg`,
  180 if the front antenna is the moving base and the rear one the rover, along the body).

## Configure the K3-A Pro (JIYI Assistant2, LINK port by USB)

1. **Receiver:** S-BUS. Do the RC calibration with the AUTO switch **off** (the pilot's
   transmitter sets the end points).
2. **CH5 flight mode:** note which position gives **GPS-SPEED**. The bench test finds the pulse
   width and writes it to the profile.
3. **CH6:** one-key return home. **RC-loss failsafe:** return home (also set on the transmitter).
4. **Pump:** *manual* mode on the channel the profile uses (CH7 by default), so the agent
   switches it. Linkage mode also works; the agent then only allows or cuts.
5. Install the official RTK module and the radar, and check the yellow RTK LED.

## Set up the companion computer

```bash
# on the Pi (Raspberry Pi OS 64-bit)
sudo raspi-config nonint do_serial_hw 0 && sudo raspi-config nonint do_serial_cons 1   # UART on, no console
sudo apt install -y python3-venv python3-libgpiod git
git clone https://github.com/Aryan221224/future-agri.git && cd future-agri
python3 -m venv ~/fa-venv && ~/fa-venv/bin/pip install -r services/agent/requirements.txt
sudo cp services/agent/agent.example.yaml /etc/fa-agent.yaml && sudo nano /etc/fa-agent.yaml   # token, ports, NTRIP
sudo cp services/agent/fa-agent.service /etc/systemd/system/ && sudo systemctl enable --now fa-agent
```

Flash the bridge: hold BOOTSEL on the Pico, plug it in by USB, copy `sbus_bridge.uf2`
(see `firmware/sbus_bridge/README.md`). Its LED blinks slowly while the pilot is in control.

Configure both ZED-F9P receivers (u-center 2): 10 Hz, UBX NAV-PVT on USB for the front unit and
NAV-RELPOSNED on USB for the rear unit. The front unit sends RTCM 4072.0/1077/1087/1097/1127/1230
out of UART2 at 460800 to the rear unit's UART2. The front unit takes RTCM in on USB (the agent
writes the NTRIP stream there).

In the Mission Console: **Live → Add a device**, kind *drone agent*. Paste the token into
`/etc/fa-agent.yaml`.

## Before the first flight

Follow [first-flight.md](first-flight.md): `check`, then the props-off `bench` test, then a
hover, then the calibration flight, then a water-only job. Do not skip steps.

## Without the kit: stock K3-A Pro

Nothing on the aircraft changes. The loop closes between sorties through JIYI Agri Assistant.
The console splits the job into tank-sized blocks, each a KML for the app. After each sortie
the flight record comes back, either uploaded or pulled from the remote, and the twin
re-plans the remaining blocks. Link the Android remote or phone running Agri Assistant by USB
(USB debugging on) or Wi-Fi ADB. The console then pushes each block's KML to
`Download/FutureAgri` on it and reads exported records from `Download/FutureAgri/records`.
This needs Android platform-tools (`adb`) on the console's computer. It cannot correct a
sortie in flight: that is what the kit is for.

## Regulation

Adding a control device to a certified agricultural drone can void its warranty or its type
certificate. Check your aviation authority's rules (in India, DGCA) before commercial use.
