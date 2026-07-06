"""backfill.py — Seed historical data from Device API v2 into both Postgres tables.

Run once to populate history so the dashboard is not empty from the start.
  * Root /v2/{id}      -> device_readings : instant + hourly + daily + monthly
  * /validated-data    -> station_readings: current (instant) + hourly

Idempotent: ON CONFLICT (resolution, ts) DO UPDATE — safe to re-run.

Required env var: SUPABASE_DB_URL (Postgres connection string)
Usage: python scripts/backfill.py
"""
import os
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

import psycopg2
from psycopg2.extras import execute_values, Json

import normalize as nz

load_dotenv()

DEVICE_ID = os.getenv("DEVICE_ID", "67ffba771bfde07577804b08")
BASE = f"https://device.iqair.com/v2/{DEVICE_ID}"
ROOT_URL = BASE
VALIDATED_URL = f"{BASE}/validated-data"

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")


def http_session():
    """HTTP session with retry/backoff and a polite User-Agent (endpoint is an internal API)."""
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "Accept": "application/json",
        "User-Agent": "air-quality-pipeline/1.0 (backfill)",
    })
    return s


def fetch(sess, url):
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def upsert(cur, table, cols, rows):
    """Batch upsert with ON CONFLICT (resolution, ts) DO UPDATE."""
    if not rows:
        return 0
    updatable = [c for c in cols if c not in ("resolution", "ts")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (resolution, ts) DO UPDATE SET {set_clause}"
    )
    execute_values(cur, sql, rows, page_size=500)
    return len(rows)


def main():
    if not SUPABASE_DB_URL:
        sys.exit("Missing SUPABASE_DB_URL in environment (.env).")

    sess = http_session()
    print("Fetching data from API...")
    root = fetch(sess, ROOT_URL)
    validated = fetch(sess, VALIDATED_URL)

    device_rows = nz.build_device_rows(root, Json)
    station_rows = nz.build_station_rows(validated, Json)
    print(f"  device_readings : {len(device_rows)} rows")
    print(f"  station_readings: {len(station_rows)} rows")

    with psycopg2.connect(SUPABASE_DB_URL) as conn:
        with conn.cursor() as cur:
            n1 = upsert(cur, "device_readings", nz.DEVICE_COLS, device_rows)
            n2 = upsert(cur, "station_readings", nz.STATION_COLS, station_rows)
        conn.commit()

    print(f"Done. Upserted {n1} device_readings rows, {n2} station_readings rows.")


if __name__ == "__main__":
    main()
