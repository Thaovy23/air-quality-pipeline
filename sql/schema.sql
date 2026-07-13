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
-- 5) Cross-field data-quality anomaly views
--    Each invariant below verified to hold on 100% of existing rows, so any
--    row surfacing here signals a real sensor/parse fault (a canary). fetch.py
--    flags these at ingestion but never drops them — the offending row is kept
--    as evidence. Grafana can COUNT/alert on these views (default: expect 0 rows).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_device_anomalies AS
SELECT resolution, ts, pm1_conc, pm25_conc, pm10_conc,
       aqius, pm25_aqius, pm10_aqius, mainus,
       CASE
         WHEN pm1_conc > pm25_conc OR pm25_conc > pm10_conc
              THEN 'PM order violated (expect PM1 <= PM2.5 <= PM10)'
         WHEN aqius <> GREATEST(pm25_aqius, pm10_aqius)
              THEN 'overall AQI <> max(component AQI)'
         ELSE 'main pollutant AQI mismatch'
       END AS reason
FROM device_readings
WHERE pm1_conc > pm25_conc
   OR pm25_conc > pm10_conc
   OR (aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND pm10_aqius IS NOT NULL
       AND aqius <> GREATEST(pm25_aqius, pm10_aqius))
   OR (mainus = 'pm25' AND aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND pm25_aqius <> aqius)
   OR (mainus = 'pm10' AND aqius IS NOT NULL AND pm10_aqius IS NOT NULL AND pm10_aqius <> aqius);

CREATE OR REPLACE VIEW v_station_anomalies AS
SELECT resolution, ts, temp_out_c, heat_index, aqius, pm25_aqius,
       CASE
         WHEN heat_index < temp_out_c THEN 'heat_index < temperature'
         ELSE 'overall AQI <> PM2.5 AQI'
       END AS reason
FROM station_readings
WHERE (heat_index IS NOT NULL AND temp_out_c IS NOT NULL AND heat_index < temp_out_c)
   OR (aqius IS NOT NULL AND pm25_aqius IS NOT NULL AND aqius <> pm25_aqius);


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
