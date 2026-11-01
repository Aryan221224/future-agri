"""The farmer's Today card: one decision, the reason, what comes next.

Language-neutral: ``code`` and ``params`` are translated by the app (en, hi, es,
pt). Codes: spray_today, wait_weather, wait_approval, scan_due, waiting_data,
no_action, season_over."""
from __future__ import annotations


def stage_code(dvs: float | None) -> str | None:
    if dvs is None:
        return None
    if dvs < 0.05:
        return "emergence"
    if dvs < 1.0:
        return "vegetative"
    if dvs < 1.3:
        return "flowering"
    if dvs < 2.0:
        return "filling"
    return "mature"


def today(state, farm: dict, outlook: dict | None) -> dict:
    st = state
    twin = st.get("twin") or {}
    health = {"lai": twin.get("lai"), "dvs": twin.get("dvs"), "stage_code": stage_code(twin.get("dvs")),
              "herbicide_option": twin.get("herbicide_option"), "scans": twin.get("scans")}
    base = {"farm": {"id": farm["id"], "name": farm["name"], "crop": farm["crop"], "area_ha": farm["area_ha"],
                     "mode": farm["mode"]}, "day": st["day"], "das": st["das"], "health": health}
    ops = st["ops"]
    if st["done"]:
        return {**base, "status": "none", "code": "season_over", "params": {}, "next": None, "window": None}
    day0 = (outlook or {}).get("days", [{}])[0] if (outlook or {}).get("days") else None
    due = [o for o in ops if o["status"] == "approved" and o["product"] != "scan" and o["due"] <= st["day"]]
    if due:
        o = due[0]
        nxt = {"product": o["product"], "due": o["due"], "op": o["id"], "targets_frac": o["targets_frac"]}
        if day0 is not None and day0.get("p_flyable", 0) >= 0.5:
            return {**base, "status": "spray", "code": "spray_today", "next": nxt, "window": day0,
                    "params": {"product": o["product"], "targets_pct": round(100 * o["targets_frac"]),
                               "wind": day0.get("wind_dawn_p50"), "p": day0.get("p_flyable")}}
        return {**base, "status": "wait", "code": "wait_weather", "next": nxt, "window": day0,
                "params": {"product": o["product"], "wind": day0 and day0.get("wind_dawn_p50"),
                           "rain": day0 and day0.get("rain_p50")}}
    proposed = [o for o in ops if o["status"] == "proposed" and o["product"] != "scan"]
    if proposed:
        o = proposed[0]
        return {**base, "status": "wait", "code": "wait_approval", "window": day0,
                "next": {"product": o["product"], "due": o["due"], "op": o["id"], "targets_frac": o["targets_frac"]},
                "params": {"product": o["product"], "due": o["due"]}}
    scans = [o for o in ops if o["status"] == "proposed" and o["product"] == "scan"]
    if scans:
        return {**base, "status": "scan", "code": "scan_due", "window": day0,
                "next": {"product": "scan", "due": scans[0]["due"], "op": scans[0]["id"]},
                "params": {"due": scans[0]["due"]}}
    if st.get("world", {}).get("waiting"):
        return {**base, "status": "none", "code": "waiting_data", "next": None, "window": day0,
                "params": {"last": st["world"]["last_observed"]}}
    upcoming = sorted((o for o in ops if o["status"] == "approved"), key=lambda o: o["due"])
    nxt = ({"product": upcoming[0]["product"], "due": upcoming[0]["due"], "op": upcoming[0]["id"]}
           if upcoming else None)
    return {**base, "status": "none", "code": "no_action", "next": nxt, "window": day0, "params": {}}
