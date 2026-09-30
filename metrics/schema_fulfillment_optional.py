from metrics.schema_fulfillment import SchemaFulfillment


class SchemaFulfillmentOptional(SchemaFulfillment):
    """
    All properties, regardless of whether optional or not, are regarded as required.
    """
    metric_name = "schema_fulfillment_optional"

    @staticmethod
    def is_row_compliant(row: tuple) -> bool | None:
        compliant = SchemaFulfillment.is_row_compliant(row)
        if compliant is None:
            return None
        return bool(compliant and row[0] >= 1)
