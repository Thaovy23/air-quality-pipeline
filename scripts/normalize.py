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
