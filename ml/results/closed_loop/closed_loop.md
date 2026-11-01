# Closed-loop benchmark

21 farm-seeds; 12 skipped rows. Differences are paired against the open-loop flight of the same job on the same farm and seed (mean and 95 % interval).

## All templates (relative to open loop)

| arm | n | Δ litres % | Δ weeds 7 d after % | Δ yield % | Δ net % |
|---|---|---|---|---|---|
| closed | 9 | 40.5 [29.8, 51.2] | -25 [-32.5, -17.5] | 0.02 [0.01, 0.03] | -0.32 [-0.58, -0.06] |
| closed_valve | 9 | 38.9 [28.8, 49] | -24 [-31.1, -17] | 0.01 [-0.01, 0.04] | -0.34 [-0.58, -0.09] |

## Per template

| template | arm | n | litres | sorties | weeds 7 d after (m⁻²) | yield t/ha | net/ha | Δ litres | Δ weeds | Δ net |
|---|---|---|---|---|---|---|---|---|---|---|
| A1 | open | 3 | 5.97 | 1 | 1.39 | 5.99 | 1.49e+06 JPY | – | – | – |
| A1 | closed | 3 | 7.87 | 2 | 1.12 | 5.99 | 1.49e+06 JPY | 1.9 | -0.269 | -1.93e+03 |
| A1 | closed_valve | 3 | 7.87 | 2 | 1.12 | 5.99 | 1.49e+06 JPY | 1.9 | -0.269 | -1.93e+03 |
| A5 | open | 3 | 58.4 | 6.33 | 1.71 | 50.9 | 6.61e+03 EUR | – | – | – |
| A5 | closed | 3 | 76.3 | 9 | 1.43 | 50.9 | 6.61e+03 EUR | 17.9 | -0.277 | -0.293 |
| A5 | closed_valve | 3 | 75.7 | 9 | 1.45 | 50.9 | 6.6e+03 EUR | 17.3 | -0.264 | -4.28 |
| A6 | open | 3 | 1.25 | 1 | 0.989 | 14.1 | 5.55e+05 KES | – | – | – |
| A6 | closed | 3 | 1.99 | 2 | 0.596 | 14.1 | 5.51e+05 KES | 0.74 | -0.393 | -4.64e+03 |
| A6 | closed_valve | 3 | 1.95 | 2 | 0.619 | 14.1 | 5.51e+05 KES | 0.697 | -0.37 | -4.57e+03 |

Skipped:
- A2 seed 0 : no herbicide job proposed this season
- A2 seed 1 : no herbicide job proposed this season
- A2 seed 2 : no herbicide job proposed this season
- A3 seed 0 : no herbicide job proposed this season
- A3 seed 1 : no herbicide job proposed this season
- A3 seed 2 : no herbicide job proposed this season
- A4 seed 0 : no herbicide job proposed this season
- A4 seed 1 : no herbicide job proposed this season
- A4 seed 2 : no herbicide job proposed this season
- A8 seed 0 : ValueError: archetype A8 (Large-scale cotton (defoliation)) not supported: Defoliant timing is the drone task here and no defoliation model exists; WOFOST cotton parameters are uncalibrated (engine.UNCALIBRATED).
- A8 seed 1 : ValueError: archetype A8 (Large-scale cotton (defoliation)) not supported: Defoliant timing is the drone task here and no defoliation model exists; WOFOST cotton parameters are uncalibrated (engine.UNCALIBRATED).
- A8 seed 2 : ValueError: archetype A8 (Large-scale cotton (defoliation)) not supported: Defoliant timing is the drone task here and no defoliation model exists; WOFOST cotton parameters are uncalibrated (engine.UNCALIBRATED).
