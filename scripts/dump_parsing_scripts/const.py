WIKIDATA_SERVICE_URL = "https://dumps.wikimedia.org/wikidatawiki/20250601/"

# Paths
DOWNLOAD_LINKS_FILE_PATH = 'data/xml_download_links.txt'
PROCESSED_FILES_PATH = 'logs/processed_files.txt'
PARSER_LOG_FILES_PATH = 'logs/parser_log_files.json'
ERROR_REVISION_TEXT_PATH = "logs/error_revision_text.txt"
REVISION_NO_CLAIMS_TEXT_PATH = "logs/revision_no_claims.txt"

CREATE_PROPERTY = "CREATE_PROPERTY"
CREATE_PROPERTY_VALUE = "CREATE_PROPERTY_VALUE"
CREATE_ENTITY = "CREATE_ENTITY"
UPDATE_PROPERTY_VALUE = "UPDATE_PROPERTY_VALUE"
UPDATE_PROPERTY_DATATYPE_METADATA = "UPDATE_PROPERTY_DATATYPE_METADATA"
DELETE_PROPERTY = "DELETE_PROPERTY"
DELETE_PROPERTY_VALUE = "DELETE_PROPERTY_VALUE"
UPDATE_RANK = "UPDATE_RANK"
CREATE_QUALIFIER = "CREATE_QUALIFIER"
DELETE_QUALIFIER = "DELETE_QUALIFIER"
CREATE_QUALIFIER_VALUE = "CREATE_QUALIFIER_VALUE"
DELETE_QUALIFIER_VALUE = "DELETE_QUALIFIER_VALUE"
CREATE_REFERENCE = "CREATE_REFERENCE"
DELETE_REFERENCE = "DELETE_REFERENCE"
DELETE_REFERENCE_VALUE = "DELETE_REFERENCE_VALUE"
CREATE_REFERENCE_VALUE = "CREATE_REFERENCE_VALUE"
CREATE_SITELINK = "CREATE_SITELINK"
UPDATE_SITELINK = "UPDATE_SITELINK"
DELETE_SITELINK = "DELETE_SITELINK"

# Label and description aren't considered "properties" with their own P-id's so we create our own
LABEL_PROP_ID = -1
DESCRIPTION_PROP_ID = -2

QUEUE_SIZE = 150

# Wikidata's special values
NO_VALUE = 'novalue'
SOME_VALUE = 'somevalue'

# Wikidata's XML namespace
NS = "http://www.mediawiki.org/xml/export-0.11/"

# Wikidata's datatypes
WD_STRING_TYPES = ['monolingualtext', 'string', 'external-id', 'url', 'commonsMedia', 'geo-shape', 'tabular-data', 'math', 'musical-notation']
WD_ENTITY_TYPES = ['wikibase-item', 'wikibase-entityid', 'wikibase-property', 'wikibase-lexeme', 'wikibase-sense', 'wikibase-form', 'entity-schema']
# datatype column can have WD_STRING_TYPES, WD_ENTITY_TYPES, NO_VALUE or SOME_VALUE

PROPERTY_LABELS_PATH = "/sc/projects/sci-naumann/mpws2025fn1/wdtk-output/property_labels.csv"
ENTITY_LABEL_ALIAS_PATH = "/sc/projects/sci-naumann/mpws2025fn1/wdtk-output/labels_aliases.csv"
SUBCLASS_OF_PATH = "/sc/projects/sci-naumann/mpws2025fn1/wdtk-output/p279.csv"
INSTANCE_OF_PATH = "/sc/projects/sci-naumann/mpws2025fn1/wdtk-output/p31.csv"

REVISION_COLS = ['prev_revision_id', 'revision_id', 'entity_id', 'timestamp', 'user_id', 'username', 'comment', 'file_path', 'redirect', 'entity_label']
REVISION_PK = ['revision_id']

VALUE_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target', 'old_hash', 'new_hash']
VALUE_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'change_target']

SITELINK_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target', 'old_hash', 'new_hash']
SITELINK_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'change_target']

RANK_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target', 'old_hash', 'new_hash']
RANK_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'change_target']

DATATYPE_METADATA_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target', 'old_hash', 'new_hash']
DATATYPE_METADATA_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'change_target']

QUALIFIER_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'qual_property_id', 'value_hash', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target']
QUALIFIER_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'qual_property_id', 'value_hash', 'change_target']

REFERENCE_CHANGE_COLS = ['revision_id', 'entity_id', 'timestamp', 'property_id', 'value_id', 'ref_property_id', 'ref_hash', 'value_hash', 'old_value', 'new_value', 'old_datatype', 'new_datatype', 'change_target', 'action', 'target']
REFERENCE_CHANGE_PK = ['revision_id', 'property_id', 'value_id', 'ref_property_id', 'value_hash', 'ref_hash', 'change_target']

# ------------------------------------------------------------------------------------------------------------------------------
# Queue size for file processing
# ------------------------------------------------------------------------------------------------------------------------------
QUEUE_SIZE = 10000
BATCH_SIZE = 5000
