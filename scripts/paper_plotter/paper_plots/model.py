from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Axis = Literal["left", "right"]
RenderMode = Literal["line", "underlay"]
Aggregation = Literal["SUM", "AVG"]

PropertyIds = int | tuple[int, ...] | None
EntitySchemaId = int | None
ClassId = int | None


# Database-backed series.
DbKey = tuple[
    str,                  # table
    int,                  # run_id
    int,                  # metric_id
    PropertyIds,          # property_id(s)
    Aggregation | None,   # aggregation_method
    EntitySchemaId,       # entity_schema_id
    ClassId,              # class_id
]


# CSV-backed series.
CsvKey = tuple[
    Literal["csv"],
    str,                  # csv_path
    str,                  # csv_column
]


DataKey = DbKey | CsvKey


@dataclass(frozen=True)
class SeriesSpec:
    # Required for database series; unused for CSV series.
    run_id: int | None = None
    metric_id: int | None = None
    table: str = "metric_value_float"

    # Read the series from a CSV file instead of the database.
    # csv_column is plotted against the CSV's "timestamp" column.
    csv_path: str | None = None
    csv_column: str | None = None

    # Optional property dimension.
    #
    # A list is converted to a tuple and aggregated per timestamp using
    # aggregation_method ("SUM" or "AVG").
    # An empty list/tuple aggregates all properties of the run/metric.
    property_id: int | list[int] | tuple[int, ...] | None = None
    aggregation_method: Aggregation | None = None

    # Optional dimension for metric_on_entity_schema_* tables.
    entity_schema_id: int | None = None

    # Optional dimension for metric_on_class_* tables.
    class_id: int | None = None

    label: str | None = None
    axis: Axis = "left"
    mode: RenderMode = "line"

    color: str | None = None
    linestyle: str | None = None
    linewidth: float = 2.0
    alpha: float | None = None

    # Right-axis series are rendered as underlays.
    fill_alpha: float = 0.22
    fill_baseline: float = 0.0

    # Optional spike-value labels.
    label_spikes: bool = False
    spike_min_prominence: float = 0.0
    spike_window: int = 3
    max_spike_labels: int = 8
    spike_value_format: str = ".3f"

    # One extra label, not counted in max_spike_labels, on the value
    # final_timestep_minus timesteps before the last one.
    final_label: bool = False
    final_timestep_minus: int = 1

    # Whether this individual series appears in the legend.
    show_in_legend: bool = True

    # Optional, right axis only: label of the left-axis line this underlay
    # belongs to.
    background_for: str | None = None

    @property
    def is_csv(self) -> bool:
        return self.csv_path is not None

    @property
    def data_key(self) -> DataKey:
        if self.is_csv:
            assert self.csv_path is not None
            assert self.csv_column is not None

            return (
                "csv",
                self.csv_path,
                self.csv_column,
            )

        assert self.run_id is not None
        assert self.metric_id is not None

        return (
            self.table,
            self.run_id,
            self.metric_id,
            self.property_id,
            self.aggregation_method,
            self.entity_schema_id,
            self.class_id,
        )

    @property
    def is_aggregated(self) -> bool:
        return isinstance(self.property_id, tuple)

    @property
    def display_label(self) -> str:
        if self.label:
            return self.label

        if self.is_csv:
            assert self.csv_path is not None
            return f"{self.csv_column} ({Path(self.csv_path).name})"

        if self.entity_schema_id is not None:
            return (
                f"run {self.run_id}, metric {self.metric_id}, "
                f"entity schema {self.entity_schema_id}"
            )

        if self.class_id is not None:
            return (
                f"run {self.run_id}, metric {self.metric_id}, "
                f"class {self.class_id}"
            )

        if self.is_aggregated:
            ids = ", ".join(map(str, self.property_id)) or "all"
            return (
                f"run {self.run_id}, metric {self.metric_id}, "
                f"{self.aggregation_method} of properties {ids}"
            )

        if self.property_id is not None:
            return (
                f"run {self.run_id}, metric {self.metric_id}, "
                f"property {self.property_id}"
            )

        return f"run {self.run_id}, metric {self.metric_id}"

    def __post_init__(self) -> None:
        if self.axis not in ("left", "right"):
            raise ValueError(f"Unknown axis: {self.axis}")

        if self.mode not in ("line", "underlay"):
            raise ValueError(f"Unknown render mode: {self.mode}")

        if self.mode == "underlay" and self.axis != "right":
            raise ValueError(
                "underlay series must use axis='right'"
            )

        # ----------------------------------------------------------
        # CSV/database configuration
        # ----------------------------------------------------------
        if self.is_csv:
            if not self.csv_column:
                raise ValueError(
                    "csv_path requires csv_column"
                )

            if (
                self.property_id is not None
                or self.aggregation_method is not None
                or self.entity_schema_id is not None
                or self.class_id is not None
            ):
                raise ValueError(
                    "csv_path cannot be combined with "
                    "property_id, aggregation_method, "
                    "entity_schema_id or class_id"
                )

        elif self.csv_column is not None:
            raise ValueError(
                "csv_column requires csv_path"
            )

        elif self.run_id is None or self.metric_id is None:
            raise ValueError(
                "database series require run_id and metric_id"
            )

        # ----------------------------------------------------------
        # Property dimension
        # ----------------------------------------------------------
        if isinstance(self.property_id, list):
            # Store lists as tuples so the spec and data_key stay hashable.
            object.__setattr__(
                self,
                "property_id",
                tuple(self.property_id),
            )

        if self.is_aggregated:
            if self.aggregation_method not in ("SUM", "AVG"):
                raise ValueError(
                    "a list of property_ids requires aggregation_method "
                    "'SUM' or 'AVG'"
                )

            if any(pid < 0 for pid in self.property_id):
                raise ValueError(
                    "property_ids must be >= 0"
                )

        else:
            if self.aggregation_method is not None:
                raise ValueError(
                    "aggregation_method requires a list of property_ids"
                )

            if (
                self.property_id is not None
                and self.property_id < 0
            ):
                raise ValueError(
                    "property_id must be >= 0 or None"
                )

        # ----------------------------------------------------------
        # Entity-schema dimension
        # ----------------------------------------------------------
        if self.entity_schema_id is not None:
            if self.entity_schema_id < 0:
                raise ValueError(
                    "entity_schema_id must be >= 0"
                )

            if self.property_id is not None:
                raise ValueError(
                    "entity_schema_id cannot be combined "
                    "with property_id"
                )

            if self.aggregation_method is not None:
                raise ValueError(
                    "entity_schema_id cannot be combined "
                    "with aggregation_method"
                )

            if self.class_id is not None:
                raise ValueError(
                    "entity_schema_id cannot be combined "
                    "with class_id"
                )

        # ----------------------------------------------------------
        # Class dimension
        # ----------------------------------------------------------
        if self.class_id is not None:
            if self.class_id < 0:
                raise ValueError(
                    "class_id must be >= 0 or None"
                )

            if self.property_id is not None:
                raise ValueError(
                    "class_id cannot be combined with property_id"
                )

            if self.aggregation_method is not None:
                raise ValueError(
                    "class_id cannot be combined "
                    "with aggregation_method"
                )

            if self.entity_schema_id is not None:
                raise ValueError(
                    "class_id cannot be combined "
                    "with entity_schema_id"
                )

        # ----------------------------------------------------------
        # Styling
        # ----------------------------------------------------------
        if self.linewidth <= 0:
            raise ValueError(
                "linewidth must be > 0"
            )

        if (
            self.alpha is not None
            and not 0.0 <= self.alpha <= 1.0
        ):
            raise ValueError(
                "alpha must be between 0 and 1 or None"
            )

        if not 0.0 <= self.fill_alpha <= 1.0:
            raise ValueError(
                "fill_alpha must be between 0 and 1"
            )

        # ----------------------------------------------------------
        # Spike/final labels
        # ----------------------------------------------------------
        if self.spike_min_prominence < 0.0:
            raise ValueError(
                "spike_min_prominence must be >= 0"
            )

        if self.spike_window < 1:
            raise ValueError(
                "spike_window must be >= 1"
            )

        if self.max_spike_labels < 1:
            raise ValueError(
                "max_spike_labels must be >= 1"
            )

        if not self.spike_value_format.strip():
            raise ValueError(
                "spike_value_format must not be empty"
            )

        if self.final_timestep_minus < 0:
            raise ValueError(
                "final_timestep_minus must be >= 0"
            )

        # ----------------------------------------------------------
        # Underlay association
        # ----------------------------------------------------------
        if (
            self.background_for is not None
            and self.axis != "right"
        ):
            raise ValueError(
                "background_for requires axis='right'"
            )


@dataclass(frozen=True)
class FigureSpec:
    series: tuple[SeriesSpec, ...]
    outputs: tuple[str, ...]

    title: str | None = None
    x_label: str | None = None
    y_label: str = "Value"
    right_y_label: str | None = None

    # Optional color for the complete right y-axis. If there is exactly one
    # unpaired right-axis underlay, it also becomes that underlay's fill color.
    # None preserves the existing grey/default behavior.
    right_axis_color: str | None = None

    # Figure size in inches: (width, height).
    figsize: tuple[float, float] = (7.2, 4.2)
    timezone: str = "Europe/Berlin"

    # Multiplies every font size.
    font_scale: float = 1.0

    # 1 = every year, 2 = every second year, None = automatic date ticks.
    year_tick_interval: int | None = None

    # Label only every second x tick; tick marks remain.
    skip_x_ticks: bool = False

    # Optional vertical guide lines.
    show_year_lines: bool = True
    show_datapoint_lines: bool = False
    year_line_alpha: float = 0.28
    datapoint_line_alpha: float = 0.10

    # Legend controls.
    show_legend: bool = True

    # A matplotlib location, or a tuple of candidate locations.
    legend_location: str | tuple[str, ...] = "best"
    legend_bbox: tuple[float, float] | None = None
    legend_ncol: int = 1

    show_grid: bool = True

    x_limits: tuple[str, str] | None = None

    # None = fit the axis to its series.
    y_limits: tuple[float, float] | None = None
    right_y_limits: tuple[float, float] | None = None

    # Log scale for the right axis.
    right_log_scale: bool = False

    # Log axis only: values below this floor are cut off.
    right_log_floor: float | None = None

    show_right_axis_labels: bool = True

    # Write ticks as 0, 0.5M, 1M, ... rather than a 1e6 offset.
    right_axis_unit_suffix: bool = False

    # Stack right-axis underlays in series order.
    stack_right_series: bool = False

    # Draw right-axis series in their own panel below the main plot.
    right_axis_panel: bool = False

    # Height of that panel relative to the main plot.
    right_panel_height_ratio: float = 0.4

    # Side of the panel's y-axis.
    right_panel_axis_side: Axis = "right"

    def __post_init__(self) -> None:
        if not self.series:
            raise ValueError(
                "FigureSpec.series must not be empty"
            )

        if not self.outputs:
            raise ValueError(
                "FigureSpec.outputs must not be empty"
            )

        if self.figsize[0] <= 0 or self.figsize[1] <= 0:
            raise ValueError(
                "FigureSpec.figsize values must be > 0"
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

        if not 0.0 <= self.year_line_alpha <= 1.0:
            raise ValueError(
                "year_line_alpha must be between 0 and 1"
            )

        if not 0.0 <= self.datapoint_line_alpha <= 1.0:
            raise ValueError(
                "datapoint_line_alpha must be between 0 and 1"
            )

        if self.legend_ncol < 1:
            raise ValueError(
                "legend_ncol must be >= 1"
            )

        if (
            self.right_log_scale
            and self.right_y_limits
            and self.right_y_limits[0] <= 0
        ):
            raise ValueError(
                "right_log_scale needs a "
                "right_y_limits lower limit > 0"
            )

        if self.right_log_floor is not None:
            if not self.right_log_scale:
                raise ValueError(
                    "right_log_floor requires right_log_scale"
                )

            if self.right_log_floor <= 0:
                raise ValueError(
                    "right_log_floor must be > 0"
                )

            if self.right_y_limits:
                raise ValueError(
                    "right_log_floor cannot be combined "
                    "with right_y_limits"
                )

        if self.right_panel_height_ratio <= 0:
            raise ValueError(
                "right_panel_height_ratio must be > 0"
            )

        if self.right_panel_axis_side not in ("left", "right"):
            raise ValueError(
                f"Unknown right_panel_axis_side: "
                f"{self.right_panel_axis_side}"
            )

        if isinstance(self.legend_location, list):
            object.__setattr__(
                self,
                "legend_location",
                tuple(self.legend_location),
            )

        if isinstance(self.legend_location, tuple):
            if not self.legend_location:
                raise ValueError(
                    "legend_location must not be an empty tuple"
                )

            if "best" in self.legend_location:
                raise ValueError(
                    "legend_location candidates must be fixed "
                    "locations, not 'best'"
                )

        left_labels = [
            series.display_label
            for series in self.series
            if series.axis == "left"
        ]

        targets = [
            series.background_for
            for series in self.series
            if series.background_for
        ]

        for target in targets:
            if left_labels.count(target) != 1:
                raise ValueError(
                    f"background_for={target!r} must match the label "
                    "of exactly one left-axis series"
                )

        if len(targets) != len(set(targets)):
            raise ValueError(
                "each left-axis series can have one background"
            )

        if self.stack_right_series and any(
            series.label_spikes or series.final_label
            for series in self.series
            if series.axis == "right"
        ):
            raise ValueError(
                "label_spikes and final_label are not supported "
                "with stack_right_series "
                "(labels would show the cumulative height)"
            )