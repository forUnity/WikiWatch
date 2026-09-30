#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import sys
from pathlib import Path
from typing import TypeAlias

from experiment_spec import (
    ExperimentSpec,
    MemoryGroup,
    RuntimeGroup,
    RuntimeInput,
)
from parse_logs import Run, Timestep, parse_run


TimestepKey: TypeAlias = tuple[str, str]

RUNTIME_FIELDS = (
    "group_key",
    "label",
    "timestep_index",
    "start",
    "end",
    "runtime_s",
    "aggregation",
    "source_files",
    "components",
)

MEMORY_FIELDS = (
    "group_key",
    "label",
    "timestep_index",
    "start",
    "end",
    "tracked_metric_gib",
    "tracked_metrics",
    "duckdb_after_load_gib",
    "duckdb_end_gib",
    "duckdb_decomposition_gib",
    "duckdb_measurement_point",
    "process_current_gib",
    "process_peak_gib",
    "non_duckdb_process_gib",
    "source_files",
    "input_count",
    "aggregation",
    "alignment",
)

MIB_PER_GIB = 1024.0


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #


def _combine(
    values: list[float],
    method: str,
) -> float:
    if not values:
        raise ValueError(
            "Cannot combine an empty list"
        )

    if method == "sum":
        return sum(values)

    if method == "mean":
        return (
            sum(values)
            / len(values)
        )

    raise ValueError(
        f"Unknown aggregation method: {method}"
    )


def _combine_optional(
    values: list[float | None],
    method: str,
) -> float | None:
    """
    Combine only when every input was actually measured.

    Missing values must not silently become zero or disappear
    from a sum/mean.
    """

    if (
        not values
        or any(
            value is None
            for value in values
        )
    ):
        return None

    return _combine(
        [
            float(value)
            for value in values
            if value is not None
        ],
        method,
    )


def _combine_peak_optional(
    values: list[float | None],
    method: str,
) -> float | None:
    if (
        not values
        or any(
            value is None
            for value in values
        )
    ):
        return None

    numeric = [
        float(value)
        for value in values
        if value is not None
    ]

    if method == "sum":
        return sum(numeric)

    if method == "mean":
        return (
            sum(numeric)
            / len(numeric)
        )

    if method == "max":
        return max(numeric)

    if method == "none":
        return None

    raise ValueError(
        f"Unknown peak aggregation method: {method}"
    )


def _timestep_key(
    step: Timestep,
) -> TimestepKey:
    return (
        step.start,
        step.end,
    )


def _resolve_path(
    path: str | Path,
    base_dir: Path,
) -> Path:
    resolved = Path(
        path
    ).expanduser()

    if not resolved.is_absolute():
        resolved = (
            base_dir
            / resolved
        )

    return resolved.resolve()


def _finite_or_none(
    value,
) -> float | None:
    if value is None:
        return None

    result = float(value)

    return (
        result
        if math.isfinite(result)
        else None
    )


# --------------------------------------------------------------------------- #
# Spec loading
# --------------------------------------------------------------------------- #


def load_spec(
    path: Path,
) -> ExperimentSpec:
    module_spec = (
        importlib.util.spec_from_file_location(
            "experiment_config",
            path,
        )
    )

    if (
        module_spec is None
        or module_spec.loader is None
    ):
        raise SystemExit(
            f"Could not load spec: {path}"
        )

    module = (
        importlib.util.module_from_spec(
            module_spec
        )
    )

    module_spec.loader.exec_module(
        module
    )

    if not hasattr(
        module,
        "SPEC",
    ):
        raise SystemExit(
            f"{path} does not define "
            "SPEC = ExperimentSpec(...)"
        )

    spec = module.SPEC

    if not isinstance(
        spec,
        ExperimentSpec,
    ):
        raise SystemExit(
            f"{path}: SPEC must be an ExperimentSpec, "
            f"got {type(spec).__name__}"
        )

    return spec


# --------------------------------------------------------------------------- #
# Run cache
# --------------------------------------------------------------------------- #


class RunCache:
    def __init__(
        self,
        base_dir: Path,
    ):
        self.base_dir = (
            base_dir
        )

        self._cache: dict[
            Path,
            Run,
        ] = {}

    def get(
        self,
        path: str | Path,
    ) -> tuple[Path, Run]:
        resolved = (
            _resolve_path(
                path,
                self.base_dir,
            )
        )

        if not resolved.exists():
            raise SystemExit(
                f"No such log file: {resolved}"
            )

        if resolved not in self._cache:
            print(
                f"Parsing {resolved}"
            )

            run = parse_run(
                resolved
            )

            if not run.timesteps:
                print(
                    f"WARNING: no completed timesteps found in "
                    f"{resolved}; this file contributes no rows.",
                    file=sys.stderr,
                )

            self._cache[
                resolved
            ] = run

        return (
            resolved,
            self._cache[
                resolved
            ],
        )


# --------------------------------------------------------------------------- #
# Generic timestep alignment
# --------------------------------------------------------------------------- #


def _aligned_keys(
    maps: list[
        dict[TimestepKey, object]
    ],
    alignment: str,
    kind: str,
    group_key: str,
) -> list[TimestepKey]:
    if not maps:
        return []

    first_keys = list(
        maps[0].keys()
    )

    if len(maps) == 1:
        return first_keys

    sets = [
        set(
            value_map.keys()
        )
        for value_map in maps
    ]

    if alignment == "strict":
        expected = sets[0]

        if any(
            key_set != expected
            for key_set in sets[1:]
        ):
            details: list[
                str
            ] = []

            for (
                index,
                key_set,
            ) in enumerate(sets):
                details.append(
                    f"input {index}: "
                    f"{len(key_set)} timestep(s), "
                    f"missing={len(expected - key_set)}, "
                    f"extra={len(key_set - expected)}"
                )

            raise SystemExit(
                f"{kind.capitalize()} group "
                f"{group_key!r} has mismatched "
                "recorded timesteps.\n"
                "Use alignment='intersection' "
                "to keep only common timesteps.\n  "
                + "\n  ".join(details)
            )

        return first_keys

    if alignment == "intersection":
        common = (
            set.intersection(
                *sets
            )
        )

        return [
            key
            for key in first_keys
            if key in common
        ]

    raise ValueError(
        f"Unknown alignment mode: "
        f"{alignment}"
    )


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


def _extract_runtime_input(
    item: RuntimeInput,
    cache: RunCache,
) -> tuple[
    dict[
        TimestepKey,
        tuple[int, float],
    ],
    Path,
    str,
]:
    """
    Return only genuinely recorded runtime measurements.

    Missing timestep -> no key.
    Missing selected metric -> no key.
    Genuine measured 0.0 -> retained.
    """

    path, run = cache.get(
        item.file
    )

    values: dict[
        TimestepKey,
        tuple[int, float],
    ] = {}

    if item.metrics is not None:
        component_description = (
            f"{item.metric_field}:"
            + "+".join(
                item.metrics
            )
        )

        for step in run.timesteps:
            metric_values: list[
                float
            ] = []

            missing: list[
                str
            ] = []

            for metric_name in item.metrics:
                sample = (
                    step.samples.get(
                        metric_name
                    )
                )

                raw = (
                    None
                    if sample is None
                    else getattr(
                        sample,
                        item.metric_field,
                        None,
                    )
                )

                value = (
                    _finite_or_none(
                        raw
                    )
                )

                if value is None:
                    missing.append(
                        metric_name
                    )
                else:
                    metric_values.append(
                        value
                    )

            if missing:
                print(
                    f"WARNING: skipping runtime timestep "
                    f"{step.start} -> {step.end} "
                    f"in {path.name}: missing "
                    f"{', '.join(missing)}",
                    file=sys.stderr,
                )
                continue

            values[
                _timestep_key(
                    step
                )
            ] = (
                step.index,
                _combine(
                    metric_values,
                    item.metric_combine,
                ),
            )

        return (
            values,
            path,
            component_description,
        )

    component_description = (
        item.run_value
    )

    for step in run.timesteps:
        value = (
            _finite_or_none(
                getattr(
                    step,
                    item.run_value,
                    None,
                )
            )
        )

        if value is None:
            print(
                f"WARNING: skipping runtime timestep "
                f"{step.start} -> {step.end} "
                f"in {path.name}: "
                f"{item.run_value} was not recorded",
                file=sys.stderr,
            )
            continue

        values[
            _timestep_key(
                step
            )
        ] = (
            step.index,
            value,
        )

    return (
        values,
        path,
        component_description,
    )


def build_runtime_rows(
    spec: ExperimentSpec,
    cache: RunCache,
) -> list[dict]:
    rows: list[
        dict
    ] = []

    for group in spec.runtime_groups:
        extracted = [
            _extract_runtime_input(
                item,
                cache,
            )
            for item in group.inputs
        ]

        maps = [
            result[0]
            for result in extracted
        ]

        paths = [
            result[1]
            for result in extracted
        ]

        components = [
            result[2]
            for result in extracted
        ]

        keys = _aligned_keys(
            maps=maps,
            alignment=(
                group.alignment
            ),
            kind="runtime",
            group_key=(
                group.key
            ),
        )

        if group.exclude_last_timesteps > 0:
            n = group.exclude_last_timesteps

            if n > len(keys):
                raise ValueError(
                    f"Runtime group {group.key!r} wants to exclude "
                    f"{n} final timesteps, but only {len(keys)} are available."
                )

            removed_keys = keys[-n:]

            print(
                f"Excluding {n} final runtime timestep(s) "
                f"for {group.key!r}:"
            )

            for removed_key in removed_keys:
                print(
                    f"  {removed_key[0]} -> {removed_key[1]}"
                )

            keys = keys[:-n]

        if not keys:
            print(
                f"WARNING: runtime group "
                f"{group.key!r} has no matching "
                "recorded timesteps; writing no rows.",
                file=sys.stderr,
            )
            continue

        for key in keys:
            observed = [
                value_map[key]
                for value_map in maps
            ]

            start, end = key

            rows.append(
                {
                    "group_key":
                        group.key,

                    "label":
                        group.label,

                    "timestep_index":
                        observed[0][0],

                    "start":
                        start,

                    "end":
                        end,

                    "runtime_s":
                        _combine(
                            [
                                value
                                for _index, value
                                in observed
                            ],
                            group.combine,
                        ),

                    "aggregation":
                        group.combine,

                    "source_files":
                        "|".join(
                            str(path)
                            for path in paths
                        ),

                    "components":
                        "|".join(
                            components
                        ),
                }
            )

    return rows


# --------------------------------------------------------------------------- #
# Per-file memory profile extraction
# --------------------------------------------------------------------------- #


def _tracked_metric_gib(
    step: Timestep,
    group: MemoryGroup,
    path: Path,
) -> tuple[
    float | None,
    str,
]:
    """
    Extract Pympler-tracked structures for one input file/timestep.
    """

    if group.tracked_metrics is None:
        available = [
            (
                sample.metric,
                float(
                    sample.own_mem_mib
                ),
            )
            for sample
            in step.samples.values()
            if sample.own_mem_mib
            is not None
        ]

        if not available:
            return (
                None,
                "",
            )

        return (
            _combine(
                [
                    value
                    for _metric, value
                    in available
                ],
                group.tracked_combine,
            )
            / MIB_PER_GIB,

            "+".join(
                metric
                for metric, _value
                in available
            ),
        )

    values: list[
        float
    ] = []

    missing: list[
        str
    ] = []

    for metric in group.tracked_metrics:
        sample = (
            step.samples.get(
                metric
            )
        )

        if (
            sample is None
            or sample.own_mem_mib
            is None
        ):
            missing.append(
                metric
            )
        else:
            values.append(
                float(
                    sample.own_mem_mib
                )
            )

    if missing:
        print(
            f"WARNING: {path.name} "
            f"timestep {step.start}: "
            "tracked metric snapshot "
            f"missing for {', '.join(missing)}; "
            "tracked_metric_gib left blank.",
            file=sys.stderr,
        )

        return (
            None,
            "+".join(
                group.tracked_metrics
            ),
        )

    return (
        _combine(
            values,
            group.tracked_combine,
        )
        / MIB_PER_GIB,

        "+".join(
            group.tracked_metrics
        ),
    )


def _memory_profile(
    step: Timestep,
    group: MemoryGroup,
    path: Path,
) -> dict:
    tracked_gib, tracked_names = (
        _tracked_metric_gib(
            step,
            group,
            path,
        )
    )

    # New parse_logs.py field, with fallback
    # to old parser objects/logs.
    duckdb_after_load = (
        _finite_or_none(
            getattr(
                step,
                "duckdb_after_load_gib",
                None,
            )
        )
    )

    if duckdb_after_load is None:
        duckdb_after_load = (
            _finite_or_none(
                getattr(
                    step,
                    "duckdb_mem_gib",
                    None,
                )
            )
        )

    duckdb_end = (
        _finite_or_none(
            getattr(
                step,
                "duckdb_end_gib",
                None,
            )
        )
    )

    if duckdb_end is not None:
        duckdb_for_decomposition = (
            duckdb_end
        )

        duckdb_measurement_point = (
            "end_timestep"
        )

    elif duckdb_after_load is not None:
        duckdb_for_decomposition = (
            duckdb_after_load
        )

        duckdb_measurement_point = (
            "after_load"
        )

    else:
        duckdb_for_decomposition = (
            None
        )

        duckdb_measurement_point = ""

    process_current = (
        _finite_or_none(
            getattr(
                step,
                "mem_end_current_gib",
                None,
            )
        )
    )

    process_peak = (
        _finite_or_none(
            getattr(
                step,
                "mem_end_peak_gib",
                None,
            )
        )
    )

    non_duckdb = None

    if (
        process_current is not None
        and duckdb_for_decomposition
        is not None
    ):
        candidate = (
            process_current
            - duckdb_for_decomposition
        )

        if candidate < 0:
            print(
                f"WARNING: {path.name} "
                f"timestep {step.start}: "
                f"DuckDB memory "
                f"({duckdb_for_decomposition:.3f} GiB) "
                f"exceeds end process memory "
                f"({process_current:.3f} GiB); "
                "non_duckdb_process_gib left blank.",
                file=sys.stderr,
            )

        else:
            non_duckdb = (
                candidate
            )

    return {
        "tracked_metric_gib":
            tracked_gib,

        "tracked_metrics":
            tracked_names,

        "duckdb_after_load_gib":
            duckdb_after_load,

        "duckdb_end_gib":
            duckdb_end,

        "duckdb_decomposition_gib":
            duckdb_for_decomposition,

        "duckdb_measurement_point":
            duckdb_measurement_point,

        "process_current_gib":
            process_current,

        "process_peak_gib":
            process_peak,

        "non_duckdb_process_gib":
            non_duckdb,
    }


def _extract_memory_input(
    file: str | Path,
    group: MemoryGroup,
    cache: RunCache,
) -> tuple[
    dict[
        TimestepKey,
        tuple[int, dict],
    ],
    Path,
]:
    path, run = cache.get(
        file
    )

    profiles: dict[
        TimestepKey,
        tuple[int, dict],
    ] = {}

    for step in run.timesteps:
        profiles[
            _timestep_key(
                step
            )
        ] = (
            step.index,
            _memory_profile(
                step,
                group,
                path,
            ),
        )

    return (
        profiles,
        path,
    )


# --------------------------------------------------------------------------- #
# Multi-file memory aggregation
# --------------------------------------------------------------------------- #


def _combine_tracked_names(
    profiles: list[dict],
) -> str:
    names: list[
        str
    ] = []

    for profile in profiles:
        raw = str(
            profile.get(
                "tracked_metrics",
                "",
            )
            or ""
        )

        if not raw:
            continue

        for name in raw.split("+"):
            if (
                name
                and name not in names
            ):
                names.append(
                    name
                )

    return "+".join(
        names
    )


def _combined_duckdb_measurement_point(
    profiles: list[dict],
    combined_duckdb: float | None,
) -> str:
    if combined_duckdb is None:
        return ""

    points = [
        str(
            profile.get(
                "duckdb_measurement_point",
                "",
            )
            or ""
        )
        for profile in profiles
    ]

    if any(
        not point
        for point in points
    ):
        return "mixed"

    unique = set(
        points
    )

    if len(unique) == 1:
        return points[0]

    return "mixed"


def _combine_memory_profiles(
    profiles: list[dict],
    group: MemoryGroup,
    key: TimestepKey,
) -> dict:
    """
    Combine aligned per-file profiles into one logical memory profile.

    Missing values are strict:
    if one file lacks a measurement, the combined value is blank
    rather than undercounting by silently ignoring it.
    """

    tracked_metric = (
        _combine_optional(
            [
                profile[
                    "tracked_metric_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    duckdb_after_load = (
        _combine_optional(
            [
                profile[
                    "duckdb_after_load_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    duckdb_end = (
        _combine_optional(
            [
                profile[
                    "duckdb_end_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    duckdb_decomposition = (
        _combine_optional(
            [
                profile[
                    "duckdb_decomposition_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    process_current = (
        _combine_optional(
            [
                profile[
                    "process_current_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    non_duckdb = (
        _combine_optional(
            [
                profile[
                    "non_duckdb_process_gib"
                ]
                for profile in profiles
            ],
            group.combine,
        )
    )

    if len(profiles) == 1:
        # Preserve the actual observed peak
        # for ordinary one-file groups.
        process_peak = (
            profiles[0][
                "process_peak_gib"
            ]
        )

    elif group.peak_combine == "none":
        # Independent peaks need not have occurred
        # at the same moment.
        process_peak = None

    else:
        process_peak = (
            _combine_peak_optional(
                [
                    profile[
                        "process_peak_gib"
                    ]
                    for profile in profiles
                ],
                group.peak_combine,
            )
        )

    measurement_point = (
        _combined_duckdb_measurement_point(
            profiles,
            duckdb_decomposition,
        )
    )

    # Sum and mean are linear, so this should remain true:
    #
    # combined process - combined DuckDB
    # =
    # combined non-DuckDB
    if (
        process_current is not None
        and duckdb_decomposition
        is not None
        and non_duckdb
        is not None
    ):
        expected = (
            process_current
            - duckdb_decomposition
        )

        if not math.isclose(
            expected,
            non_duckdb,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            start, end = key

            print(
                f"WARNING: memory group "
                f"{group.key!r} timestep "
                f"{start} -> {end}: "
                "combined non-DuckDB memory "
                f"({non_duckdb:.6f} GiB) "
                "does not match combined process "
                "minus DuckDB "
                f"({expected:.6f} GiB).",
                file=sys.stderr,
            )

    return {
        "tracked_metric_gib":
            tracked_metric,

        "tracked_metrics":
            _combine_tracked_names(
                profiles
            ),

        "duckdb_after_load_gib":
            duckdb_after_load,

        "duckdb_end_gib":
            duckdb_end,

        "duckdb_decomposition_gib":
            duckdb_decomposition,

        "duckdb_measurement_point":
            measurement_point,

        "process_current_gib":
            process_current,

        "process_peak_gib":
            process_peak,

        "non_duckdb_process_gib":
            non_duckdb,
    }


def build_memory_rows(
    spec: ExperimentSpec,
    cache: RunCache,
) -> list[dict]:
    """
    Build one logical memory profile per group/timestep.

    Each MemoryGroup may contain one or more source files.

    Files are aligned by (start, end) and combined according
    to the MemoryGroup settings.

    Missing timestep:
        strict -> error if files disagree
        intersection -> only common timesteps survive

    Missing measurement:
        row remains, combined measurement is blank

    Genuine measured zero:
        remains zero
    """

    rows: list[
        dict
    ] = []

    for group in spec.memory_groups:
        extracted = [
            _extract_memory_input(
                file,
                group,
                cache,
            )
            for file in group.files
        ]

        maps = [
            result[0]
            for result in extracted
        ]

        paths = [
            result[1]
            for result in extracted
        ]

        # Protect against accidentally summing the
        # exact same logfile twice.
        if (
            len(set(paths))
            != len(paths)
        ):
            duplicates = sorted(
                {
                    str(path)
                    for path in paths
                    if paths.count(path) > 1
                }
            )

            raise SystemExit(
                f"Memory group {group.key!r} "
                "contains the same log file "
                "more than once: "
                + ", ".join(
                    duplicates
                )
            )

        keys = _aligned_keys(
            maps=maps,
            alignment=(
                group.alignment
            ),
            kind="memory",
            group_key=(
                group.key
            ),
        )

        if not keys:
            print(
                f"WARNING: memory group "
                f"{group.key!r} has no matching "
                "completed timesteps; writing no rows.",
                file=sys.stderr,
            )
            continue

        for key in keys:
            observed = [
                value_map[key]
                for value_map in maps
            ]

            source_profiles = [
                profile
                for _index, profile
                in observed
            ]

            combined = (
                _combine_memory_profiles(
                    source_profiles,
                    group,
                    key,
                )
            )

            start, end = key

            rows.append(
                {
                    "group_key":
                        group.key,

                    "label":
                        group.label,

                    "timestep_index":
                        observed[0][0],

                    "start":
                        start,

                    "end":
                        end,

                    **combined,

                    "source_files":
                        "|".join(
                            str(path)
                            for path in paths
                        ),

                    "input_count":
                        len(paths),

                    "aggregation":
                        group.combine,

                    "alignment":
                        group.alignment,
                }
            )

    return rows


# --------------------------------------------------------------------------- #
# Validation / CSV
# --------------------------------------------------------------------------- #


def _check_unique_group_keys(
    groups,
    kind: str,
) -> None:
    seen: set[
        str
    ] = set()

    duplicates: set[
        str
    ] = set()

    for group in groups:
        if group.key in seen:
            duplicates.add(
                group.key
            )

        seen.add(
            group.key
        )

    if duplicates:
        raise SystemExit(
            f"Duplicate {kind} group key(s): "
            + ", ".join(
                sorted(
                    duplicates
                )
            )
        )


def validate_spec(
    spec: ExperimentSpec,
) -> None:
    _check_unique_group_keys(
        spec.runtime_groups,
        "runtime",
    )

    _check_unique_group_keys(
        spec.memory_groups,
        "memory",
    )

    if (
        not spec.runtime_groups
        and not spec.memory_groups
    ):
        raise SystemExit(
            "Experiment spec contains no "
            "runtime_groups or memory_groups."
        )


def write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: list[dict],
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
        )

        writer.writeheader()
        writer.writerows(
            rows
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Parse runtime and memory profiles "
            "from SLURM logs."
        )
    )

    parser.add_argument(
        "--spec",
        required=True,
    )

    parser.add_argument(
        "--outdir",
        default="timings",
    )

    parser.add_argument(
        "--prefix",
        default="experiment",
    )

    return parser


def main() -> None:
    args = (
        build_arg_parser()
        .parse_args()
    )

    spec_path = (
        Path(
            args.spec
        )
        .expanduser()
        .resolve()
    )

    if not spec_path.is_file():
        raise SystemExit(
            f"No such spec: {spec_path}"
        )

    spec = load_spec(
        spec_path
    )

    validate_spec(
        spec
    )

    cache = RunCache(
        spec_path.parent
    )

    runtime_rows = (
        build_runtime_rows(
            spec,
            cache,
        )
    )

    memory_rows = (
        build_memory_rows(
            spec,
            cache,
        )
    )

    outdir = (
        Path(
            args.outdir
        )
        .expanduser()
    )

    runtime_path = (
        outdir
        / f"{args.prefix}_runtime.csv"
    )

    memory_path = (
        outdir
        / f"{args.prefix}_memory.csv"
    )

    write_csv(
        runtime_path,
        RUNTIME_FIELDS,
        runtime_rows,
    )

    write_csv(
        memory_path,
        MEMORY_FIELDS,
        memory_rows,
    )

    print()
    print("Finished.")

    print(
        f"  runtime: {runtime_path} "
        f"({len(runtime_rows)} rows)"
    )

    print(
        f"  memory:  {memory_path} "
        f"({len(memory_rows)} rows)"
    )


if __name__ == "__main__":
    main()