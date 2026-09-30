from paper_plots import FigureSpec, SeriesSpec

PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=193,
            label="Dominated", #maybe scale this so the three actual classes add up to one. Then this metric would be on the subset of classifiable domains.
                        label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=194,
            label="Dominating",
                        label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=195,
            label="Contested",
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=196,
            label="Unchanged",
            label_spikes=False,
            spike_min_prominence=0.05,
            max_spike_labels=3,
            final_label=True
        ),
        # SeriesSpec( # NOTE: Total evidence is integral of these changes.
        #     table="metric_value_float",
        #     run_id=2559236,
        #     metric_id=192,
        #     label="Number of sessions closed", # total
        #     axis="right",
        #     show_in_legend=False,
        # ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=191,
            label="Number of sessions closed", # step
            axis="right",
            show_in_legend=False,
        ),
    ),

    outputs=(
        "figures/figure_6.pdf",
    ),

    # figsize=(6, 3),
    figsize=(14, 2.5),
    # Enlarge all text (labels, ticks, legend) by 30%; 1.0 = default sizes.
    font_scale=1.3,

    y_label="Fraction of\ntotal reference mass",
    right_y_label="Number of sessions closed",
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
    legend_ncol=2,
)