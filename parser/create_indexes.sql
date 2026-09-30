-- SQL script that is executed post parsing to create the indexes on the tables

CREATE INDEX IF NOT EXISTS idx_value_change_timestamp_btree
ON value_change (timestamp);

CREATE INDEX IF NOT EXISTS idx_reference_change_timestamp_btree
ON reference_change (timestamp);

CREATE INDEX IF NOT EXISTS idx_sitelink_change_timestamp_btree
ON sitelink_change (timestamp);

CREATE INDEX IF NOT EXISTS idx_revision_timestamp_btree
ON revision (timestamp);

CLUSTER value_change USING idx_value_change_timestamp_btree;
CLUSTER reference_change USING idx_reference_change_timestamp_btree;
CLUSTER sitelink_change USING idx_sitelink_change_timestamp_btree;
CLUSTER revision USING idx_revision_timestamp_btree;

ANALYZE value_change;
ANALYZE reference_change;
ANALYZE sitelink_change;
ANALYZE revision;
