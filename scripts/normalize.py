"""normalize.py — Flatten JSON from both Device API v2 endpoints into DB-ready tuples.

Shared by backfill.py and fetch.py so schema-handling logic is written once.
Handles known schema quirks:
  * pm1: object {conc,...} in 'current' but bare number in history rows  -> _conc()
  * pm25/pm10 use 'conc' (root) or 'concentration' (validated)          -> _conc()
  * pressure: root.pr is in Pascals -> divide by 100 for hPa; validated.pressure already hPa
  * ts is kept as ISO UTC string; Postgres parses it into timestamptz automatically
"""

# Column order MUST match the INSERT statements in backfill.py / fetch.py
DEVICE_COLS = [
    "resolution", "ts", "co2", "pm1_conc",
    "pm25_conc", "pm25_aqius", "pm25_aqicn",
    "pm10_conc", "pm10_aqius", "pm10_aqicn",
    "temp_c", "humidity_pct", "pressure_hpa",
    "aqius", "aqicn", "mainus", "maincn",
    "raw",
]

STATION_COLS = [
    "resolution", "ts", "pm25_conc", "pm25_aqius", "pm25_aqicn",
    "aqius", "aqicn", "temp_out_c", "humidity_out", "pressure_hpa",
    "wind_speed", "wind_dir", "condition", "icon", "heat_index",
    "mainus", "maincn",
    "raw",
]


def _conc(v):
    """Extract concentration value whether v is a bare number or a {conc|concentration} object."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, dict):
        return v.get("conc", v.get("concentration"))
    return None


def _sub(v, key):
    """Extract a sub-field (aqius/aqicn) from a pm object; None if not a dict."""
    return v.get(key) if isinstance(v, dict) else None


def _pa_to_hpa(pr):
    return round(pr / 100.0, 2) if isinstance(pr, (int, float)) else None


def device_row(row, resolution, Json):
    """Map one row from the ROOT endpoint to a device_readings tuple.
    Json: psycopg2.extras.Json — passed in so this module doesn't depend on psycopg2."""
    pm25 = row.get("pm25") or {}
    pm10 = row.get("pm10") or {}
    return (
        resolution,
        row.get("ts"),
        row.get("co2"),
        _conc(row.get("pm1")),
        _conc(pm25),
        _sub(pm25, "aqius"),
        _sub(pm25, "aqicn"),
        _conc(pm10),
        _sub(pm10, "aqius"),
        _sub(pm10, "aqicn"),
        row.get("tp"),
        row.get("hm"),
        _pa_to_hpa(row.get("pr")),
        row.get("aqius"),
        row.get("aqicn"),
        row.get("mainus"),
        row.get("maincn"),
        Json(row),
    )


def station_row(row, resolution, Json):
    """Map one row from the VALIDATED endpoint to a station_readings tuple.
    Historical 'hourly' rows only contain aqi+pm25 — weather columns will be None (by design)."""
    pm25 = row.get("pm25") or {}
    wind = row.get("wind") or {}
    return (
        resolution,
        row.get("ts"),
        _conc(pm25),
        _sub(pm25, "aqius"),
        _sub(pm25, "aqicn"),
        row.get("aqius"),
        row.get("aqicn"),
        row.get("temperature"),
        row.get("humidity"),
        row.get("pressure"),
        wind.get("speed"),
        wind.get("direction"),
        row.get("condition"),
        row.get("icon"),
        row.get("heatIndex"),
        row.get("mainus"),
        row.get("maincn"),
        Json(row),
    )


def build_device_rows(root, Json):
    """All history resolutions + current from the ROOT endpoint -> deduplicated tuple list.
    Keyed by (resolution, ts) so current overwrites a duplicate instant entry.

    Each resolution's retention window on the API (instant ~1h, hourly ~48h, daily ~30d,
    monthly ~12mo) is wider than the 30-min poll interval, so upserting the whole set every
    run accumulates a gap-free history at every granularity. The most recent daily/monthly
    row is a running partial average; ON CONFLICT DO UPDATE overwrites it until it settles."""
    by_key = {}
    hist = root.get("historical", {}) or {}
    for resolution in ("instant", "hourly", "daily", "monthly"):
        for r in hist.get(resolution, []) or []:
            if r.get("ts"):
                by_key[(resolution, r["ts"])] = device_row(r, resolution, Json)
    cur = root.get("current")
    if cur and cur.get("ts"):
        by_key[("instant", cur["ts"])] = device_row(cur, "instant", Json)
    return list(by_key.values())


def build_station_rows(validated, Json):
    """current (instant) + hourly history from the VALIDATED endpoint -> deduplicated tuple list.
    Keyed by (resolution, ts). Pulling hourly each run (not just current) keeps outdoor
    history gap-free the same way as the device side."""
    by_key = {}
    hist = validated.get("historical", {}) or {}
    for r in hist.get("hourly", []) or []:
        if r.get("ts"):
            by_key[("hourly", r["ts"])] = station_row(r, "hourly", Json)
    cur = validated.get("current")
    if cur and cur.get("ts"):
        by_key[("instant", cur["ts"])] = station_row(cur, "instant", Json)
    return list(by_key.values())


# ---------------------------------------------------------------------
# Cross-field data-quality checks (flag, do NOT block/drop).
# Each invariant below was verified to hold on 100% of existing rows, so a
# violation is a strong signal of a sensor or parse fault — a canary, not noise.
# Checks only fire when all needed fields are present (can't validate absent data).
# Pure functions on the raw API dict -> unit-testable.
# ---------------------------------------------------------------------
def check_device_invariants(row):
    """Cross-field violations for one ROOT (device) row. Returns list of reason strings."""
    out = []
    pm1, pm25, pm10 = _conc(row.get("pm1")), _conc(row.get("pm25")), _conc(row.get("pm10"))
    if None not in (pm1, pm25, pm10) and not (pm1 <= pm25 <= pm10):
        out.append(f"PM order violated: pm1={pm1} pm25={pm25} pm10={pm10}")

    aqius = row.get("aqius")
    pm25_aqi = _sub(row.get("pm25"), "aqius")
    pm10_aqi = _sub(row.get("pm10"), "aqius")
    if None not in (aqius, pm25_aqi, pm10_aqi) and aqius != max(pm25_aqi, pm10_aqi):
        out.append(f"overall aqius {aqius} != max(component) {max(pm25_aqi, pm10_aqi)}")

    mainus = row.get("mainus")
    main_aqi = {"pm25": pm25_aqi, "pm10": pm10_aqi}.get(mainus)
    if mainus and aqius is not None and main_aqi is not None and main_aqi != aqius:
        out.append(f"mainus='{mainus}' but its AQI {main_aqi} != overall aqius {aqius}")
    return out


def check_station_invariants(row):
    """Cross-field violations for one VALIDATED (station) row. Returns list of reason strings."""
    out = []
    hi, temp = row.get("heatIndex"), row.get("temperature")
    if None not in (hi, temp) and hi < temp:
        out.append(f"heat_index {hi} < temperature {temp}")

    aqius = row.get("aqius")
    pm25_aqi = _sub(row.get("pm25"), "aqius")
    if None not in (aqius, pm25_aqi) and aqius != pm25_aqi:
        out.append(f"station overall aqius {aqius} != pm25 aqi {pm25_aqi}")
    return out


def _iter_payload_rows(payload, resolutions):
    """Yield (resolution, row) for every historical row + current in an API payload."""
    hist = payload.get("historical", {}) or {}
    for resolution in resolutions:
        for r in hist.get(resolution, []) or []:
            yield resolution, r
    cur = payload.get("current")
    if cur and cur.get("ts"):
        yield "instant", cur


def scan_quality(root, validated):
    """Scan both payloads. Returns list of (endpoint, resolution, ts, [reasons]) for rows
    that violate any cross-field invariant."""
    findings = []
    for resolution, r in _iter_payload_rows(root, ("instant", "hourly", "daily", "monthly")):
        reasons = check_device_invariants(r)
        if reasons:
            findings.append(("device", resolution, r.get("ts"), reasons))
    for resolution, r in _iter_payload_rows(validated, ("hourly",)):
        reasons = check_station_invariants(r)
        if reasons:
            findings.append(("station", resolution, r.get("ts"), reasons))
    return findings
