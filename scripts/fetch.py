"""fetch.py — Fetch a current snapshot every 30 min from both Device API v2 endpoints and write to Postgres."""
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
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL")


def http_session():
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
        "User-Agent": "air-quality-pipeline/1.0 (fetch)",
    })
    return s


def fetch(sess, url):
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def upsert_one(cur, table, cols, row):
    updatable = [c for c in cols if c not in ("resolution", "ts")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (resolution, ts) DO UPDATE SET {set_clause}"
    )
    execute_values(cur, sql, [row])


def ping(url):
    """Ping the dead-man's switch. Network errors are swallowed — letting it time out is correct."""
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except Exception:
        pass


def main():
    if not SUPABASE_DB_URL:
        sys.exit("Missing SUPABASE_DB_URL in environment (.env).")

    sess = http_session()

    try:
        root = fetch(sess, ROOT_URL)
        validated = fetch(sess, VALIDATED_URL)
    except Exception as e:
        print(f"API fetch error: {e}", file=sys.stderr)
        sys.exit(1)

    cur_device = root.get("current")
    if not (cur_device and cur_device.get("ts")):
        print("Root endpoint returned no current.ts.", file=sys.stderr)
        sys.exit(1)

    cur_station = validated.get("current")

    device_row = nz.device_row(cur_device, "instant", Json)
    station_row = (
        nz.station_row(cur_station, "instant", Json)
        if (cur_station and cur_station.get("ts"))
        else None
    )

    with psycopg2.connect(SUPABASE_DB_URL) as conn:
        with conn.cursor() as cur:
            upsert_one(cur, "device_readings", nz.DEVICE_COLS, device_row)
            if station_row:
                upsert_one(cur, "station_readings", nz.STATION_COLS, station_row)
        conn.commit()

    ts = cur_device.get("ts", "?")
    print(f"OK ts={ts} | co2={cur_device.get('co2')} | pm25={nz._conc(cur_device.get('pm25'))}")
    ping(HEALTHCHECKS_URL)


if __name__ == "__main__":
    main()
