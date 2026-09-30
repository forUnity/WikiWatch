"""Figure 6 with the number of elicited preferences behind the number of concluded sessions.

The preference count is not saved to the database; it comes from the run log via
log_parsing/parse_preference_counts.py, which has to be run for the same run first, e.g.
    python log_parsing/parse_preference_counts.py \
        --log final_logs/additional_logs/reference_trust_flow_final_2559236.out
The last two timesteps have no usable edits and their count is unknown, so the fill ends early.
Note that the count is preference pairs (sum of |P_s|), not the number of sessions that yielded a
preference, so it can exceed the number of concluded sessions.
Everything else matches figure_6.py.
"""
from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

RUN_ID = 2559236
CSV_PATH = f"additional_data/preferences_run{RUN_ID}.csv"

PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=193,
            label="Dominated",
            label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=194,
            label="Dominating",
            label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=195,
            label="Contested",
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=196,
            label="Unchanged",
            label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        # Both underlays start at 0 on the same right axis. The preferences are drawn first as a
        # light background, the concluded sessions on top of them in a darker grey.
        SeriesSpec(
            csv_path=CSV_PATH,
            csv_column="preferences_step",
            label="Preferences elicited",
            axis="right",
            color="#BDBDBD",
            fill_alpha=0.35,
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=RUN_ID,
            metric_id=191,
            label="Sessions concluded", # step
            axis="right",
            color="#5F5F5F",
            fill_alpha=0.35,
        ),
    ),

    outputs=(
        "figures/figure_6_preferences.pdf",
    ),

    figsize=(14, 4.5),
    # Enlarge all text (labels, ticks, legend) by 30%; 1.0 = default sizes.
    font_scale=1.3,

    y_label="Fraction of total reference mass",
    right_y_label="Count per timestep",
    y_limits=(0.0, 1.0),
    # Right-axis ticks as 0, 0.5M, 1M, ... instead of a small "1e6" offset.
    right_axis_unit_suffix=True,
    # Like "best", but never in the middle of the plot.
    legend_location=("upper right", "upper left", "lower left"),

    year_tick_interval=1,
    # Label every second year only, so the enlarged labels do not crowd.
    skip_x_ticks=True,
    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,
)
