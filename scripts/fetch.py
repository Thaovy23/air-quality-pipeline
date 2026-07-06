"""fetch.py — Poll both Device API v2 endpoints every 30 min and write to Postgres.

The device samples roughly once a minute; root.historical.instant retains ~1h of that
minute-level history. Since our poll interval (30 min) is shorter than that retention
window, pulling the whole instant array each run (not just 'current') and upserting it
means no minute-level reading is ever lost, even if a poll is briefly delayed.
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


def build_device_instant_rows(root):
    """root.historical.instant (~1h of minute-level readings) + current -> deduplicated
    tuple list, keyed by ts. current overwrites a duplicate historical entry."""
    by_key = {}
    hist = root.get("historical", {}) or {}
    for r in hist.get("instant", []) or []:
        if r.get("ts"):
            by_key[r["ts"]] = nz.device_row(r, "instant", Json)
    cur = root.get("current")
    if cur and cur.get("ts"):
        by_key[cur["ts"]] = nz.device_row(cur, "instant", Json)
    return list(by_key.values())


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

    device_rows = build_device_instant_rows(root)
    station_rows = (
        [nz.station_row(cur_station, "instant", Json)]
        if (cur_station and cur_station.get("ts"))
        else []
    )

    with psycopg2.connect(SUPABASE_DB_URL) as conn:
        with conn.cursor() as cur:
            n1 = upsert(cur, "device_readings", nz.DEVICE_COLS, device_rows)
            n2 = upsert(cur, "station_readings", nz.STATION_COLS, station_rows)
        conn.commit()

    ts = cur_device.get("ts", "?")
    print(f"OK ts={ts} | co2={cur_device.get('co2')} | pm25={nz._conc(cur_device.get('pm25'))} "
          f"| device_rows={n1} station_rows={n2}")
    ping(HEALTHCHECKS_URL)


if __name__ == "__main__":
    main()
