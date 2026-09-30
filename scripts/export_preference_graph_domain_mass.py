"""Attach the reference domain mass of a run to one saved preference graph snapshot.

The preference graph snapshots (preference_graphs/preference_graph_<YYYY-MM-DD>.gt.gz) only carry
the domains and edge weights. The mass of each domain - the cumulative number of references
(insertions minus deletions) written by ReferenceCountPerDomain as `reference_counts_per_domain` -
lives in Postgres. Querying it for every analysis is slow, so this script queries it once for the
timeslice the snapshot was taken at and stores it next to the graph:

  preference_graph_<date>.domain_mass_run<run_id>.npz
      Aligned with the vertex indices of the .gt.gz graph. Arrays `count` and `fraction` have one
      entry per vertex; vertices without a row in the database (their domain was discarded into
      the tail by the time of the snapshot) are NaN. Also holds `run_id`, `timestamp`,
      `total_reference_count`, `tail_count` and `num_vertices` as scalars.
  preference_graph_<date>.domain_mass_run<run_id>.tsv.gz
      Every domain with a mass at that timestamp, keyed by domain, with its `vertex` index in the
      graph (-1 if the domain never appears in a preference). Usable with pandas alone.

The suffixes are deliberately not .gt.gz or .csv.gz, so latest_snapshot() in
reference_trustworthiness_preference_graph.py keeps picking the graph files.

The snapshot is dated by the end timestamp of its timeslice, which is exactly the timestamp the
mass was written with, so the date in the file name selects the matching rows.

The export does not need graph-tool: the vertex domains are read straight from the .gt.gz binary
format (read_graph_vertex_domains). Only load_preference_graph_with_mass, which returns a
graph-tool Graph, imports it.

Usage:
    python scripts/export_preference_graph_domain_mass.py --graph 2019-09-23 --run-id 1814757
    python scripts/export_preference_graph_domain_mass.py --all --run-id 1814757

Loading afterwards:
    from export_preference_graph_domain_mass import load_preference_graph_with_mass
    g, meta = load_preference_graph_with_mass("preference_graphs/preference_graph_2019-09-23.gt.gz", 1814757)
    g.vp["mass_count"], g.vp["mass_fraction"]
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import logging
import os
import re
import struct
import time
from contextlib import contextmanager

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)

REFERENCE_COUNTS_METRIC = "reference_counts_per_domain"
TOTAL_REFERENCE_COUNT_METRIC = "reference_counts_per_domain_total_reference_count"
TAIL_COUNT_METRIC = "reference_counts_per_domain_tail_count"

DEFAULT_GRAPH_DIR = "preference_graphs"
GRAPH_FILE_STEM = "preference_graph"
GRAPH_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})\.gt\.gz$")

# graph-tool binary format, see https://graph-tool.skewed.de/static/doc/gt_format.html
GT_MAGIC = b"\xe2\x9b\xbe gt"
GT_KEY_VERTEX = 1
GT_KEY_EDGE = 2
# Value types 0-5 are bool, int16, int32, int64, double and long double; 6 is string; 7-13 are
# vectors of those seven, in the same order.
GT_SCALAR_SIZES = {0: 1, 1: 2, 2: 4, 3: 8, 4: 8, 5: 16}
GT_STRING = 6
GT_VECTOR_OFFSET = 7
GT_VECTOR_OF_STRING = GT_VECTOR_OFFSET + GT_STRING


@contextmanager
def _timed_step(step_name: str):
    started = time.perf_counter()
    log.info("[START] %s", step_name)
    try:
        yield
    finally:
        log.info("[DONE] %s in %.2fs", step_name, time.perf_counter() - started)


def mass_base_path(graph_path: str, run_id: int) -> str:
    """Path of the mass files without their extension, e.g. .../preference_graph_2019-09-23.domain_mass_run42"""
    if not graph_path.endswith(".gt.gz"):
        raise ValueError(f"Expected a .gt.gz preference graph, got {graph_path}")
    return f"{graph_path[: -len('.gt.gz')]}.domain_mass_run{run_id}"


def list_graph_paths(graph_dir: str) -> list[str]:
    """The complete .gt.gz snapshots in graph_dir, oldest first (the dated names sort chronologically)."""
    if not os.path.isdir(graph_dir):
        return []
    return [
        os.path.abspath(os.path.join(graph_dir, name))
        for name in sorted(os.listdir(graph_dir))
        if GRAPH_DATE_PATTERN.search(name) and ".partial." not in name
    ]


def resolve_graph_path(graph: str, graph_dir: str) -> str:
    """Accept a path to a .gt.gz snapshot, or just its date (YYYY-MM-DD) inside graph_dir."""
    if os.path.isfile(graph):
        path = graph
    else:
        path = os.path.join(graph_dir, f"{GRAPH_FILE_STEM}_{graph}.gt.gz")
    if not os.path.isfile(path):
        available = [os.path.basename(graph_path) for graph_path in list_graph_paths(graph_dir)]
        raise FileNotFoundError(f"No preference graph at {path}. Available in {graph_dir}: {available}")
    return os.path.abspath(path)


def graph_date(graph_path: str) -> datetime.date:
    match = GRAPH_DATE_PATTERN.search(os.path.basename(graph_path))
    if match is None:
        raise ValueError(f"Cannot read the snapshot date from {graph_path}")
    return datetime.date.fromisoformat(match.group(1))


def read_graph_vertex_domains(graph_path: str) -> tuple[list[str], int]:
    """The `domain` vertex property of a .gt.gz snapshot in vertex index order, and the edge count.

    Parses graph-tool's binary format directly, so graph-tool need not be installed. The
    adjacency list is only walked to count the edges, which sizes the edge properties that may
    precede `domain` and have to be skipped.
    """
    with gzip.open(graph_path, "rb") as handle:
        data = handle.read()
    if data[: len(GT_MAGIC)] != GT_MAGIC:
        raise ValueError(f"{graph_path} is not a graph-tool binary file")

    byte_order = ">" if data[len(GT_MAGIC) + 1] else "<"  # after the magic: version, then big-endian flag
    position = len(GT_MAGIC) + 2

    def read_uint64() -> int:
        nonlocal position
        (value,) = struct.unpack_from(byte_order + "Q", data, position)
        position += 8
        return value

    def read_bytes() -> bytes:
        nonlocal position
        length = read_uint64()
        position += length
        return data[position - length : position]

    def skip_values(value_type: int, count: int) -> None:
        nonlocal position
        if value_type in GT_SCALAR_SIZES:
            position += count * GT_SCALAR_SIZES[value_type]
            return
        for _ in range(count):
            if value_type == GT_STRING:
                read_bytes()
            elif value_type == GT_VECTOR_OF_STRING:
                for _ in range(read_uint64()):
                    read_bytes()
            elif value_type - GT_VECTOR_OFFSET in GT_SCALAR_SIZES:
                position += read_uint64() * GT_SCALAR_SIZES[value_type - GT_VECTOR_OFFSET]
            else:
                raise ValueError(f"Unsupported property value type {value_type} in {graph_path}")

    read_bytes()  # comment
    position += 1  # directed flag
    num_vertices = read_uint64()
    index_size = next(size for size in (1, 2, 4, 8) if num_vertices < 2 ** (8 * size))

    num_edges = 0
    for _ in range(num_vertices):
        degree = read_uint64()
        position += degree * index_size
        num_edges += degree

    for _ in range(read_uint64()):
        key_type = data[position]
        position += 1
        name = read_bytes().decode("utf-8")
        value_type = data[position]
        position += 1
        count = num_vertices if key_type == GT_KEY_VERTEX else num_edges if key_type == GT_KEY_EDGE else 1

        if key_type == GT_KEY_VERTEX and name == "domain":
            if value_type != GT_STRING:
                raise ValueError(f"Vertex property 'domain' in {graph_path} has value type {value_type}, not string")
            return [read_bytes().decode("utf-8") for _ in range(count)], num_edges
        skip_values(value_type, count)

    raise ValueError(f"{graph_path} has no vertex property 'domain'")


def build_engine_read_only():
    load_dotenv(".env")
    settings = {name: os.environ.get(name) for name in ("DB_USER", "DB_PASS", "DB_NAME", "DB_HOST", "DB_PORT")}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(f"Missing DB env vars in .env: {', '.join(missing)}")

    return create_engine(
        f"postgresql+psycopg2://{settings['DB_USER']}:{settings['DB_PASS']}@{settings['DB_HOST']}:{settings['DB_PORT']}/{settings['DB_NAME']}",
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def resolve_mass_timestamp(engine, run_id: int, date: datetime.date):
    """The timestamp of the mass rows written on `date`; the latest one if there are several."""
    sql = """
    SELECT DISTINCT mos.timestamp
    FROM metric_on_string AS mos
    JOIN metrics AS m ON m.id = mos.metric_id
    WHERE mos.run_id = :run_id
      AND m.name = :metric
      AND mos.timestamp >= :day_start
      AND mos.timestamp < :day_end
    ORDER BY mos.timestamp
    """
    day_start = datetime.datetime.combine(date, datetime.time())
    params = {
        "run_id": run_id,
        "metric": REFERENCE_COUNTS_METRIC,
        "day_start": day_start,
        "day_end": day_start + datetime.timedelta(days=1),
    }
    with engine.connect() as connection:
        timestamps = [row[0] for row in connection.execute(text(sql), params)]

    if not timestamps:
        raise RuntimeError(
            f"run_id={run_id} has no '{REFERENCE_COUNTS_METRIC}' rows on {date}. "
            f"Nearest timestamps of that run: {nearest_mass_timestamps(engine, run_id, day_start)}"
        )
    if len(timestamps) > 1:
        log.warning("Several mass timestamps on %s (%s); using the latest.", date, timestamps)
    return timestamps[-1]


def nearest_mass_timestamps(engine, run_id: int, around: datetime.datetime, limit: int = 3) -> list:
    sql = """
    (SELECT mos.timestamp FROM metric_on_string AS mos JOIN metrics AS m ON m.id = mos.metric_id
     WHERE mos.run_id = :run_id AND m.name = :metric AND mos.timestamp < :around
     GROUP BY mos.timestamp ORDER BY mos.timestamp DESC LIMIT :limit)
    UNION ALL
    (SELECT mos.timestamp FROM metric_on_string AS mos JOIN metrics AS m ON m.id = mos.metric_id
     WHERE mos.run_id = :run_id AND m.name = :metric AND mos.timestamp >= :around
     GROUP BY mos.timestamp ORDER BY mos.timestamp ASC LIMIT :limit)
    """
    params = {"run_id": run_id, "metric": REFERENCE_COUNTS_METRIC, "around": around, "limit": limit}
    with engine.connect() as connection:
        return sorted(str(row[0]) for row in connection.execute(text(sql), params))


def load_domain_mass(engine, run_id: int, timestamp) -> pd.DataFrame:
    sql = """
    SELECT mos.key_string AS domain, mos.value AS count
    FROM metric_on_string AS mos
    JOIN metrics AS m ON m.id = mos.metric_id
    WHERE mos.run_id = :run_id
      AND m.name = :metric
      AND mos.timestamp = :timestamp
    """
    params = {"run_id": run_id, "metric": REFERENCE_COUNTS_METRIC, "timestamp": timestamp}
    return pd.read_sql_query(text(sql), engine, params=params)


def load_global_value(engine, run_id: int, timestamp, metric_name: str) -> float:
    """The global value written in the same timestep as the mass.

    metric_value_float.timestamp is timestamptz while metric_on_string.timestamp is not, so an
    exact comparison is shifted by the session's UTC offset and can miss. Timesteps are far more
    than a day apart, so the nearest value within a day is the one from the same timestep.
    """
    sql = """
    SELECT mvf.timestamp, mvf.value
    FROM metric_value_float AS mvf
    JOIN metrics AS m ON m.id = mvf.metric_id
    WHERE mvf.run_id = :run_id
      AND m.name = :metric
      AND mvf.timestamp BETWEEN CAST(:timestamp AS timestamp) - INTERVAL '1 day'
                            AND CAST(:timestamp AS timestamp) + INTERVAL '1 day'
    ORDER BY ABS(EXTRACT(EPOCH FROM (mvf.timestamp::timestamp - CAST(:timestamp AS timestamp))))
    LIMIT 1
    """
    params = {"run_id": run_id, "metric": metric_name, "timestamp": timestamp}
    with engine.connect() as connection:
        row = connection.execute(text(sql), params).one_or_none()
    if row is None:
        log.warning("No '%s' value for run_id=%d within a day of %s.", metric_name, run_id, timestamp)
        return float("nan")
    log.info("Using '%s' = %s written at %s", metric_name, row.value, row.timestamp)
    return float(row.value)


def _write_atomically(final_path: str, temp_path: str, write) -> None:
    write(temp_path)
    os.replace(temp_path, final_path)


def export_domain_mass(graph_path: str, run_id: int, engine=None) -> str:
    date = graph_date(graph_path)

    with _timed_step(f"Load graph {graph_path}"):
        vertex_domains, num_edges = read_graph_vertex_domains(graph_path)
        num_vertices = len(vertex_domains)
        log.info("Graph has %d vertices and %d edges", num_vertices, num_edges)

    if engine is None:
        engine = build_engine_read_only()

    with _timed_step(f"Resolve mass timestamp for run_id={run_id} on {date}"):
        timestamp = resolve_mass_timestamp(engine, run_id, date)
        log.info("Using mass timestamp %s", timestamp)

    with _timed_step("Load domain mass"):
        mass = load_domain_mass(engine, run_id, timestamp)
        total_reference_count = load_global_value(engine, run_id, timestamp, TOTAL_REFERENCE_COUNT_METRIC)
        tail_count = load_global_value(engine, run_id, timestamp, TAIL_COUNT_METRIC)
        log.info("Loaded %d domains, total reference count %s, tail count %s", len(mass), total_reference_count, tail_count)

    # Same definition as reference_counts_per_domain_fractions: the total includes the discarded tail.
    if total_reference_count and not np.isnan(total_reference_count):
        mass["fraction"] = mass["count"].astype(float) / total_reference_count
    else:
        mass["fraction"] = np.nan

    vertex_of_domain = pd.Series(np.arange(num_vertices, dtype=np.int64), index=pd.Index(vertex_domains, name="domain"))
    mass["vertex"] = mass["domain"].map(vertex_of_domain).fillna(-1).astype(np.int64)

    in_graph = mass[mass["vertex"] >= 0]
    count = np.full(num_vertices, np.nan)
    fraction = np.full(num_vertices, np.nan)
    count[in_graph["vertex"].to_numpy()] = in_graph["count"].to_numpy(dtype=float)
    fraction[in_graph["vertex"].to_numpy()] = in_graph["fraction"].to_numpy(dtype=float)

    log.info(
        "%d of %d graph vertices have a mass (%.1f%%); %d domains with mass are not in the graph",
        len(in_graph),
        num_vertices,
        100.0 * len(in_graph) / num_vertices if num_vertices else 0.0,
        len(mass) - len(in_graph),
    )

    base = mass_base_path(graph_path, run_id)
    with _timed_step(f"Write {base}.{{npz,tsv.gz}}"):
        _write_atomically(
            f"{base}.npz",
            f"{base}.partial.npz",
            lambda path: np.savez(
                path,
                count=count,
                fraction=fraction,
                run_id=np.int64(run_id),
                timestamp=np.str_(pd.Timestamp(timestamp).isoformat()),
                total_reference_count=np.float64(total_reference_count),
                tail_count=np.float64(tail_count),
                num_vertices=np.int64(num_vertices),
            ),
        )
        _write_atomically(
            f"{base}.tsv.gz",
            f"{base}.partial.tsv.gz",
            lambda path: mass[["domain", "vertex", "count", "fraction"]]
            .sort_values("count", ascending=False)
            .to_csv(path, sep="\t", index=False, compression="gzip"),
        )

    return base


def load_domain_mass_arrays(graph_path: str, run_id: int) -> dict:
    """The vertex-aligned mass arrays and metadata written by export_domain_mass."""
    with np.load(f"{mass_base_path(graph_path, run_id)}.npz") as data:
        return {key: (data[key] if data[key].ndim else data[key].item()) for key in data.files}


def load_preference_graph_with_mass(graph_path: str, run_id: int):
    """Load a .gt.gz snapshot with its mass as vertex properties `mass_count` and `mass_fraction`.

    Returns (graph, metadata) where metadata holds run_id, timestamp, total_reference_count,
    tail_count and num_vertices. NaN marks vertices whose domain was in the discarded tail.
    """
    from graph_tool import load_graph

    graph = load_graph(graph_path)
    mass = load_domain_mass_arrays(graph_path, run_id)
    if mass["num_vertices"] != graph.num_vertices():
        raise ValueError(
            f"Mass file has {mass['num_vertices']} vertices but the graph has {graph.num_vertices()}; "
            "was the graph rewritten after the mass was exported?"
        )

    for name in ("count", "fraction"):
        prop = graph.new_vertex_property("double")
        prop.a[:] = mass.pop(name)
        graph.vp[f"mass_{name}"] = prop

    return graph, mass


def load_domain_mass_table(graph_path: str, run_id: int) -> pd.DataFrame:
    """The domain-keyed mass table; needs nothing but pandas."""
    return pd.read_csv(f"{mass_base_path(graph_path, run_id)}.tsv.gz", sep="\t", keep_default_na=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the domain mass of a run for one preference graph snapshot.")
    which_graphs = parser.add_mutually_exclusive_group(required=True)
    which_graphs.add_argument(
        "--graph",
        help="Snapshot date (YYYY-MM-DD) or path to a preference_graph_<date>.gt.gz file.",
    )
    which_graphs.add_argument(
        "--all",
        action="store_true",
        help="Export the mass for every snapshot in --graph-dir.",
    )
    parser.add_argument("--run-id", type=int, required=True, help="Run whose reference_counts_per_domain is used.")
    parser.add_argument(
        "--graph-dir",
        default=DEFAULT_GRAPH_DIR,
        help="Directory holding the snapshots when --graph is a date or --all is given. The mass files are written next to the graph.",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR).")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.all:
        graph_path = resolve_graph_path(args.graph, args.graph_dir)
        base = export_domain_mass(graph_path, args.run_id)
        print(f"Wrote {base}.npz and {base}.tsv.gz")
        return

    graph_paths = list_graph_paths(args.graph_dir)
    if not graph_paths:
        raise FileNotFoundError(f"No preference graphs in {args.graph_dir}")
    log.info("Exporting the mass of run_id=%d for %d snapshots in %s", args.run_id, len(graph_paths), args.graph_dir)

    # A snapshot the run has no mass for must not stop the others; the failures are reported at the end.
    engine = build_engine_read_only()
    failed = []
    for graph_path in graph_paths:
        try:
            base = export_domain_mass(graph_path, args.run_id, engine)
            print(f"Wrote {base}.npz and {base}.tsv.gz")
        except Exception:
            log.exception("Export failed for %s", graph_path)
            failed.append(os.path.basename(graph_path))

    if failed:
        raise SystemExit(f"Export failed for {len(failed)} of {len(graph_paths)} snapshots: {failed}")


if __name__ == "__main__":
    main()
