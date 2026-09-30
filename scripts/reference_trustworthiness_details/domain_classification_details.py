"""Per-domain analysis of the reference domain classifiers.

Every classifier writes the same two things, so one tool can read all of them:

  * per domain, for the largest domains of each timestep, a class into
    ``metric_on_string`` under ``<classifier>_domain_classification``:

        -1.0 = dominated   0.0 = contested   +1.0 = dominating   2.0 = unclassified

    The flow classifiers write the fourth value explicitly; the older replacement
    rule leaves unclassified domains out of the metric instead.  A domain absent
    from a timestep is therefore either unclassified or outside the
    top-1000-by-size cap the metric applies before writing.

  * per timestep, a set of global diagnostic series into ``metric_value_float``,
    all named with the classifier's own prefix (``_dominated``, ``_median_deff``,
    ``_avg_session_size_step``, ...).  Which ones exist differs per classifier, so
    they are discovered by prefix rather than listed here.

Domain sizes come from a third, string-keyed metric, ``reference_counts_per_domain``.

For each classifier this script produces:

  * the global diagnostic series, as a console table (value at the as-of timestep
    plus min/max/mean over the run) and a long-format CSV,
  * a CSV + console table of the largest classified domains at the final timestep,
    with their end class and how stable that class was over time, and
  * a figure in the paper_plots style: one line per domain for the top-k
    dominated / dominating / contested domains *as classified at the end*, with
    domain size on a log y axis over time.  Each line segment [t_i, t_i+1] is
    coloured by the domain's class at t_i -- the class that held during that
    interval, until the next reclassification.

For the supermajority flow classifier only, it also prints and writes the k
largest domains of each class (dominated / dominating / contested /
unclassified) at every year tick of the figure's x axis, with how much of the
class's mass they hold.

With more than one classifier it additionally prints and writes a side-by-side
comparison of the end classes they assign to the same domains.

Performance note: ``metric_on_string`` has only the composite primary key
``(run_id, metric_id, timestamp, key_string)`` and no secondary index, and the
size metric holds hundreds of thousands of domains per timestep (tens of
millions of rows per run).  Because ``key_string`` is the *last* PK column,
filtering on it alone forces a walk of the whole per-metric index range.  Every
query below therefore either pins the full four-column PK or pins the
three-column prefix on the small (class) metric.  See fetch_from_db().
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# The paper plot library is not installed as a package; add its directory so the
# DB connection helper and the shared figure style can be reused rather than
# duplicated (the connection helper carries cluster-specific host discovery).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_PAPER_PLOTTER = _REPO_ROOT / "log_parsing" / "paper_plotter"
# Same for the Wikidata label resolver of the preference graph explorer, whose
# label cache is shared so a QID fetched by either tool is not fetched again.
_PREFERENCE_GRAPH_DETAILS = _REPO_ROOT / "preference_graph_details"
for _path in (_PAPER_PLOTTER, _PREFERENCE_GRAPH_DETAILS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_RUN_ID = 2559236
DEFAULT_SIZE_METRIC = "reference_counts_per_domain"

# Short names accepted on the command line, so the long metric names do not have
# to be typed. The keys double as the labels used in output filenames.
CLASS_METRIC_ALIASES = {
    "replacements": (
        "reference_replacements_simple_with_path_discard_edge_heuristic"
        "_dis_5pct_domain_classification"
    ),
    "supermajority": "flow_classification_supermajority_domain_classification",
    "wilson": "flow_classification_wilson_domain_classification",
}
_ALIAS_OF_METRIC = {name: alias for alias, name in CLASS_METRIC_ALIASES.items()}

# All three by default: a run usually holds only some of them, and the ones it
# does not hold are reported and skipped rather than treated as an error.
DEFAULT_CLASS_METRICS = tuple(CLASS_METRIC_ALIASES.values())

# The suffix every classifier appends to its own name for the per-domain metric.
_CLASSIFICATION_SUFFIX = "_domain_classification"

# Table the global diagnostic series live in (DataHandler.write_global_metric_value).
_GLOBAL_VALUE_TABLE = "metric_value_float"

# Value encoding written by the classifiers' write_top_domain_classification_results.
CLASS_DOMINATED = -1.0
CLASS_CONTESTED = 0.0
CLASS_DOMINATING = 1.0
CLASS_UNCLASSIFIED = 2.0

CLASS_LABELS = {
    CLASS_DOMINATED: "dominated",
    CLASS_CONTESTED: "contested",
    CLASS_DOMINATING: "dominating",
    CLASS_UNCLASSIFIED: "unclassified",
}
# Order used for the figure rows and the legend. Unclassified is not a class a
# domain is selected for -- it is what is left over -- so it is not in here.
CLASS_ORDER = (CLASS_DOMINATED, CLASS_DOMINATING, CLASS_CONTESTED)

# Okabe-Ito, matching log_parsing/paper_plotter/paper_plots/render.py.
CLASS_COLORS = {
    CLASS_DOMINATED: "#D55E00",     # vermillion
    CLASS_CONTESTED: "#E69F00",     # orange
    CLASS_DOMINATING: "#009E73",    # bluish green
    CLASS_UNCLASSIFIED: "#BDBDBD",  # written as unclassified
    None: "#BDBDBD",                # no row for this timestep
}

# Diagnostic series printed first, in this order; everything else follows
# alphabetically. These are the ones every classifier writes.
_STATS_ORDER_HEAD = (
    "dominated",
    "dominating",
    "contested",
    "unclassified",
    "not_in_graph",
)

# Rows per statement for the PK-probe VALUES join. Keeps the statement size and
# the bound parameter count reasonable without extra round trips in practice.
_VALUES_CHUNK_SIZE = 5000

# The one classifier the per-year breakdown runs for. It writes the class of every
# domain in its top-by-size set, unclassified ones included, and its four class
# fractions add up to one, so each class's mass is known exactly: fraction x the
# mass the classifier divides by.
YEARLY_CLASS_METRIC = CLASS_METRIC_ALIASES["supermajority"]
YEARLY_CLASS_ORDER = (
    CLASS_DOMINATED,
    CLASS_DOMINATING,
    CLASS_CONTESTED,
    CLASS_UNCLASSIFIED,
)

# Global series of the size metric. Their difference is the mass the classifiers
# divide by: the sum of reference_counts_per_domain, i.e. everything but the tail.
_TOTAL_MASS_STAT = "total_reference_count"
_TAIL_MASS_STAT = "tail_count"

# Timezone the figure's year ticks are placed in (render_figure's default).
_PLOT_TIMEZONE = "Europe/Berlin"

_CACHE_FILES = {
    "end_state": "end_state.csv",
    "class_history": "class_history.csv",
    "size_history": "size_history.csv",
    "stats": "stats.csv",
    "yearly_state": "yearly_state.csv",
    "yearly_mass": "yearly_mass.csv",
}
# A cache taken with --no-stats has no stats.csv, and only the supermajority
# classifier has the yearly files.
_OPTIONAL_CACHE_FILES = {"stats", "yearly_state", "yearly_mass"}
_CACHE_META = "meta.json"


# --------------------------------------------------------------------------
# Metric naming
# --------------------------------------------------------------------------


def classifier_prefix(class_metric: str) -> str:
    """The classifier's own metric name, i.e. the prefix its global series share."""
    if class_metric.endswith(_CLASSIFICATION_SUFFIX):
        return class_metric[: -len(_CLASSIFICATION_SUFFIX)]
    return class_metric


def short_label(class_metric: str) -> str:
    """Human-readable name for console output; the alias when there is one."""
    return _ALIAS_OF_METRIC.get(class_metric) or classifier_prefix(class_metric)


def metric_slug(class_metric: str) -> str:
    """Filename component. Bounded in length so paths stay well inside MAX_PATH."""
    label = short_label(class_metric)
    if len(label) <= 48:
        return label
    digest = hashlib.sha1(class_metric.encode("utf-8")).hexdigest()[:6]
    return f"{label[:41]}_{digest}"


def _empty_yearly_state() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "domain", "classification", "size"])


def _empty_yearly_mass() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "timestamp",
            _TOTAL_MASS_STAT,
            _TAIL_MASS_STAT,
            "total_mass",
            *(CLASS_LABELS[value] for value in YEARLY_CLASS_ORDER),
        ]
    )


@dataclass
class MetricResult:
    """Everything fetched for one classifier."""

    name: str
    metric_id: int | None
    as_of: pd.Timestamp
    timesteps: list[pd.Timestamp]
    end_state: pd.DataFrame
    class_history: pd.DataFrame
    size_history: pd.DataFrame
    stats: pd.DataFrame
    # Only filled for YEARLY_CLASS_METRIC: the classified domains at each year
    # tick, and per tick the total mass and the class fractions of it.
    yearly_state: pd.DataFrame = field(default_factory=_empty_yearly_state)
    yearly_mass: pd.DataFrame = field(default_factory=_empty_yearly_mass)

    @property
    def label(self) -> str:
        return short_label(self.name)

    @property
    def slug(self) -> str:
        return metric_slug(self.name)


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------


def _connect(timeout_seconds: int):
    """Connect via the paper_plots helper and bound every statement."""
    from dotenv import load_dotenv

    from paper_plots.db import _connect as _paper_plots_connect

    # paper_plots._connect calls load_dotenv(".env") relative to the working
    # directory. Pre-load both plausible locations first; python-dotenv does not
    # override variables that are already set, so the first hit wins.
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(_PAPER_PLOTTER / ".env")

    connection = _paper_plots_connect()
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = %s", (timeout_seconds * 1000,))
        # metric_on_string.timestamp is TIMESTAMP WITHOUT TIME ZONE (the ORM
        # declares a plain datetime), and the writer stamps UTC values. Pin the
        # session so a naive literal can never be read in a local zone.
        cursor.execute("SET TIME ZONE 'UTC'")
    return connection


def _to_db_timestamp(timestamp: pd.Timestamp):
    """Naive UTC datetime, matching the column's TIMESTAMP WITHOUT TIME ZONE."""
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.to_pydatetime()


def resolve_metric_ids(conn, names: list[str]) -> dict[str, int]:
    """Name -> id for those that exist. Callers decide what a miss means."""
    query = "SELECT id, name FROM metrics WHERE name = ANY(%s)"
    frame = pd.read_sql_query(query, conn, params=(list(names),))
    return dict(zip(frame["name"], frame["id"].astype(int)))


def list_metrics(conn) -> pd.DataFrame:
    query = """
        SELECT id, name, table_name
        FROM metrics
        WHERE name LIKE %s
           OR name LIKE %s
           OR name LIKE %s
        ORDER BY name
    """
    return pd.read_sql_query(
        query,
        conn,
        params=(
            "%domain_classification",
            "reference_counts_per_domain%",
            "flow_classification%",
        ),
    )


def fetch_timesteps(conn, run_id: int, class_metric_id: int) -> list[pd.Timestamp]:
    """Timesteps of the run, read off the small (class) metric.

    Pins the ``(run_id, metric_id)`` PK prefix, so this only walks the class
    metric's own index range (~1000 rows per timestep), never the size metric.
    Returns an empty list when the metric holds no rows for this run.
    """
    query = """
        SELECT DISTINCT timestamp
        FROM metric_on_string
        WHERE run_id = %s AND metric_id = %s
        ORDER BY timestamp
    """
    frame = pd.read_sql_query(query, conn, params=(run_id, class_metric_id))
    if frame.empty:
        return []
    return list(pd.to_datetime(frame["timestamp"], utc=True))


_END_STATE_QUERY = """
    SELECT c.key_string AS domain,
           c.value      AS classification,
           s.value      AS size
    FROM metric_on_string c
    LEFT JOIN metric_on_string s
           ON s.run_id     = c.run_id
          AND s.metric_id  = %(size_id)s
          AND s.timestamp  = c.timestamp
          AND s.key_string = c.key_string
    WHERE c.run_id = %(run_id)s
      AND c.metric_id = %(class_id)s
      AND c.timestamp = %(as_of)s
    ORDER BY s.value DESC NULLS LAST
"""


def fetch_end_state(
    conn,
    run_id: int,
    class_metric_id: int,
    size_metric_id: int,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """Every classified domain at ``as_of``, with its size, largest first.

    Driven from the class side on purpose: that metric is capped at the top
    1000 domains by size, so it already contains every large classified domain,
    and each size is then a single full-PK index probe. Scanning the size side
    for this timestep would mean sorting several hundred thousand rows instead.
    """
    params = {
        "run_id": run_id,
        "class_id": class_metric_id,
        "size_id": size_metric_id,
        "as_of": _to_db_timestamp(as_of),
    }
    return pd.read_sql_query(_END_STATE_QUERY, conn, params=params)


def _cells_query(pair_count: int) -> str:
    values = ", ".join(["(%s::timestamp, %s::text)"] * pair_count)
    return f"""
        SELECT m.timestamp, m.key_string, m.value
        FROM (VALUES {values}) AS w(ts, ks)
        JOIN metric_on_string m
          ON m.run_id     = %s
         AND m.metric_id  = %s
         AND m.timestamp  = w.ts
         AND m.key_string = w.ks
    """


def fetch_cells(
    conn,
    run_id: int,
    metric_id: int,
    timestamps: list[pd.Timestamp],
    keys: list[str],
    explain: bool = False,
) -> pd.DataFrame:
    """Fetch the (timestamp, key) cells of one metric as full-PK probes.

    A ``key_string = ANY(...)`` filter would have to walk the metric's entire
    index range because key_string is the last PK column. Joining against an
    explicit VALUES list makes every lookup an equality on the complete PK.
    """
    if not timestamps or not keys:
        return pd.DataFrame(columns=["timestamp", "key_string", "value"])

    pairs = [
        (_to_db_timestamp(timestamp), key)
        for timestamp in timestamps
        for key in keys
    ]

    frames = []
    for start in range(0, len(pairs), _VALUES_CHUNK_SIZE):
        chunk = pairs[start : start + _VALUES_CHUNK_SIZE]
        query = _cells_query(len(chunk))
        params: list = [item for pair in chunk for item in pair]
        params.extend([run_id, metric_id])

        if explain and start == 0:
            _print_explain(conn, query, params, f"cells (metric_id={metric_id})")

        frames.append(pd.read_sql_query(query, conn, params=params))

    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values(["key_string", "timestamp"], ignore_index=True)


def _empty_stats() -> pd.DataFrame:
    return pd.DataFrame(columns=["stat", "timestamp", "value"])


def fetch_stats(conn, run_id: int, class_metric: str) -> pd.DataFrame:
    """The classifier's global diagnostic series, long format.

    Discovered by prefix instead of by a hardcoded list: the three classifiers
    write different diagnostics (deff and p0 for the Wilson test, session and
    graph statistics for the replacement rule), and they gain and lose series as
    the metric code changes. Restricted to the global value table, which also
    keeps the per-domain metric itself out of the result.

    Cost: a handful of metric ids, then one indexed read per run of a few hundred
    rows per series.
    """
    prefix = classifier_prefix(class_metric)
    # "_" is a LIKE wildcard, so this pattern is deliberately loose; the exact
    # prefix test happens in pandas below.
    catalog = pd.read_sql_query(
        """
        SELECT id, name
        FROM metrics
        WHERE table_name = %s AND name LIKE %s
        ORDER BY name
        """,
        conn,
        params=(_GLOBAL_VALUE_TABLE, f"{prefix}%"),
    )
    if not catalog.empty:
        catalog = catalog[catalog["name"].str.startswith(f"{prefix}_")]
    if catalog.empty:
        return _empty_stats()

    ids = [int(value) for value in catalog["id"]]
    values = pd.read_sql_query(
        f"""
        SELECT metric_id, timestamp, value
        FROM {_GLOBAL_VALUE_TABLE}
        WHERE run_id = %s AND metric_id = ANY(%s)
        ORDER BY metric_id, timestamp
        """,
        conn,
        params=(run_id, ids),
    )
    if values.empty:
        return _empty_stats()

    names = dict(zip(catalog["id"].astype(int), catalog["name"]))
    values["stat"] = (
        values["metric_id"].map(names).str.slice(len(prefix) + 1)
    )
    values["timestamp"] = pd.to_datetime(values["timestamp"], utc=True)
    return values[["stat", "timestamp", "value"]].sort_values(
        ["stat", "timestamp"], ignore_index=True
    )


def fetch_yearly_state(
    conn,
    run_id: int,
    class_metric_id: int,
    size_metric_id: int,
    timestamps: list[pd.Timestamp],
) -> pd.DataFrame:
    """Every classified domain, with its size, at each of the given timesteps.

    One fetch_end_state per timestep, so the same bound holds: at most the class
    metric's top-1000 rows each, and every size a full-PK probe.
    """
    frames = []
    for timestamp in timestamps:
        frame = fetch_end_state(conn, run_id, class_metric_id, size_metric_id, timestamp)
        frame.insert(0, "timestamp", timestamp)
        frames.append(frame)
    if not frames:
        return _empty_yearly_state()
    return pd.concat(frames, ignore_index=True)


def _print_explain(conn, query: str, params, label: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute("EXPLAIN " + query, params)
        print(f"--- EXPLAIN: {label} ---")
        for (line,) in cursor.fetchall():
            print("   " + line)
        print("--- end EXPLAIN ---")


def _resolve_as_of(
    args, class_metric: str, timesteps: list[pd.Timestamp]
) -> pd.Timestamp | None:
    """The timestep to classify by, or None if the request cannot be served."""
    if args.as_of is None:
        return timesteps[-1]

    as_of = pd.Timestamp(args.as_of, tz="UTC")
    if as_of not in timesteps:
        print(
            f"Skipping {short_label(class_metric)}: --as-of {args.as_of} is not one "
            f"of its {len(timesteps)} timesteps "
            f"({timesteps[0]} .. {timesteps[-1]})."
        )
        return None
    return as_of


def fetch_one_metric(
    conn, args, class_metric: str, class_id: int, size_id: int
) -> MetricResult | None:
    """The full per-classifier fetch. None when the run holds nothing for it."""
    label = short_label(class_metric)
    timesteps = fetch_timesteps(conn, args.run_id, class_id)
    if not timesteps:
        print(
            f"Skipping {label}: no rows in metric_on_string for "
            f"run_id={args.run_id}, metric_id={class_id}."
        )
        return None

    as_of = _resolve_as_of(args, class_metric, timesteps)
    if as_of is None:
        return None
    print(f"{label}: {len(timesteps)} timesteps; using as-of = {as_of}")

    if args.explain:
        _print_explain(
            conn,
            _END_STATE_QUERY,
            {
                "run_id": args.run_id,
                "class_id": class_id,
                "size_id": size_id,
                "as_of": _to_db_timestamp(as_of),
            },
            f"end state ({label})",
        )

    end_state = fetch_end_state(conn, args.run_id, class_id, size_id, as_of)
    print(f"{label}: {len(end_state)} classified domains at the as-of timestep")

    keys = select_history_keys(end_state, args.top, args.per_class)
    print(
        f"{label}: fetching history for {len(keys)} domains x "
        f"{len(timesteps)} timesteps"
    )

    class_history = fetch_cells(
        conn, args.run_id, class_id, timesteps, keys, explain=args.explain
    )
    size_history = fetch_cells(conn, args.run_id, size_id, timesteps, keys)

    stats = fetch_stats(conn, args.run_id, class_metric) if args.stats else _empty_stats()
    if args.stats:
        series_count = 0 if stats.empty else stats["stat"].nunique()
        print(f"{label}: {series_count} global diagnostic series")

    yearly_state, yearly_mass = _empty_yearly_state(), _empty_yearly_mass()
    if class_metric == YEARLY_CLASS_METRIC and args.yearly_top > 0:
        year_steps = list(breakdown_timesteps(timesteps, args.detailed)["timestamp"])
        where = "timesteps" if args.detailed else "year ticks"
        print(f"{label}: fetching the classified domains at {len(year_steps)} {where}")
        yearly_state = fetch_yearly_state(
            conn, args.run_id, class_id, size_id, year_steps
        )
        # The class fractions are among the classifier's own series; total and
        # tail mass are series of the size metric, found by the same prefix rule.
        class_stats = stats if args.stats else fetch_stats(conn, args.run_id, class_metric)
        size_stats = fetch_stats(conn, args.run_id, args.size_metric)
        yearly_mass = build_yearly_mass(class_stats, size_stats, year_steps)

    return MetricResult(
        name=class_metric,
        metric_id=class_id,
        as_of=as_of,
        timesteps=timesteps,
        end_state=end_state,
        class_history=class_history,
        size_history=size_history,
        stats=stats,
        yearly_state=yearly_state,
        yearly_mass=yearly_mass,
    )


def fetch_from_db(args) -> tuple[dict, list[MetricResult]]:
    """Run the bounded query set, once per requested classifier.

    Total rows pulled per classifier, for the defaults and a 156-timestep run:
        metric ids            2
        timesteps           156
        end state         <=1000  (the class metric's own top-N cap)
        histories        <=18400  ((top + per_class*3) domains x timesteps x 2)
        global stats      <=6000  (series count x timesteps)
    i.e. under 30k rows and well under 10 MB of pandas, independent of how many
    domains or timesteps the run has. The supermajority classifier adds, for the
    per-year breakdown,
        yearly state      <=1000 per year tick (~15k for 15 years)
        size-metric stats  a few series x timesteps
    which keeps it in the same range. With --detailed the yearly state covers
    every timestep instead: <=1000 x timesteps (~156k rows), one query each.

    Rule to preserve when editing: never SELECT from metric_on_string for the
    size metric without either an equality on a single timestamp or a full-PK
    join. Anything else scans tens of millions of rows.
    """
    conn = _connect(args.timeout)
    try:
        if args.list_metrics:
            print(list_metrics(conn).to_string(index=False))
            raise SystemExit(0)

        ids = resolve_metric_ids(conn, [args.size_metric, *args.class_metric])
        if args.size_metric not in ids:
            raise SystemExit(
                f"Could not resolve the size metric {args.size_metric!r}.\n"
                "Run with --list-metrics to see the candidates in this database."
            )
        size_id = ids[args.size_metric]
        print(f"Resolved {args.size_metric!r} -> metric_id {size_id}")

        known = [name for name in args.class_metric if name in ids]
        for name in args.class_metric:
            if name in ids:
                print(f"Resolved {name!r} -> metric_id {ids[name]}")
            else:
                print(f"Skipping {name!r}: no such metric in this database.")
        if not known:
            raise SystemExit(
                "None of the requested classification metrics exist in this "
                "database.\nRun with --list-metrics to see the candidates."
            )

        results = []
        for name in known:
            result = fetch_one_metric(conn, args, name, ids[name], size_id)
            if result is not None:
                results.append(result)

        if not results:
            raise SystemExit(
                f"None of the requested classification metrics have data for "
                f"run {args.run_id}."
            )

        meta = {
            "run_id": args.run_id,
            "size_metric": args.size_metric,
            "size_metric_id": size_id,
            "top": args.top,
            "per_class": args.per_class,
            "yearly_top": args.yearly_top,
            "detailed": args.detailed,
            "metrics": [
                {
                    "class_metric": result.name,
                    "class_metric_id": result.metric_id,
                    "as_of": result.as_of.isoformat(),
                    "timesteps": [
                        timestamp.isoformat() for timestamp in result.timesteps
                    ],
                }
                for result in results
            ],
        }
        return meta, results
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def write_cache(cache_dir: Path, meta: dict, results: list[MetricResult]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        metric_dir = cache_dir / result.slug
        metric_dir.mkdir(parents=True, exist_ok=True)
        result.end_state.to_csv(metric_dir / _CACHE_FILES["end_state"], index=False)
        result.class_history.to_csv(
            metric_dir / _CACHE_FILES["class_history"], index=False
        )
        result.size_history.to_csv(
            metric_dir / _CACHE_FILES["size_history"], index=False
        )
        result.stats.to_csv(metric_dir / _CACHE_FILES["stats"], index=False)
        if not result.yearly_state.empty:
            result.yearly_state.to_csv(
                metric_dir / _CACHE_FILES["yearly_state"], index=False
            )
            result.yearly_mass.to_csv(
                metric_dir / _CACHE_FILES["yearly_mass"], index=False
            )
    (cache_dir / _CACHE_META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote query cache to {cache_dir}")


def read_cache(cache_dir: Path) -> tuple[dict, list[MetricResult]]:
    meta_path = cache_dir / _CACHE_META
    if not meta_path.exists():
        raise SystemExit(
            f"No {_CACHE_META} in {cache_dir}.\n"
            "Re-run against the database with --cache-dir to populate it."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if "metrics" not in meta:
        raise SystemExit(
            f"{meta_path} is in the old single-metric cache format.\n"
            "Re-run against the database with --cache-dir to repopulate it."
        )

    results = []
    for entry in meta["metrics"]:
        name = entry["class_metric"]
        metric_dir = cache_dir / metric_slug(name)
        missing = [
            filename
            for key, filename in _CACHE_FILES.items()
            if key not in _OPTIONAL_CACHE_FILES and not (metric_dir / filename).exists()
        ]
        if missing:
            raise SystemExit(
                f"Incomplete cache in {metric_dir}: missing "
                + ", ".join(missing)
                + "\nRe-run against the database with --cache-dir to populate it."
            )

        frames = {
            key: pd.read_csv(metric_dir / filename)
            for key, filename in _CACHE_FILES.items()
            if (metric_dir / filename).exists()
        }
        for key in (
            "class_history",
            "size_history",
            "stats",
            "yearly_state",
            "yearly_mass",
        ):
            if key in frames and not frames[key].empty:
                frames[key]["timestamp"] = pd.to_datetime(
                    frames[key]["timestamp"], utc=True
                )

        results.append(
            MetricResult(
                name=name,
                metric_id=entry.get("class_metric_id"),
                as_of=pd.Timestamp(entry["as_of"]),
                timesteps=[
                    pd.Timestamp(value) for value in entry.get("timesteps", [])
                ],
                end_state=frames["end_state"],
                class_history=frames["class_history"],
                size_history=frames["size_history"],
                stats=frames.get("stats", _empty_stats()),
                yearly_state=frames.get("yearly_state", _empty_yearly_state()),
                yearly_mass=frames.get("yearly_mass", _empty_yearly_mass()),
            )
        )
    return meta, results


# --------------------------------------------------------------------------
# Selection and summary (pure functions over frames -- shared by both paths)
# --------------------------------------------------------------------------


def select_per_class(
    end_state: pd.DataFrame, per_class: int, label: str = ""
) -> dict[float, list[str]]:
    """The ``per_class`` largest domains of each class at the as-of timestep."""
    prefix = f"{label}: " if label else ""
    selected: dict[float, list[str]] = {}
    sized = end_state.dropna(subset=["size"])
    for class_value in CLASS_ORDER:
        members = sized[sized["classification"] == class_value]
        members = members.sort_values("size", ascending=False)
        chosen = list(members["domain"].head(per_class))
        if len(chosen) < per_class:
            print(
                f"Warning: {prefix}only {len(chosen)} domain(s) classified as "
                f"{CLASS_LABELS[class_value]} at the as-of timestep "
                f"(asked for {per_class})."
            )
        selected[class_value] = chosen
    return selected


def select_history_keys(
    end_state: pd.DataFrame, top: int, per_class: int
) -> list[str]:
    """Domains whose full history is needed: the CSV top-N plus the plotted ones."""
    keys = list(end_state["domain"].head(top))
    seen = set(keys)
    for chosen in select_per_class(end_state, per_class).values():
        for domain in chosen:
            if domain not in seen:
                seen.add(domain)
                keys.append(domain)
    return keys


def summarise_domains(
    end_state: pd.DataFrame,
    class_history: pd.DataFrame,
    top: int,
    plotted: set[str],
) -> pd.DataFrame:
    """Top-N table: end size, end class, and how stable that class was."""
    rows = []
    by_domain = {
        domain: frame.sort_values("timestamp")
        for domain, frame in class_history.groupby("key_string")
    }

    for rank, record in enumerate(
        end_state.head(top).itertuples(index=False), start=1
    ):
        history = by_domain.get(record.domain)
        if history is None or history.empty:
            counts, switches, first, last, classified = {}, 0, None, None, 0
        else:
            values = list(history["value"])
            counts = history["value"].value_counts().to_dict()
            switches = sum(
                1 for a, b in zip(values, values[1:]) if a != b
            )
            first = history["timestamp"].iloc[0]
            last = history["timestamp"].iloc[-1]
            classified = len(values)

        rows.append(
            {
                "rank": rank,
                "domain": record.domain,
                "size_end": record.size,
                "class_end": CLASS_LABELS.get(record.classification, "unknown"),
                "timesteps_present": classified,
                "timesteps_dominated": int(counts.get(CLASS_DOMINATED, 0)),
                "timesteps_contested": int(counts.get(CLASS_CONTESTED, 0)),
                "timesteps_dominating": int(counts.get(CLASS_DOMINATING, 0)),
                "timesteps_unclassified": int(counts.get(CLASS_UNCLASSIFIED, 0)),
                "class_switches": switches,
                "first_seen": first,
                "last_seen": last,
                "selected_for_plot": record.domain in plotted,
            }
        )

    return pd.DataFrame(rows)


_SUMMARY_ROW = "{:>4}  {:<38} {:>14}  {:<12} {:>5} {:>5} {:>5} {:>5} {:>5} {:>6}  {}"


def print_summary(summary: pd.DataFrame, label: str, limit: int = 25) -> None:
    header = _SUMMARY_ROW.format(
        "rank",
        "domain",
        "size",
        "class",
        "dom-",
        "cont",
        "dom+",
        "uncl",
        "n",
        "switch",
        "plot",
    )
    print()
    print(f"Largest domains by end class -- {label}")
    print(header)
    print("-" * len(header))

    for record in summary.head(limit).itertuples(index=False):
        size = "n/a" if pd.isna(record.size_end) else f"{record.size_end:,.0f}"
        print(
            _SUMMARY_ROW.format(
                record.rank,
                record.domain[:38],
                size,
                record.class_end,
                record.timesteps_dominated,
                record.timesteps_contested,
                record.timesteps_dominating,
                record.timesteps_unclassified,
                record.timesteps_present,
                record.class_switches,
                "*" if record.selected_for_plot else "",
            )
        )

    if len(summary) > limit:
        print(f"... {len(summary) - limit} more row(s) in the CSV")
    print()


# --------------------------------------------------------------------------
# Per-year breakdown (supermajority classifier only)
# --------------------------------------------------------------------------


def year_tick_timesteps(
    timesteps: list[pd.Timestamp], timezone: str = _PLOT_TIMEZONE
) -> pd.DataFrame:
    """The timestep closest to each year tick of the figure's x axis.

    The figure places its ticks with YearLocator on data shown in ``timezone``,
    i.e. at Jan 1 00:00 local time, and only draws the ones inside the data range.
    Returns one row per tick with ``year``, ``tick`` and ``timestamp`` (the
    chosen timestep, UTC); a timestep closest to two ticks is kept for the first.
    """
    if not timesteps:
        return pd.DataFrame(columns=["year", "tick", "timestamp"])

    steps = pd.Series(pd.to_datetime(sorted(timesteps), utc=True))
    first = steps.iloc[0].tz_convert(timezone)
    last = steps.iloc[-1].tz_convert(timezone)

    rows = []
    for year in range(first.year, last.year + 1):
        tick = pd.Timestamp(year=year, month=1, day=1, tz=timezone)
        if not first <= tick <= last:
            continue
        closest = (steps - tick).abs().idxmin()
        rows.append({"year": year, "tick": tick, "timestamp": steps.iloc[closest]})

    frame = pd.DataFrame(rows, columns=["year", "tick", "timestamp"])
    return frame.drop_duplicates("timestamp", ignore_index=True)


def breakdown_timesteps(
    timesteps: list[pd.Timestamp], detailed: bool, timezone: str = _PLOT_TIMEZONE
) -> pd.DataFrame:
    """The timesteps the top-k breakdown is reported at, as year_tick_timesteps.

    Without ``detailed`` these are the year ticks. With it, every timestep; the
    ones chosen for a year tick keep that tick, the others have none (NaT).
    """
    ticks = year_tick_timesteps(timesteps, timezone)
    if not detailed or not timesteps:
        return ticks

    steps = pd.Series(pd.to_datetime(sorted(timesteps), utc=True))
    tick_of_step = ticks.set_index("timestamp")["tick"]
    return pd.DataFrame(
        {
            "year": steps.dt.tz_convert(timezone).dt.year,
            "tick": steps.map(tick_of_step),
            "timestamp": steps,
        }
    )


def build_yearly_mass(
    class_stats: pd.DataFrame,
    size_stats: pd.DataFrame,
    timestamps: list[pd.Timestamp],
) -> pd.DataFrame:
    """Per timestep: the mass the classifier divides by, and each class's fraction.

    total_mass = total_reference_count - tail_count, the sum of the per-domain
    sizes, which is what the classifier's class fractions are fractions of. The
    series are in metric_value_float (timestamptz) and the timesteps come from
    metric_on_string (timestamp without time zone), so they are matched to the
    nearest value within a day rather than by equality.
    """
    columns = list(_empty_yearly_mass().columns)
    if not timestamps:
        return _empty_yearly_mass()

    class_names = [CLASS_LABELS[value] for value in YEARLY_CLASS_ORDER]
    wanted = pd.concat(
        [
            class_stats[class_stats["stat"].isin(class_names)],
            size_stats[size_stats["stat"].isin([_TOTAL_MASS_STAT, _TAIL_MASS_STAT])],
        ],
        ignore_index=True,
    )

    left = pd.DataFrame(
        {"timestamp": pd.to_datetime(pd.Series(timestamps), utc=True)}
    ).astype({"timestamp": "datetime64[ns, UTC]"}).sort_values("timestamp")
    # Each series is matched on its own, so series written a little apart in
    # time cannot split one timestep's values over two rows.
    merged = left
    for name in (_TOTAL_MASS_STAT, _TAIL_MASS_STAT, *class_names):
        series = wanted.loc[wanted["stat"] == name, ["timestamp", "value"]]
        if series.empty:
            merged[name] = float("nan")
            continue
        series = series.rename(columns={"value": name})
        series["timestamp"] = pd.to_datetime(series["timestamp"], utc=True).astype(
            "datetime64[ns, UTC]"
        )
        merged = pd.merge_asof(
            merged,
            series.sort_values("timestamp").drop_duplicates("timestamp", keep="last"),
            on="timestamp",
            direction="nearest",
            tolerance=pd.Timedelta(days=1),
        )

    merged["total_mass"] = merged[_TOTAL_MASS_STAT] - merged[_TAIL_MASS_STAT]
    return merged[columns].reset_index(drop=True)


def _mass_by_timestep(yearly_mass: pd.DataFrame) -> pd.DataFrame:
    mass = yearly_mass.copy()
    mass["timestamp"] = pd.to_datetime(mass["timestamp"], utc=True)
    return mass.set_index("timestamp")


def _masses_at(mass: pd.DataFrame, timestamp: pd.Timestamp) -> pd.Series:
    """Total mass and class fractions at one timestep; empty if not recorded."""
    if timestamp in mass.index:
        return mass.loc[timestamp]
    return pd.Series(dtype="float64")


def summarise_yearly(
    yearly_state: pd.DataFrame,
    yearly_mass: pd.DataFrame,
    ticks: pd.DataFrame,
    k: int,
) -> pd.DataFrame:
    """The k largest domains of every class at every year tick, long format.

    One row per (year, class, rank); a class with no domain at a tick gets one
    row with an empty rank so its mass is still reported. The class metric holds
    the largest domains by size regardless of class, so within it the top-k of a
    class are its true top-k -- unless fewer than k of the class are in it, which
    ``complete`` marks: the class's next domains are then below the cap.
    """
    mass = _mass_by_timestep(yearly_mass)

    rows = []
    for tick in ticks.itertuples(index=False):
        at_tick = yearly_state[yearly_state["timestamp"] == tick.timestamp]
        domains_written = len(at_tick)
        at_tick = at_tick.dropna(subset=["size"])

        masses = _masses_at(mass, tick.timestamp)
        total_mass = float(masses.get("total_mass", float("nan")))

        for class_value in YEARLY_CLASS_ORDER:
            class_name = CLASS_LABELS[class_value]
            fraction = float(masses.get(class_name, float("nan")))
            class_mass = fraction * total_mass
            in_class = at_tick[at_tick["classification"] == class_value]
            members = in_class.nlargest(k, "size")
            topk_mass = float(members["size"].sum())

            def share(value: float) -> float:
                return value / class_mass if class_mass > 0 else float("nan")

            base = {
                "year": tick.year,
                "tick": tick.tick,
                "timestep": tick.timestamp,
                "class": class_name,
                "total_mass": total_mass,
                "class_mass": class_mass,
                "class_share_of_total": fraction,
                "topk_mass": topk_mass,
                "topk_share_of_class": share(topk_mass),
                "class_domains_written": len(in_class),
                "domains_written": domains_written,
                "complete": len(in_class) >= k,
            }
            if members.empty:
                rows.append(
                    {
                        **base,
                        "rank": pd.NA,
                        "domain": None,
                        "mass": float("nan"),
                        "share_of_class": float("nan"),
                    }
                )
            for rank, record in enumerate(members.itertuples(index=False), start=1):
                rows.append(
                    {
                        **base,
                        "rank": rank,
                        "domain": record.domain,
                        "mass": float(record.size),
                        "share_of_class": share(record.size),
                    }
                )

    columns = [
        "year",
        "tick",
        "timestep",
        "class",
        "rank",
        "domain",
        "mass",
        "share_of_class",
        "class_mass",
        "class_share_of_total",
        "topk_mass",
        "topk_share_of_class",
        "class_domains_written",
        "domains_written",
        "complete",
        "total_mass",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["rank"] = frame["rank"].astype("Int64")
    return frame


def _format_mass(value: float) -> str:
    return "n/a" if pd.isna(value) else f"{value:,.0f}"


def _format_share(value: float) -> str:
    return "n/a" if pd.isna(value) else f"{value:.1%}"


# The Wikidata label of a QID domain goes last, so the numbers stay aligned next
# to the domain and a missing label leaves no gap.
_YEARLY_ROW = "      {:>3}  {:<38} {:>15}  {:>6}  {}"


def _step_heading(head: pd.Series, detailed: bool, extra: str = "") -> str:
    """Heading of one timestep's block, from the first row of that block."""
    notes = f"total mass {_format_mass(head['total_mass'])}{extra}"
    if detailed:
        tick = "" if pd.isna(head["tick"]) else f"  <- year tick {head['year']}"
        return f"  {head['timestep']:%Y-%m-%d}{tick}  ({notes})"
    return (
        f"  {head['year']}  (tick {head['tick']:%Y-%m-%d}, timestep "
        f"{head['timestep']:%Y-%m-%d}, {notes})"
    )


def print_yearly(
    summary: pd.DataFrame, label: str, k: int, detailed: bool = False
) -> None:
    where = "every timestep" if detailed else "each year tick"
    print()
    print(f"Top {k} domains per class at {where} -- {label}")
    print(
        "  mass = references of the domain at that timestep; "
        "% = its share of the class mass"
    )
    for _, frame in summary.groupby("timestep", sort=False):
        head = frame.iloc[0]
        print()
        print(_step_heading(head, detailed))
        if head["domains_written"] == 0:
            print("    no classified domains recorded at this timestep")
            continue

        for class_name, rows in frame.groupby("class", sort=False):
            first = rows.iloc[0]
            line = (
                f"    {class_name:<13} class mass {_format_mass(first['class_mass']):>15} "
                f"({_format_share(first['class_share_of_total'])} of total)   "
                f"top {k} hold {_format_share(first['topk_share_of_class'])} of the class"
            )
            if not first["complete"]:
                line += (
                    f"   [only {first['class_domains_written']} among the "
                    f"{first['domains_written']} largest domains]"
                )
            print(line)
            for record in rows.dropna(subset=["rank"]).itertuples(index=False):
                print(
                    _YEARLY_ROW.format(
                        int(record.rank),
                        str(record.domain)[:38],
                        _format_mass(record.mass),
                        _format_share(record.share_of_class),
                        getattr(record, "label", ""),
                    ).rstrip()
                )
    print()


def summarise_top_overall(
    yearly_state: pd.DataFrame,
    yearly_mass: pd.DataFrame,
    steps: pd.DataFrame,
    k: int,
) -> pd.DataFrame:
    """The k largest domains at each timestep, regardless of class, long format.

    ``share_of_total`` is each domain's share of the total mass (the summed
    per-domain mass the class fractions also refer to), ``cumulative_share``
    that of ranks 1..rank together. The class metric holds the largest domains
    by size whatever their class, so this is exact for k up to its cap.
    """
    mass = _mass_by_timestep(yearly_mass)

    rows = []
    for step in steps.itertuples(index=False):
        at_step = yearly_state[yearly_state["timestamp"] == step.timestamp]
        domains_written = len(at_step)
        top = at_step.dropna(subset=["size"]).nlargest(k, "size")

        total_mass = float(
            _masses_at(mass, step.timestamp).get("total_mass", float("nan"))
        )

        def share(value: float) -> float:
            return value / total_mass if total_mass > 0 else float("nan")

        base = {
            "year": step.year,
            "tick": step.tick,
            "timestep": step.timestamp,
            "total_mass": total_mass,
            "topk_share_of_total": share(float(top["size"].sum())),
            "domains_written": domains_written,
        }
        if top.empty:
            rows.append(
                {
                    **base,
                    "rank": pd.NA,
                    "domain": None,
                    "class": None,
                    "mass": float("nan"),
                    "share_of_total": float("nan"),
                    "cumulative_share": float("nan"),
                }
            )
        cumulative = 0.0
        for rank, record in enumerate(top.itertuples(index=False), start=1):
            cumulative += float(record.size)
            rows.append(
                {
                    **base,
                    "rank": rank,
                    "domain": record.domain,
                    "class": CLASS_LABELS.get(record.classification, "unknown"),
                    "mass": float(record.size),
                    "share_of_total": share(record.size),
                    "cumulative_share": share(cumulative),
                }
            )

    columns = [
        "year",
        "tick",
        "timestep",
        "rank",
        "domain",
        "mass",
        "share_of_total",
        "cumulative_share",
        "class",
        "total_mass",
        "topk_share_of_total",
        "domains_written",
    ]
    frame = pd.DataFrame(rows, columns=columns)
    frame["rank"] = frame["rank"].astype("Int64")
    return frame


_TOP_OVERALL_ROW = "      {:>3}  {:<38} {:>15}  {:>6}  {:>6}  {:<12}  {}"


def print_top_overall(
    summary: pd.DataFrame, label: str, k: int, detailed: bool = False
) -> None:
    where = "every timestep" if detailed else "each year tick"
    print()
    print(f"Top {k} domains by mass at {where}, regardless of class -- {label}")
    header = _TOP_OVERALL_ROW.format(
        "#", "domain", "mass", "%", "cum %", "class", "label"
    )
    for _, frame in summary.groupby("timestep", sort=False):
        head = frame.iloc[0]
        print()
        print(
            _step_heading(
                head,
                detailed,
                f"; top {k} hold {_format_share(head['topk_share_of_total'])}",
            )
        )
        if head["domains_written"] == 0:
            print("    no domains recorded at this timestep")
            continue
        print(header)
        # iterrows, not itertuples: "class" is a keyword and would be renamed.
        for _, row in frame.dropna(subset=["rank"]).iterrows():
            print(
                _TOP_OVERALL_ROW.format(
                    int(row["rank"]),
                    str(row["domain"])[:38],
                    _format_mass(row["mass"]),
                    _format_share(row["share_of_total"]),
                    _format_share(row["cumulative_share"]),
                    row["class"],
                    row.get("label", ""),
                ).rstrip()
            )
    print()


# --------------------------------------------------------------------------
# Global diagnostic series
# --------------------------------------------------------------------------


def _stat_sort_key(stat: str) -> tuple[int, str]:
    if stat in _STATS_ORDER_HEAD:
        return (_STATS_ORDER_HEAD.index(stat), "")
    return (len(_STATS_ORDER_HEAD), stat)


def summarise_stats(stats: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """One row per global series: value at the as-of timestep, and its range."""
    if stats.empty:
        return pd.DataFrame(
            columns=["stat", "at_as_of", "first", "min", "max", "mean", "timesteps"]
        )

    rows = []
    for stat, frame in stats.groupby("stat"):
        frame = frame.sort_values("timestamp")
        at_as_of = frame.loc[frame["timestamp"] == as_of, "value"]
        rows.append(
            {
                "stat": stat,
                "at_as_of": float(at_as_of.iloc[0]) if len(at_as_of) else float("nan"),
                "first": float(frame["value"].iloc[0]),
                "min": float(frame["value"].min()),
                "max": float(frame["value"].max()),
                "mean": float(frame["value"].mean()),
                "timesteps": int(len(frame)),
            }
        )

    summary = pd.DataFrame(rows)
    summary = summary.sort_values(
        "stat", key=lambda column: column.map(_stat_sort_key), ignore_index=True
    )
    return summary


def _format_stat_value(value: float) -> str:
    """Counts and fractions share these columns, so the scale picks the format."""
    if pd.isna(value):
        return "n/a"
    if value != 0 and abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.4f}"


_STATS_ROW = "  {:<34} {:>12} {:>12} {:>12} {:>12} {:>12} {:>5}"


def print_stats(summary: pd.DataFrame, label: str, as_of: pd.Timestamp) -> None:
    print()
    print(f"Global series for {label} (as of {as_of})")
    if summary.empty:
        print(f"  (no series in {_GLOBAL_VALUE_TABLE} with this classifier's prefix)")
        print()
        return

    header = _STATS_ROW.format(
        "stat", "at as-of", "first", "min", "max", "mean", "n"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for record in summary.itertuples(index=False):
        print(
            _STATS_ROW.format(
                record.stat[:34],
                _format_stat_value(record.at_as_of),
                _format_stat_value(record.first),
                _format_stat_value(record.min),
                _format_stat_value(record.max),
                _format_stat_value(record.mean),
                record.timesteps,
            )
        )
    print()


# --------------------------------------------------------------------------
# Cross-classifier comparison
# --------------------------------------------------------------------------


_ABSENT = "absent"


def compare_end_states(results: list[MetricResult], top: int) -> pd.DataFrame:
    """Side-by-side end class per domain, over the union of each metric's top-N.

    "absent" means the domain has no row in that classifier's metric at its
    as-of timestep: unclassified, or outside the top-1000-by-size cap.
    """
    domains: list[str] = []
    seen: set[str] = set()
    for result in results:
        for domain in result.end_state.head(top)["domain"]:
            if domain not in seen:
                seen.add(domain)
                domains.append(domain)

    frame = pd.DataFrame({"domain": domains})
    size = pd.Series(index=pd.Index(domains, name="domain"), dtype="float64")
    for result in results:
        indexed = result.end_state.drop_duplicates("domain").set_index("domain")
        size = size.combine_first(indexed["size"].reindex(size.index))
        frame[result.label] = (
            frame["domain"]
            .map(indexed["classification"])
            .map(lambda value: CLASS_LABELS.get(value, _ABSENT))
            .fillna(_ABSENT)
        )

    frame.insert(1, "size_end", frame["domain"].map(size))
    return frame.sort_values("size_end", ascending=False, ignore_index=True)


def print_comparison(
    comparison: pd.DataFrame, results: list[MetricResult], limit: int = 25
) -> None:
    labels = [result.label for result in results]
    width = max(12, *(len(label) for label in labels))
    row_format = "  {:<38} {:>14}  " + "  ".join(
        [f"{{:<{width}}}"] * len(labels)
    )

    print()
    print("End classification per classifier (same run, same size metric)")
    header = row_format.format("domain", "size", *labels)
    print(header)
    print("  " + "-" * (len(header) - 2))
    # iterrows, not itertuples: the class columns are named after the metrics,
    # and itertuples would rename any label that is not a valid identifier.
    for _, row in comparison.head(limit).iterrows():
        size = "n/a" if pd.isna(row["size_end"]) else f"{row['size_end']:,.0f}"
        classes = [row[label] for label in labels]
        print(row_format.format(str(row["domain"])[:38], size, *classes))
    if len(comparison) > limit:
        print(f"  ... {len(comparison) - limit} more row(s) in the CSV")

    print()
    print("  Agreement over domains both classifiers place (excluding 'absent'):")
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            both = comparison[
                (comparison[left] != _ABSENT) & (comparison[right] != _ABSENT)
            ]
            if both.empty:
                print(f"    {left} vs {right}: no shared domains")
                continue
            same = int((both[left] == both[right]).sum())
            print(
                f"    {left} vs {right}: {same}/{len(both)} "
                f"({same / len(both):.1%}) identical"
            )
            crosstab = pd.crosstab(both[left], both[right])
            for line in crosstab.to_string().splitlines():
                print("      " + line)
    print()


# --------------------------------------------------------------------------
# Figure
# --------------------------------------------------------------------------


def _draw_year_guides(axis, ticks, alpha: float) -> None:
    """Year guide lines, matching paper_plots.render._draw_vertical_guides."""
    from paper_plots.render import VERTICAL_GUIDE_COLOR

    for x in ticks:
        axis.axvline(
            x=x,
            color=VERTICAL_GUIDE_COLOR,
            linewidth=0.7,
            alpha=alpha,
            zorder=0.4,
        )


def _domain_series(
    domain: str,
    size_history: pd.DataFrame,
    class_history: pd.DataFrame,
    timezone: str,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Size series of one domain, aligned with its class per timestep."""
    sizes = (
        size_history[size_history["key_string"] == domain]
        .sort_values("timestamp")
        .copy()
    )
    classes = class_history[class_history["key_string"] == domain].set_index(
        "timestamp"
    )["value"]

    dropped = int((sizes["value"] <= 0).sum())
    if dropped:
        print(
            f"Warning: dropping {dropped} non-positive size value(s) for "
            f"{domain} (log scale)."
        )
        sizes = sizes[sizes["value"] > 0]

    timestamps = sizes["timestamp"].dt.tz_convert(timezone)
    return timestamps, sizes["value"], sizes["timestamp"].map(classes)


def _place_end_labels(axis, labels: list[tuple[float, float, str, str]]) -> None:
    """Domain labels at each line's end, pushed apart so they stay readable.

    Anchored to the last data point rather than the axes edge: the caller pads
    the x range to make room, so the text stays inside the axes and does not
    fight constrained_layout.
    """
    if not labels:
        return

    figure = axis.figure
    figure.canvas.draw()
    # transData works in pixels at the figure dpi, not in points.
    minimum_gap = 9.0 * figure.dpi / 72.0  # ~9pt, just over the 7pt label height

    ordered = sorted(labels, key=lambda item: item[0])
    positions = [axis.transData.transform((0, y))[1] for y, _, _, _ in ordered]
    for index in range(1, len(positions)):
        positions[index] = max(positions[index], positions[index - 1] + minimum_gap)

    for (_, x, text, color), display_y in zip(ordered, positions):
        data_y = axis.transData.inverted().transform((0, display_y))[1]
        axis.annotate(
            text,
            xy=(x, data_y),
            xytext=(5, 0),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=7,
            color=color,
        )


def render_figure(
    title: str,
    selected: dict[float, list[str]],
    class_history: pd.DataFrame,
    size_history: pd.DataFrame,
    outputs: list[Path],
    timezone: str = "Europe/Berlin",
) -> bool:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    from matplotlib.lines import Line2D

    from paper_plots.render import _RC_PARAMS

    with plt.rc_context(_RC_PARAMS):
        figure, axis = plt.subplots(figsize=(14, 4.5), constrained_layout=True)
        axis.set_yscale("log")

        end_labels: list[tuple[float, float, str, str]] = []
        bounds: list[tuple[float, float, float, float]] = []

        for class_value in CLASS_ORDER:
            for domain in selected[class_value]:
                timestamps, sizes, classes = _domain_series(
                    domain, size_history, class_history, timezone
                )
                if len(timestamps) < 2:
                    print(f"Warning: {domain} has too few size points to draw.")
                    continue

                x = mdates.date2num(timestamps.to_numpy())
                y = sizes.to_numpy(dtype=float)
                class_values = list(classes)
                step = pd.Timedelta(days=45)  # a timestep is 30 days

                segments = []
                colors = []
                for index in range(len(x) - 1):
                    # A gap in the size series means the domain was pruned from
                    # the mass table; do not draw a line across the hole.
                    if timestamps.iloc[index + 1] - timestamps.iloc[index] > step:
                        continue
                    segments.append([(x[index], y[index]), (x[index + 1], y[index + 1])])
                    held = class_values[index]
                    colors.append(
                        CLASS_COLORS[None]
                        if pd.isna(held)
                        else CLASS_COLORS.get(held, CLASS_COLORS[None])
                    )

                # autolim=False: on a log axis add_collection records the data
                # limits in log-transformed space, which autoscale_view would
                # then take at face value. The limits are set explicitly below.
                axis.add_collection(
                    LineCollection(
                        segments, colors=colors, linewidths=1.8, zorder=2
                    ),
                    autolim=False,
                )
                bounds.append((x.min(), x.max(), y.min(), y.max()))
                end_labels.append(
                    (y[-1], x[-1], domain, CLASS_COLORS[class_value])
                )

        if not end_labels:
            print(
                f"Warning: nothing to plot for {title}: no selected domain had a "
                "size series."
            )
            plt.close(figure)
            return False

        x_min = min(bound[0] for bound in bounds)
        x_max = max(bound[1] for bound in bounds)
        y_min = min(bound[2] for bound in bounds)
        y_max = max(bound[3] for bound in bounds)

        span = x_max - x_min
        # Padding on the right holds the end labels inside the axes.
        axis.set_xlim(x_min - 0.01 * span, x_max + 0.14 * span)
        axis.set_ylim(y_min / 1.6, y_max * 1.6)

        # Year ticks, but none inside the label padding: an axis running past
        # the last datapoint would read as time with no data in it.
        year_ticks = mdates.YearLocator(base=1).tick_values(
            mdates.num2date(x_min), mdates.num2date(x_max)
        )
        axis.set_xticks([tick for tick in year_ticks if x_min <= tick <= x_max])
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        axis.set_ylabel("Reference count (domain size)")
        # Several classifiers produce the same figure for the same run, so the
        # figure has to say which one it is.
        axis.set_title(title, loc="left", fontsize=9)
        axis.grid(axis="y", alpha=0.22, linewidth=0.7)
        axis.grid(axis="x", visible=False)

        _draw_year_guides(axis, axis.get_xticks(), alpha=0.30)

        handles = [
            Line2D([], [], color=CLASS_COLORS[CLASS_DOMINATING], linewidth=1.8),
            Line2D([], [], color=CLASS_COLORS[CLASS_CONTESTED], linewidth=1.8),
            Line2D([], [], color=CLASS_COLORS[CLASS_DOMINATED], linewidth=1.8),
            Line2D([], [], color=CLASS_COLORS[None], linewidth=1.8),
        ]
        axis.legend(
            handles,
            ["Dominating", "Contested", "Dominated", "Unclassified"],
            loc="upper left",
            frameon=False,
        )

        _place_end_labels(axis, end_labels)

        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
            figure.savefig(output, bbox_inches="tight")
            print(f"Wrote {output}")

        plt.close(figure)
    return True


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _expand_class_metric(value: str) -> str:
    return CLASS_METRIC_ALIASES.get(value, value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query the largest reference domains and their classification, per "
            "classifier: the classifier's global diagnostic series, a table of "
            "the largest domains, and their size history coloured by class."
        )
    )
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument(
        "--class-metric",
        nargs="+",
        default=list(DEFAULT_CLASS_METRICS),
        metavar="METRIC",
        help=(
            "one or more classification metrics; the short names "
            + ", ".join(sorted(CLASS_METRIC_ALIASES))
            + " are accepted for the known ones. Metrics that do not exist in "
            "the database, or hold no data for the run, are skipped with a note "
            "(default: all known ones)"
        ),
    )
    parser.add_argument("--size-metric", default=DEFAULT_SIZE_METRIC)
    parser.add_argument(
        "--per-class",
        type=int,
        default=3,
        help="lines per class in the figure (default: 3)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="rows in the CSV and console table (default: 50)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="timestep to classify by (default: each metric's last timestep)",
    )
    parser.add_argument(
        "--yearly-top",
        type=int,
        default=5,
        metavar="K",
        help=(
            "domains per class at each year tick, for the supermajority "
            "classifier only; covers the whole run regardless of --as-of "
            "(default: 5, 0 = off)"
        ),
    )
    parser.add_argument("--outdir", type=Path, default=Path("."))
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="also write the fetched frames here for offline re-plotting",
    )
    parser.add_argument(
        "--from-cache",
        type=Path,
        default=None,
        help="read a previously written cache instead of connecting to the DB",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="statement_timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--no-stats",
        dest="stats",
        action="store_false",
        help="skip the classifiers' global diagnostic series",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help=(
            "report the --yearly-top breakdown at every timestep instead of only "
            "at the year ticks (one query per timestep; written to "
            "timestep_top_domains_*.csv)"
        ),
    )
    parser.add_argument(
        "--just-top",
        action="store_true",
        help=(
            "only list the --yearly-top largest domains by mass, regardless of "
            "class, at each year tick (every timestep with --detailed), with "
            "each one's share of the total mass; nothing else is reported. "
            "Uses the supermajority classifier's data"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "do not ask the Wikidata API for the labels of QID domains; labels "
            "already in the shared label cache are still shown"
        ),
    )
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--list-metrics", action="store_true")
    args = parser.parse_args(argv)

    args.class_metric = list(
        dict.fromkeys(_expand_class_metric(value) for value in args.class_metric)
    )
    if args.per_class < 1:
        parser.error("--per-class must be >= 1")
    if args.list_metrics and args.from_cache is not None:
        parser.error("--list-metrics needs a database connection; drop --from-cache")
    if args.top < 1:
        parser.error("--top must be >= 1")
    if args.yearly_top < 0:
        parser.error("--yearly-top must be >= 0")
    if args.detailed and args.yearly_top == 0:
        parser.error("--detailed needs --yearly-top > 0")
    if args.just_top:
        if args.yearly_top == 0:
            parser.error("--just-top needs --yearly-top > 0")
        # The list is read off the supermajority classifier's per-timestep data,
        # which holds the largest domains of every class; no other is needed.
        args.class_metric = [YEARLY_CLASS_METRIC]
    return args


def report_metric(args, meta: dict, result: MetricResult) -> None:
    """Console output and files for one classifier."""
    as_of_date = str(result.as_of)[:10]
    output_dir = args.outdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 78)
    print(f"{result.label}  ({result.name})")
    print("=" * 78)

    if args.just_top:
        report_top_overall(args, meta, result, output_dir)
        return

    if args.stats:
        stats_summary = summarise_stats(result.stats, result.as_of)
        print_stats(stats_summary, result.label, result.as_of)
        if not result.stats.empty:
            stats_path = (
                output_dir
                / f"classification_stats_{meta['run_id']}_{result.slug}.csv"
            )
            result.stats.to_csv(stats_path, index=False)
            print(f"Wrote {stats_path}")

    selected = select_per_class(result.end_state, args.per_class, result.label)
    plotted = {domain for chosen in selected.values() for domain in chosen}

    summary = summarise_domains(
        result.end_state, result.class_history, args.top, plotted
    )
    print_summary(summary, result.label)

    csv_path = (
        output_dir
        / f"largest_domains_{meta['run_id']}_{as_of_date}_{result.slug}.csv"
    )
    summary.to_csv(csv_path, index=False)
    print(f"Wrote {csv_path}")

    if result.name == YEARLY_CLASS_METRIC and args.yearly_top > 0:
        report_yearly(args, meta, result, output_dir)

    for class_value in CLASS_ORDER:
        chosen = ", ".join(selected[class_value]) or "(none)"
        print(f"Plotting {CLASS_LABELS[class_value]}: {chosen}")

    stem = f"domain_classification_history_{meta['run_id']}_{result.slug}"
    outputs = [
        args.outdir / "figures" / f"{stem}.pdf",
        args.outdir / "figures" / f"{stem}.svg",
    ]
    render_figure(
        f"{result.label} -- run {meta['run_id']}, as of {as_of_date}",
        selected,
        result.class_history,
        result.size_history,
        outputs,
    )


# One label resolver per process, and the QIDs it was already asked for. The
# resolver keeps every label it got in memory (and in its file cache), but a QID
# whose request failed is not remembered there; this set keeps such a QID from
# being requested again within the same run.
_label_resolver = None
_labels_requested: set[str] = set()


def resolve_labels(domains, offline: bool) -> dict[str, str]:
    """Wikidata label of every QID among ``domains``; "" for everything else.

    Roughly half the reference domains are bare QIDs (references that are not
    URLs). The resolver never raises: without network, or with ``offline``,
    unresolved QIDs just get no label, while cached ones are still used. Each
    QID goes to the API at most once per run, however often this is called.
    """
    global _label_resolver
    from query_preference_graph import DEFAULT_LABEL_CACHE, QID_PATTERN, LabelResolver

    if _label_resolver is None or _label_resolver.fetch == offline:
        _label_resolver = LabelResolver(DEFAULT_LABEL_CACHE, fetch=not offline)

    names = sorted({str(domain) for domain in domains})
    qids = [name for name in names if QID_PATTERN.fullmatch(name)]
    new = [
        qid
        for qid in qids
        if qid not in _label_resolver.labels and qid not in _labels_requested
    ]
    _label_resolver.resolve(new)
    if not offline:
        _labels_requested.update(new)

    labelled = sum(1 for qid in qids if _label_resolver.labels.get(qid))
    source = "cache only (--offline)" if offline else f"{len(new)} requested from Wikidata"
    print(f"Labels: {labelled} of {len(qids)} QID domain(s) resolved ({source}).")
    return {name: _label_resolver.labels.get(name, "") for name in names}


def _breakdown_steps(args, result: MetricResult) -> tuple[pd.DataFrame, bool] | None:
    """Timesteps to report the top-k breakdowns at, and whether that is all of them.

    None when the result holds no yearly data. --detailed falls back to the year
    ticks when the data only covers those (a cache taken without --detailed).
    """
    if result.yearly_state.empty:
        print(
            f"Skipping the top-domain breakdown for {result.label}: no yearly data "
            "(the cache predates it or was written with --yearly-top 0; re-fetch)."
        )
        return None

    detailed = args.detailed
    steps = breakdown_timesteps(result.timesteps, detailed)
    fetched = pd.to_datetime(result.yearly_state["timestamp"], utc=True)
    if detailed and not steps["timestamp"].isin(fetched).all():
        print(
            f"Warning: the cache holds the classified domains of {result.label} "
            "only at the year ticks; showing those. Re-fetch with --detailed "
            "for every timestep."
        )
        detailed = False
        steps = breakdown_timesteps(result.timesteps, detailed)
    return steps, detailed


def _add_labels(frame: pd.DataFrame, offline: bool) -> pd.DataFrame:
    """A ``label`` column after ``domain``, from one lookup for all its domains.

    One lookup for the whole breakdown, so a QID that recurs across timesteps
    is resolved once.
    """
    labels = resolve_labels(frame["domain"].dropna().unique(), offline)
    frame.insert(
        frame.columns.get_loc("domain") + 1,
        "label",
        frame["domain"].map(labels).fillna(""),
    )
    return frame


def report_yearly(args, meta: dict, result: MetricResult, output_dir: Path) -> None:
    """Console blocks and CSV of the per-year top domains per class."""
    breakdown = _breakdown_steps(args, result)
    if breakdown is None:
        return
    steps, detailed = breakdown

    yearly = summarise_yearly(
        result.yearly_state, result.yearly_mass, steps, args.yearly_top
    )
    yearly = _add_labels(yearly, args.offline)
    print_yearly(yearly, result.label, args.yearly_top, detailed)

    stem = "timestep_top_domains" if detailed else "yearly_top_domains"
    yearly_path = output_dir / f"{stem}_{meta['run_id']}_{result.slug}.csv"
    yearly.to_csv(yearly_path, index=False)
    print(f"Wrote {yearly_path}")


def report_top_overall(
    args, meta: dict, result: MetricResult, output_dir: Path
) -> None:
    """Console blocks and CSV of the largest domains regardless of class."""
    breakdown = _breakdown_steps(args, result)
    if breakdown is None:
        return
    steps, detailed = breakdown

    top = summarise_top_overall(
        result.yearly_state, result.yearly_mass, steps, args.yearly_top
    )
    top = _add_labels(top, args.offline)
    print_top_overall(top, result.label, args.yearly_top, detailed)

    stem = "timestep_largest_overall" if detailed else "yearly_largest_overall"
    top_path = output_dir / f"{stem}_{meta['run_id']}_{result.slug}.csv"
    top.to_csv(top_path, index=False)
    print(f"Wrote {top_path}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.from_cache is not None:
        meta, results = read_cache(args.from_cache)
        cached_top = meta.get("top", args.top)
        if args.top > cached_top:
            print(
                f"Warning: cache holds history for the top {cached_top} domains; "
                f"clamping --top {args.top} to {cached_top}."
            )
            args.top = cached_top
        wanted = set(args.class_metric)
        # The default is "every known classifier", which a cache need not hold;
        # only an explicit selection is worth complaining about.
        if wanted != set(DEFAULT_CLASS_METRICS):
            for name in args.class_metric:
                if name not in {result.name for result in results}:
                    print(f"Skipping {short_label(name)}: not in {args.from_cache}.")
            results = [result for result in results if result.name in wanted]
            if not results:
                raise SystemExit(
                    f"None of the requested classification metrics are in "
                    f"{args.from_cache}."
                )
    else:
        meta, results = fetch_from_db(args)
        if args.cache_dir is not None:
            write_cache(args.cache_dir, meta, results)

    for result in results:
        report_metric(args, meta, result)

    if args.yearly_top > 0 and all(
        result.name != YEARLY_CLASS_METRIC for result in results
    ):
        print(
            f"Note: the per-year top domains are only computed for "
            f"{short_label(YEARLY_CLASS_METRIC)}, which run {meta['run_id']} "
            f"does not hold (or was not selected); run {DEFAULT_RUN_ID} does."
        )

    if len(results) > 1 and not args.just_top:
        comparison = compare_end_states(results, args.top)
        print_comparison(comparison, results)
        comparison_path = (
            args.outdir / "output" / f"classification_comparison_{meta['run_id']}.csv"
        )
        comparison_path.parent.mkdir(parents=True, exist_ok=True)
        comparison.to_csv(comparison_path, index=False)
        print(f"Wrote {comparison_path}")


if __name__ == "__main__":
    main()
