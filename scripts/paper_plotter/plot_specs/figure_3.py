from paper_plots import FigureSpec, SeriesSpec

PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=109,
            label="Combined range constraint",
            label_spikes=True,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=1,
            spike_value_format=".3f",
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=121,
            label="Range constraint mandatory",
            label_spikes=True,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=2,
            spike_value_format=".3f",
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=108,
            label="Subject type constraint",
            label_spikes=True,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=6,
            spike_value_format=".3f",
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=122,
            label="Range constraint regular",
            label_spikes=True,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=2,
            spike_value_format=".3f",
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=123,
            label="Range constraint suggestion",
            label_spikes=True,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=1,
            spike_value_format=".3f",
        ),

        # Subject-count underlay:
        # inherits the color/style of "Subject type constraint"
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=77,
            label="Subject constraint statement count",
            axis="right",
            fill_alpha=0.16,
            background_for="Subject type constraint",
            show_in_legend=True,
        ),

        # Range-count underlay:
        # inherits the color/style of "Combined range constraint"
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=113,
            label="Range constraint statement count",
            axis="right",
            fill_alpha=0.16,
            background_for="Combined range constraint",
            show_in_legend=True,
        ),
    ),

    outputs=(
        "figures/figure_3.pdf",
        "figures/figure_3.svg",
    ),

    figsize=(8, 3.2),
    font_scale=1.3,

    y_label="Consistency value",
    y_limits=(0.984, 1.0),

    # Shared neutral scale for both count underlays.
    right_y_label="Statement count",
    right_axis_unit_suffix=True,

    year_tick_interval=2,
    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,

    show_legend=True,
    legend_location="lower left",
    legend_bbox=(0.04, 0.24),
    legend_ncol=1,

)