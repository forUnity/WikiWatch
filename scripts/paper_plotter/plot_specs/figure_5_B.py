from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

# how this figure was created:
# 1. use the metric_list script to find metrics for the timeliness run
# 2. For the days on property metric, list the properties in the using the corresponding metric. Parse their names. Select properties of interest from there. Add to plot. 
PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=223,
            class_id=5,
            label="Human", 
            final_label=True
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=225,
            class_id=5,
            label="Statement count for Human",
            axis="right",
            background_for="Human",
            fill_alpha=0.05,
        ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=223,
        #     class_id=41176,
        #     label="Building",
        #     final_label=True
        # ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=225,
        #     class_id=41176,
        #     label="Statement count for Building",
        #     axis="right",
        #     background_for="Building",
        #     fill_alpha=0.05,
        # ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=223,
            class_id=13442814,
            label="Scholarly article",
            final_label=True
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=225,
            class_id=13442814,
            label="Statement count for Scholarly article",
            axis="right",
            background_for="Scholarly article",
            fill_alpha=0.05,
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=223,
            class_id=2020153,
            label="Academic conference",
            final_label=True
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=225,
            class_id=2020153,
            label="Statement count for Academic conference",
            axis="right",
            background_for="Academic conference",
            fill_alpha=0.05,
        ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=223,
        #     class_id=24050099,
        #     label="Auto race",
        #     final_label=True
        # ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=225,
        #     class_id=24050099,
        #     label="Statement count for Auto race",
        #     axis="right",
        #     background_for="Auto race",
        #     fill_alpha=0.05,
        # ),

        # SeriesSpec( # NOTE: avg delay 2,6years someone added 100k days then never again (5y after creation (2018)) so the avg is just avg of filter.
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=223,
        #     class_id=47150325,
        #     label="Calendar day of a given year",
        #     final_label=True
        # ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=225,
        #     class_id=47150325,
        #     label="Statement count for Calendar day of a given year",
        #     axis="right",
        #     background_for="Calendar day of a given year",
        #     fill_alpha=0.05,
        # ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=223,
            class_id=3464665,
            label="Television series season",
            final_label=True
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=225,
            class_id=3464665,
            label="Statement count for Television series season",
            axis="right",
            background_for="Television series season",
            fill_alpha=0.05,
        ),
        SeriesSpec( #NOTE: there is ony big import and then also by script?
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=223,
            class_id=30612,
            label="Clinical trial",
            final_label=True
        ),
        SeriesSpec(
            table="metric_on_class_value_float",
            run_id=2560932,
            metric_id=225,
            class_id=30612,
            label="Statement count for Clinical trial",
            axis="right",
            background_for="Clinical trial",
            fill_alpha=0.05,
        ),
        # SeriesSpec( #TO few entities.
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=223,
        #     class_id=9135,
        #     label="Operating system",
        #     final_label=True
        # ),
        # SeriesSpec(
        #     table="metric_on_class_value_float",
        #     run_id=2560932,
        #     metric_id=225,
        #     class_id=9135,
        #     label="Statement count for Operating system",
        #     axis="right",
        #     background_for="Operating system",
        #     fill_alpha=0.05,
        # ),
       
    ),

    outputs=(
        "figures/figure_5b.pdf",
    ),

    figsize=(14, 2.8),
    # Enlarge all text (labels, ticks, legend) by 30%; 1.0 = default sizes.
    font_scale=1.3,

    y_label="Average\ncompleteness\ndelay (years)",
    # Wrapped: the label has to fit the height of the lower panel.
    right_y_label="Entities\nper class",
    # No y_limits: the left axis fits the delay in days, starting at 0.
    # Right-axis ticks as 0, 0.5M, 1M, ... instead of a small "1e6" offset.
    right_axis_unit_suffix=True,
    # Stack the four counts (spec order, bottom to top); the top is their sum.
    # stack_right_series=True,
    right_log_scale=True,
    # The axis ends just below 100; the small counts at the start are cut off.
    right_log_floor=100,
    # Counts in their own panel below the delays instead of behind them.
    right_axis_panel=True,
    # Axis of the count panel on the left, in one column with the delay axis.
    right_panel_axis_side="left",
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