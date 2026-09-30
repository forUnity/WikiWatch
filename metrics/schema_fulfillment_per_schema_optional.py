from metrics.schema_fulfillment_optional import SchemaFulfillmentOptional
from metrics.schema_fulfillment_per_schema import SchemaFulfillmentPerSchema


class SchemaFulfillmentPerSchemaOptional(SchemaFulfillmentPerSchema):
    """
    All properties, regardless of whether optional or not, are regarded as required.
    """
    metric_name = "schema_fulfillment_per_schema_optional"

    # Same row check as the global optional metric
    is_row_compliant = staticmethod(SchemaFulfillmentOptional.is_row_compliant)
