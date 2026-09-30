from __future__ import annotations

import math
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.legend import Legend
from matplotlib.patches import Patch
from matplotlib.text import Annotation
from matplotlib.transforms import Bbox
import numpy as np
import pandas as pd

from .model import DataKey, FigureSpec, SeriesSpec

# Okabe-Ito palette for primary series.
COLORBLIND_PALETTE = (
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#000000",  # black
)
LINESTYLES = ("-", "--", "-.", ":")

# All right-axis series share one scale and use different greys.
RIGHT_FILL_COLORS = (
    "#A9A9A9",
    "#868686",
    "#666666",
    "#595959",
    "#3F3F3F",
)
RIGHT_AXIS_COLOR = "#5F5F5F"
# Edge of a background_for underlay: thin and faint, so it stays background.
BACKGROUND_EDGE_WIDTH = 0.8
BACKGROUND_EDGE_ALPHA = 0.7
VERTICAL_GUIDE_COLOR = "#656565"

_RC_PARAMS = {
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
SPIKE_LABEL_FONTSIZE = 7
# Value labels that would cross the right edge of the plot are moved left to
# end this many points before it.
EDGE_LABEL_GAP = 2.0
MAX_EDGE_PASSES = 3
# Overlapping final labels are stacked with this many points between them.
FINAL_LABEL_GAP = 1.0

# Largest first: the first unit not exceeding a value is used.
_UNIT_SUFFIXES = ((1e9, "B"), (1e6, "M"), (1e3, "k"))

# A log axis labels between these many ticks.
LOG_AXIS_MIN_TICKS = 3
LOG_AXIS_MAX_TICKS = 5
# Finer tick grids for a log axis spanning too few decades, coarsest first:
# values per decade.
_LOG_TICK_GRIDS = ((1.0, 3.0), (1.0, 2.0, 5.0))
# A log axis ends this fraction of a tick step below its floor.
LOG_AXIS_BOTTOM_PAD = 0.15
# Relative tolerance when comparing a tick with a data value.
_LOG_TICK_TOLERANCE = 1e-9


def _rc_params(font_scale: float) -> dict:
    params = dict(_RC_PARAMS)
    for key in _FONT_SIZE_KEYS:
        params[key] = params[key] * font_scale
    return params


def _unit_for(value: float) -> tuple[float, str]:
    """Largest unit (scale, suffix) not exceeding value; (1.0, "") below 1000."""
    for scale, suffix in _UNIT_SUFFIXES:
        if value >= scale:
            return scale, suffix
    return 1.0, ""


class _UnitSuffixFormatter(mticker.Formatter):
    """Format ticks as 0, 0.5M, 1M, ... instead of using a "1e6" offset.

    By default one unit is used for the whole axis, picked from the largest
    visible tick, so all labels on the axis are directly comparable. With
    per_tick, each tick gets its own unit (1k, 10k, 100k, 1M), which suits a
    log axis spanning several units.
    """

    def __init__(self, per_tick: bool = False) -> None:
        self._per_tick = per_tick
        self._scale = 1.0
        self._suffix = ""
        self._decimals = 0

    def set_locs(self, locs) -> None:
        super().set_locs(locs)
        if self._per_tick:
            return
        locs = np.asarray(locs, dtype=float)
        if self.axis is not None and locs.size:
            low, high = sorted(self.axis.get_view_interval())
            tolerance = 1e-9 * max(abs(low), abs(high), 1.0)
            visible = locs[(locs >= low - tolerance) & (locs <= high + tolerance)]
            if visible.size:
                locs = visible
        locs = locs[np.isfinite(locs)]

        largest = float(np.max(np.abs(locs))) if locs.size else 0.0
        self._scale, self._suffix = _unit_for(largest)

        # Fewest decimals that still represent every tick exactly.
        scaled = locs / self._scale
        self._decimals = 0
        for decimals in range(4):
            self._decimals = decimals
            if np.allclose(scaled, np.round(scaled, decimals), rtol=0, atol=1e-9):
                break

    def __call__(self, x, pos=None) -> str:
        if self._per_tick:
            scale, suffix = _unit_for(abs(x))
            # Rounded to hide float noise, e.g. 0.30000000000000004 -> 0.3.
            value = round(x / scale, 9)
            text = f"{value:g}"
        else:
            scale, suffix = self._scale, self._suffix
            value = round(x / scale, self._decimals)
            text = f"{value:.{self._decimals}f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
        if value == 0:
            return "0"
        if plt.rcParams["axes.unicode_minus"]:
            text = text.replace("-", "\N{MINUS SIGN}")
        return f"{text}{suffix}"


def _style_for(spec: SeriesSpec, index: int) -> tuple[str, str]:
    color = spec.color or COLORBLIND_PALETTE[index % len(COLORBLIND_PALETTE)]
    linestyle = spec.linestyle or LINESTYLES[index % len(LINESTYLES)]
    return color, linestyle


def _localized(frame: pd.DataFrame, timezone: str) -> pd.DataFrame:
    data = frame.copy()
    # Robust to both tz-aware and tz-naive input frames.
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).dt.tz_convert(
        timezone
    )
    return data


def _prominence_at(y: np.ndarray, i: int, window: int) -> tuple[float, int] | None:
    """Prominence and direction of point i against its finite neighbours.

    Direction is 1 for a local high and -1 for a local low. Uses whichever
    sides have finite neighbours, so it also works at the ends of the series.
    None if there is no finite neighbour at all.
    """
    left = y[max(0, i - window):i]
    right = y[i + 1:min(len(y), i + window + 1)]
    sides = [side[np.isfinite(side)] for side in (left, right)]
    sides = [side for side in sides if side.size]
    if not sides:
        return None

    value = float(y[i])
    upward_prominence = value - max(float(np.min(side)) for side in sides)
    downward_prominence = min(float(np.max(side)) for side in sides) - value

    if upward_prominence >= downward_prominence:
        return upward_prominence, 1
    return downward_prominence, -1


def _final_index(values: pd.Series, timestep_minus: int) -> int | None:
    """Index of the timestep that gets the final-value label, if it has a value."""
    index = len(values) - 1 - timestep_minus
    if index < 0 or not np.isfinite(float(values.iloc[index])):
        return None
    return index


def _spike_indices(
    values: pd.Series,
    min_prominence: float,
    limit: int,
    window: int,
    reserved_index: int | None = None,
) -> list[tuple[int, int]]:
    """Return prominent upward and downward spikes.

    Direction is 1 for an upward spike and -1 for a downward spike. Spikes
    within `window` of `reserved_index` are skipped without counting towards
    `limit`, so a label placed there (the final value) replaces them.
    """
    y = values.to_numpy(dtype=float)
    if len(y) < 3:
        return []

    finite = np.isfinite(y)
    if not finite.any():
        return []

    value_range = float(np.nanmax(y) - np.nanmin(y))
    threshold = min_prominence * value_range
    candidates: list[tuple[float, float, int, int]] = []

    for i in range(1, len(y) - 1):
        if not finite[i]:
            continue

        # A spike needs finite neighbours on both sides.
        left = y[max(0, i - window):i]
        right = y[i + 1:min(len(y), i + window + 1)]
        if not np.isfinite(left).any() or not np.isfinite(right).any():
            continue

        prominence, direction = _prominence_at(y, i, window)

        if prominence <= 0 or prominence < threshold:
            continue

        candidates.append((prominence, abs(float(y[i])), i, direction))

    # Keep strongest spikes and suppress nearby duplicates.
    selected: list[tuple[int, int]] = []
    for _, _, index, direction in sorted(candidates, reverse=True):
        if reserved_index is not None and abs(index - reserved_index) <= window:
            continue
        if any(abs(index - other_index) <= window for other_index, _ in selected):
            continue
        selected.append((index, direction))
        if len(selected) >= limit:
            break

    return sorted(selected, key=lambda item: item[0])


def _format_spike_value(value: float, value_format: str) -> str:
    """Accept '.2f', ':.2f', '{:.2f}', and old '%' formats."""
    fmt = value_format.strip()

    if not fmt:
        return str(value)
    if "{" in fmt:
        return fmt.format(value)
    if fmt.startswith("%"):
        return fmt % value
    if fmt.startswith(":"):
        fmt = fmt[1:]

    return format(value, fmt)


def _annotate_spikes(
    axis: plt.Axes,
    frame: pd.DataFrame,
    spec: SeriesSpec,
    color: str,
    font_scale: float = 1.0,
) -> tuple[list[Annotation], Annotation | None]:
    """Draw the value labels of one series: (spike labels, final label)."""
    if not spec.label_spikes and not spec.final_label:
        return [], None

    final_index = (
        _final_index(frame["value"], spec.final_timestep_minus)
        if spec.final_label
        else None
    )

    indices = (
        _spike_indices(
            frame["value"],
            min_prominence=spec.spike_min_prominence,
            limit=spec.max_spike_labels,
            window=spec.spike_window,
            reserved_index=final_index,
        )
        if spec.label_spikes
        else []
    )

    if final_index is not None:
        # Extra label on top of the spike limit, placed by the same rule.
        placement = _prominence_at(
            frame["value"].to_numpy(dtype=float), final_index, spec.spike_window
        )
        direction = placement[1] if placement is not None else 1
        indices = sorted(indices + [(final_index, direction)])

    spike_labels: list[Annotation] = []
    final_label: Annotation | None = None
    for index, direction in indices:
        timestamp = frame["timestamp"].iloc[index]
        value = float(frame["value"].iloc[index])
        is_downward = direction < 0

        annotation = axis.annotate(
            _format_spike_value(value, spec.spike_value_format),
            xy=(timestamp, value),
            xytext=(0, -7 if is_downward else 7),
            textcoords="offset points",
            ha="center",
            va="top" if is_downward else "bottom",
            fontsize=SPIKE_LABEL_FONTSIZE * font_scale,
            color=color,
            zorder=5,
            clip_on=False,
            bbox={
                "boxstyle": "round,pad=0.15",
                "facecolor": "white",
                "edgecolor": "none",
                "alpha": 0.78,
            },
        )
        if index == final_index:
            final_label = annotation
        else:
            spike_labels.append(annotation)

    return spike_labels, final_label


def _align_final_labels(final_labels: list[Annotation]) -> None:
    """Put all final labels in one column, centred over the latest final point.

    Each label keeps its own value (y); only the x of its anchor moves, so the
    column stays exact whatever the layout. Both axes share the x-axis, so this
    also aligns labels of right-axis series with left-axis ones.
    """
    if len(final_labels) < 2:
        return
    column_x = max(pd.Timestamp(label.xy[0]) for label in final_labels)
    for label in final_labels:
        label.xy = (column_x, label.xy[1])


def _keep_labels_inside_right_edge(
    fig: plt.Figure,
    axis: plt.Axes,
    groups: list[list[Annotation]],
) -> None:
    """Shift value labels left just enough that they end before the plot's right edge.

    Labels in one group move together, by what their widest member needs, so an
    aligned column stays aligned. Labels that fit are left where they are. The
    layout is recomputed on every draw and a shift can change it slightly, so
    this repeats until nothing crosses the edge.
    """
    groups = [group for group in groups if group]
    if not groups:
        return

    points_per_pixel = 72.0 / fig.dpi
    for _ in range(MAX_EDGE_PASSES):
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        limit = axis.get_window_extent(renderer).x1 - EDGE_LABEL_GAP * fig.dpi / 72.0

        moved = False
        for group in groups:
            overflow = max(_label_extent(label, renderer).x1 for label in group) - limit
            if overflow > 0.5:  # below half a pixel it is rounding, not a crossing
                for label in group:
                    dx, dy = label.xyann
                    label.xyann = (dx - overflow * points_per_pixel, dy)
                moved = True
        if not moved:
            return


def _separate_final_labels(fig: plt.Figure, final_labels: list[Annotation]) -> None:
    """Stack overlapping final labels in the order of their values.

    The final labels share one column, so two overlap as soon as their boxes
    overlap vertically. Such labels are joined into one block, ordered by the
    height of their points (the highest on top), FINAL_LABEL_GAP apart and
    centred where they were. Blocks that then overlap are joined as well.
    Labels that overlap nothing stay where they are; only their y moves.
    """
    if len(final_labels) < 2:
        return

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    gap = FINAL_LABEL_GAP * fig.dpi / 72.0

    # (height of the point, label centre, label height), all in pixels.
    # Left and right axis labels are compared on the screen, not by value.
    items = []
    for label in final_labels:
        axis = label.axes
        point_y = axis.transData.transform((axis.get_xlim()[0], label.xy[1]))[1]
        extent = _label_extent(label, renderer)
        items.append((point_y, (extent.y0 + extent.y1) / 2, extent.height, label))
    items.sort(key=lambda item: item[0])

    def span(block: list) -> tuple[float, float]:
        centre = sum(item[1] for item in block) / len(block)
        height = sum(item[2] for item in block) + gap * (len(block) - 1)
        return centre - height / 2, centre + height / 2

    # Bottom to top; a label below the top of the block under it (overlapping
    # it, or out of order) joins that block.
    blocks: list[list] = []
    for item in items:
        blocks.append([item])
        while len(blocks) > 1 and span(blocks[-1])[0] < span(blocks[-2])[1] + gap:
            upper = blocks.pop()
            blocks[-1].extend(upper)

    points_per_pixel = 72.0 / fig.dpi
    for block in blocks:
        if len(block) < 2:
            continue
        bottom = span(block)[0]
        for _, centre, height, label in block:
            dx, dy = label.xyann
            label.xyann = (dx, dy + (bottom + height / 2 - centre) * points_per_pixel)
            bottom += height + gap


def _label_extent(label: Annotation, renderer) -> Bbox:
    """Window extent of a label including its background box."""
    extent = label.get_window_extent(renderer)
    patch = label.get_bbox_patch()
    if patch is not None:
        extent = Bbox.union([extent, patch.get_window_extent(renderer)])
    return extent


def _log_grid(subs: tuple[float, ...], low: float, high: float) -> list[float]:
    """Values sub * 10**k of a log tick grid within [low, high], ascending."""
    decades = range(math.floor(math.log10(low)), math.floor(math.log10(high)) + 1)
    values = (sub * 10.0**decade for decade in decades for sub in subs)
    return [
        value
        for value in values
        if low * (1 - _LOG_TICK_TOLERANCE) <= value <= high * (1 + _LOG_TICK_TOLERANCE)
    ]


def _log_ticks(low: float, high: float) -> list[float]:
    """Round log-axis ticks within [low, high], ascending.

    Every decade, or every n-th decade from the lowest one up if there would be
    more than LOG_AXIS_MAX_TICKS. If there would be fewer than
    LOG_AXIS_MIN_TICKS, the 1-3 or else the 1-2-5 grid, extended below `low`
    if even that is too few.
    """
    ticks = _log_grid((1.0,), low, high)
    if len(ticks) > LOG_AXIS_MAX_TICKS:
        stride = math.ceil((len(ticks) - 1) / (LOG_AXIS_MAX_TICKS - 1))
        return ticks[::stride]
    for subs in _LOG_TICK_GRIDS:
        if len(ticks) >= LOG_AXIS_MIN_TICKS:
            return ticks
        ticks = _log_grid(subs, low, high)
    if len(ticks) < LOG_AXIS_MIN_TICKS:
        # Two 1-2-5 decades below `high` hold enough values.
        ticks = _log_grid(_LOG_TICK_GRIDS[-1], high / 100.0, high)[-LOG_AXIS_MIN_TICKS:]
    return ticks


def _set_log_ticks(
    axis: plt.Axes,
    values: np.ndarray,
    floor: float | None,
    fixed_limits: bool,
) -> None:
    """Label round ticks from the floor to the top of the data.

    The floor is `floor`, or the smallest positive value. The axis ends just
    below it; smaller values are cut off at the x-axis. The top tick is the
    largest round value not above the data, so it is always inside the axis.
    With `fixed_limits` the limits are kept and the ticks lie within them.
    Minor ticks stay unlabelled.
    """
    if fixed_limits:
        low, high = axis.get_ylim()
    else:
        positive = values[np.isfinite(values) & (values > 0)]
        if not positive.size:
            return
        low = floor if floor is not None else float(positive.min())
        high = float(positive.max())

    ticks = _log_ticks(low, high)
    axis.yaxis.set_major_locator(mticker.FixedLocator(ticks))
    axis.yaxis.set_minor_formatter(mticker.NullFormatter())
    if not fixed_limits:
        step = ticks[1] / ticks[0]
        axis.set_ylim(bottom=min(low, ticks[0]) / step**LOG_AXIS_BOTTOM_PAD)


def _style_right_axis(
    axis: plt.Axes,
    label: str | None,
    show_labels: bool,
    color: str = RIGHT_AXIS_COLOR,
    side: str = "right",
    own_panel: bool = False,
) -> None:
    """Style the complete secondary y-axis with one color.

    `side` is where its spine, ticks and label go. A twinx() axis shares the
    plot's frame, so only that spine is shown; an axis in its own panel also
    keeps its bottom spine, which carries the shared x-axis.
    """
    other_side = "left" if side == "right" else "right"
    axis.spines[side].set_visible(True)
    axis.spines[side].set_color(color)
    axis.spines[side].set_linewidth(0.9)

    # Hide duplicate spines created by twinx().
    axis.spines[other_side].set_visible(False)
    axis.spines["bottom"].set_visible(own_panel)
    axis.spines["top"].set_visible(False)

    axis.yaxis.set_ticks_position(side)
    axis.yaxis.set_label_position(side)
    axis.tick_params(
        axis="y",
        which="both",
        color=color,
        labelcolor=color,
        **{side: True, other_side: False},
        **{f"label{side}": show_labels, f"label{other_side}": False},
    )

    offset = axis.yaxis.get_offset_text()
    offset.set_color(color)
    offset.set_visible(show_labels)

    axis.yaxis.label.set_color(color)

    if show_labels and label:
        axis.set_ylabel(label, color=color)
    else:
        axis.set_ylabel("")


def _draw_vertical_guides(
    axis: plt.Axes,
    spec: FigureSpec,
    data: dict[DataKey, pd.DataFrame],
) -> None:
    """Draw year and/or datapoint guide lines without changing x-limits."""
    x_min, x_max = axis.get_xlim()

    if spec.show_year_lines:
        interval = spec.year_tick_interval or 1
        year_locator = mdates.YearLocator(base=interval)
        tick_values = year_locator.tick_values(
            mdates.num2date(x_min),
            mdates.num2date(x_max),
        )
        for x in tick_values:
            # Do not let a Jan-1 guide line expand the plot before the first
            # actual datapoint.
            if x < x_min or x > x_max:
                continue
            axis.axvline(
                x=x,
                color=VERTICAL_GUIDE_COLOR,
                linewidth=0.7,
                alpha=spec.year_line_alpha,
                zorder=0.4,
            )

    if spec.show_datapoint_lines:
        timestamps: set[pd.Timestamp] = set()
        for series in spec.series:
            frame = _localized(data[series.data_key], spec.timezone)
            timestamps.update(frame["timestamp"].dropna().tolist())

        for timestamp in sorted(timestamps):
            x = mdates.date2num(timestamp.to_pydatetime())
            if x < x_min or x > x_max:
                continue
            axis.axvline(
                x=timestamp,
                color=VERTICAL_GUIDE_COLOR,
                linewidth=0.45,
                alpha=spec.datapoint_line_alpha,
                zorder=0.3,
            )

    # axvline normally participates in autoscaling. Restore the exact limits
    # from before the guides were drawn.
    axis.set_xlim(x_min, x_max)


def _draw_legend(
    axis: plt.Axes,
    spec: FigureSpec,
    handles: list,
    labels: list[str],
) -> Legend | None:
    if not spec.show_legend or not handles:
        return None

    location = spec.legend_location
    kwargs = {
        # With candidates, start at the first; _choose_legend_location decides.
        "loc": location[0] if isinstance(location, tuple) else location,
        "frameon": False,
        "ncol": spec.legend_ncol,
    }
    if spec.legend_bbox is not None:
        kwargs["bbox_to_anchor"] = spec.legend_bbox

    return axis.legend(handles, labels, **kwargs)


def _choose_legend_location(
    fig: plt.Figure,
    legend: Legend | None,
    candidates: str | tuple[str, ...],
) -> None:
    """Like loc="best", but only over the given locations; ties go to the earlier one.

    Uses matplotlib's own "best" data and score (Legend._auto_legend_data is
    private, but it is what loc="best" runs on).
    """
    if legend is None or not isinstance(candidates, tuple) or len(candidates) < 2:
        return

    scored = []
    for order, location in enumerate(candidates):
        legend.set_loc(location)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        box = legend.get_window_extent(renderer)
        bboxes, lines, offsets = legend._auto_legend_data(renderer)
        badness = (
            sum(box.count_contains(line.vertices) for line in lines)
            + box.count_contains(offsets)
            + box.count_overlaps(bboxes)
            + sum(line.intersects_bbox(box, filled=False) for line in lines)
        )
        scored.append((badness, order, location))
        if badness == 0:
            break

    legend.set_loc(min(scored)[2])


def _create_axes(
    spec: FigureSpec,
    has_right: bool,
) -> tuple[plt.Figure, plt.Axes, plt.Axes | None]:
    """Figure with the left axis and, if needed, the axis for right-axis series.

    That axis is a twin behind the left one, or with right_axis_panel a smaller
    panel below it that shares the x-axis.
    """
    if has_right and spec.right_axis_panel:
        fig, (left, right) = plt.subplots(
            2,
            1,
            sharex=True,
            figsize=spec.figsize,
            constrained_layout=True,
            gridspec_kw={"height_ratios": (1.0, spec.right_panel_height_ratio)},
        )
        return fig, left, right

    fig, left = plt.subplots(figsize=spec.figsize, constrained_layout=True)
    if not has_right:
        return fig, left, None

    right = left.twinx()
    # Keep primary lines above the grey contextual underlays.
    right.set_zorder(0)
    left.set_zorder(1)
    left.patch.set_visible(False)
    # The left axis is drawn on top; its own right spine would cover the
    # colored spine of the right axis.
    left.spines["right"].set_visible(False)
    right.spines["top"].set_visible(False)
    return fig, left, right


def render_figure(
    spec: FigureSpec,
    data: dict[DataKey, pd.DataFrame],
) -> None:
    with plt.rc_context(_rc_params(spec.font_scale)):
        right_specs = [series for series in spec.series if series.axis == "right"]
        fig, left, right = _create_axes(spec, bool(right_specs))
        # Right-axis series in their own panel below the left axis.
        panel = right is not None and spec.right_axis_panel
        # The axis whose x tick labels and x label are shown.
        x_axis = right if panel else left
        right_axis_color = spec.right_axis_color or RIGHT_AXIS_COLOR

        handles: list = []
        labels: list[str] = []
        spike_labels: list[Annotation] = []
        final_labels: list[Annotation] = []
        right_index = 0
        # Plotted top edge of every right-axis series, for the log-axis ticks.
        right_values: list[np.ndarray] = []

        # Styles of the left lines by label, so a background_for underlay can
        # match its line wherever it appears in the series list.
        left_series = [series for series in spec.series if series.axis == "left"]
        left_styles = {
            series.display_label: _style_for(series, index)
            for index, series in enumerate(left_series)
        }
        line_legend_index: dict[str, int] = {}
        background_patches: dict[str, Patch] = {}

        if spec.stack_right_series and right_specs:
            # Stack on the union of all timestamps; a missing value counts as 0.
            stack_x = (
                pd.concat(
                    [
                        _localized(data[s.data_key], spec.timezone)["timestamp"]
                        for s in right_specs
                    ]
                )
                .drop_duplicates()
                .sort_values()
                .reset_index(drop=True)
            )
            stack_bottom = pd.Series(0.0, index=stack_x)

        for series in spec.series:
            # Important: property-aware key. Ordinary series use property_id=None.
            frame = _localized(data[series.data_key], spec.timezone)

            if series.axis == "right":
                assert right is not None

                pair_style = (
                    left_styles[series.background_for]
                    if series.background_for
                    else None
                )
                if series.color is not None:
                    # Explicit per-series color always wins.
                    fill_color = series.color
                elif pair_style is not None:
                    # background_for keeps its existing linked left-series color.
                    fill_color = pair_style[0]
                elif spec.right_axis_color is not None and len(right_specs) == 1:
                    # For one standalone underlay, use the same color as the right axis.
                    fill_color = spec.right_axis_color
                else:
                    # Existing behavior for multiple independent right-axis series.
                    fill_color = RIGHT_FILL_COLORS[
                        right_index % len(RIGHT_FILL_COLORS)
                    ]

                if spec.stack_right_series:
                    values = (
                        frame.set_index("timestamp")["value"]
                        .astype(float)
                        .reindex(stack_x)
                        .fillna(0.0)
                    )
                    x = stack_x
                    lower = stack_bottom.to_numpy()
                    stack_bottom = stack_bottom + values
                    upper = stack_bottom.to_numpy()
                else:
                    x = frame["timestamp"]
                    lower = series.fill_baseline
                    upper = frame["value"].astype(float)

                right_values.append(np.asarray(upper, dtype=float))

                # Right-axis series are fill-only and all use the SAME right axis.
                right.fill_between(
                    x,
                    lower,
                    upper,
                    color=fill_color,
                    alpha=series.fill_alpha,
                    linewidth=0,
                    zorder=0.1 + right_index * 0.01,
                )

                patch = Patch(
                    facecolor=fill_color,
                    edgecolor="none",
                    alpha=series.fill_alpha,
                )
                if pair_style:
                    # Thin edge in the line's dash pattern links the pair.
                    right.plot(
                        x,
                        upper,
                        color=fill_color,
                        linestyle=pair_style[1],
                        linewidth=BACKGROUND_EDGE_WIDTH,
                        alpha=BACKGROUND_EDGE_ALPHA,
                        zorder=0.5 + right_index * 0.01,
                    )
                    background_patches[series.background_for] = patch
                elif series.show_in_legend:
                    handles.append(patch)
                    labels.append(series.display_label)

                series_spike_labels, series_final_label = _annotate_spikes(
                    right,
                    frame,
                    series,
                    right_axis_color,
                    spec.font_scale,
                )
                spike_labels += series_spike_labels
                if series_final_label is not None:
                    final_labels.append(series_final_label)
                right_index += 1
                continue

            color, linestyle = left_styles[series.display_label]
            alpha = 1.0 if series.alpha is None else series.alpha

            (line,) = left.plot(
                frame["timestamp"],
                frame["value"],
                color=color,
                linestyle=linestyle,
                linewidth=series.linewidth,
                alpha=alpha,
                zorder=2,
            )

            if series.show_in_legend:
                line_legend_index[series.display_label] = len(handles)
                handles.append(line)
                labels.append(series.display_label)

            series_spike_labels, series_final_label = _annotate_spikes(
                left,
                frame,
                series,
                color,
                spec.font_scale,
            )
            spike_labels += series_spike_labels
            if series_final_label is not None:
                final_labels.append(series_final_label)

        # A line and its background share one legend entry: swatch + line.
        for target, patch in background_patches.items():
            if target in line_legend_index:
                index = line_legend_index[target]
                handles[index] = (patch, handles[index])

        # X-axis ticks.
        if spec.year_tick_interval is not None:
            locator = mdates.YearLocator(base=spec.year_tick_interval)
            left.xaxis.set_major_locator(locator)
            left.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        else:
            locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
            left.xaxis.set_major_locator(locator)
            left.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

        left.margins(x=0.01)

        if spec.title:
            left.set_title(spec.title)
        if spec.x_label:
            x_axis.set_xlabel(spec.x_label)
        left.set_ylabel(spec.y_label)

        if spec.show_grid:
            for axis in (left, right) if panel else (left,):
                axis.grid(axis="y", alpha=0.22, linewidth=0.7)
                axis.grid(axis="x", visible=False)

        # Explicit x-limits win over automatic limits.
        if spec.x_limits:
            start = pd.Timestamp(spec.x_limits[0])
            end = pd.Timestamp(spec.x_limits[1])
            if start.tzinfo is None:
                start = start.tz_localize(spec.timezone)
            else:
                start = start.tz_convert(spec.timezone)
            if end.tzinfo is None:
                end = end.tz_localize(spec.timezone)
            else:
                end = end.tz_convert(spec.timezone)
            left.set_xlim(start, end)

        _draw_vertical_guides(left, spec, data)
        if panel:
            _draw_vertical_guides(right, spec, data)

        if spec.skip_x_ticks:
            # Keep every tick mark, but label only the 1st, 3rd, ... visible one.
            x_min, x_max = left.get_xlim()
            ticks = [t for t in left.get_xticks() if x_min <= t <= x_max]
            tick_labels = left.xaxis.get_major_formatter().format_ticks(ticks)
            left.set_xticks(
                ticks,
                [l if i % 2 == 0 else "" for i, l in enumerate(tick_labels)],
            )
        if panel:
            # The x ticks are shared; only the lower panel labels them.
            left.tick_params(axis="x", labelbottom=False)

        if spec.y_limits:
            left.set_ylim(*spec.y_limits)
        elif left.dataLim.y0 >= 0:
            # Autoscaled non-negative data (e.g. durations) is read against 0.
            left.set_ylim(bottom=0)
        if right is not None and spec.right_log_scale:
            right.set_yscale("log")
        if right is not None and spec.right_y_limits:
            right.set_ylim(*spec.right_y_limits)
        elif right is not None and right.dataLim.y0 >= 0 and not spec.right_log_scale:
            # Same rule as the left axis, so both zeros sit on an x-axis.
            right.set_ylim(bottom=0)
        if right is not None and spec.right_log_scale:
            _set_log_ticks(
                right,
                np.concatenate(right_values),
                spec.right_log_floor,
                fixed_limits=spec.right_y_limits is not None,
            )
        if right is not None and spec.right_axis_unit_suffix:
            right.yaxis.set_major_formatter(
                _UnitSuffixFormatter(per_tick=spec.right_log_scale)
            )
        if right is not None:
            _style_right_axis(
                right,
                spec.right_y_label,
                show_labels=spec.show_right_axis_labels,
                color=right_axis_color,
                side=spec.right_panel_axis_side if panel else "right",
                own_panel=panel,
            )

        legend = _draw_legend(left, spec, handles, labels)

        # Last, once limits, ticks and legend are final: it measures the layout.
        # The final labels move as one group so their column stays aligned.
        _align_final_labels(final_labels)
        _keep_labels_inside_right_edge(
            fig,
            left,
            [[label] for label in spike_labels] + [final_labels],
        )
        _separate_final_labels(fig, final_labels)
        # After the labels, whose final positions it has to avoid.
        _choose_legend_location(fig, legend, spec.legend_location)

        for output in spec.outputs:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(path, bbox_inches="tight")
            print(f"Wrote {path.resolve()}")

        plt.close(fig)