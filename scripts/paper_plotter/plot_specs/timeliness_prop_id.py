from paper_plots import FigureSpec, SeriesSpec

PLOT = FigureSpec(
    series=(
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=165,
            property_id=580,
            label="Start time",
        ),
        SeriesSpec(
            table="metric_on_property_value_float",
            run_id=2399001,
            metric_id=165,
            property_id=570,
            label="Date of death",
        ),
    ),

    outputs=(
        "figures/age_insertion_per_prop.pdf",
        "figures/age_insertion_per_prop.svg",
    ),

    figsize=(14, 4.5),
    y_label="Metric value",

    year_tick_interval=1,
    show_year_lines=True,
    show_datapoint_lines=True,
)