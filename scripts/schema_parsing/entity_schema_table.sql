DROP TABLE IF EXISTS entity_schemas;
DROP TABLE IF EXISTS schema_class_mapping;
DROP TABLE IF EXISTS entity_schema_labels;

CREATE TABLE entity_schemas (
  id SERIAL,
  entity_schema_id INT,
  property_id INT,
  min INT,
  max INT,
  value_constraint VARCHAR(2000),
  PRIMARY KEY (id)
);

\copy entity_schemas(entity_schema_id, property_id, min, max, value_constraint) FROM 'scripts/schema_parsing/simple_schema_rows.csv' DELIMITER ';' CSV HEADER;

CREATE INDEX IF NOT EXISTS idx_entity_schemas_class_property
ON entity_schemas(entity_schema_id, property_id);

CREATE TABLE schema_class_mapping (
  id SERIAL,
  class_id INT,
  entity_schema_id INT,
  PRIMARY KEY (id)
);

\copy schema_class_mapping(class_id, entity_schema_id) FROM 'scripts/schema_parsing/schema_class_mapping.csv' DELIMITER ',' CSV HEADER;

CREATE TABLE entity_schema_labels (
  entity_schema_id INT,
  schema_label VARCHAR(200),
  PRIMARY KEY (entity_schema_id)
);

\copy entity_schema_labels(entity_schema_id, schema_label) FROM 'scripts/schema_parsing/entity_schema_labels.csv' DELIMITER ';' CSV HEADER;