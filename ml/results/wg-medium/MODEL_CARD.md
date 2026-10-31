# Model card: rfdetr-medium-wg-medium

| | |
|---|---|
| Architecture | RF-DETR-medium (Roboflow, Apache-2.0 code and weights) at 608 px |
| Training data | WeedsGalore (CC BY 4.0) — Celikkan E., Kunzmann T., Yeskaliyev Y., Itzerott S., Klein N., Herold M. WeedsGalore: A Multispectral and Multitemporal UAV-Based Dataset for Crop and Weed Segmentation in Agricultural Maize Fields. WACV 2025, pp. 4767-4777. |
| Classes | maize, amaranth, barnyard_grass, quickweed, weed_other + background |
| Future Agri taxonomy | maize → crop; amaranth → broadleaf_weed; quickweed → broadleaf_weed; barnyard_grass → grass_weed; weed_other → weed_other |
| Trained on | NVIDIA H200, torch 2.12.0a0+0291f960b6.nv26.04.48445190, 16.2 min, git unknown |
| Synthetic data | no |

## Results (test split, never used for any choice)

* COCO mAP@[.5:.95] **0.2805**, AP50 **0.4872**
* At the operating threshold 0.35 (best F1 on valid): precision 0.7819, recall 0.6165
* Conformal gate, α = 0.10: coverage **0.9232** (target 0.90), mean set size 1.762, review rate **59.7%**

## Intended use

Per-plant crop/weed detection on nadir UAV RGB at millimetre GSD, feeding the Future Agri edge gateway, which routes every non-singleton conformal set to human review.

## Limitations

* Trained on one German maize site, four dates in May–June 2023, 2.5 mm GSD. It has never seen Indian fields, other crops, other soils or other altitudes; recalibrate (and expect to retrain) before any use elsewhere. The date-shift test in `report.md` shows what happens when growth stage changes.
* RGB composites built from the multispectral camera's R, G, B bands, not a consumer RGB camera; colour differs.
* Detects plants, not disease or nutrient stress.
* Coverage is guaranteed only under exchangeability with the calibration data; the gateway monitors it and alarms when it drops.
