# Runtime and Memory Figure Pipeline

This directory extracts runtime and memory measurements from SLURM `.out` logs, groups them according to an experiment specification, writes normalized CSV files, and generates paper-style runtime and memory figures.

The pipeline follows two main principles:

1. **Runtime and memory are configured independently.**
2. **Memory is stored as a profile instead of being collapsed into one number.**

This allows the pipeline to preserve and report process memory, DuckDB memory, Pympler-tracked metric structures, and derived non-DuckDB process memory separately.

## Workflow

```text
.out logs
   ↓
experiment spec
   ↓
parse_experiment.py
   ↓
*_runtime.csv
*_memory.csv
   ↓
plot_runtime_memory.py
   ↓
figures
```

The experiment spec defines:

- which `.out` files are used,
- which runtime metrics belong to each logical runtime group,
- how runtime metrics are combined,
- how timesteps are aligned across multiple runtime inputs,
- which `.out` files define memory groups,
- which metric snapshots contribute to tracked metric memory,
- which memory components are shown in the memory figure,
- and the complete plotting configuration for runtime and memory.

Paths inside the spec are resolved relative to the spec file.

---

## 1. Experiment specification

A spec contains exactly one:

```python
SPEC = ExperimentSpec(
    runtime_groups=(...),
    memory_groups=(...),

    plot=PlotSpec(
        runtime=FigurePlotSpec(...),
        memory=MemoryPlotSpec(...),
    ),
)
```

Runtime and memory do not need to use the same files or the same grouping structure.

For example, `subject` and `range` may be separate runtime groups while memory uses one combined `Consistency` run.

---

## 2. Runtime groups

Use `RuntimeInput(metrics=(...))` to select individual metric runtimes from one `.out` file.

```python
RuntimeGroup(
    key="subject_consistency",
    label="Subject consistency",
    inputs=(
        RuntimeInput(
            file=CONSISTENCY_SPEED,
            metrics=(
                "subject_constraint_violation_count",
                "statement_count_property_subject",
                "subject_constraint_violation_total",
                "subject_constraint_completeness",
                "subject_constraint_violation_ratio",
            ),
            metric_combine="sum",
        ),
    ),
)
```

Available metric runtime fields are:

```text
real_s
cpu_s
```

Selected metrics can be combined with:

```python
metric_combine="sum"
```

or:

```python
metric_combine="mean"
```

### Whole-timestep runtime

If `metrics` is omitted, a run-level runtime is used:

```python
RuntimeInput(
    file="../logs/example.out",
    run_value="runtime_total_s",
)
```

Available run-level values are:

```text
runtime_total_s
runtime_primary_s
iteration_total_s
```

---

## 3. Combining runtime inputs

A `RuntimeGroup` may contain multiple `RuntimeInput`s:

```python
RuntimeGroup(
    key="consistency",
    label="Consistency",
    inputs=(
        RuntimeInput(file="subject.out"),
        RuntimeInput(file="range.out"),
    ),
    combine="sum",
    alignment="intersection",
)
```

`combine` may be:

```text
sum
mean
```

Timesteps are aligned using their actual `(start, end)` interval, not their timestep index.

### Strict alignment

```python
alignment="strict"
```

All inputs must contain exactly the same recorded timesteps. Otherwise parsing stops with an error.

### Intersection alignment

```python
alignment="intersection"
```

Only timesteps present in every input are written.

Missing timesteps are never replaced with zero.

---

## 4. Missing-value semantics

The parser distinguishes real zero values from missing data.

```text
recorded timestep + measured value 0
    -> write 0

timestep not recorded or not completed
    -> write no row

completed memory timestep but one measurement is missing
    -> keep the row and leave that field blank

selected runtime metric missing for a timestep
    -> do not create a fake 0 measurement
```

Incomplete timesteps at the end of truncated logs are not written.

---

## 5. Memory groups

A memory group no longer selects one single memory value.

Instead, every completed timestep is written as a full memory profile.

```python
MemoryGroup(
    key="timeliness",
    label="Timeliness",
    file=TIMELINESS_MEMORY,
    tracked_metrics=(
        "data_age_at_insertion",
    ),
)
```

`tracked_metrics` only controls the Pympler-derived `tracked_metric_gib` column.

### Explicit tracked metrics

```python
tracked_metrics=(
    "data_age_at_insertion",
)
```

The configured metric snapshots are combined for each timestep.

If one requested snapshot is missing, `tracked_metric_gib` is left blank for that timestep.

### All tracked metrics

```python
tracked_metrics=None
```

All Pympler snapshots present in the timestep are used.

The combination method can be controlled with:

```python
tracked_combine="sum"
```

or:

```python
tracked_combine="mean"
```

---

## 6. Memory measurements

The generated memory CSV contains the following quantities.

### `tracked_metric_gib`

Pympler-tracked Python structures belonging to the configured metric or metrics.

Source in the log:

```text
[data_age_at_insertion] Memory snapshot — total: ... MB
```

This is metric-specific, but only represents objects explicitly tracked by the memory tracker. It is not the complete process memory caused by the metric.

### `duckdb_after_load_gib`

DuckDB `memory_usage` recorded around the timestep data-loading phase.

Existing logs contain this measurement.

### `duckdb_end_gib`

Optional DuckDB memory measurement recorded at the end of the timestep.

Existing logs usually do not contain this value, so it remains blank.

### `duckdb_decomposition_gib`

DuckDB value used for the process-memory decomposition.

The parser chooses:

```text
duckdb_end_gib
```

when available, otherwise:

```text
duckdb_after_load_gib
```

### `duckdb_measurement_point`

Records which DuckDB value was used:

```text
end_timestep
after_load
```

### `process_current_gib`

Current process memory at the end of the timestep.

Source:

```text
Memory usage (end of timestep, current): ... GiB
```

### `process_peak_gib`

Peak process memory reported at the end of the timestep.

Source:

```text
Memory usage (end of timestep, peak): ... GiB
```

### `non_duckdb_process_gib`

Derived as:

```text
process_current_gib - duckdb_decomposition_gib
```

This should be interpreted as **non-DuckDB process memory**, not as metric memory.

It may include Python interpreter memory, dependencies, caches, temporary structures, allocator overhead, and other process state.

If the subtraction would be negative, the value is left blank instead of silently being converted to zero. This can happen with older logs because the DuckDB and process measurements were not sampled at exactly the same moment.

---

## 7. Memory CSV schema

The memory CSV contains one row per completed timestep:

```text
group_key
label
timestep_index
start
end
tracked_metric_gib
tracked_metrics
duckdb_after_load_gib
duckdb_end_gib
duckdb_decomposition_gib
duckdb_measurement_point
process_current_gib
process_peak_gib
non_duckdb_process_gib
source_file
```

---

## 8. Parse the logs

Run:

```bash
python parse_experiment.py \
    --spec specs/final_metrics.py \
    --outdir timings \
    --prefix final
```

This produces:

```text
timings/final_runtime.csv
timings/final_memory.csv
```

Useful sanity checks:

```bash
head -3 timings/final_runtime.csv
head -3 timings/final_memory.csv
```

---

## 9. Runtime figure configuration

Runtime has its own complete `FigurePlotSpec`.

```python
runtime=FigurePlotSpec(
    figsize=(14, 4.5),
    font_scale=1.3,

    title=None,
    x_label=None,
    y_label="Runtime per timestep (s)",

    year_tick_interval=1,
    skip_x_ticks=True,
    x_limits=None,

    y_scale="log",
    y_limits=None,

    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,

    show_grid=True,

    show_legend=True,
    legend_location="lower right",
    legend_bbox=None,
    legend_ncol=3,

    linewidth=1.6,

    output_stem="runtime",
    formats=("pdf", "png"),
)
```

Runtime can independently control figure size, font size, labels, x/y limits, scale, guide lines, grid, legend placement, line width, filename, and output formats.

---

## 10. Memory figure configuration

Memory has its own `MemoryPlotSpec`.

In addition to the normal figure settings, it contains a list of memory components to render.

```python
memory=MemoryPlotSpec(
    figsize=(12, 4.5),
    font_scale=1.3,

    y_label="Memory usage (GiB)",

    year_tick_interval=1,
    skip_x_ticks=True,

    y_scale="linear",

    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=False,

    show_grid=True,

    show_legend=True,
    legend_location="upper right",
    legend_ncol=2,

    linewidth=1.6,

    output_stem="memory",
    formats=("pdf", "png"),

    series=(
        MemorySeriesSpec(
            value="duckdb_decomposition_gib",
            mode="underlay",
            label="DuckDB memory",
            fill_alpha=0.20,
            show_in_legend=True,
        ),

        MemorySeriesSpec(
            value="process_current_gib",
            mode="line",
        ),
    ),
)
```

The recommended paper view is:

```text
colored line:
    process_current_gib

grey filled underlay:
    duckdb_decomposition_gib
```

---

## 11. Memory series

Available values are:

```text
tracked_metric_gib
duckdb_after_load_gib
duckdb_end_gib
duckdb_decomposition_gib
process_current_gib
process_peak_gib
non_duckdb_process_gib
```

Available rendering modes are:

```text
line
underlay
```

### DuckDB underlay

```python
MemorySeriesSpec(
    value="duckdb_decomposition_gib",
    mode="underlay",
    label="DuckDB memory",
    fill_alpha=0.20,
)
```

Underlays are drawn before lines and use grey shades by default.

### Tracked metric structures

```python
MemorySeriesSpec(
    value="tracked_metric_gib",
    mode="line",
    label="Tracked metric structures",
    linestyle="--",
    linewidth=1.2,
)
```

### Peak process memory

```python
MemorySeriesSpec(
    value="process_peak_gib",
    mode="line",
    label="Peak process memory",
    linestyle=":",
    linewidth=1.2,
)
```

### Non-DuckDB process memory

```python
MemorySeriesSpec(
    value="non_duckdb_process_gib",
    mode="line",
    label="Non-DuckDB process memory",
    linestyle="--",
)
```

A memory component can be restricted to selected groups:

```python
MemorySeriesSpec(
    value="tracked_metric_gib",
    mode="line",
    groups=("timeliness",),
)
```

---

## 12. Generate the figures

Run:

```bash
python plot_runtime_memory.py \
    --spec specs/final_metrics.py \
    --runtime-csv timings/final_runtime.csv \
    --memory-csv timings/final_memory.csv \
    --outdir figures \
    --reset-colors
```

Typical outputs:

```text
figures/runtime.pdf
figures/runtime.png
figures/memory.pdf
figures/memory.png
```

---

## 13. Plot only one figure

Runtime only:

```bash
python plot_runtime_memory.py \
    --spec specs/final_metrics.py \
    --runtime-csv timings/final_runtime.csv \
    --memory-csv timings/final_memory.csv \
    --outdir figures \
    --only runtime
```

Memory only:

```bash
python plot_runtime_memory.py \
    --spec specs/final_metrics.py \
    --runtime-csv timings/final_runtime.csv \
    --memory-csv timings/final_memory.csv \
    --outdir figures \
    --only memory
```

---

## 14. Select specific groups

```bash
python plot_runtime_memory.py \
    --spec specs/final_metrics.py \
    --runtime-csv timings/final_runtime.csv \
    --memory-csv timings/final_memory.csv \
    --outdir figures \
    --select timeliness consistency
```

Selection uses the `key` values defined in the experiment spec.

---

## 15. Style consistency

The plotter uses the same paper-oriented visual conventions as `paper_plotter`, including a colorblind-friendly palette, consistent typography, guide lines, frameless legends, and PDF-friendly font embedding.

Logical groups receive persistent color/linestyle assignments stored in:

```text
figures/color_assignments.json
```

The same `group_key` keeps the same visual style across runtime and memory figures.

To recreate assignments:

```bash
--reset-colors
```

To force a specific group order:

```bash
--order group_a group_b group_c
```

---

## 16. Legend placement

Runtime and memory legends are configured independently.

Inside the plot:

```python
legend_location="lower right"
legend_bbox=None
legend_ncol=3
```

Outside the plot:

```python
legend_location="upper left"
legend_bbox=(1.01, 1.0)
legend_ncol=1
```

---

## 17. Linear vs log memory scale

A linear scale is recommended for the memory figure when using a filled DuckDB underlay:

```python
y_scale="linear"
```

Log scale is also supported:

```python
y_scale="log"
```

On a log scale, the underlay starts at the smallest positive plotting floor instead of zero.

---

## 18. Existing and future DuckDB logs

Existing logs already contain enough information to plot DuckDB memory.

Their DuckDB value is interpreted as:

```text
duckdb_after_load_gib
```

The pipeline therefore works with existing runs immediately.

Future logging can additionally provide:

```text
duckdb_end_gib
```

When available, that value is preferred for the process-memory decomposition because it is sampled closer in time to `process_current_gib`.

---

## 19. Interpretation notes

Recommended terminology:

```text
Tracked metric structures
DuckDB memory
Process memory
Peak process memory
Non-DuckDB process memory
```

Avoid calling:

```text
process_current_gib - duckdb_decomposition_gib
```

"metric memory".

It represents all non-DuckDB process memory.

Likewise, `tracked_metric_gib` only represents objects explicitly included in the Pympler memory snapshots.

---

## Recommended paper configuration

For the main paper memory figure, the clearest default is:

```text
Process memory
    colored line

DuckDB memory
    grey filled underlay
```

Optional diagnostic components can be enabled without reparsing the logs:

```text
Tracked metric structures
Peak process memory
Non-DuckDB process memory
```

The intended separation is:

```text
parse_experiment.py
    extracts and preserves measurements

plot_runtime_memory.py
    decides which measurements to visualize
```