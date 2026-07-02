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

    aqius        integer,                           -- overall US AQI (may differ from pm25_aqius when CO2/PM10 is dominant)
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
--    Joined on hour bucket (NOT on ts directly) because root data can be
--    minute-level while validated is hourly only.
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
      AND s.ts = date_trunc('hour', d.ts)
WHERE d.resolution = 'hourly';


-- ---------------------------------------------------------------------
-- 4) Freshness alert query (use in Grafana Alerting)
--    Age of the latest measurement in seconds. Alert when > 5400 (90 min).
-- ---------------------------------------------------------------------
-- SELECT EXTRACT(EPOCH FROM (now() - max(ts))) AS age_seconds
-- FROM device_readings WHERE resolution = 'instant';
