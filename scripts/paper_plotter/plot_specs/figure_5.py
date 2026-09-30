from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

# how this figure was created:
# 1. use the metric_list script to find metrics for the timeliness run
# 2. For the days on property metric, list the properties in the using the corresponding metric. Parse their names. Select properties of interest from there. Add to plot. 
PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=166,
            property_id=570 ,
            label="Date Of Death", 
            final_label=True
        ),
                SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=166,
            property_id=585,
            label="Point In Time", 
                        final_label=True
        ), 

        SeriesSpec( #TODO: add back
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=166,
            property_id=577 ,
            label="Publication Date", 
                        final_label=True
        ),
        # TODO: add more properties if they have enough statements
        #  python scripts/list_written_properties.py --run-id 2399001 --metric-id 166
        #  python scripts/inspect_age_at_insertion_property.py --run-id 2399001 --property Pxxx
        # not date of brith - this has to few on avg
        # inception : mostly historic "time entitiy begins to exist (e.g. dracula birthdates but also church 14th centory ...)". But interessting for recent ones?
        # point in time : has many with many future and may too late. Sounds the most general.
        # start time : also has many with many future and may too late. 
        # SeriesSpec( #NOTE: inception is really mostly too late, leaving few statements to base the average on. Report this as well.
        #     table="metric_on_property_value_float",
        #     run_id=2399001,
        #     metric_id=166,
        #     property_id=571 ,
        #     label="inception", 
        # ), 
        SeriesSpec(#NOTE: Too historical and way too few
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=166,
            property_id=580,
            label="Start Time", 
        ), 
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=166,
            property_id=582 ,
            label="End Time", 
                        final_label=True
        ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=2399001,
        #     metric_id=166,
        #     property_id=575 ,
        #     label="time of discovery or invention", # sounds quite historic. # -> also looks random - i.e. historic because most likely lie outside time window and are filtered.
        # ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=2399001,
        #     metric_id=171,
        #     property_id=[570, 582, 6949, 577],# 575],
        #     aggregation_method="SUM",
        #     label="Sum of statement counts for the 4 properties",
        #     axis="right",
        #     show_in_legend=False,
        # ),
        # data_age_at_insertion_statement_count per property:
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=171,
            property_id=570,
            label="Statement count for Date Of Death",
            axis="right",
            background_for="Date Of Death",
            fill_alpha=0.05,
        ),
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=171,
            property_id=582,
            label="Statement count for End time",
            axis="right",
            background_for="End Time",
            fill_alpha=0.07,
        ),
        # SeriesSpec( # rmv because it has to few statments (avg 40) 
        #     table="metric_on_property_value_float",
        #     run_id=2399001,
        #     metric_id=171,
        #     property_id=6949,
        #     label="Statement count for announcement date",
        #     axis="right",
        #     background_for="announcement date",
        # ),
        SeriesSpec( #TODO: add back OR REPORT.
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=171,
            property_id=577,
            label="Statement count for publication date",
            axis="right",
            background_for="Publication Date",
            fill_alpha=0.04,
        ),
        # SeriesSpec(
        #     table="metric_on_property_value_float",
        #     run_id=2399001,
        #     metric_id=171,
        #     property_id=571,
        #     label="Statement count for inception",
        #     axis="right",
        #     background_for="inception",
        # ),
        SeriesSpec( #NOTE: Too historical and way too few
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=171,
            property_id=580,
            label="Statement count for start time",
            axis="right",
            background_for="Start Time",
            fill_alpha=0.04,
        ),
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=171,
            property_id=585,
            label="Statement count for point in time",
            axis="right",
            background_for="Point In Time",
            fill_alpha=0.06,
        ),
    ),

    outputs=(
        "figures/figure_5_w_start.pdf",
    ),

    figsize=(14, 3),
    # Enlarge all text (labels, ticks, legend) by 30%; 1.0 = default sizes.
    font_scale=1.3,

    y_label="Average insertion\n delay (days)",
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
    #legend_location=("upper right", "upper left", "lower left"),

    year_tick_interval=1,
    # Label every second year only, so the enlarged labels do not crowd.
    skip_x_ticks=True,
    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,

    show_legend=True,
    legend_location="upper right",
    #legend_bbox=(0.5, -0.07),
    legend_ncol=5,
)