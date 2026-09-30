from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

PLOT = FigureSpec(
    series=(
        # SeriesSpec(
        #     table="metric_value_float",
        #     run_id=2516036,
        #     metric_id=193,
        #     label="Dominated reference mass", #maybe scale this so the three actual classes add up to one. Then this metric would be on the subset of classifiable domains.
        # ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2399001,
            metric_id=173,
            label="age of insertion",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=8,
            max_spike_labels=20,
            spike_value_format=".3f",
                        final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2560932,
            metric_id=221,
            label="completeness currency",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=8,
            max_spike_labels=20,
            spike_value_format=".3f",
                        final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=108,
            label="subject type constraint",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=5,
            spike_value_format=".3f",
                        final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2559236,
            metric_id=194,
            label="Dominating reference mass",
            final_label=True,
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2399000,
            metric_id=83,
            label="reference completeness",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=5,
            spike_value_format=".3f",
                        final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=1812093,
            metric_id=109,
            label="combined range constraint",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=1,
            spike_value_format=".3f",
                        final_label=True
        ),
        # SeriesSpec(
        #     table="metric_value_float",
        #     run_id=2575163,
        #     metric_id=91,
        #     label="schema compliance opt.",
        #     label_spikes=False,
        #     spike_min_prominence=0.0,
        #     spike_window=10,
        #     max_spike_labels=5,
        #     spike_value_format=".3f",
        # ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2575162,
            metric_id=90,
            label="schema compliance",
            label_spikes=False,
            spike_min_prominence=0.0,
            spike_window=7,
            max_spike_labels=5,
            spike_value_format=".3f",
            final_label=True
        ),
        SeriesSpec(
            table="metric_value_float",
            run_id=2504230,
            metric_id=70,
            label="statement count",
            axis="right",
            mode="underlay",
            fill_alpha=0.22,
            show_in_legend=True,
        ),
    ),

    outputs=(
        "figures/figure_2.pdf",
        # "figures/figure_2.svg",
    ),

    y_label="metric value",
    right_y_label="total statement count",

    # Same color for statement-count underlay + complete right y-axis.
    right_axis_color="#614770",

    figsize=(12, 4.5),
    font_scale=1.2,

    right_axis_unit_suffix=True,

    year_tick_interval=1,
    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,

    show_legend=True,
    legend_location="upper center",
    legend_bbox=(0.5, -0.07),
    legend_ncol=4,
)