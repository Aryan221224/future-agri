# fa_aircraft: ArduPilot back-end

The console flies a spray job on an ArduPilot Copter over MAVLink 2: parameter check against
`fa_aircraft/params/t1_10l_quad.param`, preflight gate, fence + mission upload with read-back,
GUIDED arm and take-off, AUTO mission with the sprayer switched at every pass, RTL, landing,
battery swap, next sortie. ArduPilot's own failsafes stay in charge in the air.

Links: `serial:COM5:57600` (USB or SiK radio), `udpin:0.0.0.0:14550`, `udpout:IP:14550`,
`tcp:IP:5760` (mavlink-router on the companion over Wi-Fi / 4G, or SITL).

Tested end to end against ArduCopter 4.5.7 SITL (`tests/test_aircraft_sitl.py`):

    git clone --branch Copter-4.5.7 https://github.com/ArduPilot/ardupilot && cd ardupilot
    git submodule update --init --recursive
    ./waf configure --board sitl && ./waf copter
    FA_SITL=$PWD/build/sitl/bin/arducopter python -m pytest tests/test_aircraft_sitl.py
