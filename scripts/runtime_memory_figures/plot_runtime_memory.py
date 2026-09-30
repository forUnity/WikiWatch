#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import matplotlib
import matplotlib.ticker as mticker

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from experiment_spec import (
    ExperimentSpec,
    FigurePlotSpec,
    MemoryPlotSpec,
    MemorySeriesSpec,
    RuntimePlotSpec,
)


# --------------------------------------------------------------------------- #
# Style
# --------------------------------------------------------------------------- #

COLORBLIND_PALETTE = (
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
    "#56B4E9",
    "#000000",
)

LINESTYLES = (
    "-",
    "--",
    "-.",
    ":",
)

UNDERLAY_GREYS = (
    "#D9D9D9",
    "#C8C8C8",
    "#B5B5B5",
    "#A3A3A3",
    "#919191",
    "#7F7F7F",
)

VERTICAL_GUIDE_COLOR = "#656565"

_BASE_RC_PARAMS = {
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 10,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "axes.spines.top": False,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}

_FONT_SIZE_KEYS = (
    "font.size",
    "axes.labelsize",
    "axes.titlesize",
    "xtick.labelsize",
    "ytick.labelsize",
    "legend.fontsize",
)

MAX_STYLE_SLOTS = (
    len(COLORBLIND_PALETTE)
    * len(LINESTYLES)
)


# --------------------------------------------------------------------------- #
# Runtime display units
# --------------------------------------------------------------------------- #

RUNTIME_UNITS = {
    "seconds": {
        "divisor": 1.0,
        "suffix": "s",
    },
    "minutes": {
        "divisor": 60.0,
        "suffix": "min",
    },
    "hours": {
        "divisor": 3600.0,
        "suffix": "h",
    },
}


def convert_runtime_values(
    values: pd.Series,
    unit: str,
) -> pd.Series:
    """
    Convert runtime_s into the unit used only for plotting.

    The CSV itself always remains in seconds.
    """

    config = RUNTIME_UNITS.get(
        unit
    )

    if config is None:
        raise ValueError(
            f"Unknown runtime unit: {unit}"
        )

    return (
        values
        / config["divisor"]
    )


def runtime_axis_label(
    spec: RuntimePlotSpec,
) -> str:
    """
    Use the explicit y_label when supplied.

    Otherwise generate:
        Runtime per timestep (s)
        Runtime per timestep (min)
        Runtime per timestep (h)
    """

    if spec.y_label is not None:
        return spec.y_label

    config = RUNTIME_UNITS[
        spec.unit
    ]

    return (
        "Runtime per timestep "
        f"({config['suffix']})"
    )


# --------------------------------------------------------------------------- #
# Spec loading
# --------------------------------------------------------------------------- #


def load_experiment_spec(
    path: Path,
) -> ExperimentSpec:
    module_spec = (
        importlib.util.spec_from_file_location(
            "runtime_memory_experiment_spec",
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

    experiment = module.SPEC

    if not isinstance(
        experiment,
        ExperimentSpec,
    ):
        raise SystemExit(
            f"{path}: SPEC must be an ExperimentSpec, "
            f"got {type(experiment).__name__}"
        )

    return experiment


# --------------------------------------------------------------------------- #
# RC style
# --------------------------------------------------------------------------- #


def rc_params(
    font_scale: float,
) -> dict:
    params = dict(
        _BASE_RC_PARAMS
    )

    for key in _FONT_SIZE_KEYS:
        params[key] *= font_scale

    return params


# --------------------------------------------------------------------------- #
# Persistent styles
# --------------------------------------------------------------------------- #


def style_for_slot(
    slot: int,
) -> tuple[str, str]:
    color = COLORBLIND_PALETTE[
        slot
        % len(COLORBLIND_PALETTE)
    ]

    linestyle = LINESTYLES[
        slot
        % len(LINESTYLES)
    ]

    return (
        color,
        linestyle,
    )


def assign_slots(
    group_keys: list[str],
    store_path: Path,
    order: list[str] | None,
    reset: bool,
) -> dict[str, int]:
    stored: dict[
        str,
        int,
    ] = {}

    if (
        store_path.exists()
        and not reset
    ):
        try:
            stored = {
                key: int(value)
                for key, value
                in json.loads(
                    store_path.read_text()
                ).items()
            }

        except (
            ValueError,
            OSError,
        ) as exc:
            print(
                f"WARNING: ignoring unreadable "
                f"{store_path}: {exc}",
                file=sys.stderr,
            )

    invalid = [
        key
        for key, slot
        in stored.items()
        if (
            slot < 0
            or slot >= MAX_STYLE_SLOTS
        )
    ]

    for key in invalid:
        del stored[key]

    if order:
        unknown = [
            key
            for key in order
            if key not in group_keys
        ]

        if unknown:
            raise SystemExit(
                "--order contains unknown groups: "
                + ", ".join(unknown)
            )

        stored = {
            key: index
            for index, key
            in enumerate(order)
        }

    taken = set(
        stored.values()
    )

    for key in group_keys:
        if key in stored:
            continue

        slot = next(
            (
                index
                for index
                in range(MAX_STYLE_SLOTS)
                if index not in taken
            ),
            None,
        )

        if slot is None:
            raise SystemExit(
                "No visual style slots remaining."
            )

        stored[key] = slot
        taken.add(slot)

    store_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    store_path.write_text(
        json.dumps(
            stored,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    return stored


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def load_frame(
    path: Path,
    value_column: str,
    select: list[str] | None,
    timezone: str,
) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(
            f"No such CSV: {path}"
        )

    frame = pd.read_csv(
        path
    )

    required = {
        "group_key",
        "label",
        "start",
        value_column,
    }

    missing = (
        required
        - set(frame.columns)
    )

    if missing:
        raise SystemExit(
            f"{path} is missing required columns: "
            + ", ".join(
                sorted(missing)
            )
        )

    if select:
        frame = frame[
            frame[
                "group_key"
            ].isin(select)
        ]

    frame = frame.copy()

    frame["timestamp"] = (
        pd.to_datetime(
            frame["start"],
            utc=True,
        )
        .dt.tz_convert(
            timezone
        )
    )

    return frame.sort_values(
        [
            "group_key",
            "timestamp",
        ]
    )


# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #


def apply_x_ticks(
    axis: plt.Axes,
    spec: FigurePlotSpec,
) -> None:
    if (
        spec.year_tick_interval
        is not None
    ):
        locator = (
            mdates.YearLocator(
                base=spec.year_tick_interval
            )
        )

        axis.xaxis.set_major_locator(
            locator
        )

        axis.xaxis.set_major_formatter(
            mdates.DateFormatter(
                "%Y"
            )
        )

    else:
        locator = (
            mdates.AutoDateLocator(
                minticks=4,
                maxticks=9,
            )
        )

        axis.xaxis.set_major_locator(
            locator
        )

        axis.xaxis.set_major_formatter(
            mdates.ConciseDateFormatter(
                locator
            )
        )


def apply_x_limits(
    axis: plt.Axes,
    spec: FigurePlotSpec,
) -> None:
    if spec.x_limits is None:
        return

    start = pd.Timestamp(
        spec.x_limits[0]
    )

    end = pd.Timestamp(
        spec.x_limits[1]
    )

    if start.tzinfo is None:
        start = start.tz_localize(
            spec.timezone
        )
    else:
        start = start.tz_convert(
            spec.timezone
        )

    if end.tzinfo is None:
        end = end.tz_localize(
            spec.timezone
        )
    else:
        end = end.tz_convert(
            spec.timezone
        )

    axis.set_xlim(
        start,
        end,
    )


def draw_vertical_guides(
    axis: plt.Axes,
    frame: pd.DataFrame,
    spec: FigurePlotSpec,
) -> None:
    x_min, x_max = (
        axis.get_xlim()
    )

    if spec.show_year_lines:
        locator = (
            mdates.YearLocator(
                base=(
                    spec.year_tick_interval
                    or 1
                )
            )
        )

        tick_values = (
            locator.tick_values(
                mdates.num2date(
                    x_min
                ),
                mdates.num2date(
                    x_max
                ),
            )
        )

        for x in tick_values:
            if (
                x < x_min
                or x > x_max
            ):
                continue

            axis.axvline(
                x=x,
                color=VERTICAL_GUIDE_COLOR,
                linewidth=0.7,
                alpha=spec.year_line_alpha,
                zorder=0.4,
            )

    if spec.show_datapoint_lines:
        timestamps = (
            frame["timestamp"]
            .dropna()
            .drop_duplicates()
            .sort_values()
        )

        for timestamp in timestamps:
            axis.axvline(
                x=timestamp,
                color=VERTICAL_GUIDE_COLOR,
                linewidth=0.45,
                alpha=(
                    spec.datapoint_line_alpha
                ),
                zorder=0.3,
            )

    axis.set_xlim(
        x_min,
        x_max,
    )


def skip_alternate_x_labels(
    axis: plt.Axes,
) -> None:
    x_min, x_max = (
        axis.get_xlim()
    )

    ticks = [
        tick
        for tick in axis.get_xticks()
        if x_min <= tick <= x_max
    ]

    labels = (
        axis.xaxis
        .get_major_formatter()
        .format_ticks(ticks)
    )

    axis.set_xticks(
        ticks,
        [
            (
                label
                if index % 2 == 0
                else ""
            )
            for index, label
            in enumerate(labels)
        ],
    )


def draw_legend(
    axis: plt.Axes,
    spec: FigurePlotSpec,
) -> None:
    if not spec.show_legend:
        return

    handles, labels = (
        axis.get_legend_handles_labels()
    )

    if not handles:
        return

    kwargs = {
        "loc": (
            spec.legend_location
        ),
        "frameon": False,
        "ncol": (
            spec.legend_ncol
        ),
    }

    if (
        spec.legend_bbox
        is not None
    ):
        kwargs[
            "bbox_to_anchor"
        ] = spec.legend_bbox

    axis.legend(
        handles,
        labels,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


def draw_runtime_figure(
    frame: pd.DataFrame,
    slots: dict[str, int],
    spec: RuntimePlotSpec,
) -> plt.Figure:
    with plt.rc_context(
        rc_params(
            spec.font_scale
        )
    ):
        fig, axis = plt.subplots(
            figsize=spec.figsize,
            constrained_layout=True,
        )

        for (
            group_key,
            group,
        ) in frame.groupby(
            "group_key",
            sort=False,
        ):
            group = (
                group.sort_values(
                    "timestamp"
                )
            )

            raw_values = (
                pd.to_numeric(
                    group[
                        "runtime_s"
                    ],
                    errors="coerce",
                )
            )

            values = (
                convert_runtime_values(
                    raw_values,
                    spec.unit,
                )
            )

            valid = (
                values.notna()
            )

            if not valid.any():
                continue

            color, linestyle = (
                style_for_slot(
                    slots[
                        group_key
                    ]
                )
            )

            axis.plot(
                group.loc[
                    valid,
                    "timestamp",
                ],
                values.loc[
                    valid
                ],
                color=color,
                linestyle=linestyle,
                linewidth=spec.linewidth,
                label=(
                    group[
                        "label"
                    ].iloc[0]
                ),
            )

        _finish_figure(
            axis=axis,
            frame=frame,
            spec=spec,
            y_label_override=(
                runtime_axis_label(
                    spec
                )
            ),
        )

        return fig


# --------------------------------------------------------------------------- #
# Memory labels
# --------------------------------------------------------------------------- #


def pretty_memory_name(
    value: str,
) -> str:
    return {
        "tracked_metric_gib":
            "Tracked metric structures",

        "duckdb_after_load_gib":
            "DuckDB memory after load",

        "duckdb_end_gib":
            "DuckDB memory at end of timestep",

        "duckdb_decomposition_gib":
            "DuckDB memory",

        "process_current_gib":
            "Process memory",

        "process_peak_gib":
            "Peak process memory",

        "non_duckdb_process_gib":
            "Non-DuckDB process memory",

    }.get(
        value,
        value,
    )


def memory_series_applies(
    series: MemorySeriesSpec,
    group_key: str,
) -> bool:
    return (
        series.groups is None
        or group_key in series.groups
    )


def make_memory_label(
    group_label: str,
    series: MemorySeriesSpec,
    multiple_groups: bool,
) -> str:
    if series.label is not None:
        component_label = (
            series.label
        )

    elif series.value in (
        "process_current_gib",
        "non_duckdb_process_gib",
    ):
        component_label = None

    else:
        component_label = (
            pretty_memory_name(
                series.value
            )
        )

    if component_label is None:
        return group_label

    if not series.include_group_in_label:
        return component_label

    if multiple_groups:
        return (
            f"{group_label} — "
            f"{component_label}"
        )

    return component_label


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #


def validate_memory_columns(
    frame: pd.DataFrame,
    spec: MemoryPlotSpec,
) -> None:
    requested = {
        series.value
        for series in spec.series
    }

    missing = (
        requested
        - set(frame.columns)
    )

    if missing:
        raise SystemExit(
            "Memory CSV is missing columns: "
            + ", ".join(
                sorted(missing)
            )
        )


def draw_memory_figure(
    frame: pd.DataFrame,
    slots: dict[str, int],
    spec: MemoryPlotSpec,
) -> plt.Figure:
    validate_memory_columns(
        frame,
        spec,
    )

    unique_groups = list(
        dict.fromkeys(
            frame[
                "group_key"
            ]
        )
    )

    multiple_groups = (
        len(unique_groups) > 1
    )

    grey_for_group = {
        group_key: UNDERLAY_GREYS[
            index
            % len(UNDERLAY_GREYS)
        ]
        for index, group_key
        in enumerate(unique_groups)
    }

    with plt.rc_context(
        rc_params(
            spec.font_scale
        )
    ):
        fig, axis = plt.subplots(
            figsize=spec.figsize,
            constrained_layout=True,
        )

        # --------------------------------------------------------------- #
        # Underlays
        # --------------------------------------------------------------- #

        for series in spec.series:
            if series.mode != "underlay":
                continue

            for (
                group_key,
                group,
            ) in frame.groupby(
                "group_key",
                sort=False,
            ):
                if not memory_series_applies(
                    series,
                    group_key,
                ):
                    continue

                group = (
                    group.sort_values(
                        "timestamp"
                    )
                )

                values = (
                    pd.to_numeric(
                        group[
                            series.value
                        ],
                        errors="coerce",
                    )
                )

                valid = (
                    values.notna()
                )

                if not valid.any():
                    continue

                color = (
                    series.color
                    or grey_for_group[
                        group_key
                    ]
                )

                label = None

                if series.show_in_legend:
                    label = (
                        make_memory_label(
                            group_label=(
                                group[
                                    "label"
                                ].iloc[0]
                            ),
                            series=series,
                            multiple_groups=(
                                multiple_groups
                            ),
                        )
                    )

                axis.fill_between(
                    group.loc[
                        valid,
                        "timestamp",
                    ],
                    0,
                    values.loc[
                        valid
                    ],
                    facecolor=color,
                    alpha=(
                        series.fill_alpha
                    ),
                    linewidth=0,
                    label=label,
                    zorder=0.5,
                )

        # --------------------------------------------------------------- #
        # Lines
        # --------------------------------------------------------------- #

        for series in spec.series:
            if series.mode != "line":
                continue

            for (
                group_key,
                group,
            ) in frame.groupby(
                "group_key",
                sort=False,
            ):
                if not memory_series_applies(
                    series,
                    group_key,
                ):
                    continue

                group = (
                    group.sort_values(
                        "timestamp"
                    )
                )

                values = (
                    pd.to_numeric(
                        group[
                            series.value
                        ],
                        errors="coerce",
                    )
                )

                valid = (
                    values.notna()
                )

                if not valid.any():
                    continue

                default_color, default_linestyle = (
                    style_for_slot(
                        slots[
                            group_key
                        ]
                    )
                )

                color = (
                    series.color
                    or default_color
                )

                linestyle = (
                    series.linestyle
                    or default_linestyle
                )

                linewidth = (
                    series.linewidth
                    if series.linewidth
                    is not None
                    else spec.linewidth
                )

                label = None

                if series.show_in_legend:
                    label = (
                        make_memory_label(
                            group_label=(
                                group[
                                    "label"
                                ].iloc[0]
                            ),
                            series=series,
                            multiple_groups=(
                                multiple_groups
                            ),
                        )
                    )

                axis.plot(
                    group.loc[
                        valid,
                        "timestamp",
                    ],
                    values.loc[
                        valid
                    ],
                    color=color,
                    linestyle=linestyle,
                    linewidth=linewidth,
                    alpha=series.alpha,
                    label=label,
                    zorder=2,
                )

        _finish_figure(
            axis=axis,
            frame=frame,
            spec=spec,
        )

        return fig


# --------------------------------------------------------------------------- #
# Common figure finishing
# --------------------------------------------------------------------------- #


def _finish_figure(
    axis: plt.Axes,
    frame: pd.DataFrame,
    spec: FigurePlotSpec,
    y_label_override: str | None = None,
) -> None:
    if spec.title:
        axis.set_title(
            spec.title
        )

    if spec.x_label:
        axis.set_xlabel(
            spec.x_label
        )

    y_label = (
        y_label_override
        if y_label_override is not None
        else spec.y_label
    )

    if y_label:
        axis.set_ylabel(
            y_label
        )

    axis.set_yscale(
        spec.y_scale
    )

    if spec.y_scale == "log":
        axis.yaxis.set_major_formatter(
            mticker.FuncFormatter(
                lambda value, _pos: f"{value:g}"
            )
        )

    apply_x_ticks(
        axis,
        spec,
    )

    axis.margins(
        x=0.01
    )

    if spec.show_grid:
        axis.grid(
            axis="y",
            alpha=0.22,
            linewidth=0.7,
        )

        axis.grid(
            axis="x",
            visible=False,
        )

    apply_x_limits(
        axis,
        spec,
    )

    draw_vertical_guides(
        axis,
        frame,
        spec,
    )

    if spec.skip_x_ticks:
        skip_alternate_x_labels(
            axis
        )

    if spec.y_limits is not None:
        axis.set_ylim(
            *spec.y_limits
        )

    elif spec.y_scale == "linear":
        axis.set_ylim(
            bottom=0
        )

    draw_legend(
        axis,
        spec,
    )


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #


def save_figure(
    fig: plt.Figure,
    outdir: Path,
    spec: FigurePlotSpec,
    override_formats: tuple[
        str,
        ...,
    ] | None,
) -> list[Path]:
    formats = (
        override_formats
        if override_formats is not None
        else spec.formats
    )

    written: list[
        Path
    ] = []

    for fmt in formats:
        path = (
            outdir
            / f"{spec.output_stem}.{fmt}"
        )

        fig.savefig(
            path,
            bbox_inches="tight",
        )

        written.append(
            path
        )

    plt.close(
        fig
    )

    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--spec",
        required=True,
    )

    parser.add_argument(
        "--runtime-csv",
        default=(
            "timings/"
            "experiment_runtime.csv"
        ),
    )

    parser.add_argument(
        "--memory-csv",
        default=(
            "timings/"
            "experiment_memory.csv"
        ),
    )

    parser.add_argument(
        "--outdir",
        default="figures",
    )

    parser.add_argument(
        "--select",
        nargs="*",
        default=None,
    )

    parser.add_argument(
        "--order",
        nargs="*",
        default=None,
    )

    parser.add_argument(
        "--reset-colors",
        action="store_true",
    )

    parser.add_argument(
        "--only",
        choices=(
            "runtime",
            "memory",
        ),
        default=None,
    )

    parser.add_argument(
        "--format",
        nargs="+",
        default=None,
    )

    return parser


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


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

    experiment = (
        load_experiment_spec(
            spec_path
        )
    )

    runtime_spec = (
        experiment.plot.runtime
    )

    memory_specs = (
        experiment.plot.memory_figures
    )

    runtime_frame = None
    memory_frame = None

    if args.only != "memory":
        runtime_frame = load_frame(
            Path(
                args.runtime_csv
            ),
            "runtime_s",
            args.select,
            runtime_spec.timezone,
        )

    if args.only != "runtime":
        memory_frame = load_frame(
            Path(
                args.memory_csv
            ),
            "process_current_gib",
            args.select,
            memory_specs[
                0
            ].timezone,
        )

    group_keys: list[
        str
    ] = []

    for frame in (
        runtime_frame,
        memory_frame,
    ):
        if frame is None:
            continue

        for key in frame[
            "group_key"
        ]:
            if key not in group_keys:
                group_keys.append(
                    key
                )

    if not group_keys:
        raise SystemExit(
            "Nothing selected"
        )

    outdir = (
        Path(
            args.outdir
        )
        .expanduser()
    )

    outdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    slots = assign_slots(
        group_keys,
        outdir
        / "color_assignments.json",
        args.order,
        args.reset_colors,
    )

    override_formats = (
        tuple(
            args.format
        )
        if args.format
        else None
    )

    written: list[
        Path
    ] = []

    # Runtime
    if (
        runtime_frame is not None
        and not runtime_frame.empty
    ):
        fig = (
            draw_runtime_figure(
                runtime_frame,
                slots,
                runtime_spec,
            )
        )

        written += (
            save_figure(
                fig,
                outdir,
                runtime_spec,
                override_formats,
            )
        )

    # Memory
    if (
        memory_frame is not None
        and not memory_frame.empty
    ):
        for memory_spec in memory_specs:
            fig = (
                draw_memory_figure(
                    memory_frame,
                    slots,
                    memory_spec,
                )
            )

            written += (
                save_figure(
                    fig,
                    outdir,
                    memory_spec,
                    override_formats,
                )
            )

    for path in written:
        print(
            f"Wrote {path}"
        )


if __name__ == "__main__":
    main()