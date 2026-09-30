import psycopg2
from dotenv import load_dotenv
import os

from dump_parsing_scripts.const import PROPERTY_LABELS_PATH, ENTITY_LABEL_ALIAS_PATH, SUBCLASS_OF_PATH, INSTANCE_OF_PATH

import csv

def preprocess_csv(input_file, output_file, delimiter=';'):
    """
    Read CSV with mixed quotes and write with standardized double quotes
    """
    with open(input_file, 'r', encoding='utf-8') as infile, \
         open(output_file, 'w', encoding='utf-8', newline='') as outfile:
        
        # Read with Python's csv module (handles mixed quotes)
        reader = csv.reader(infile, delimiter=delimiter)
        
        # Write with standard double quotes
        writer = csv.writer(outfile, delimiter=delimiter, quotechar='"', 
                          quoting=csv.QUOTE_MINIMAL)
        
        for row in reader:
            writer.writerow(row)
    
    print(f"Preprocessed CSV saved to {output_file}")


def copy_from_csv(conn, csv_file_path, table_name, columns, primary_keys, delimiter=';'):
    temp_table = f"{table_name}_temp"

    with conn.cursor() as cur:
        cols_definition = ', '.join([f"{col} VARCHAR" for col in columns])
        cur.execute(f"CREATE TEMP TABLE {temp_table} ({cols_definition});")
        
        cols = ','.join(columns)
        with open(csv_file_path, 'r', encoding='utf-8') as f:
            cur.copy_expert(f"""
                COPY {temp_table} ({cols})
                FROM STDIN
                WITH (FORMAT csv, HEADER FALSE, DELIMITER '{delimiter}', QUOTE '"');
            """, f)
        
        print(f"Loaded data into temp table.")
        
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT DISTINCT * FROM {temp_table};")

        # add PK
        if primary_keys:
            
            pk_cols_str = ', '.join(primary_keys)
            # remove duplicates based on primary key columns
            print("Removing duplicates...")
            cur.execute(f"""
                DELETE FROM {table_name} a
                USING {table_name} b
                WHERE a.ctid < b.ctid
                AND {' AND '.join([f'a.{col} = b.{col}' for col in primary_keys])};
            """)

            print("Adding PK")
            cur.execute(f"ALTER TABLE {table_name} ADD PRIMARY KEY ({pk_cols_str});")
    
    conn.commit()

def load_entity_labels(conn):
    """
    Creates table entity_labels from csv, including redirected entities
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'entity_labels_aliases'
            );
        """)
        exists = cur.fetchone()[0]
        
    conn.commit()
    
    if exists:
        return
    
    copy_from_csv(conn, ENTITY_LABEL_ALIAS_PATH, 'entity_labels_aliases', ['entity_id', 'label', 'alias'], ['entity_id'], ';')

    #Add redirected entities labels to entity_labels_aliases
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO entity_labels_aliases (entity_id, label, alias)
            SELECT DISTINCT
                r.entity_id as entity_id,  -- redirected Q-id
                ela.label,
                ela.alias
            FROM revision r
            -- target Q-id (second Q-id from comment)
            CROSS JOIN LATERAL (
                SELECT REGEXP_REPLACE(SPLIT_PART(r.comment, '|', 4), '[^Q0-9]', '', 'g') as target_qid
            ) extracted
            -- label + alias from target entity
            JOIN entity_labels_aliases ela 
                ON ela.entity_id = extracted.target_qid
            WHERE r.redirect = TRUE
            -- for duplicates
            ON CONFLICT (entity_id) DO NOTHING;  
        """)
    conn.commit()

def load_property_labels(conn):
    """
    Creates table property_labels from csv
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = 'property_labels'
            );
        """)
        exists = cur.fetchone()[0]
        
    conn.commit()

    if exists:
        return
    
    copy_from_csv(conn, PROPERTY_LABELS_PATH, 'property_labels', ['property_id', 'label'], ['property_id'], ';')

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE property_labels
            SET property_id = REPLACE(property_id, 'P', '');
            ALTER TABLE property_labels
            ALTER COLUMN property_id TYPE INT USING property_id::INT;
        """)
    conn.commit()

def load_entity_type(conn):
    """
    Creates table entity_type from csv file which containes the columns 'entity_id', 'class_id', 'class_label'
    """
    with conn.cursor() as cur:

        cur.execute("""
            SELECT 
                EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'subclass_of'
                ) as exists_p279,
                EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'entity_type'
                ) as exists_p31;
        """)
        result = cur.fetchone()
        exists_p279 = result[0]
        exists_p31 = result[1]
        
    conn.commit()

    if not exists_p279:
        copy_from_csv(conn, SUBCLASS_OF_PATH, 'subclass_of', ['entity_id', 'class_id', 'rank'], ['entity_id', 'class_id'], ';')

    if not exists_p31:
        copy_from_csv(conn, INSTANCE_OF_PATH, 'entity_type', ['entity_id', 'class_id', 'rank'], None, ';') # set to None so it doesn't create the PK again

    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE subclass_of
                    ADD COLUMN IF NOT EXISTS class_label VARCHAR DEFAULT NULL;
            ALTER TABLE entity_type
                    ADD COLUMN IF NOT EXISTS class_label VARCHAR DEFAULT NULL; 
        """)
        
    conn.commit()

    # Update columns in entity_type table
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_value_change_qid
            ON subclass_of (class_id);

            UPDATE subclass_of et
            SET class_label = 
                    CASE 
                        WHEN (el.label IS NOT NULL and el.label <> '') THEN el.label
                        ELSE el.alias 
                    END
            FROM entity_labels_aliases el
            WHERE
                et.class_label IS NULL  -- only update the ones that don't have a label yet
                AND
                et.class_id = el.entity_id;
                    
            CREATE INDEX IF NOT EXISTS idx_value_change_qid
            ON entity_type (class_id);

            UPDATE entity_type et
            SET  class_label = 
                    CASE 
                        WHEN (el.label IS NOT NULL and el.label <> '') THEN el.label
                        ELSE el.alias 
                    END
            FROM entity_labels_aliases el
            WHERE
                et.class_label IS NULL  -- only update the ones that don't have a label yet
                AND
                et.class_id = el.entity_id;
            
            UPDATE entity_type
            SET entity_id = REPLACE(entity_id, 'Q', ''),
            class_id = REPLACE(class_id, 'Q', '');
            UPDATE subclass_of
            SET entity_id = REPLACE(entity_id, 'Q', ''),
            class_id = REPLACE(class_id, 'Q', '');

            ALTER TABLE entity_type
            ALTER COLUMN entity_id TYPE INT USING entity_id::INT,
            ALTER COLUMN class_id TYPE INT USING class_id::INT;
            ALTER TABLE subclass_of
            ALTER COLUMN entity_id TYPE INT USING entity_id::INT,
            ALTER COLUMN class_id TYPE INT USING class_id::INT;
        """)
    conn.commit()


def main():
    """
    Loads labels for new value and old value, when the value is a Q-id.
    Loads property labels.
    Loads entity_type data
    """

    dotenv_path = ".env"
    load_dotenv(dotenv_path)

    # credentials for DB connection
    DB_USER = os.environ.get("DB_USER")
    DB_PASS = os.environ.get("DB_PASS")
    DB_NAME = os.environ.get("DB_NAME")
    DB_HOST = os.environ.get("DB_HOST")
    DB_PORT = os.environ.get("DB_PORT")
    
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS, 
        host=DB_HOST,
        port=DB_PORT,
        connect_timeout=30,
        gssencmode='disable'
    )
    
    load_entity_labels(conn)
    load_property_labels(conn) 
    load_entity_type(conn)
    
    conn.close()


if __name__ == "__main__":
    main()
