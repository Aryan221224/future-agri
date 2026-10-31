# rfdetr-medium-wg-medium — evaluation

Backend: `rfdetr-medium-wg-medium+torch` · threshold 0.35 chosen on valid · IoU 0.5

| metric | value |
|---|---:|
| mAP@[.5:.95] (test) | 0.2805 |
| AP50 (test) | 0.4872 |
| precision / recall @ thr (test) | 0.7819 / 0.6165 |

## Conformal gate (calibrated on valid, measured on test)

| α | coverage | 95% CI (images) | target | mean set | review rate | alarm | RAPS coverage | RAPS review |
|---:|---:|:---:|---:|---:|---:|:---:|---:|---:|
| 0.05 | 0.9558 | 0.9391–0.972 | 0.95 | 2.131 | 77.2% | no | 0.9783 | 100.0% |
| 0.10 | 0.9232 | 0.9007–0.9456 | 0.90 | 1.762 | 59.7% | no | 0.9674 | 96.3% |
| 0.20 | 0.8247 | 0.7976–0.8522 | 0.80 | 1.291 | 28.8% | no | 0.9586 | 81.5% |

![coverage](coverage.png)

## Growth-stage shift (α = 0.10)

| test date | early-only calibration | alarm | per-date calibration |
|---|---:|:---:|---:|
| 2023-05-25 (seen) | 0.9315 | no | 0.8584 |
| 2023-05-30 (seen) | 0.836 | **yes** | 0.8925 |
| 2023-06-06 | 0.8762 | no | 0.9269 |
| 2023-06-15 | 0.8412 | no | 0.9059 |

![shift](shift.png)

