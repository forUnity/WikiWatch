"""List the properties a metric wrote values for in a run, with their Wikidata labels.

For metrics stored in a per-property table (e.g. `metric_on_property_value_float`), prints every
property_id the metric has rows for in the run, with row count and first/last timestamp.

The cluster can reach the database but not Wikidata, the local PC the other way round, so this runs
in two stages that hand over through CSVs next to this script (both tracked in git):

  1. (default, on the cluster) query the database, print the table and overwrite
     `list_written_properties_result.csv`. Labels already in `property_names.csv` are shown.
  2. (--query-property-names, locally) read the result CSV, fetch labels missing from
     `property_names.csv` from the Wikidata API, add them to that CSV and print the table.

Usage:
    python scripts/list_written_properties.py --run-id 1800355 --metric-id 42
    python scripts/list_written_properties.py --query-property-names
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RESULT_CSV = SCRIPT_DIR / "list_written_properties_result.csv"
NAMES_CSV = SCRIPT_DIR / "property_names.csv"
RESULT_COLUMNS = ("run_id", "metric_id", "metric_name", "table_name", "property_id", "rows", "first_timestamp", "last_timestamp")
NAMES_COLUMNS = ("property_id", "label")

# Same lookup as the label resolution in preference_graph_details/query_preference_graph.py
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "mp2025-wikiwatch-property-names/1.0 (hpi.de)"
LABEL_LANG = "en"
LABEL_BATCH_SIZE = 40
LABEL_BATCH_PAUSE = 0.1
THROTTLED_PAUSE = 2.0
TIMEOUT = 10


# --------------------------------------------------------------------------- #
# Stage 1: database (cluster)
# --------------------------------------------------------------------------- #


def written_properties(engine, run_id: int, metric_id: int) -> list[dict]:
    from sqlalchemy import text

    with engine.connect() as conn:
        metric = conn.execute(text("SELECT name, table_name FROM metrics WHERE id = :id"), {"id": metric_id}).first()
        if metric is None:
            raise SystemExit(f"Metric {metric_id} does not exist")
        name, table = metric.name, metric.table_name

        columns = set(conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ), {"table": table}).scalars())
        if not columns:
            raise SystemExit(f"Table {table} of metric {metric_id} does not exist")
        if "property_id" not in columns:
            raise SystemExit(f"Metric {metric_id} ({name}) writes to {table}, which has no property_id column")

        # (run_id, metric_id) is the primary key prefix, so this only touches the metric's rows
        rows = conn.execute(text(f"""
            SELECT property_id, COUNT(*) AS n, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
            FROM "{table}"
            WHERE run_id = :run_id AND metric_id = :metric_id
            GROUP BY property_id
            ORDER BY property_id
        """), {"run_id": run_id, "metric_id": metric_id}).all()

    return [
        {
            "run_id": run_id, "metric_id": metric_id, "metric_name": name, "table_name": table,
            "property_id": property_id, "rows": n, "first_timestamp": str(first_ts), "last_timestamp": str(last_ts),
        }
        for property_id, n, first_ts, last_ts in rows
    ]


# --------------------------------------------------------------------------- #
# CSV hand-over
# --------------------------------------------------------------------------- #


def write_csv_atomic(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    """Write through a sibling temp file, so an interrupted write leaves the old CSV intact."""
    temp_path = path.with_suffix(path.suffix + ".partial")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, path)


def read_result_csv() -> list[dict]:
    if not RESULT_CSV.exists():
        raise SystemExit(f"{RESULT_CSV} does not exist; run stage 1 (--run-id, --metric-id) on the cluster first")
    with RESULT_CSV.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in ("run_id", "metric_id", "property_id", "rows"):
            row[key] = int(row[key])
    return rows


def read_names_csv() -> dict[int, str]:
    """property_id -> label. An empty label means Wikidata has none in LABEL_LANG (or no such property)."""
    if not NAMES_CSV.exists():
        return {}
    with NAMES_CSV.open(encoding="utf-8", newline="") as handle:
        return {int(row["property_id"]): row["label"] for row in csv.DictReader(handle)}


def write_names_csv(names: dict[int, str]) -> None:
    rows = [{"property_id": property_id, "label": label} for property_id, label in sorted(names.items())]
    write_csv_atomic(NAMES_CSV, NAMES_COLUMNS, rows)


# --------------------------------------------------------------------------- #
# Stage 2: Wikidata (local)
# --------------------------------------------------------------------------- #


def fetch_property_labels(property_ids: list[int]) -> dict[int, str]:
    """Fetch labels in batches. A failed batch is skipped (and retried on the next run), never raised."""
    labels: dict[int, str] = {}
    for start in range(0, len(property_ids), LABEL_BATCH_SIZE):
        batch = property_ids[start : start + LABEL_BATCH_SIZE]
        query = urllib.parse.urlencode({
            "action": "wbgetentities",
            "props": "labels",
            "ids": "|".join(f"P{property_id}" for property_id in batch),
            "languages": LABEL_LANG,
            "format": "json",
        })
        request = urllib.request.Request(f"{WIKIDATA_API}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            print(f"Warning: Wikidata refused the batch starting at P{batch[0]} (HTTP {error.code}); skipping it.")
            if error.code in (403, 429):
                time.sleep(THROTTLED_PAUSE)
            continue
        except Exception as error:
            print(f"Warning: could not fetch labels for the batch starting at P{batch[0]} ({error}); skipping it.")
            continue

        if "error" in payload:
            print(f"Warning: Wikidata rejected the batch starting at P{batch[0]} ({payload['error'].get('info')}); skipping it.")
            continue
        for entity_id, entity in payload.get("entities", {}).items():
            # Deleted/nonexistent properties come back as {"missing": ""}; they get an empty label too
            labels[int(entity_id.lstrip("P"))] = entity.get("labels", {}).get(LABEL_LANG, {}).get("value", "")
        time.sleep(LABEL_BATCH_PAUSE)
    return labels


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def print_table(rows: list[dict], names: dict[int, str], run_id: int | None = None, metric_id: int | None = None) -> None:
    if not rows:
        print(f"metric {metric_id} in run {run_id}: 0 properties")
        return
    first = rows[0]
    print(f"metric {first['metric_id']} ({first['metric_name']}) in {first['table_name']}, run {first['run_id']}: {len(rows)} properties")
    print(f"{'property':>10}  {'rows':>8}  {'first timestamp':<25}  {'last timestamp':<25}  label")
    for row in rows:
        label = names.get(row["property_id"])
        label = "?" if label is None else label or "-"
        print(f"{'P' + str(row['property_id']):>10}  {row['rows']:>8}  {row['first_timestamp']:<25}  {row['last_timestamp']:<25}  {label}")


def stage_database(run_id: int, metric_id: int) -> None:
    from list_run_metrics import build_engine_read_only

    rows = written_properties(build_engine_read_only(), run_id, metric_id)
    write_csv_atomic(RESULT_CSV, RESULT_COLUMNS, rows)
    names = read_names_csv()
    print_table(rows, names, run_id, metric_id)
    print(f"\nWrote {RESULT_CSV.name}.", end=" ")
    missing = sum(row["property_id"] not in names for row in rows)
    if missing:
        print(f"{missing} label(s) unknown (?): run with --query-property-names where Wikidata is reachable.")
    else:
        print()


def stage_names() -> None:
    rows = read_result_csv()
    if not rows:
        print(f"{RESULT_CSV.name} has no properties; the last stage 1 run found nothing to label.")
        return
    names = read_names_csv()
    missing = sorted({row["property_id"] for row in rows} - names.keys())
    if missing:
        fetched = fetch_property_labels(missing)
        if fetched:
            names.update(fetched)
            write_names_csv(names)
        print(f"Fetched {len(fetched)} of {len(missing)} missing label(s) into {NAMES_CSV.name}.\n")
    print_table(rows, names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--metric-id", type=int)
    parser.add_argument("--query-property-names", action="store_true",
                        help=f"stage 2: label the properties in {RESULT_CSV.name} via Wikidata instead of querying the database")
    args = parser.parse_args()

    if args.query_property_names:
        if args.run_id is not None or args.metric_id is not None:
            parser.error(f"--query-property-names works on {RESULT_CSV.name} and takes no --run-id/--metric-id")
        stage_names()
    else:
        if args.run_id is None or args.metric_id is None:
            parser.error("--run-id and --metric-id are required (or pass --query-property-names)")
        stage_database(args.run_id, args.metric_id)


if __name__ == "__main__":
    main()
