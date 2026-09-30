from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

# how this figure was created:
# 1. use the metric_list script to find metrics for the timeliness run
# 2. For the days on property metric, list the properties in the using the corresponding metric. Parse their names. Select properties of interest from there. Add to plot. 
PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=1812093,
            metric_id=96,
            property_id=19 ,
            label="Place of birth", 
            final_label=True
        ),
        SeriesSpec( #TODO: add back
            table="metric_on_property_value_float",
            run_id=1812093,
            metric_id=96,
            property_id=50 ,
            label="author", 
                        final_label=True
        ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=1812093,
        #     metric_id=96,
        #     property_id=218,
        #     label="ISO 639-1 code", 
        #     final_label=True
        # ), 
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=1812093,
            metric_id=96,
            property_id=2671,
            label="Google Knowledge Graph ID", 
            final_label=True
        ), 
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=1812093,
            metric_id=96,
            property_id=646,
            label="Freebase ID", 
            final_label=True
        ), 
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=1812093,
            metric_id=96,
            property_id=4101,
            label="thesis submitted to", 
            final_label=True
        ), 
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=1812093,
        #     metric_id=96,
        #     property_id=570,
        #     label="Statement count for Date Of Death",
        #     axis="right",
        #     background_for="Date Of Death",
        #     fill_alpha=0.05,
        # ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=1812093,
        #     metric_id=96,
        #     property_id=582,
        #     label="Statement count for End Time",
        #     axis="right",
        #     background_for="End Time",
        #     fill_alpha=0.07,
        # ),
        # SeriesSpec( #TODO: add back OR REPORT.
        #     table="metric_on_property_value_float",
        #     run_id=1812093,
        #     metric_id=96,
        #     property_id=577,
        #     label="Statement count for publication date",
        #     axis="right",
        #     background_for="publication date",
        #     fill_alpha=0.04,
        # ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=1812093,
        #     metric_id=96,
        #     property_id=585,
        #     label="Statement count for point in time",
        #     axis="right",
        #     background_for="point in time",
        #     fill_alpha=0.06,
        # ),
    ),

    outputs=(
        "figures/figure_3.1.pdf",
    ),

    figsize=(14, 4.5),
    # Enlarge all text (labels, ticks, legend) by 30%; 1.0 = default sizes.
    font_scale=1.3,

    y_label="number of suject type violations",
    # Wrapped: the label has to fit the height of the lower panel.
    right_y_label="Statements\nper property",
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