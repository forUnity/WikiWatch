# TODO! Update with new run!
from scripts.paper_plotter.paper_plots import FigureSpec, SeriesSpec

# 92  schema_fulfillment_per_schema           metric_on_entity_schema_value_float
# 93  schema_fulfillment_per_schema_optional  metric_on_entity_schema_value_float

PLOT = FigureSpec(
    series=(
        # Schema 10
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=10,
            label="Human",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=10,
        #     label="Human optional",
        # ),

        # Schema 35
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=35,
            label="Written Work",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=35,
        #     label="Written Work optional",
        # ),

        # Schema 112
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=112,
            label="Dataset",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=112,
        #     label="Dataset optional",
        # ),

        # Schema 178
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=178,
            label="Algorithm",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=178,
        #     label="Algorithm optional",
        # ),

        # Schema 270
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=270,
            label="Building",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=270,
        #     label="Building optional",
        # ),

        # Schema 446
        SeriesSpec(
            table="metric_on_entity_schema_value_float",
            run_id=2587637,
            metric_id=92,
            entity_schema_id=446,
            label="Collection",
        ),
        # SeriesSpec(
        #     table="metric_on_entity_schema_value_float",
        #     run_id=2587638,
        #     metric_id=93,
        #     entity_schema_id=446,
        #     label="Collection optional",
        # ),
    ),

    outputs=(
        "figures/figure_4.pdf",
        "figures/figure_4.svg",
    ),

    figsize=(14, 4.5),
    font_scale=1.8,

    y_label="Schema fulfillment",

    year_tick_interval=1,
    skip_x_ticks=True,

    show_year_lines=True,
    year_line_alpha=0.30,

    show_datapoint_lines=True,
    datapoint_line_alpha=0.07,

    show_legend=True,
    legend_location="upper center",
    legend_bbox=(0.5, -0.15),
    legend_ncol=4,
)