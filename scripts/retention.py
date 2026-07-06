"""retention.py — Archive-then-delete instant rows older than the retention window.

Only 'instant' rows are pruned: hourly/daily/monthly are vendor-computed rollups pulled
every fetch.py run, so long-term trend history survives independent of how much
minute-level detail is kept. Before deleting, each run exports the doomed rows as a
gzipped CSV to Supabase Storage — a 1GB tier separate from the 500MB database — so
nothing is lost to a cold archive.

Fail-safe ordering: archive upload happens BEFORE the delete. If the upload fails, the
exception propagates and the delete never runs — better to keep un-archived rows than
to lose them. The delete uses the same fixed cutoff value as the select, so it removes
exactly what was archived.

Idempotent: the archive is uploaded with x-upsert (re-running the same day overwrites
the same file), and DELETE ... WHERE ts < cutoff is naturally repeatable.

Required env: SUPABASE_DB_URL, plus SUPABASE_URL + SUPABASE_SERVICE_KEY (only needed
when there are rows to archive). Optional: ARCHIVE_BUCKET (default 'archive'),
RETENTION_DAYS (default 90).
Usage: python scripts/retention.py
"""
import csv
import gzip
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")               # https://<ref>.supabase.co
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ARCHIVE_BUCKET = os.getenv("ARCHIVE_BUCKET", "archive")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))

TABLES = ("device_readings", "station_readings")


def fetch_doomed(cur, table, cutoff):
    """Rows to be removed: instant older than cutoff. Returns (colnames, rows)."""
    cur.execute(
        f"SELECT * FROM {table} WHERE resolution = 'instant' AND ts < %s ORDER BY ts",
        (cutoff,),
    )
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def _cell(v):
    """jsonb comes back from psycopg2 as dict/list — serialize as JSON so the archive
    stays valid JSON rather than Python repr."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def to_gzip_csv(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(c) for c in r])
    return gzip.compress(buf.getvalue().encode("utf-8"))


def upload(path, data):
    """PUT-like upsert to Supabase Storage. Raises if credentials are missing or the
    request fails — callers rely on this to abort before deleting."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY not set — cannot archive, refusing to delete."
        )
    url = f"{SUPABASE_URL}/storage/v1/object/{ARCHIVE_BUCKET}/{path}"
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/gzip",
            "x-upsert": "true",
        },
        data=data,
        timeout=60,
    )
    r.raise_for_status()


def delete_doomed(cur, table, cutoff):
    cur.execute(
        f"DELETE FROM {table} WHERE resolution = 'instant' AND ts < %s", (cutoff,)
    )
    return cur.rowcount


def main():
    if not SUPABASE_DB_URL:
        sys.exit("Missing SUPABASE_DB_URL in environment (.env).")

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with psycopg2.connect(SUPABASE_DB_URL) as conn:
        for table in TABLES:
            with conn.cursor() as cur:
                cols, rows = fetch_doomed(cur, table, cutoff)
                if not rows:
                    print(f"{table}: 0 instant rows older than {cutoff.date()} "
                          f"({RETENTION_DAYS}d) — nothing to archive/delete.")
                    continue
                path = f"{table}/{stamp}.csv.gz"
                blob = to_gzip_csv(cols, rows)
                upload(path, blob)  # raises on failure -> delete below is skipped
                print(f"{table}: archived {len(rows)} rows -> "
                      f"{ARCHIVE_BUCKET}/{path} ({len(blob)} bytes)")
                n = delete_doomed(cur, table, cutoff)
                print(f"{table}: deleted {n} rows.")
            conn.commit()


if __name__ == "__main__":
    main()
