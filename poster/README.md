# Future Agri 2.0 — A1 conference poster

`Future_Agri_Poster_A1.pdf` — 594 × 841 mm, single page, vector text and figures.
Print at 100% scale, no fit-to-page.

## Editing

`poster.html` is the source. Re-render with:

    /opt/pw-browsers/chromium-*/chrome-linux/chrome --headless --no-sandbox \
      --print-to-pdf=Future_Agri_Poster_A1.pdf --no-pdf-header-footer poster.html

The layout fills the sheet to within 0.2 mm, so **any text you add must be
balanced by text removed**, or it spills to a second page. To check:

    chrome --headless --no-sandbox --hide-scrollbars --window-size=2245,4200 \
      --screenshot=m.png poster.html
    python3 -c "from PIL import Image; import numpy as np; \
      a=np.asarray(Image.open('m.png').convert('L')); r=np.where(a.min(axis=1)<245)[0]; \
      print(f'headroom {(841/25.4*96-r.max())/96*25.4:+.1f} mm')"

## Placeholders to replace

Two slots are still empty; everything else is generated.

| Slot | Box (mm) | Ratio | Min px @300 dpi | Subject |
|---|---|---|---|---|
| Header logo L | 30 × 30 | 1:1 | 355 × 355 | Bennett University crest, transparent PNG or SVG |
| Header logo R | 30 × 30 | 1:1 | 355 × 355 | SEAS / Drones 2026 mark, transparent PNG or SVG |
| Footer QR | 23 × 23 | 1:1 | 600 × 600 | QR to the repository or the Sciforum abstract |

## Generated figures

All five are embedded as base64, so the PDF is self-contained and nothing goes
soft when it is printed. Each is regenerated from the code it describes, so a
figure cannot drift from the system:

| Figure | Script | Slot (mm) |
|---|---|---|
| Fig. 3 inset — capture protocol strip | `scripts/fig_station_strip.py` | 176.6 × 21 |
| Fig. 6 — trial layout | `scripts/fig_trial_layout.py` | 135.4 × 86 |
| Fig. 7 — capture geometry | `scripts/fig_capture_geometry.py` | 135.4 × 86 |
| Fig. 8 — operator dashboard | Chromium screenshot of `web/index.html` | 135.4 × 86 |
| Fig. 9 — generated mission | `scripts/fig_mission.py` | 135.4 × 86 |

    chrome --headless --no-sandbox --hide-scrollbars --window-size=1500,980 \
      --screenshot=fig_dashboard.png ../web/index.html

After regenerating any of them, re-embed and re-render — `poster.html` holds the
base64, not a file reference.

## Replacing a generated figure with a photograph

Figs. 6 and 7 are diagrams standing in for photographs that do not exist yet.
Once the plot is pegged out and the rig is in hand, shoot landscape, crop to
**1.574:1**, and swap the `<img src="data:image/png;base64,…">` for the photo at
**1600 × 1016 px or better**. Keep `height:86mm`. Anything under ~1400 px across
will print visibly soft at A1 and should not go on the sheet.

## Colour

Chart palette `#0F7B5F / #2B5FAE / #B5461F` passes all six checks of the dataviz
validator (lightness band, chroma floor, CVD separation, normal-vision floor,
contrast). Do not substitute colours without re-running it.

## Numbers

Every figure comes from `bench/results/*.json`. Regenerate with `make bench` from
the repository root; nothing on the poster is quoted from elsewhere.
