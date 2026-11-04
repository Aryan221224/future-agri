# S-BUS bridge (RP2040)

Sits between the pilot's RC receiver and the JIYI K3-A Pro so that `fa-agent` can fly the
aircraft, while the pilot can always take over. Design: `docs/superpowers/specs/2026-11-04-drone-control-design.md`;
wiring: `docs/hardware/k3a-autonomy-kit.md`.

* `src/bridge_core.c` – all the logic (S-BUS codec, companion frames, source selection and
  fail-over). Portable C, no hardware: the same file runs in the firmware, the host tests
  and the Python simulator.
* `src/main.c` – RP2040 glue: UARTs (S-BUS inverted in hardware), fail-over mux select,
  USB/UART companion link, 50 ms watchdog, LED.

## Test on a computer

    make -f host.mk test   # 51 checks, with address/undefined-behaviour sanitizers
    make -f host.mk lib    # build-host/libbridgecore.so, loaded by fa_agent's simulator

## Build the firmware

Needs the Pico SDK 2.x and the Arm GNU toolchain (`arm-none-eabi-gcc`).

    export PICO_SDK_PATH=/path/to/pico-sdk
    mkdir build && cd build && cmake .. && make      # -> sbus_bridge.uf2

Hold BOOTSEL on the Pico, plug it in by USB, and copy `sbus_bridge.uf2` onto the drive it
shows. The LED blinks slowly when the pilot is in control.

**Status:** the core is tested (host, sanitizers, and flown end to end in the simulator).
`main.c` has been type-checked against the Pico SDK 2.1.1 headers but not yet compiled with
the Arm toolchain or run on a board: the bench test (`python -m fa_agent bench`) is the
first run on hardware.
