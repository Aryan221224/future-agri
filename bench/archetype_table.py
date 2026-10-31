"""Markdown summary of bench/results/archetypes.json for the README."""
import json
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "results" / "archetypes.json")
d = json.loads(p.read_text())
print("| archetype | field | best plan | net (mean / CVaR20) | vs calendar | vs none | best flight (h, v, spacing, rate, RTK) | sprays | drone h/season | forecasts |")
print("|---|---|---|---|---|---|---|---|---|---|")
for aid, r in d["runs"].items():
    if "error" in r:
        print(f"| {aid} | — | failed: {r['error'][:80]} | | | | | | | |")
        continue
    f, b, cur = r["farm"], r["best"], r["currency"]
    rk = {t["plan"]: t for t in r["ranked"]}
    cal, none = rk.get("calendar", {}), rk.get("none", {})
    fl = b["jiyi_settings"]
    src = ", ".join(f"{k} x{v}" for k, v in r["forecast_issues_by_source"].items()) or "n/a (no weekly plan)"
    print(f"| {aid} {f['name']} ({f['site']}, {f['year']}) | {f['area_ha']} ha | {b['plan']} | "
          f"{b['net_mean']:,.0f} / {b['net_cvar20']:,.0f} {cur} | {b['net_mean'] - cal.get('net_mean', 0):+,.0f} | "
          f"{b['net_mean'] - none.get('net_mean', 0):+,.0f} | {fl['height_m']} m, {fl['speed_ms']} m/s, {fl['swath_m']} m, "
          f"{fl['rate_l_ha']} L/ha, {'RTK' if fl['rtk'] else 'no RTK'} | {b['fungicide_sprays_mean']} | "
          f"{b['drone_time_min_mean'] / 60:.1f} | {src} |")
