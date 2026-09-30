from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias


PathLike: TypeAlias = str | Path

Aggregation = Literal["sum", "mean"]
Alignment = Literal["strict", "intersection"]
PeakAggregation = Literal["none", "sum", "mean", "max"]

MetricRuntimeField = Literal[
    "real_s",
    "cpu_s",
]

RunRuntimeField = Literal[
    "runtime_total_s",
    "runtime_primary_s",
    "iteration_total_s",
]

RuntimeUnit = Literal[
    "seconds",
    "minutes",
    "hours",
]

YScale = Literal[
    "linear",
    "log",
]

MemoryRenderMode = Literal[
    "line",
    "underlay",
]

MemoryValue = Literal[
    "tracked_metric_gib",
    "duckdb_after_load_gib",
    "duckdb_end_gib",
    "duckdb_decomposition_gib",
    "process_current_gib",
    "process_peak_gib",
    "non_duckdb_process_gib",
]


# --------------------------------------------------------------------------- #
# Runtime / memory data specification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuntimeInput:
    """
    One runtime source.
    """

    file: PathLike

    # None:
    #     use a run-level timestep runtime.
    #
    # tuple:
    #     combine selected MetricSample runtimes.
    metrics: tuple[str, ...] | None = None

    metric_field: MetricRuntimeField = "real_s"
    metric_combine: Aggregation = "sum"

    run_value: RunRuntimeField = "runtime_total_s"


@dataclass(frozen=True)
class RuntimeGroup:
    key: str
    label: str

    inputs: tuple[RuntimeInput, ...]

    combine: Aggregation = "sum"
    alignment: Alignment = "strict"

    # Remove the final two recorded/aligned timestep
    # from this runtime group.
    exclude_last_timesteps: int = 0

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError(
                f"RuntimeGroup {self.key!r} "
                "must contain at least one RuntimeInput"
            )

        if self.exclude_last_timesteps < 0:
            raise ValueError(
                "exclude_last_timesteps must be >= 0"
            )


@dataclass(frozen=True)
class MemoryGroup:
    """
    One logical memory series built from one or more log files.

    Files are aligned by (start, end).

    combine:
        How memory measurements from multiple files are combined.

    alignment:
        strict:
            all files must contain exactly the same timesteps

        intersection:
            only timesteps occurring in every file are kept

    peak_combine:
        For multi-file groups, process peaks are not combined by default,
        because independent process peaks may have occurred at different
        moments.

    tracked_metrics / tracked_combine:
        Optional Pympler settings. These may simply be omitted from the
        experiment spec when tracked_metric_gib is not used.
    """

    key: str
    label: str

    files: tuple[PathLike, ...]

    combine: Aggregation = "sum"
    alignment: Alignment = "strict"

    peak_combine: PeakAggregation = "none"

    tracked_metrics: tuple[str, ...] | None = None
    tracked_combine: Aggregation = "sum"

    def __post_init__(self) -> None:
        if not self.files:
            raise ValueError(
                f"MemoryGroup {self.key!r} "
                "must contain at least one file"
            )


# --------------------------------------------------------------------------- #
# General figure configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FigurePlotSpec:
    # Canvas
    figsize: tuple[float, float] = (
        12.0,
        4.5,
    )

    font_scale: float = 1.3
    timezone: str = "Europe/Berlin"

    # Labels
    title: str | None = None
    x_label: str | None = None
    y_label: str | None = "Value"

    # X axis
    year_tick_interval: int | None = 1
    skip_x_ticks: bool = False
    x_limits: tuple[str, str] | None = None

    # Y axis
    y_scale: YScale = "linear"
    y_limits: tuple[float, float] | None = None

    # Guides
    show_year_lines: bool = True
    show_datapoint_lines: bool = False

    year_line_alpha: float = 0.30
    datapoint_line_alpha: float = 0.07

    show_grid: bool = True

    # Legend
    show_legend: bool = True
    legend_location: str = "best"
    legend_bbox: tuple[float, float] | None = None
    legend_ncol: int = 1

    # Lines
    linewidth: float = 1.6

    # Output
    output_stem: str = "figure"

    formats: tuple[str, ...] = (
        "pdf",
        "png",
    )

    def __post_init__(self) -> None:
        if (
            self.figsize[0] <= 0
            or self.figsize[1] <= 0
        ):
            raise ValueError(
                "figsize values must be > 0"
            )

        if self.font_scale <= 0:
            raise ValueError(
                "font_scale must be > 0"
            )

        if (
            self.year_tick_interval is not None
            and self.year_tick_interval < 1
        ):
            raise ValueError(
                "year_tick_interval must be >= 1 or None"
            )

        if not (
            0.0 <= self.year_line_alpha <= 1.0
        ):
            raise ValueError(
                "year_line_alpha must be between 0 and 1"
            )

        if not (
            0.0 <= self.datapoint_line_alpha <= 1.0
        ):
            raise ValueError(
                "datapoint_line_alpha must be between 0 and 1"
            )

        if self.legend_ncol < 1:
            raise ValueError(
                "legend_ncol must be >= 1"
            )

        if self.linewidth <= 0:
            raise ValueError(
                "linewidth must be > 0"
            )

        if not self.output_stem:
            raise ValueError(
                "output_stem must not be empty"
            )

        if not self.formats:
            raise ValueError(
                "formats must not be empty"
            )

        if self.y_limits is not None:
            lower, upper = self.y_limits

            if lower >= upper:
                raise ValueError(
                    "y_limits lower bound must be "
                    "smaller than upper bound"
                )

            if (
                self.y_scale == "log"
                and lower <= 0
            ):
                raise ValueError(
                    "log-scale y_limits must be positive"
                )


# --------------------------------------------------------------------------- #
# Runtime figure configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RuntimePlotSpec(FigurePlotSpec):
    """
    Runtime-specific figure configuration.

    The runtime CSV always stores seconds.

    unit controls only how the values are displayed in the plot:

        seconds -> runtime_s
        minutes -> runtime_s / 60
        hours   -> runtime_s / 3600

    y_limits are interpreted in the selected display unit.

    If y_label=None, an appropriate label is generated automatically.
    """

    unit: RuntimeUnit = "seconds"

    # None -> automatically generated based on unit.
    y_label: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()

        if self.unit not in (
            "seconds",
            "minutes",
            "hours",
        ):
            raise ValueError(
                f"Unknown runtime unit: {self.unit}"
            )


# --------------------------------------------------------------------------- #
# Memory component specification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MemorySeriesSpec:
    value: MemoryValue

    mode: MemoryRenderMode = "line"

    # None = all memory groups.
    groups: tuple[str, ...] | None = None

    # Optional custom component label.
    label: str | None = None

    # True:
    #     Timeliness — DuckDB memory
    #
    # False:
    #     DuckDB memory
    include_group_in_label: bool = True

    # Style overrides
    color: str | None = None
    linestyle: str | None = None
    linewidth: float | None = None

    alpha: float = 1.0
    fill_alpha: float = 0.22

    show_in_legend: bool = True

    def __post_init__(self) -> None:
        if self.mode not in (
            "line",
            "underlay",
        ):
            raise ValueError(
                f"Unknown memory render mode: {self.mode}"
            )

        if (
            self.linewidth is not None
            and self.linewidth <= 0
        ):
            raise ValueError(
                "MemorySeriesSpec.linewidth "
                "must be > 0 or None"
            )

        if not (
            0.0 <= self.alpha <= 1.0
        ):
            raise ValueError(
                "MemorySeriesSpec.alpha "
                "must be between 0 and 1"
            )

        if not (
            0.0 <= self.fill_alpha <= 1.0
        ):
            raise ValueError(
                "MemorySeriesSpec.fill_alpha "
                "must be between 0 and 1"
            )


@dataclass(frozen=True)
class MemoryPlotSpec(FigurePlotSpec):
    series: tuple[
        MemorySeriesSpec,
        ...,
    ] = (
        MemorySeriesSpec(
            value="duckdb_decomposition_gib",
            mode="underlay",
            label="DuckDB memory",
            show_in_legend=True,
        ),
        MemorySeriesSpec(
            value="process_current_gib",
            mode="line",
        ),
    )

    def __post_init__(self) -> None:
        super().__post_init__()

        if not self.series:
            raise ValueError(
                "MemoryPlotSpec.series must not be empty"
            )


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def _default_runtime_plot() -> RuntimePlotSpec:
    return RuntimePlotSpec(
        unit="seconds",
        y_scale="log",
        output_stem="runtime",
    )


def _default_memory_plot() -> MemoryPlotSpec:
    return MemoryPlotSpec(
        y_label="Memory usage (GiB)",
        y_scale="linear",
        output_stem="memory",
    )


def _default_memory_figures() -> tuple[
    MemoryPlotSpec,
    ...,
]:
    return (
        _default_memory_plot(),
    )


# --------------------------------------------------------------------------- #
# Plot specification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PlotSpec:
    runtime: RuntimePlotSpec = field(
        default_factory=_default_runtime_plot
    )

    memory_figures: tuple[
        MemoryPlotSpec,
        ...,
    ] = field(
        default_factory=_default_memory_figures
    )

    def __post_init__(self) -> None:
        if not self.memory_figures:
            raise ValueError(
                "PlotSpec.memory_figures "
                "must not be empty"
            )

        stems = [
            self.runtime.output_stem,
            *[
                figure.output_stem
                for figure
                in self.memory_figures
            ],
        ]

        duplicates = {
            stem
            for stem in stems
            if stems.count(stem) > 1
        }

        if duplicates:
            raise ValueError(
                "Duplicate output_stem values: "
                + ", ".join(
                    sorted(duplicates)
                )
            )


# --------------------------------------------------------------------------- #
# Complete experiment
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExperimentSpec:
    runtime_groups: tuple[
        RuntimeGroup,
        ...,
    ] = ()

    memory_groups: tuple[
        MemoryGroup,
        ...,
    ] = ()

    plot: PlotSpec = field(
        default_factory=PlotSpec
    )