#!/usr/bin/env python3
"""Extract per-timestep runtime and memory usage of metrics from SLURM .out logs.

Step 1 of a two-step pipeline:

    parse_logs.py  ->  timings*.csv  ->  plot_runtime_memory.py  ->  figures

One .out file is one *run*: one metric of interest plus the dependencies it pulls
in.  Runtime and memory are therefore attributed to the whole dependency chain,
which the CLI output always spells out.

The regexes below are pinned to the logging format emitted by
metric_computation_scripts/main.py and utils/memory_tracker.py.  If that logging
changes, change them here.

Usage
-----
    python parse_logs.py --logs ../final_logs                 # list + prompt
    python parse_logs.py --logs ../final_logs --select 1,4 --out timings.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Log format
# --------------------------------------------------------------------------- #

# main.py: log.info("Start timestep %s -> %s", start_time, end_time)
TIMESTEP_RE = re.compile(r"Start timestep (?P<start>.+?) -> (?P<end>.+?)\s*$")

# The DuckDB table is printed via pandas and is truncated in the middle ("..."),
# but the last three cells are always `wal_size memory_usage memory_limit`, each
# a "<number> <unit>" pair -- so anchor to the end of the line.
_UNIT = r"bytes|KiB|MiB|GiB|TiB"
DUCKDB_ROW_RE = re.compile(
    r"^\s*\d+\s+\S+.*?"
    rf"[\d.]+\s+(?:{_UNIT})\s+"
    rf"(?P<usage>[\d.]+)\s+(?P<uunit>{_UNIT})\s+"
    rf"(?P<limit>[\d.]+)\s+(?P<lunit>{_UNIT})\s*$"
)

MEM_END_CUR_RE = re.compile(
    r"Memory usage \(end of timestep, current\): (?P<gib>[\d.]+) GiB"
)
MEM_END_PEAK_RE = re.compile(
    r"Memory usage \(end of timestep, peak\): (?P<gib>[\d.]+) GiB"
)

CALC_RE = re.compile(r"Calculating metric: (?P<metric>\S+)\s*$")

ITER_RE = re.compile(
    r"\(Iteration (?P<i>\d+)\) (?P<metric>[A-Za-z0-9_]+): "
    r"real (?P<real>[\d.]+)s \| cpu (?P<cpu>[\d.]+)s"
)
ITER_TOTAL_RE = re.compile(
    r"Iteration (?P<i>\d+) finished in (?P<real>[\d.]+)s \(real\)"
)

# memory_tracker.log_memory_snapshot emits two different shapes.  They must NOT
# be collapsed into one pattern: in the multi-attribute case the header carries
# the total and the indented lines carry the breakdown, while in the
# single-attribute case the header value *is* the total.
SNAP_TOTAL_RE = re.compile(
    r"\[(?P<metric>[^\]]+)\] Memory snapshot [—-] total: (?P<mb>[\d.]+) MB\s*$"
)
SNAP_ONE_RE = re.compile(
    r"\[(?P<metric>[^\]]+)\] Memory snapshot [—-] (?P<attr>[^:]+): "
    r"(?P<mb>[\d.]+) MB \(type .*?, (?P<entries>.+?) entries\)\s*$"
)
SNAP_ATTR_RE = re.compile(
    r"^  (?P<attr>[^:]+): (?P<mb>[\d.]+) MB \((?P<entries>.+?) entries\)\s*$"
)
SNAP_ERR_RE = re.compile(r"Memory snapshot [—-] .*: ERROR sizing object")

# Header lines, emitted once per run before the first timestep.
CONFIG_RE = re.compile(
    r"(?P<key>Memory logging|Less entities|Use cache): (?P<value>True|False)\s*$"
)
DUCKDB_LIMIT_RE = re.compile(r"DuckDB memory_limit: (?P<value>.+?)\s*$")

BYTES_PER = {
    "bytes": 1,
    "KiB": 1024,
    "MiB": 1024 ** 2,
    "GiB": 1024 ** 3,
    "TiB": 1024 ** 4,
}

# pympler sizes are logged as "MB" but memory_tracker divides by 1024*1024,
# so they are really MiB.
MIB_PER_GIB = 1024.0


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class MetricSample:
    """One metric's contribution within one timestep."""

    metric: str
    real_s: Optional[float] = None
    cpu_s: Optional[float] = None
    own_mem_mib: Optional[float] = None
    breakdown: list[tuple[str, float, str]] = field(default_factory=list)


@dataclass
class Timestep:
    index: int
    start: str
    end: str
    duckdb_mem_gib: Optional[float] = None
    duckdb_limit_gib: Optional[float] = None
    mem_end_current_gib: Optional[float] = None
    mem_end_peak_gib: Optional[float] = None
    iteration_total_s: Optional[float] = None
    samples: dict[str, MetricSample] = field(default_factory=dict)
    chain: list[str] = field(default_factory=list)

    def sample(self, metric: str) -> MetricSample:
        if metric not in self.samples:
            self.samples[metric] = MetricSample(metric=metric)
            self.chain.append(metric)
        return self.samples[metric]

    @property
    def memory_rss_gib(self) -> Optional[float]:
        if self.mem_end_current_gib is None or self.duckdb_mem_gib is None:
            return None
        return self.mem_end_current_gib - self.duckdb_mem_gib

    @property
    def memory_own_gib(self) -> Optional[float]:
        values = [s.own_mem_mib for s in self.samples.values() if s.own_mem_mib is not None]
        if not values:
            return None
        return sum(values) / MIB_PER_GIB

    @property
    def runtime_total_s(self) -> Optional[float]:
        values = [s.real_s for s in self.samples.values() if s.real_s is not None]
        return sum(values) if values else None

    @property
    def runtime_primary_s(self) -> Optional[float]:
        if not self.chain:
            return None
        return self.samples[self.chain[-1]].real_s


@dataclass
class Run:
    key: str
    path: Path
    config: dict[str, bool] = field(default_factory=dict)
    duckdb_limit: Optional[str] = None
    timesteps: list[Timestep] = field(default_factory=list)
    dropped_partial: Optional[tuple[str, str]] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def chain(self) -> list[str]:
        """Metric execution order, taken from the first complete timestep."""
        return self.timesteps[0].chain if self.timesteps else []

    @property
    def primary_metric(self) -> str:
        chain = self.chain
        return chain[-1] if chain else self.key

    @property
    def memory_own_available(self) -> bool:
        return any(t.memory_own_gib is not None for t in self.timesteps)

    @property
    def config_summary(self) -> str:
        scope = "less_entities" if self.config.get("Less entities") else "full"
        mem = "mem-log on" if self.config.get("Memory logging") else "mem-log off"
        cache = ", cache" if self.config.get("Use cache") else ""
        return f"{scope}, {mem}{cache}"

    @property
    def date_range(self) -> str:
        if not self.timesteps:
            return "-"
        return f"{self.timesteps[0].start[:7]} .. {self.timesteps[-1].start[:7]}"

    @property
    def timestep_summary(self) -> str:
        n = len(self.timesteps)
        if self.dropped_partial:
            return f"{n} (+1 partial, dropped)"
        return str(n)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _to_gib(value: str, unit: str) -> float:
    return float(value) * BYTES_PER[unit] / BYTES_PER["GiB"]


def parse_run(path: Path) -> Run:
    """Parse one .out file into a Run.

    State machine: `Start timestep` opens a record, `Iteration N finished` closes
    it.  Anything still open at EOF is a truncated run and gets dropped.
    """
    run = Run(key=path.stem, path=path)
    current: Optional[Timestep] = None
    snapshot_metric: Optional[str] = None  # set while collecting indented breakdown
    index = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")

            # Indented breakdown lines only follow a "total:" snapshot header.
            if snapshot_metric is not None:
                match = SNAP_ATTR_RE.match(line)
                if match and current is not None:
                    current.sample(snapshot_metric).breakdown.append(
                        (
                            match.group("attr").strip(),
                            float(match.group("mb")),
                            match.group("entries").strip(),
                        )
                    )
                    continue
                snapshot_metric = None

            if current is None:
                match = CONFIG_RE.search(line)
                if match:
                    run.config[match.group("key")] = match.group("value") == "True"
                    continue
                match = DUCKDB_LIMIT_RE.search(line)
                if match:
                    run.duckdb_limit = match.group("value")
                    continue

            match = TIMESTEP_RE.search(line)
            if match:
                if current is not None:
                    run.warnings.append(
                        f"timestep {current.start} never finished; discarded"
                    )
                current = Timestep(
                    index=index,
                    start=match.group("start").strip(),
                    end=match.group("end").strip(),
                )
                continue

            if current is None:
                continue

            if current.duckdb_mem_gib is None:
                match = DUCKDB_ROW_RE.match(line)
                if match:
                    # Both table rows carry the same process-global value.
                    current.duckdb_mem_gib = _to_gib(
                        match.group("usage"), match.group("uunit")
                    )
                    current.duckdb_limit_gib = _to_gib(
                        match.group("limit"), match.group("lunit")
                    )
                    continue

            match = MEM_END_CUR_RE.search(line)
            if match:
                current.mem_end_current_gib = float(match.group("gib"))
                continue

            match = MEM_END_PEAK_RE.search(line)
            if match:
                current.mem_end_peak_gib = float(match.group("gib"))
                continue

            match = CALC_RE.search(line)
            if match:
                current.sample(match.group("metric"))
                continue

            if SNAP_ERR_RE.search(line):
                run.warnings.append(f"pympler failed to size an object: {line.strip()}")
                continue

            match = SNAP_TOTAL_RE.search(line)
            if match:
                metric = match.group("metric").strip()
                current.sample(metric).own_mem_mib = float(match.group("mb"))
                snapshot_metric = metric
                continue

            match = SNAP_ONE_RE.search(line)
            if match:
                metric = match.group("metric").strip()
                sample = current.sample(metric)
                mib = float(match.group("mb"))
                sample.own_mem_mib = mib
                sample.breakdown.append(
                    (match.group("attr").strip(), mib, match.group("entries").strip())
                )
                continue

            match = ITER_RE.search(line)
            if match:
                sample = current.sample(match.group("metric"))
                sample.real_s = float(match.group("real"))
                sample.cpu_s = float(match.group("cpu"))
                continue

            match = ITER_TOTAL_RE.search(line)
            if match:
                current.iteration_total_s = float(match.group("real"))
                _validate_timestep(run, current)
                run.timesteps.append(current)
                index += 1
                current = None
                continue

    if current is not None:
        run.dropped_partial = (current.start, current.end)

    return run


def _validate_timestep(run: Run, step: Timestep) -> None:
    """Warn (never crash) about anything missing or implausible."""
    if step.mem_end_current_gib is None:
        run.warnings.append(f"{step.start}: no end-of-timestep memory")
    if step.duckdb_mem_gib is None:
        run.warnings.append(f"{step.start}: no DuckDB memory row")

    rss = step.memory_rss_gib
    if rss is not None and rss < 0:
        run.warnings.append(
            f"{step.start}: DuckDB memory ({step.duckdb_mem_gib:.2f} GiB) exceeds "
            f"process RSS ({step.mem_end_current_gib:.2f} GiB); clamped to 0"
        )

    for sample in step.samples.values():
        if sample.own_mem_mib is None or not sample.breakdown:
            continue
        total = sum(mib for _, mib, _ in sample.breakdown)
        # Every value is printf-rounded to 2 decimals before it hits the log, so
        # the sum of n parts can drift from the logged total by up to n/2 * 0.01.
        tolerance = 0.005 * (len(sample.breakdown) + 1)
        if abs(total - sample.own_mem_mib) > tolerance:
            run.warnings.append(
                f"{step.start}: {sample.metric} breakdown sums to {total:.2f} MiB "
                f"but header says {sample.own_mem_mib:.2f} MiB"
            )


def discover_runs(log_dir: Path) -> list[Run]:
    paths = sorted(log_dir.glob("*.out"))
    if not paths:
        raise SystemExit(f"No .out files found in {log_dir}")
    return [parse_run(p) for p in paths]


# --------------------------------------------------------------------------- #
# Listing and selection
# --------------------------------------------------------------------------- #


def print_run_table(runs: list[Run]) -> None:
    key_w = max(len(r.key) for r in runs)
    label_w = max(len(r.primary_metric) for r in runs)
    cfg_w = max(len(r.config_summary) for r in runs)

    print()
    print(
        f"{'#':>2}  {'key':<{key_w}}  {'metric (label)':<{label_w}}  "
        f"{'run config':<{cfg_w}}  {'timesteps':<26}  range"
    )
    print("-" * (key_w + label_w + cfg_w + 42))
    for i, run in enumerate(runs, start=1):
        print(
            f"{i:>2}  {run.key:<{key_w}}  {run.primary_metric:<{label_w}}  "
            f"{run.config_summary:<{cfg_w}}  {run.timestep_summary:<26}  {run.date_range}"
        )
        # Always show what the reported time and memory actually cover.
        print(f"      chain: {' -> '.join(run.chain) or '(none)'}")
    print()


def resolve_selection(runs: list[Run], selection: str) -> list[Run]:
    selection = selection.strip()
    if not selection or selection.lower() == "all":
        return list(runs)

    by_key = {r.key: r for r in runs}
    chosen: list[Run] = []
    for token in re.split(r"[,\s]+", selection):
        if not token:
            continue
        if token.isdigit():
            idx = int(token)
            if not 1 <= idx <= len(runs):
                raise SystemExit(f"Selection out of range: {idx}")
            candidate = runs[idx - 1]
        elif token in by_key:
            candidate = by_key[token]
        else:
            raise SystemExit(f"Unknown run: {token!r}")
        if candidate not in chosen:
            chosen.append(candidate)

    if not chosen:
        raise SystemExit("Empty selection")
    return chosen


def parse_label_overrides(specs: Iterable[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(
                f"Invalid --label {spec!r}. Use --label <run_key_or_metric>=<Pretty Name>"
            )
        key, value = spec.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise SystemExit(f"Invalid --label {spec!r}")
        overrides[key] = value
    return overrides


def label_for(run: Run, overrides: dict[str, str]) -> str:
    # A run key wins over a metric name, so two runs of the same metric can be
    # told apart in the legend.
    return overrides.get(run.key, overrides.get(run.primary_metric, run.primary_metric))


# --------------------------------------------------------------------------- #
# Detail output
# --------------------------------------------------------------------------- #


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    return (
        sum(values),
        statistics.mean(values),
        statistics.median(values),
        max(values),
    )


def print_run_detail(run: Run, label: str) -> None:
    print("=" * 96)
    print(f'{run.key}  ->  "{label}"   {len(run.timesteps)} timesteps')
    if run.duckdb_limit:
        print(f"  config: {run.config_summary}, DuckDB memory_limit {run.duckdb_limit}")
    print()

    metrics = run.chain
    name_w = max([len(m) for m in metrics] + [len("metric")])

    # --- runtime share -----------------------------------------------------
    runtime: dict[str, list[float]] = {m: [] for m in metrics}
    for step in run.timesteps:
        for metric, sample in step.samples.items():
            if sample.real_s is not None:
                runtime.setdefault(metric, []).append(sample.real_s)

    grand_total = sum(sum(v) for v in runtime.values()) or 1.0
    print(
        f"  {'metric':<{name_w}}  {'total_s':>12}  {'mean_s':>10}  "
        f"{'median_s':>10}  {'max_s':>10}  {'share':>7}"
    )
    for metric, values in sorted(runtime.items(), key=lambda kv: -sum(kv[1])):
        if not values:
            continue
        total, mean, median, peak = _stats(values)
        print(
            f"  {metric:<{name_w}}  {total:>12.2f}  {mean:>10.2f}  "
            f"{median:>10.2f}  {peak:>10.2f}  {total / grand_total:>6.1%}"
        )
    print()

    # --- own-memory share --------------------------------------------------
    if run.memory_own_available:
        own: dict[str, list[float]] = {}
        for step in run.timesteps:
            for metric, sample in step.samples.items():
                if sample.own_mem_mib is not None:
                    own.setdefault(metric, []).append(sample.own_mem_mib / MIB_PER_GIB)
        peak_of_peaks = max((max(v) for v in own.values() if v), default=0.0) or 1.0
        print(
            f"  {'metric':<{name_w}}  {'own-mem max':>14}  "
            f"{'own-mem median':>16}  {'share of max':>13}"
        )
        for metric, values in sorted(own.items(), key=lambda kv: -max(kv[1])):
            print(
                f"  {metric:<{name_w}}  {max(values):>10.2f} GiB  "
                f"{statistics.median(values):>12.2f} GiB  "
                f"{max(values) / peak_of_peaks:>12.1%}"
            )
        print()

    # --- per-timestep memory ----------------------------------------------
    print("  " + "-" * 90)
    rss = [t.memory_rss_gib for t in run.timesteps if t.memory_rss_gib is not None]
    if rss:
        print(
            f"  memory rss (end current - duckdb):  min {min(rss):.2f}  "
            f"median {statistics.median(rss):.2f}  max {max(rss):.2f} GiB"
        )
    if run.memory_own_available:
        own_totals = [
            t.memory_own_gib for t in run.timesteps if t.memory_own_gib is not None
        ]
        print(
            f"  memory own (sum of pympler totals): min {min(own_totals):.2f}  "
            f"median {statistics.median(own_totals):.2f}  max {max(own_totals):.2f} GiB"
        )
    else:
        print("  memory own: unavailable (run had Memory logging: False)")

    if run.dropped_partial:
        start, end = run.dropped_partial
        print(f"  dropped 1 incomplete timestep ({start} -> {end}, run truncated)")

    for warning in run.warnings[:10]:
        print(f"  WARNING: {warning}")
    if len(run.warnings) > 10:
        print(f"  WARNING: ... and {len(run.warnings) - 10} more")
    print()


# --------------------------------------------------------------------------- #
# CSV output
# --------------------------------------------------------------------------- #

TIMINGS_FIELDS = [
    "run_key",
    "label",
    "chain",
    "timestep_index",
    "start",
    "end",
    "runtime_total_s",
    "runtime_primary_s",
    "iteration_total_s",
    "mem_end_current_gib",
    "mem_end_peak_gib",
    "duckdb_mem_gib",
    "memory_rss_gib",
    "memory_own_gib",
    "memory_own_available",
]

PER_METRIC_FIELDS = [
    "run_key",
    "timestep_index",
    "start",
    "metric",
    "real_s",
    "cpu_s",
    "own_mem_mib",
]

BREAKDOWN_FIELDS = [
    "run_key",
    "timestep_index",
    "start",
    "metric",
    "attr",
    "size_mib",
    "entries",
]


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return None if value is None else round(value, digits)


def write_csvs(
    runs: list[Run],
    labels: dict[str, str],
    out_path: Path,
) -> tuple[Path, Path, Path]:
    per_metric_path = out_path.with_name(f"{out_path.stem}_per_metric.csv")
    breakdown_path = out_path.with_name(f"{out_path.stem}_memory_breakdown.csv")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[tuple[str, int]] = set()

    with out_path.open("w", newline="", encoding="utf-8") as f_main, \
            per_metric_path.open("w", newline="", encoding="utf-8") as f_metric, \
            breakdown_path.open("w", newline="", encoding="utf-8") as f_break:

        main_writer = csv.DictWriter(f_main, fieldnames=TIMINGS_FIELDS)
        metric_writer = csv.DictWriter(f_metric, fieldnames=PER_METRIC_FIELDS)
        break_writer = csv.DictWriter(f_break, fieldnames=BREAKDOWN_FIELDS)
        main_writer.writeheader()
        metric_writer.writeheader()
        break_writer.writeheader()

        for run in runs:
            label = labels[run.key]
            chain = " -> ".join(run.chain)
            for step in run.timesteps:
                key = (run.key, step.index)
                if key in seen:
                    raise SystemExit(f"Duplicate timestep {key} -- parser bug")
                seen.add(key)

                rss = step.memory_rss_gib
                if rss is not None and rss < 0:
                    rss = 0.0

                main_writer.writerow(
                    {
                        "run_key": run.key,
                        "label": label,
                        "chain": chain,
                        "timestep_index": step.index,
                        "start": step.start,
                        "end": step.end,
                        "runtime_total_s": _round(step.runtime_total_s),
                        "runtime_primary_s": _round(step.runtime_primary_s),
                        "iteration_total_s": _round(step.iteration_total_s),
                        "mem_end_current_gib": _round(step.mem_end_current_gib),
                        "mem_end_peak_gib": _round(step.mem_end_peak_gib),
                        "duckdb_mem_gib": _round(step.duckdb_mem_gib),
                        "memory_rss_gib": _round(rss),
                        "memory_own_gib": _round(step.memory_own_gib),
                        "memory_own_available": run.memory_own_available,
                    }
                )

                for metric in step.chain:
                    sample = step.samples[metric]
                    metric_writer.writerow(
                        {
                            "run_key": run.key,
                            "timestep_index": step.index,
                            "start": step.start,
                            "metric": metric,
                            "real_s": _round(sample.real_s),
                            "cpu_s": _round(sample.cpu_s),
                            "own_mem_mib": _round(sample.own_mem_mib),
                        }
                    )
                    for attr, mib, entries in sample.breakdown:
                        break_writer.writerow(
                            {
                                "run_key": run.key,
                                "timestep_index": step.index,
                                "start": step.start,
                                "metric": metric,
                                "attr": attr,
                                "size_mib": round(mib, 4),
                                "entries": entries,
                            }
                        )

    return out_path, per_metric_path, breakdown_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--logs",
        default="../final_logs",
        help="Directory containing the .out log files (default: ../final_logs).",
    )
    parser.add_argument(
        "--select",
        default=None,
        help='Runs to extract: indices, run keys, or "all". Omit to be prompted.',
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Legend name override: --label <run_key_or_metric>=<Pretty Name>. Repeatable.",
    )
    parser.add_argument(
        "--out",
        default="timings.csv",
        help="Main output CSV; the other two are named after it (default: timings.csv).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Only list the runs found, then exit.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; requires --select.",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    log_dir = Path(args.logs).expanduser()
    if not log_dir.is_dir():
        raise SystemExit(f"Not a directory: {log_dir}")

    runs = discover_runs(log_dir)
    print_run_table(runs)

    if args.list:
        return

    selection = args.select
    if selection is None:
        if args.non_interactive:
            raise SystemExit("--non-interactive requires --select")
        try:
            selection = input('Select runs (e.g. "1,4", "all"): ')
        except EOFError:
            raise SystemExit("No selection given")

    selected = resolve_selection(runs, selection)
    overrides = parse_label_overrides(args.label)
    labels = {run.key: label_for(run, overrides) for run in selected}

    for run in selected:
        if not run.timesteps:
            raise SystemExit(f"{run.key}: no complete timesteps parsed")
        print_run_detail(run, labels[run.key])

    seen_labels: dict[str, str] = {}
    for run in selected:
        label = labels[run.key]
        if label in seen_labels:
            print(
                f"WARNING: {run.key} and {seen_labels[label]} both use the legend "
                f'name "{label}". Disambiguate with --label {run.key}=...',
                file=sys.stderr,
            )
        seen_labels[label] = run.key

    main_path, metric_path, break_path = write_csvs(
        selected, labels, Path(args.out).expanduser()
    )
    rows = sum(len(r.timesteps) for r in selected)
    print(f"Wrote {rows} timestep rows for {len(selected)} run(s):")
    print(f"  {main_path}")
    print(f"  {metric_path}")
    print(f"  {break_path}")
    print()
    print("Next: python plot_runtime_memory.py --csv " f"{main_path} --outdir figures")


if __name__ == "__main__":
    main()
