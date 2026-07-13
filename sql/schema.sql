-- =====================================================================
--  AirVisual pipeline — Database schema (Postgres / Supabase)
--  Two tables: device_readings (indoor) + station_readings (outdoor)
--
--  Design notes:
--   * All ts stored as UTC (timestamptz). Display in ICT (+7) via Grafana.
--   * Pressure normalised to hPa in both tables (root returns Pa -> /100).
--   * raw jsonb column preserves the original payload for schema replay.
--   * Composite primary key (resolution, ts):
--       A daily row for 2026-06-01T00:00 and a monthly row for the same
--       ts would collide on ts alone. The resolution column keeps all four
--       levels (instant/hourly/daily/monthly) conflict-free.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) device_readings  —  Root endpoint  /v2/{id}
--    INDOOR sensor: CO2, PM1/2.5/10, temperature, humidity, pressure.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_readings (
    resolution   text        NOT NULL
                 CHECK (resolution IN ('instant','hourly','daily','monthly')),
    ts           timestamptz NOT NULL,              -- UTC

    co2          real,                              -- ppm
    pm1_conc     real,                              -- µg/m³

    pm25_conc    real,                              -- µg/m³
    pm25_aqius   integer,                           -- US AQI
    pm25_aqicn   integer,                           -- China AQI

    pm10_conc    real,                              -- µg/m³
    pm10_aqius   integer,
    pm10_aqicn   integer,

    temp_c       real,                              -- tp — INDOOR temperature (°C)
    humidity_pct real,                              -- hm — INDOOR humidity (%)
    pressure_hpa real,                              -- pr/100 — pressure (hPa)

    aqius        integer,                           -- overall US AQI = GREATEST(pm25_aqius, pm10_aqius); CO2 is measured but is NOT a US AQI pollutant (US AQI = O3/PM2.5/PM10/CO/SO2/NO2, and CO here means carbon monoxide, not CO2)
    aqicn        integer,                           -- overall China AQI
    mainus       text,                              -- dominant pollutant US, e.g. "pm25", "co2"
    maincn       text,                              -- dominant pollutant China

    raw          jsonb,                             -- original payload for this row
    inserted_at  timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (resolution, ts)
);

-- Index for time-range queries and latest-point lookups
CREATE INDEX IF NOT EXISTS idx_device_ts          ON device_readings (ts DESC);
CREATE INDEX IF NOT EXISTS idx_device_instant_ts  ON device_readings (ts DESC)
    WHERE resolution = 'instant';

COMMENT ON TABLE  device_readings IS 'Root endpoint — indoor sensors + history at 4 resolutions';
COMMENT ON COLUMN device_readings.temp_c        IS 'INDOOR temperature (root.tp)';
COMMENT ON COLUMN device_readings.pressure_hpa  IS 'Pressure hPa = root.pr / 100 (root returns Pa)';


-- ---------------------------------------------------------------------
-- 2) station_readings  —  /validated-data endpoint
--    PM2.5 (same device source) + OUTDOOR weather context.
--    Note: validated has only 'current' (weather-rich) and hourly history
--    (AQI + PM2.5 only). Weather columns will be NULL on hourly rows.
--    resolution: 'instant' = current snapshot; 'hourly' = history.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS station_readings (
    resolution     text        NOT NULL
                   CHECK (resolution IN ('instant','hourly')),
    ts             timestamptz NOT NULL,            -- UTC

    pm25_conc      real,                            -- µg/m³ (validated.pm25.concentration)
    pm25_aqius     integer,
    pm25_aqicn     integer,
    aqius          integer,                         -- overall US AQI
    aqicn          integer,                         -- overall China AQI

    temp_out_c     real,                            -- OUTDOOR temperature (°C)
    humidity_out   real,                            -- outdoor humidity (%)
    pressure_hpa   real,                            -- pressure (hPa) — validated returns hPa directly
    wind_speed     real,                            -- m/s
    wind_dir       integer,                         -- degrees 0..360
    condition      text,                            -- e.g. "Broken clouds"
    icon           text,                            -- e.g. "04d"
    heat_index     real,                            -- feels-like temperature (°C)
    mainus         text,                            -- dominant pollutant US
    maincn         text,                            -- dominant pollutant China

    raw            jsonb,
    inserted_at    timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (resolution, ts)
);

CREATE INDEX IF NOT EXISTS idx_station_ts ON station_readings (ts DESC);

COMMENT ON TABLE  station_readings IS 'Validated endpoint — PM2.5 + outdoor weather';
COMMENT ON COLUMN station_readings.temp_out_c IS 'OUTDOOR temperature (distinct from device_readings.temp_c)';


-- ---------------------------------------------------------------------
-- 3) Convenience view — compare INDOOR vs OUTDOOR metrics by hour
--    Both sides are resolution='hourly' rows pulled directly from each
--    endpoint's vendor-computed historical.hourly, so their ts values are
--    already round-hour — join on ts directly, no truncation needed.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_indoor_outdoor_hourly AS
SELECT
    d.ts                                   AS hour_utc,
    d.co2,
    d.pm25_conc      AS pm25_in,
    s.pm25_conc      AS pm25_out,
    d.temp_c         AS temp_in,
    s.temp_out_c     AS temp_out,
    d.humidity_pct   AS humidity_in,
    s.humidity_out   AS humidity_out,
    s.wind_speed,
    s.condition
FROM device_readings  d
LEFT JOIN station_readings s
       ON s.resolution = 'hourly'
      AND s.ts = d.ts
WHERE d.resolution = 'hourly';


-- ---------------------------------------------------------------------
-- 4) Freshness alert query (use in Grafana Alerting)
--    Age of the latest measurement in seconds. Alert when > 5400 (90 min).
-- ---------------------------------------------------------------------
-- SELECT EXTRACT(EPOCH FROM (now() - max(ts))) AS age_seconds
-- FROM device_readings WHERE resolution = 'instant';


-- ---------------------------------------------------------------------
-- 5) Data-quality anomaly views (long format: one row per violation)
--    Two check_type families:
--      cross_field — relationship between columns (verified to hold 100%)
--      range       — a single column holding a physically impossible value.
--                    Bounds are calibrated LOOSE from observed data to catch
--                    garbage (negative mass, humidity>100), not normal variation.
--    Flag, never block: offending rows are kept as evidence (see the
--    flag-not-block principle); Grafana counts/alerts, default expects 0 rows.
--    column_name + observed_value let a dashboard group and debug without
--    parsing the reason text.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_device_anomalies AS
-- cross_field
SELECT resolution, ts, 'cross_field' AS check_type, 'pm_order' AS column_name,
       format('pm1=%s pm25=%s pm10=%s', pm1_conc, pm25_conc, pm10_conc) AS observed_value,
       'expected PM1 <= PM2.5 <= PM10' AS reason
FROM device_readings WHERE pm1_conc > pm25_conc OR pm25_conc > pm10_conc
UNION ALL
SELECT resolution, ts, 'cross_field', 'aqi_consistency',
       format('aqius=%s pm25_aqi=%s pm10_aqi=%s', aqius, pm25_aqius, pm10_aqius),
       'overall AQI <> max(component AQI)'
FROM device_readings
WHERE aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND pm10_aqius IS NOT NULL
  AND aqius <> GREATEST(pm25_aqius, pm10_aqius)
UNION ALL
SELECT resolution, ts, 'cross_field', 'main_pollutant',
       format('mainus=%s aqius=%s pm25_aqi=%s pm10_aqi=%s', mainus, aqius, pm25_aqius, pm10_aqius),
       'main pollutant AQI mismatch'
FROM device_readings
WHERE (mainus = 'pm25' AND aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND pm25_aqius <> aqius)
   OR (mainus = 'pm10' AND aqius IS NOT NULL AND pm10_aqius IS NOT NULL AND pm10_aqius <> aqius)
-- range (garbage bounds; observed: co2 393-489, pm 1-179, temp 24.8-35.6, hum 48-91, pr 998-1008)
UNION ALL SELECT resolution, ts, 'range', 'co2',          co2::text,          'co2 outside (0, 10000]'         FROM device_readings WHERE co2 <= 0 OR co2 > 10000
UNION ALL SELECT resolution, ts, 'range', 'pm1_conc',     pm1_conc::text,     'pm1_conc < 0'                   FROM device_readings WHERE pm1_conc < 0
UNION ALL SELECT resolution, ts, 'range', 'pm25_conc',    pm25_conc::text,    'pm25_conc < 0'                  FROM device_readings WHERE pm25_conc < 0
UNION ALL SELECT resolution, ts, 'range', 'pm10_conc',    pm10_conc::text,    'pm10_conc < 0'                  FROM device_readings WHERE pm10_conc < 0
UNION ALL SELECT resolution, ts, 'range', 'temp_c',       temp_c::text,       'temp_c outside [-50, 70]'       FROM device_readings WHERE temp_c < -50 OR temp_c > 70
UNION ALL SELECT resolution, ts, 'range', 'humidity_pct', humidity_pct::text, 'humidity_pct outside [0, 100]'  FROM device_readings WHERE humidity_pct < 0 OR humidity_pct > 100
UNION ALL SELECT resolution, ts, 'range', 'pressure_hpa', pressure_hpa::text, 'pressure_hpa outside [800,1100]' FROM device_readings WHERE pressure_hpa < 800 OR pressure_hpa > 1100
UNION ALL SELECT resolution, ts, 'range', 'aqius',        aqius::text,        'aqius outside [0, 500]'          FROM device_readings WHERE aqius < 0 OR aqius > 500;

CREATE OR REPLACE VIEW v_station_anomalies AS
-- cross_field
SELECT resolution, ts, 'cross_field' AS check_type, 'heat_index_vs_temp' AS column_name,
       format('heat_index=%s temp=%s', heat_index, temp_out_c) AS observed_value,
       'heat_index < temperature' AS reason
FROM station_readings WHERE heat_index IS NOT NULL AND temp_out_c IS NOT NULL AND heat_index < temp_out_c
UNION ALL
SELECT resolution, ts, 'cross_field', 'aqi_source',
       format('aqius=%s pm25_aqi=%s', aqius, pm25_aqius), 'overall AQI <> PM2.5 AQI'
FROM station_readings WHERE aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND aqius <> pm25_aqius
-- range (observed: pm25 2-71, temp 24-35, hum 45-97, pr 1003-1013, wind 0.56-8.06, dir 2-360)
UNION ALL SELECT resolution, ts, 'range', 'pm25_conc',    pm25_conc::text,    'pm25_conc < 0'                  FROM station_readings WHERE pm25_conc < 0
UNION ALL SELECT resolution, ts, 'range', 'aqius',        aqius::text,        'aqius outside [0, 500]'          FROM station_readings WHERE aqius < 0 OR aqius > 500
UNION ALL SELECT resolution, ts, 'range', 'temp_out_c',   temp_out_c::text,   'temp_out_c outside [-50, 70]'    FROM station_readings WHERE temp_out_c < -50 OR temp_out_c > 70
UNION ALL SELECT resolution, ts, 'range', 'humidity_out', humidity_out::text, 'humidity_out outside [0, 100]'   FROM station_readings WHERE humidity_out < 0 OR humidity_out > 100
UNION ALL SELECT resolution, ts, 'range', 'pressure_hpa', pressure_hpa::text, 'pressure_hpa outside [800,1100]' FROM station_readings WHERE pressure_hpa < 800 OR pressure_hpa > 1100
UNION ALL SELECT resolution, ts, 'range', 'wind_speed',   wind_speed::text,   'wind_speed < 0'                 FROM station_readings WHERE wind_speed < 0
UNION ALL SELECT resolution, ts, 'range', 'wind_dir',     wind_dir::text,     'wind_dir outside [0, 360]'       FROM station_readings WHERE wind_dir < 0 OR wind_dir > 360
UNION ALL SELECT resolution, ts, 'range', 'heat_index',   heat_index::text,   'heat_index outside [-50, 80]'    FROM station_readings WHERE heat_index < -50 OR heat_index > 80;


-- ---------------------------------------------------------------------
-- 5b) v_column_health — per-column NULL rate over the last 24h (instant only).
--    Schema-drift canary: a key column silently going all-NULL means the API
--    changed. Windowed to 24h and to resolution='instant' on purpose — an
--    all-time view would false-alarm (aqius is ~98% NULL historically because
--    it was added late), and station weather columns are NULL by design on
--    hourly rows. The LATERAL VALUES unpivot keeps one row per (column) tidy.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_column_health AS
SELECT 'device_readings' AS table_name, 'instant' AS resolution, kv.column_name,
       count(*) AS rows_recent,
       count(*) FILTER (WHERE kv.val IS NULL) AS null_count,
       round(100.0 * count(*) FILTER (WHERE kv.val IS NULL) / NULLIF(count(*), 0), 1) AS null_pct
FROM device_readings d
CROSS JOIN LATERAL (VALUES
    ('co2', d.co2::double precision), ('pm1_conc', d.pm1_conc::double precision),
    ('pm25_conc', d.pm25_conc::double precision), ('pm10_conc', d.pm10_conc::double precision),
    ('temp_c', d.temp_c::double precision), ('humidity_pct', d.humidity_pct::double precision),
    ('pressure_hpa', d.pressure_hpa::double precision)
) AS kv(column_name, val)
WHERE d.resolution = 'instant' AND d.ts > now() - interval '24 hours'
GROUP BY kv.column_name
UNION ALL
SELECT 'station_readings', 'instant', kv.column_name,
       count(*), count(*) FILTER (WHERE kv.val IS NULL),
       round(100.0 * count(*) FILTER (WHERE kv.val IS NULL) / NULLIF(count(*), 0), 1)
FROM station_readings s
CROSS JOIN LATERAL (VALUES
    ('pm25_conc', s.pm25_conc::double precision), ('aqius', s.aqius::double precision),
    ('temp_out_c', s.temp_out_c::double precision), ('humidity_out', s.humidity_out::double precision),
    ('pressure_hpa', s.pressure_hpa::double precision), ('wind_speed', s.wind_speed::double precision),
    ('wind_dir', s.wind_dir::double precision), ('heat_index', s.heat_index::double precision)
) AS kv(column_name, val)
WHERE s.resolution = 'instant' AND s.ts > now() - interval '24 hours'
GROUP BY kv.column_name;


-- ---------------------------------------------------------------------
-- 6) pipeline_run_log — one row per fetch run (Coverage KPI + run summary)
--    Answers what freshness/liveness cannot: "the pipeline ran, but did it
--    write EVERYTHING it should have?" Expected coverage is 6 buckets:
--    device {instant,hourly,daily,monthly} + station {instant,hourly}.
--    A run with coverage_pct < 1.0 means the API dropped a resolution.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_run_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_at         timestamptz NOT NULL DEFAULT now(),
    device_res     text[],                            -- resolutions present this run
    station_res    text[],
    missing        text[],                            -- expected buckets absent, e.g. {device:daily}
    covered        integer NOT NULL,                  -- expected buckets that got >=1 row
    expected       integer NOT NULL DEFAULT 6,        -- device{4} + station{2}
    -- Ratio derived by the DB from covered/expected: single source of truth, so a
    -- buggy caller can never insert a wrong percentage.
    coverage_pct   numeric GENERATED ALWAYS AS (
                       CASE WHEN expected = 0 THEN NULL ELSE covered::numeric / expected END
                   ) STORED,
    device_rows    integer NOT NULL,                  -- rows built & upserted (ON CONFLICT upserts every row sent)
    station_rows   integer NOT NULL,
    qc_violations  integer NOT NULL DEFAULT 0,        -- cross-field violations this run

    -- Constraints protect this log (our own computed metadata) against buggy inserts.
    -- NOTE: no such CHECK is placed on the sensor tables — bad vendor readings are
    -- kept as evidence and flagged by the anomaly views (flag, not block).
    CONSTRAINT chk_run_log_expected CHECK (expected = 6),
    CONSTRAINT chk_run_log_covered  CHECK (covered BETWEEN 0 AND expected),
    CONSTRAINT chk_run_log_counts   CHECK (device_rows >= 0 AND station_rows >= 0 AND qc_violations >= 0)
);

CREATE INDEX IF NOT EXISTS idx_run_log_run_at ON pipeline_run_log (run_at DESC);

-- Coverage KPI for Grafana (report the ratio of full-coverage runs, not an average;
-- compare integers covered=expected, never the float coverage_pct):
-- SELECT count(*) FILTER (WHERE covered = expected)::real / NULLIF(count(*), 0) AS full_coverage_ratio
-- FROM pipeline_run_log WHERE run_at > now() - interval '7 days';
