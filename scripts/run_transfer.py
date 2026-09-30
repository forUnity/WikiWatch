"""Move the results of metric runs from the cluster database into another Postgres with the same schema.

`export` writes one self-contained folder per run:

  run_<id>/
    manifest.json            run id, source database, tool commit; per file: columns and types,
                             row count, sha256 and the rows per metric id
    runs.csv.gz              the run's row of `runs`
    metrics.csv.gz           the `metrics` rows the run wrote values for
    <table>.csv.gz           the run's rows of every table with run_id and metric_id columns

All files of a run come from one snapshot, so a run that is still writing cannot produce files
that disagree with each other.

`import` loads export folders into the database of --env-file, each run in its own transaction.
Run ids and metric ids are kept exactly as they are in the export; nothing is ever remapped. A
run is written completely or not at all, and the import aborts without writing when:
  - a metric id is on the target with another name or table, or a metric name with another id
  - the run id is on the target for a different run (other user, git commit or creation time)
  - a table on the target has other columns or types
Tables missing on the target are created from the exported columns, with the keys of the ORM
models in metric_computation_scripts/datahandler.py; a new result table gets its keys only after
its rows are loaded, which is much faster for large runs. Importing a run that is already there is skipped; --replace deletes that run's rows and loads it
again. Other runs are never touched.

The target only hosts imported results for read-only analysis and never creates runs or metrics
itself, so its id sequences are left alone.

Usage:
    python scripts/run_transfer.py export --run-id 1800355 1812044 --out /sc/projects/sci-naumann/mpws2025fn1/exports
    python scripts/run_transfer.py import exports/run_1800355 exports/run_1812044 --env-file .env.server --dry-run
    python scripts/run_transfer.py import exports/run_1800355 exports/run_1812044 --env-file .env.server

DB_HOST in the env file is optional on the cluster: without it the host of the wikiwatch-db
SLURM job is used.

The files are gzipped CSV with a header, so one table can also be loaded by hand, e.g.
    \\copy metric_value_float (run_id, metric_id, timestamp, value) FROM PROGRAM 'gunzip -c run_1800355/metric_value_float.csv.gz' CSV HEADER
That skips every check above, so load metrics.csv.gz and runs.csv.gz first and check the ids yourself.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ

log = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "exports")

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
RUNS_TABLE = "runs"
METRICS_TABLE = "metrics"

# UTC and ISO dates so timestamptz values are written and read back unchanged; extra_float_digits=3
# prints floats with enough digits to round-trip exactly on every Postgres version
SESSION_OPTIONS = "-c TimeZone=UTC -c DateStyle=ISO,YMD -c extra_float_digits=3 -c client_encoding=UTF8"
# Imports all write to `metrics`, so they take this advisory lock one after another
IMPORT_LOCK_KEY = 0x77696B69
COPY_CHUNK_SIZE = 1 << 20

_CXNODE_RE = re.compile(r"^cx(\d+)$")


class TransferError(RuntimeError):
    """A check failed. The message says what differs; nothing of the affected run was written."""


@contextmanager
def _timed_step(step_name: str):
    started = time.perf_counter()
    log.info("[START] %s", step_name)
    try:
        yield
    finally:
        log.info("[DONE] %s in %.2fs", step_name, time.perf_counter() - started)


# --------------------------------------------------------------------------- connection

def _get_db_host() -> str:
    """Host of the wikiwatch-db SLURM job, as in log_parsing/paper_plotter/paper_plots/db.py."""
    try:
        cxnode = subprocess.check_output(
            ["squeue", "--name=wikiwatch-db", "--noheader", "--format=%N"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise TransferError(f"DB_HOST is not set and squeue is not available to find wikiwatch-db ({error})") from error

    cxnode = cxnode.splitlines()[0].strip() if cxnode else ""
    match = _CXNODE_RE.fullmatch(cxnode)
    if match is None:
        raise TransferError(f"Could not resolve the wikiwatch-db node. Expected something like 'cx13', got {cxnode!r}.")
    return f"10.130.31.{int(match.group(1))}"


def connect(env_file: str, read_only: bool):
    if not os.path.isfile(env_file):
        raise TransferError(f"No env file at {env_file}")
    # The file passed on the command line wins over variables already set in the shell
    load_dotenv(env_file, override=True)

    settings = {name: os.environ.get(name) for name in ("DB_USER", "DB_NAME")}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise TransferError(f"Missing DB settings in {env_file}: {', '.join(missing)}")
    host = os.environ.get("DB_HOST") or _get_db_host()

    options = SESSION_OPTIONS + (" -c default_transaction_read_only=on" if read_only else "")
    conn = psycopg2.connect(
        dbname=settings["DB_NAME"],
        user=settings["DB_USER"],
        password=os.environ.get("DB_PASS") or None,
        host=host,
        port=os.environ.get("DB_PORT") or "5432",
        connect_timeout=30,
        gssencmode="disable",
        options=options,
    )
    log.info("Connected to database %s on %s (%s)", settings["DB_NAME"], host, "read-only" if read_only else "read-write")
    return conn


# --------------------------------------------------------------------------- schema

def result_tables(cur) -> list[str]:
    """Tables holding per-run metric values: every base table of the current schema with run_id and metric_id."""
    cur.execute("""
        SELECT c.table_name
        FROM information_schema.columns AS c
        JOIN information_schema.tables AS t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.table_schema = current_schema()
          AND t.table_type = 'BASE TABLE'
          AND c.column_name IN ('run_id', 'metric_id')
        GROUP BY c.table_name
        HAVING count(*) = 2
        ORDER BY c.table_name
    """)
    return [name for (name,) in cur.fetchall() if not name.startswith("cache_")]


def table_columns(cur, table: str) -> list[list[str]] | None:
    """[name, type] of each column in table order, or None if the table does not exist."""
    cur.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute AS a
        WHERE a.attrelid = to_regclass(quote_ident(%s))
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
    """, (table,))
    return [[name, type_] for name, type_ in cur.fetchall()] or None


# Enum types of `metrics`: SQLAlchemy names them after Type and Dimension in datahandler.py and uses the member names as labels
ENUM_TYPES = {
    "type": ["type_float", "type_int", "type_interval", "type_bigint", "type_datetime"],
    "dimension": ["default"],
}


def create_missing_tables(cur, entries: dict[str, dict]) -> list[str]:
    """Create the exported tables the target lacks, keyed like the ORM models. Runs and metrics get their keys
    right away; the result tables are returned without keys so add_result_table_keys can add them after the COPY,
    which is much faster than updating the index and checking foreign keys row by row."""
    created_result_tables = []
    for table, entry in entries.items():
        if table_columns(cur, table) is not None:
            continue
        for _, type_ in entry["columns"]:
            if type_ in ENUM_TYPES:
                cur.execute("SELECT to_regtype(%s)", (type_,))
                if cur.fetchone()[0] is None:
                    cur.execute(sql.SQL("CREATE TYPE {} AS ENUM ({})").format(
                        sql.Identifier(type_), sql.SQL(", ").join(map(sql.Literal, ENUM_TYPES[type_])),
                    ))
        parts = [sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(type_)) for name, type_ in entry["columns"]]
        if table in (METRICS_TABLE, RUNS_TABLE):
            parts.append(sql.SQL("PRIMARY KEY (id)"))
        else:
            created_result_tables.append(table)
        if table == METRICS_TABLE:
            parts.append(sql.SQL("UNIQUE (name)"))
        cur.execute(sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table), sql.SQL(", ").join(parts)))
        log.info("Created table %s", table)
    return created_result_tables


def add_result_table_keys(cur, table: str, columns: list[list[str]]) -> None:
    """Primary key on every column except value, and foreign keys to runs and metrics, as in the ORM models."""
    key = [name for name, _ in columns if name != "value"]
    # Memory for sorting the rows when building the primary key; only for this transaction
    cur.execute("SET LOCAL maintenance_work_mem = '1GB'")
    cur.execute(sql.SQL(
        "ALTER TABLE {table} ADD PRIMARY KEY ({key}), "
        "ADD FOREIGN KEY (run_id) REFERENCES {runs}, ADD FOREIGN KEY (metric_id) REFERENCES {metrics}"
    ).format(
        table=sql.Identifier(table), key=sql.SQL(", ").join(map(sql.Identifier, key)),
        runs=sql.Identifier(RUNS_TABLE), metrics=sql.Identifier(METRICS_TABLE),
    ))


def schema_differences(expected: dict[str, list], actual: dict[str, list | None]) -> list[str]:
    """What differs between the exported tables and the target's, one line per column."""
    problems = []
    for table in sorted(expected):
        target = actual.get(table)
        if target is None:
            problems.append(f"{table}: missing on the target")
            continue
        expected_types = {name: type_ for name, type_ in expected[table]}
        target_types = {name: type_ for name, type_ in target}
        for name in sorted(expected_types.keys() - target_types.keys()):
            problems.append(f"{table}.{name}: missing on the target")
        for name in sorted(target_types.keys() - expected_types.keys()):
            problems.append(f"{table}.{name}: only on the target")
        for name in sorted(expected_types.keys() & target_types.keys()):
            if expected_types[name] != target_types[name]:
                problems.append(f"{table}.{name}: {expected_types[name]} in the export, {target_types[name]} on the target")
    return problems


def rows_per_metric(cur, table: str, run_id: int) -> dict[int, int]:
    cur.execute(
        sql.SQL("SELECT metric_id, count(*) FROM {} WHERE run_id = %s GROUP BY metric_id").format(sql.Identifier(table)),
        (run_id,),
    )
    return {metric_id: count for metric_id, count in cur.fetchall()}


def row_count_difference(table: str, expected: dict[int, int], actual: dict[int, int]) -> str | None:
    """None if both have the same rows per metric id, else a line saying where they differ."""
    if expected == actual:
        return None
    differing = sorted(metric_id for metric_id in expected.keys() | actual.keys() if expected.get(metric_id) != actual.get(metric_id))
    shown = ", ".join(map(str, differing[:10])) + (", ..." if len(differing) > 10 else "")
    return (
        f"{table}: {sum(expected.values())} rows in the export, {sum(actual.values())} on the target; "
        f"row counts differ for metric ids {shown}"
    )


# --------------------------------------------------------------------------- files

class _HashingWriter:
    """Binary file wrapper that hashes every byte written through it."""

    def __init__(self, raw):
        self.raw = raw
        self.sha256 = hashlib.sha256()

    def write(self, data):
        self.sha256.update(data)
        return self.raw.write(data)

    def flush(self):
        self.raw.flush()


def write_gzip(path: str, write) -> str:
    """Gzip whatever write(file) writes into path. Returns the sha256 of the compressed file."""
    with open(path, "wb") as raw:
        hashing = _HashingWriter(raw)
        # mtime=0 keeps the file identical when the same rows are exported again
        with gzip.GzipFile(filename="", fileobj=hashing, mode="wb", mtime=0) as file:
            write(file)
    return hashing.sha256.hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(run_dir: str) -> dict:
    path = os.path.join(run_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        raise TransferError(f"No {MANIFEST_NAME} in {run_dir}; is this an export folder?")
    with open(path, encoding="utf-8") as file:
        manifest = json.load(file)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise TransferError(f"{path} has format version {manifest.get('format_version')}, this tool reads {FORMAT_VERSION}")
    return manifest


def manifest_entries(manifest: dict) -> dict[str, dict]:
    """Every exported table with its file entry, in load order: the rows the result tables reference come first."""
    return {METRICS_TABLE: manifest["metrics"], RUNS_TABLE: manifest["runs"], **manifest["result_tables"]}


def verify_files(run_dir: str, manifest: dict) -> None:
    for entry in manifest_entries(manifest).values():
        path = os.path.join(run_dir, entry["file"])
        if not os.path.isfile(path):
            raise TransferError(f"{path} is missing")
        actual = file_sha256(path)
        if actual != entry["sha256"]:
            raise TransferError(f"{path} is damaged or incomplete (sha256 {actual}, manifest {entry['sha256']}); copy it again")


# --------------------------------------------------------------------------- export

def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GIT_COMMIT_HASH")


def warn_if_job_running(run_id: int) -> None:
    """Run ids are SLURM job ids; a job still in the queue may not have written all its results yet."""
    try:
        result = subprocess.run(
            ["squeue", "--noheader", "--jobs", str(run_id), "--format=%T"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    state = result.stdout.strip()
    if result.returncode == 0 and state:
        log.warning("SLURM job %d is still %s; the export only holds what the run wrote so far", run_id, state)


def copy_out(cur, directory: str, table: str, columns: list[list[str]], condition: sql.Composable) -> dict:
    """Stream the selected rows of table into <table>.csv.gz. Returns its manifest entry without the row count."""
    file_name = f"{table}.csv.gz"
    query = sql.SQL("COPY (SELECT {columns} FROM {table} {condition}) TO STDOUT WITH (FORMAT csv, HEADER true)").format(
        columns=sql.SQL(", ").join(sql.Identifier(name) for name, _ in columns),
        table=sql.Identifier(table),
        condition=condition,
    ).as_string(cur)
    sha256 = write_gzip(os.path.join(directory, file_name), lambda file: cur.copy_expert(query, file, size=COPY_CHUNK_SIZE))
    return {"file": file_name, "columns": columns, "sha256": sha256}


def export_run(conn, run_id: int, out_dir: str, overwrite: bool) -> str:
    final_dir = os.path.join(out_dir, f"run_{run_id}")
    partial_dir = f"{final_dir}.partial"
    if os.path.exists(final_dir) and not overwrite:
        raise TransferError(f"{final_dir} already exists; pass --overwrite to export run {run_id} again")
    warn_if_job_running(run_id)

    shutil.rmtree(partial_dir, ignore_errors=True)
    os.makedirs(partial_dir)
    try:
        # The session is REPEATABLE READ, so everything inside this transaction reads one snapshot
        with conn, conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT 1 FROM {} WHERE id = %s").format(sql.Identifier(RUNS_TABLE)), (run_id,))
            if cur.fetchone() is None:
                raise TransferError(f"Run {run_id} is not in `{RUNS_TABLE}`")

            result_entries = {}
            metric_ids: set[int] = set()
            for table in result_tables(cur):
                with _timed_step(f"Count rows of run {run_id} in {table}"):
                    metric_rows = rows_per_metric(cur, table, run_id)
                if not metric_rows:
                    continue
                rows = sum(metric_rows.values())
                with _timed_step(f"Export {rows} rows of {table}"):
                    entry = copy_out(
                        cur, partial_dir, table, table_columns(cur, table),
                        sql.SQL("WHERE run_id = {}").format(sql.Literal(run_id)),
                    )
                entry["rows"] = rows
                entry["metric_rows"] = {str(metric_id): count for metric_id, count in sorted(metric_rows.items())}
                result_entries[table] = entry
                metric_ids |= metric_rows.keys()

            if not result_entries:
                log.warning("Run %d has no rows in any result table; exporting only its runs row", run_id)

            runs_entry = copy_out(
                cur, partial_dir, RUNS_TABLE, table_columns(cur, RUNS_TABLE),
                sql.SQL("WHERE id = {}").format(sql.Literal(run_id)),
            )
            runs_entry["rows"] = 1
            metrics_entry = copy_out(
                cur, partial_dir, METRICS_TABLE, table_columns(cur, METRICS_TABLE),
                sql.SQL("WHERE id = ANY({}) ORDER BY id").format(sql.Literal(sorted(metric_ids))),
            )
            metrics_entry["rows"] = len(metric_ids)

        manifest = {
            "format_version": FORMAT_VERSION,
            "run_id": run_id,
            "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "source": {"db_name": conn.info.dbname, "db_host": conn.info.host},
            "tool_git_commit": _git_commit(),
            "runs": runs_entry,
            "metrics": metrics_entry,
            "result_tables": result_entries,
        }
        with open(os.path.join(partial_dir, MANIFEST_NAME), "w", encoding="utf-8") as file:
            json.dump(manifest, file, indent=2)
    except BaseException:
        shutil.rmtree(partial_dir, ignore_errors=True)
        raise

    if os.path.exists(final_dir):
        shutil.rmtree(final_dir)
    os.replace(partial_dir, final_dir)

    total_rows = sum(entry["rows"] for entry in result_entries.values())
    log.info("Run %d: %d rows in %d tables, %d metric ids", run_id, total_rows, len(result_entries), len(metric_ids))
    return final_dir


# --------------------------------------------------------------------------- import

def _stage_name(table: str) -> str:
    return f"_import_{table}"


def copy_in(cur, run_dir: str, table: str, entry: dict) -> None:
    query = sql.SQL("COPY {table} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(sql.Identifier(name) for name, _ in entry["columns"]),
    ).as_string(cur)
    with gzip.open(os.path.join(run_dir, entry["file"]), "rb") as file:
        cur.copy_expert(query, file, size=COPY_CHUNK_SIZE)


def stage(cur, run_dir: str, table: str, entry: dict) -> None:
    """Load an exported file into a temp table shaped like table. Only this session sees it; it is dropped at the end of the transaction."""
    cur.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(
        sql.Identifier(_stage_name(table)), sql.Identifier(table),
    ))
    copy_in(cur, run_dir, _stage_name(table), entry)


def existing_run_matches(cur, columns: list[list[str]], run_id: int) -> bool | None:
    """None if the target has no run with this id, else whether its runs row equals the exported one."""
    compared = [name for name, _ in columns if name != "id"]
    same = sql.SQL(" AND ").join(
        sql.SQL("t.{0} IS NOT DISTINCT FROM s.{0}").format(sql.Identifier(name)) for name in compared
    ) if compared else sql.SQL("true")
    cur.execute(sql.SQL("SELECT {same} FROM {target} AS t JOIN {stage} AS s ON s.id = t.id WHERE t.id = %s").format(
        same=same, target=sql.Identifier(RUNS_TABLE), stage=sql.Identifier(_stage_name(RUNS_TABLE)),
    ), (run_id,))
    row = cur.fetchone()
    return None if row is None else row[0]


def run_row_differences(cur, run_id: int, result_entries: dict) -> list[str]:
    """Where the target's rows of the run differ from the export, compared as row counts per table and metric id."""
    differences = []
    for table in sorted(set(result_tables(cur)) | result_entries.keys()):
        expected = {int(metric_id): count for metric_id, count in result_entries.get(table, {}).get("metric_rows", {}).items()}
        difference = row_count_difference(table, expected, rows_per_metric(cur, table, run_id))
        if difference is not None:
            differences.append(difference)
    return differences


def delete_run_rows(cur, run_id: int) -> None:
    """--replace: remove the run's rows from every result table. Its runs row is updated later; metrics stay."""
    for table in result_tables(cur):
        cur.execute(sql.SQL("DELETE FROM {} WHERE run_id = %s").format(sql.Identifier(table)), (run_id,))
        if cur.rowcount:
            log.info("Deleted %d rows of run %d from %s", cur.rowcount, run_id, table)


def format_metric_conflicts(conflicts: list[tuple]) -> str:
    """conflicts: (id, name, table_name) in the export followed by (id, name, table_name) on the target."""
    header = ("export id", "export name", "export table", "target id", "target name", "target table")
    rows = [header] + [tuple("" if value is None else str(value) for value in conflict) for conflict in conflicts]
    widths = [max(len(row[column]) for row in rows) for column in range(len(header))]
    return "\n".join("  " + "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip() for row in rows)


def insert_metrics(cur, columns: list[list[str]]) -> None:
    target = sql.Identifier(METRICS_TABLE)
    staged = sql.Identifier(_stage_name(METRICS_TABLE))
    # Same id with another name or table, or same name with another id
    cur.execute(sql.SQL("""
        SELECT s.id, s.name, s.table_name, t.id, t.name, t.table_name
        FROM {staged} AS s
        JOIN {target} AS t ON t.id = s.id OR t.name = s.name
        WHERE (t.id, t.name, t.table_name) IS DISTINCT FROM (s.id, s.name, s.table_name)
        ORDER BY s.id, t.id
    """).format(staged=staged, target=target))
    conflicts = cur.fetchall()
    if conflicts:
        raise TransferError(
            f"{len(conflicts)} metric id conflict(s) between the export and the target. "
            f"Ids are never remapped, so nothing of this run was written:\n{format_metric_conflicts(conflicts)}"
        )

    names = sql.SQL(", ").join(sql.Identifier(name) for name, _ in columns)
    cur.execute(sql.SQL("INSERT INTO {target} ({names}) SELECT {names} FROM {staged} ON CONFLICT (id) DO NOTHING").format(
        target=target, names=names, staged=staged,
    ))
    log.info("metrics: %d new, the others were already on the target with the same id", cur.rowcount)


def upsert_run(cur, columns: list[list[str]]) -> None:
    names = sql.SQL(", ").join(sql.Identifier(name) for name, _ in columns)
    updates = [sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(name)) for name, _ in columns if name != "id"]
    on_conflict = sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(updates) if updates else sql.SQL("DO NOTHING")
    cur.execute(sql.SQL("INSERT INTO {target} ({names}) SELECT {names} FROM {staged} ON CONFLICT (id) {on_conflict}").format(
        target=sql.Identifier(RUNS_TABLE), names=names, staged=sql.Identifier(_stage_name(RUNS_TABLE)), on_conflict=on_conflict,
    ))


def check_ids_preserved(cur, run_id: int, manifest: dict) -> None:
    """Last check before COMMIT: every exported id is on the target exactly as in the export."""
    problems = []

    cur.execute(sql.SQL("""
        SELECT count(*) FROM {staged} AS s
        JOIN {target} AS t ON t.id = s.id AND t.name = s.name AND t.table_name = s.table_name
    """).format(staged=sql.Identifier(_stage_name(METRICS_TABLE)), target=sql.Identifier(METRICS_TABLE)))
    matched = cur.fetchone()[0]
    if matched != manifest["metrics"]["rows"]:
        problems.append(f"metrics: {matched} of {manifest['metrics']['rows']} exported metrics are on the target with the same id, name and table")

    cur.execute(sql.SQL("SELECT array_agg(id) FROM {}").format(sql.Identifier(_stage_name(RUNS_TABLE))))
    staged_run_ids = cur.fetchone()[0]
    cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE id = %s").format(sql.Identifier(RUNS_TABLE)), (run_id,))
    if staged_run_ids != [run_id] or cur.fetchone()[0] != 1:
        problems.append(f"runs: expected exactly run {run_id}, the export holds {staged_run_ids}")

    problems += run_row_differences(cur, run_id, manifest["result_tables"])
    if problems:
        raise TransferError("The imported rows do not match the export, rolled back:\n  " + "\n  ".join(problems))


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except psycopg2.Error:
        pass


def import_run(conn, run_dir: str, replace: bool, dry_run: bool) -> str:
    """Load one export folder in one transaction. Returns what happened, for the summary."""
    manifest = read_manifest(run_dir)
    run_id = manifest["run_id"]
    entries = manifest_entries(manifest)
    with _timed_step(f"Verify checksums in {run_dir}"):
        verify_files(run_dir, manifest)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
            unkeyed_tables = create_missing_tables(cur, entries)

            problems = schema_differences(
                {table: entry["columns"] for table, entry in entries.items()},
                {table: table_columns(cur, table) for table in entries},
            )
            if problems:
                raise TransferError("The target schema differs from the export:\n  " + "\n  ".join(problems))

            stage(cur, run_dir, METRICS_TABLE, entries[METRICS_TABLE])
            stage(cur, run_dir, RUNS_TABLE, entries[RUNS_TABLE])

            existing = existing_run_matches(cur, entries[RUNS_TABLE]["columns"], run_id)
            if existing is not None and not replace:
                if not existing:
                    raise TransferError(
                        f"Run id {run_id} is already on the target for a different run (other user, git commit or "
                        f"creation time). Ids are never remapped; pass --replace to overwrite that run."
                    )
                differences = run_row_differences(cur, run_id, manifest["result_tables"])
                if differences:
                    raise TransferError(
                        f"Run {run_id} is already on the target with other rows; pass --replace to load it again:\n  "
                        + "\n  ".join(differences)
                    )
                conn.rollback()
                return "already imported, skipped"
            if existing is not None:
                if not existing:
                    log.warning("Replacing run %d on the target, although its runs row differs from the export", run_id)
                delete_run_rows(cur, run_id)

            insert_metrics(cur, entries[METRICS_TABLE]["columns"])
            upsert_run(cur, entries[RUNS_TABLE]["columns"])
            for table, entry in manifest["result_tables"].items():
                with _timed_step(f"Import {entry['rows']} rows into {table}"):
                    copy_in(cur, run_dir, table, entry)
            for table in unkeyed_tables:
                with _timed_step(f"Add primary and foreign keys to {table}"):
                    add_result_table_keys(cur, table, entries[table]["columns"])

            with _timed_step("Check that all ids arrived unchanged"):
                check_ids_preserved(cur, run_id, manifest)

            if dry_run:
                conn.rollback()
                return "dry run passed, rolled back"
        conn.commit()
    except BaseException:
        _rollback_quietly(conn)
        raise

    metric_ids = sorted({int(metric_id) for entry in manifest["result_tables"].values() for metric_id in entry["metric_rows"]})
    log.info(
        "Run %d: %d rows in %d tables with %d metric ids%s, all ids unchanged",
        run_id,
        sum(entry["rows"] for entry in manifest["result_tables"].values()),
        len(manifest["result_tables"]),
        len(metric_ids),
        f" ({metric_ids[0]}..{metric_ids[-1]})" if metric_ids else "",
    )
    return "replaced" if existing is not None else "imported"


# --------------------------------------------------------------------------- command line

def run_export(args: argparse.Namespace) -> None:
    os.makedirs(args.out, exist_ok=True)
    conn = connect(args.env_file, read_only=True)
    try:
        conn.set_session(isolation_level=ISOLATION_LEVEL_REPEATABLE_READ, readonly=True)
        for run_id in dict.fromkeys(args.run_id):
            with _timed_step(f"Export run {run_id}"):
                path = export_run(conn, run_id, args.out, args.overwrite)
            print(f"Exported run {run_id} to {path}")
    finally:
        conn.close()


def run_import(args: argparse.Namespace) -> None:
    conn = connect(args.env_file, read_only=False)
    finished = []
    try:
        for run_dir in args.run_dirs:
            with _timed_step(f"Import {run_dir}"):
                outcome = import_run(conn, run_dir, args.replace, args.dry_run)
            finished.append(run_dir)
            print(f"{run_dir}: {outcome}")
    except Exception:
        not_done = args.run_dirs[len(finished):]
        log.error("Stopped at %s. Done before: %s. Not imported: %s", not_done[0], finished or "none", not_done)
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=".env", help="File with DB_USER, DB_PASS, DB_NAME, DB_HOST, DB_PORT (default .env).")
    common.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR).")
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", parents=[common], help="Write runs from the database into export folders.")
    export.add_argument("--run-id", type=int, nargs="+", required=True, help="One or more run ids; each gets its own folder.")
    export.add_argument("--out", default=DEFAULT_OUT_DIR, help=f"Directory for the run_<id> folders (default {DEFAULT_OUT_DIR}).")
    export.add_argument("--overwrite", action="store_true", help="Export again even if run_<id> already exists in --out.")

    load = commands.add_parser("import", parents=[common], help="Load export folders into the database.")
    load.add_argument("run_dirs", nargs="+", metavar="RUN_DIR", help="run_<id> folders written by export.")
    load.add_argument("--replace", action="store_true", help="If a run is already on the target, delete its rows and load it again.")
    load.add_argument("--dry-run", action="store_true", help="Run all checks and loads, then roll back.")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if args.command == "export":
            run_export(args)
        else:
            run_import(args)
    except TransferError as error:
        log.error("%s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
