"""Unit tests for normalize.py — the shared transform layer.

Hermetic: no network, no DB, no secrets. normalize.py has zero third-party
dependencies and takes the psycopg2 `Json` wrapper as a parameter, so we stub
it with the identity function and test pure input -> output.

Each test locks in a schema quirk or invariant discovered the hard way, so a
future edit that regresses one turns CI red instead of silently emitting NULLs.
"""
import normalize as nz

# Stub for the psycopg2 Json wrapper: keep the raw dict as-is.
J = lambda r: r


# --- _conc: the pm1 type-duality trap ---------------------------------------
def test_conc_from_conc_object():
    assert nz._conc({"conc": 6, "aqius": 33}) == 6

def test_conc_from_concentration_object():
    # validated endpoint uses 'concentration' instead of 'conc'
    assert nz._conc({"concentration": 8, "aqius": 44}) == 8

def test_conc_from_bare_number():
    # history rows store pm1 as a bare number, not an object
    assert nz._conc(6) == 6
    assert nz._conc(6.7) == 6.7

def test_conc_none():
    assert nz._conc(None) is None
    assert nz._conc("garbage") is None


# --- _sub / _pa_to_hpa -------------------------------------------------------
def test_sub_reads_field_or_none():
    assert nz._sub({"aqius": 44}, "aqius") == 44
    assert nz._sub(6, "aqius") is None          # bare number has no sub-field
    assert nz._sub({}, "aqius") is None

def test_pa_to_hpa_divides_by_100():
    assert nz._pa_to_hpa(100281) == 1002.81
    assert nz._pa_to_hpa(None) is None
    assert nz._pa_to_hpa("x") is None


# --- device_row / station_row: field mapping + column-order contract --------
def test_device_row_field_mapping_and_length():
    row = {
        "ts": "2026-07-07T07:00:27.000Z", "co2": 409,
        "pm1": {"conc": 5}, "pm25": {"conc": 8, "aqius": 44, "aqicn": 11},
        "pm10": {"conc": 11, "aqius": 10, "aqicn": 11},
        "tp": 30.9, "hm": 75, "pr": 100281,
        "aqius": 44, "aqicn": 11, "mainus": "pm25", "maincn": "pm10",
    }
    t = nz.device_row(row, "instant", J)
    # tuple length MUST match the column list (order contract with the INSERT)
    assert len(t) == len(nz.DEVICE_COLS)
    d = dict(zip(nz.DEVICE_COLS, t))
    assert d["resolution"] == "instant"
    assert d["co2"] == 409
    assert d["pm1_conc"] == 5
    assert d["pm25_conc"] == 8
    assert d["temp_c"] == 30.9          # tp -> temp_c
    assert d["humidity_pct"] == 75      # hm -> humidity_pct
    assert d["pressure_hpa"] == 1002.81 # pr Pa -> hPa
    assert d["mainus"] == "pm25"

def test_station_row_field_mapping_and_length():
    row = {
        "ts": "2026-07-07T07:00:00.000Z",
        "pm25": {"concentration": 8, "aqius": 44, "aqicn": 11},
        "aqius": 44, "aqicn": 11,
        "temperature": 31, "humidity": 71, "pressure": 1006,
        "wind": {"speed": 3.06, "direction": 233},
        "condition": "Broken clouds", "icon": "04d", "heatIndex": 38,
        "mainus": "pm25", "maincn": "pm25",
    }
    t = nz.station_row(row, "instant", J)
    assert len(t) == len(nz.STATION_COLS)
    d = dict(zip(nz.STATION_COLS, t))
    assert d["pm25_conc"] == 8              # concentration -> pm25_conc
    assert d["temp_out_c"] == 31            # temperature -> temp_out_c
    assert d["humidity_out"] == 71
    assert d["pressure_hpa"] == 1006        # validated already hPa (no /100)
    assert d["wind_speed"] == 3.06
    assert d["wind_dir"] == 233
    assert d["heat_index"] == 38


# --- build_device_rows / build_station_rows: dedup by (resolution, ts) -------
def test_build_device_rows_current_overwrites_instant_same_ts():
    ts = "2026-07-07T07:00:00.000Z"
    root = {
        "historical": {
            "instant": [{"ts": ts, "co2": 400, "pm25": {"conc": 5}}],
            "hourly":  [{"ts": "2026-07-07T06:00:00.000Z", "co2": 410, "pm25": {"conc": 9}}],
            "daily":   [{"ts": "2026-07-06T00:00:00.000Z", "co2": 420, "pm25": {"conc": 12}}],
            "monthly": [{"ts": "2026-07-01T00:00:00.000Z", "co2": 430, "pm25": {"conc": 15}}],
        },
        "current": {"ts": ts, "co2": 999, "pm25": {"conc": 7}},  # same ts as instant
    }
    rows = nz.build_device_rows(root, J)
    by_key = {(r[0], r[1]): r for r in rows}
    # one instant row at ts, and it carries current's value (999), not 400
    assert dict(zip(nz.DEVICE_COLS, by_key[("instant", ts)]))["co2"] == 999
    # all four resolutions present
    assert {k[0] for k in by_key} == {"instant", "hourly", "daily", "monthly"}

def test_build_station_rows_hourly_plus_current():
    root = {
        "historical": {"hourly": [{"ts": "2026-07-07T06:00:00.000Z", "pm25": {"concentration": 9, "aqius": 50}}]},
        "current": {"ts": "2026-07-07T07:00:00.000Z", "pm25": {"concentration": 8, "aqius": 44},
                    "temperature": 31, "heatIndex": 38, "aqius": 44},
    }
    rows = nz.build_station_rows(root, J)
    keys = {(r[0], r[1]) for r in rows}
    assert ("hourly", "2026-07-07T06:00:00.000Z") in keys
    assert ("instant", "2026-07-07T07:00:00.000Z") in keys


# --- cross-field invariants: detect, pass-clean, and skip-when-absent -------
def test_device_invariants_clean_row_passes():
    clean = {"pm1": {"conc": 5}, "pm25": {"conc": 8, "aqius": 44},
             "pm10": {"conc": 11, "aqius": 10}, "aqius": 44, "mainus": "pm25"}
    assert nz.check_device_invariants(clean) == []

def test_device_invariant_pm_order():
    bad = {"pm1": {"conc": 10}, "pm25": {"conc": 5}, "pm10": {"conc": 20}}
    assert any("PM order" in m for m in nz.check_device_invariants(bad))

def test_device_invariant_aqi_not_max():
    bad = {"pm25": {"conc": 5, "aqius": 30}, "pm10": {"conc": 20, "aqius": 40},
           "aqius": 30, "mainus": "pm25"}
    assert any("max(component)" in m for m in nz.check_device_invariants(bad))

def test_device_invariant_main_pollutant_mismatch():
    # mainus says pm25 but pm25's aqi (30) != overall aqius (40)
    bad = {"pm25": {"conc": 5, "aqius": 30}, "pm10": {"conc": 20, "aqius": 40},
           "aqius": 40, "mainus": "pm25"}
    assert any("mainus" in m for m in nz.check_device_invariants(bad))

def test_device_invariants_skip_when_fields_absent():
    # a daily/monthly-style row with no overall aqius and bare pm1 must not false-positive
    partial = {"pm1": 5, "pm25": {"conc": 8}, "pm10": {"conc": 11}}
    assert nz.check_device_invariants(partial) == []

def test_station_invariant_heat_index_below_temp():
    bad = {"heatIndex": 20, "temperature": 30}
    assert any("heat_index" in m for m in nz.check_station_invariants(bad))

def test_station_invariant_aqi_mismatch():
    bad = {"heatIndex": 35, "temperature": 30, "aqius": 60, "pm25": {"aqius": 50}}
    assert any("pm25 aqi" in m for m in nz.check_station_invariants(bad))

def test_station_invariants_clean_and_absent():
    clean = {"heatIndex": 38, "temperature": 31, "aqius": 44, "pm25": {"aqius": 44}}
    assert nz.check_station_invariants(clean) == []
    assert nz.check_station_invariants({"pm25": {"conc": 8}}) == []  # nothing to check


# --- scan_quality: integrates over both payloads ----------------------------
def test_scan_quality_finds_planted_violation():
    root = {
        "historical": {"instant": [
            {"ts": "t1", "pm1": {"conc": 5}, "pm25": {"conc": 8}, "pm10": {"conc": 11}},   # clean
            {"ts": "t2", "pm1": {"conc": 20}, "pm25": {"conc": 8}, "pm10": {"conc": 11}},  # PM order bad
        ]},
    }
    validated = {"historical": {"hourly": []}}
    findings = nz.scan_quality(root, validated)
    assert len(findings) == 1
    endpoint, resolution, ts, reasons = findings[0]
    assert endpoint == "device" and ts == "t2"
    assert any("PM order" in r for r in reasons)
