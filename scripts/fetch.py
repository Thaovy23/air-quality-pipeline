"""fetch.py — Poll both Device API v2 endpoints every 30 min and write to Postgres.

Each run pulls the full historical set at all four resolutions (instant/hourly/daily/
monthly) plus current, not just the latest snapshot. Every resolution's API retention
window is wider than the 30-min poll interval, so upserting the whole set each run keeps
a gap-free history at every granularity and avoids the 'frozen' hourly/daily/monthly that
resulted from only writing instant. Uses the shared builders in normalize.py, the same
ones backfill.py uses — the two scripts now differ only in intent (recurring vs one-shot).
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

    device_rows = nz.build_device_rows(root, Json)
    station_rows = nz.build_station_rows(validated, Json)

    if not device_rows:
        # No history and no current from root — nothing worth writing; skip the ping so the
        # dead-man's switch fires if this keeps happening.
        print("Root endpoint returned no usable rows.", file=sys.stderr)
        sys.exit(1)

    # Cross-field data-quality scan: FLAG, don't block. A violation is a data-correctness
    # signal, not a pipeline-liveness one — so we still write every row and still ping the
    # dead-man's switch below (the pipeline is alive and doing its job).
    findings = nz.scan_quality(root, validated)
    for endpoint, resolution, ts_v, reasons in findings:
        print(f"QC WARNING [{endpoint}/{resolution} ts={ts_v}]: {'; '.join(reasons)}",
              file=sys.stderr)

    # Coverage KPI: did the API deliver all 6 expected (endpoint, resolution)
    # buckets? Below 6 is a correctness signal, not a liveness one — still write,
    # still ping. Persisted to pipeline_run_log so it can be trended in Grafana.
    cov = nz.coverage(device_rows, station_rows)
    if cov["missing"]:
        print(f"COVERAGE WARNING: {cov['covered']}/{cov['expected']} buckets, "
              f"missing {cov['missing']}", file=sys.stderr)

    with psycopg2.connect(SUPABASE_DB_URL) as conn:
        with conn.cursor() as cur:
            n1 = upsert(cur, "device_readings", nz.DEVICE_COLS, device_rows)
            n2 = upsert(cur, "station_readings", nz.STATION_COLS, station_rows)
            # coverage_pct is a generated column — do not insert it (DB derives it).
            cur.execute(
                "INSERT INTO pipeline_run_log (device_res, station_res, missing, "
                "covered, expected, device_rows, station_rows, qc_violations) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (cov["device_res"], cov["station_res"], cov["missing"], cov["covered"],
                 cov["expected"], n1, n2, len(findings)),
            )
        conn.commit()

    cur_device = root.get("current") or {}
    ts = cur_device.get("ts", "?")
    print(f"OK ts={ts} | co2={cur_device.get('co2')} | pm25={nz._conc(cur_device.get('pm25'))} "
          f"| device_rows={n1} station_rows={n2} | qc_violations={len(findings)} "
          f"| coverage={cov['covered']}/{cov['expected']}")
    ping(HEALTHCHECKS_URL)


if __name__ == "__main__":
    main()
