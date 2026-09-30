"""Figure 6 with some domains left out of the class fractions.

The fractions come from scripts/reference_classification_breakdown.py, which has to be run for the
same run first and its CSV copied to CSV_PATH, e.g.
    python scripts/reference_classification_breakdown.py --run-id 2516036 --exclude wikipedia.org \
        --out-dir log_parsing/paper_plotter/data
Everything else matches figure_6.py.
"""
from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

RUN_ID = 2559236
CSV_PATH = f"additional_data/class_fractions_without_3.csv"

PLOT = FigureSpec(
    series=(
        SeriesSpec(
            csv_path=CSV_PATH,
            csv_column="dominated_without",
            label="Dominated",
            label_spikes=True,
            spike_min_prominence=0.05,
            max_spike_labels=6,
            final_label=True
        ),
        SeriesSpec(
            csv_path=CSV_PATH,
            csv_column="dominating_without",
            label="Dominating",
            label_spikes=True,
            spike_min_prominence=0.05,
            max_spike_labels=6,
            final_label=True
        ),
        SeriesSpec(
            csv_path=CSV_PATH,
            csv_column="contested_without",
            label="Contested",
            final_label=True
        ),
        SeriesSpec(
            csv_path=CSV_PATH,
            csv_column="unclassified_without",
            label="Unchanged",
            label_spikes=True,
            spike_min_prominence=0.05,
            max_spike_labels=6,
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=192,
            label="Number of sessions closed",
            axis="right",
            show_in_legend=False,
        ),
    ),

    outputs=(
        "figures/figure_6_without.pdf",
    ),

    figsize=(14, 4.5),
    font_scale=1.3,

    y_label="Fraction of total reference mass",
    right_y_label="Number of sessions closed",
    y_limits=(0.0, 1.0),
    right_axis_unit_suffix=True,
    # Like "best", but never in the middle of the plot.
    legend_location=("upper right", "upper left", "lower left"),

    year_tick_interval=1,
    skip_x_ticks=True,
    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,
)
