# Paper Plotter

Small plotting utility for WikiWatch paper figures.

## Install

```bash
python -m pip install -r requirements.txt
cp .env.example .env
# fill DB_USER / DB_PASS / DB_NAME
```

## Render a figure

```bash
python -m paper_plots plot_specs/example_figure.py
```

To render every spec in `plot_specs/` at once, run `python plot_all.py`. A
failing spec is reported at the end; the others still render.

Each series is identified by `(table, run_id, metric_id)`. Requested pairs from
the same table are fetched in one SQL query.

## Aggregate over properties

For tables with a `property_id` column (e.g. `metric_on_property_value_float`),
`property_id` can be a list. The values are then aggregated per timestamp with
`aggregation_method` (`"SUM"` or `"AVG"`), which is required in that case.
An empty list aggregates all properties of the run and metric.

```python
SeriesSpec(
    table="metric_on_property_value_float",
    run_id=2399001,
    metric_id=172,
    property_id=[],          # or e.g. [569, 570]
    aggregation_method="SUM",
)
```

The aggregate uses the rows present at each timestamp (a plain, unweighted
SQL `SUM`/`AVG`). A listed id without any data raises an error.

## Series from a CSV file

A series can read a column of a CSV file instead of the database. The file
needs a `timestamp` column (tz-naive timestamps are taken as UTC); `run_id` and
`metric_id` are then not needed. The path is relative to the working directory,
like the outputs. Empty values stay gaps in the line.

```python
SeriesSpec(
    csv_path="data/class_fractions_without_run2516036.csv",
    csv_column="dominated_without",
    label="Dominated",
)
```

CSV and database series can be mixed in one figure; each file is read once.

## Secondary-axis underlay

A series on `axis="right"` is rendered as a grey filled area only.

```python
SeriesSpec(
    run_id=1800355,
    metric_id=70,
    label="Statement count",
    axis="right",
    mode="underlay",
    fill_alpha=0.22,
    show_in_legend=False,
)
```

`show_in_legend=False` hides only this series' legend entry.

### Background for a specific line

When each right-axis series belongs to one left-axis line (e.g. a statement
count behind an average delay), link them with `background_for`:

```python
SeriesSpec(..., label="Date Of Death"),
SeriesSpec(..., axis="right", background_for="Date Of Death"),
```

The underlay then uses a pale tint of that line's colour, gets a thin edge in
the line's dash pattern, and shares the line's legend entry (swatch + line)
instead of adding its own. The value must match exactly one left-axis label.
Series without `background_for` are unaffected.

To stack all right-axis underlays in series order (bottom to top) instead of
overlapping them, set `stack_right_series=True` on `FigureSpec`. The top of
the stack is their total; a missing timestamp counts as 0. `label_spikes`
and `final_label` cannot be combined with stacking on right-axis series.

To hide the numeric tick labels and title on the right y-axis while keeping the
grey axis line and tick marks, set this on `FigureSpec`:

```python
FigureSpec(
    ...,
    show_right_axis_labels=False,
)
```

To write the right-axis ticks as `0, 0.5M, 1M, 1.5M` instead of plain numbers
with a small `1e6` offset above the axis, set:

```python
FigureSpec(
    ...,
    right_axis_unit_suffix=True,
)
```

One unit (`k`, `M` or `B`) is chosen for the whole axis from the largest
visible tick.

For a logarithmic right axis set `right_log_scale=True`. The range fits the
data automatically; with `right_y_limits` the lower limit must be > 0. It can
be combined with `right_axis_unit_suffix`; each tick then gets its own unit
(`1k`, `10k`, `100k`, `1M`). Axes whose ticks stay below 1000 are left unchanged.
The log axis labels round values from its floor to the top of the data:
every decade (every n-th if there would be more than five), or the 1-3 or
1-2-5 grid if the data spans too few decades for three ticks. The top tick is
the largest one not above the data. By default the floor is the smallest
value. A few small values, e.g. at the start of a run, would then stretch
the axis far down; set a floor to cut them off at the x-axis instead:

```python
FigureSpec(
    ...,
    right_log_scale=True,
    right_log_floor=100,    # axis ends just below 100
)
```

With `right_y_limits` (not combinable with `right_log_floor`) the limits are
kept and the ticks lie within them.

### Right-axis series in their own panel

When the underlays clutter the plot, draw them in a smaller panel below it
instead, sharing the x-axis (like a price-volume chart):

```python
FigureSpec(
    ...,
    right_axis_panel=True,
    right_panel_height_ratio=0.4,    # panel height relative to the main plot
    right_panel_axis_side="right",   # or "left"
)
```

All other right-axis settings (`right_log_scale`, `right_y_limits`,
`right_axis_unit_suffix`, `right_y_label`, `right_axis_color`,
`stack_right_series`, `background_for`, value labels) apply to the panel.
`figsize` is the size of the whole figure, both panels together. The panel is
lower than the plot, so a long `right_y_label` may need a `"\n"`. The option is
off by default and has no effect on figures without right-axis series.

## Legend position

`legend_location` takes any matplotlib location. A tuple of locations limits
`"best"` to those, e.g. to keep the legend out of the middle of the plot:

```python
FigureSpec(
    ...,
    legend_location=("upper right", "upper left", "lower left"),
)
```

The legend goes to the listed location that covers the least (same score as
`"best"`, taken after the value labels are placed).

## Font size

`font_scale` on `FigureSpec` multiplies every font size (axis labels, tick
labels, legend, title and spike labels). The default is `1.0`.

```python
FigureSpec(
    ...,
    font_scale=1.3,
)
```

If the enlarged x-axis labels get crowded, `skip_x_ticks=True` labels only
every second x tick (1st, 3rd, ...). All tick marks and year lines are kept.

## Label spike values

```python
SeriesSpec(
    run_id=1800355,
    metric_id=70,
    axis="right",
    label_spikes=True,
    spike_min_prominence=0.0,
    spike_window=3,
    max_spike_labels=6,
    spike_value_format=",.0f",
)
```

Spike detection uses a neighbourhood of `spike_window` timesteps on each side,
so broad peaks are detected as well as one-timestep spikes. Nearby candidates
are de-duplicated so one broad peak gets one value label.

`spike_min_prominence` is relative to the full value range. Leave it at `0.0`
to label the strongest local peaks, or increase it (for example to `0.05`) to
ignore smaller peaks.

`spike_value_format` accepts `",.0f"`, `".2f"`, `":.2f"`, `"{:.2f}"`, and
`"%.2f"`.

### Label the final value

`final_label=True` adds a label with the value `final_timestep_minus`
timesteps before the last one (default `1`, the next-to-last timestep; `0` is
the last timestep). It works with or without `label_spikes`:

```python
SeriesSpec(
    ...,
    final_label=True,
    final_timestep_minus=1,
)
```

The label uses `spike_value_format` and is placed above or below its point by
the same rule as spike labels (using `spike_window`). If that timestep has no
value, no final label is drawn.

All final labels of a figure (left and right axis) are centred in one column
at the latest final timestep, each at its own value. A label whose point is
earlier, e.g. with a larger `final_timestep_minus`, therefore sits slightly to
the right of its point.

A value label that would cross the right edge of the plot is moved left just
far enough to end before the axis; labels that fit stay where they are. The
final labels move together, by what the widest one needs, so the column stays
aligned.

Together with `label_spikes=True`, the final label does not count towards
`max_spike_labels`. Spike labels within `spike_window` of it are dropped in its
favour, and the next-strongest spikes take their place.
