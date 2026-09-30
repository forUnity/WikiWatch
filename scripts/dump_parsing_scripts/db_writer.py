import os
import traceback
import time
import psycopg2
import queue
from pathlib import Path
from dotenv import load_dotenv

from dump_parsing_scripts.const import *
from dump_parsing_scripts.utils import insert_rows_copy

def batch_insert(conn, batch):

    """Function to insert into DB in parallel."""
    
    try:

        if len(batch['revision']) > 0:
            insert_rows_copy(conn, f'revision', batch['revision'], REVISION_COLS, REVISION_PK)
        
        if len(batch['value_change']) > 0:
            insert_rows_copy(conn, f'value_change', batch['value_change'], VALUE_CHANGE_COLS, VALUE_CHANGE_PK)
        
        if len(batch['sitelink_change']) > 0:
            insert_rows_copy(conn, f'sitelink_change', batch['sitelink_change'], SITELINK_CHANGE_COLS, SITELINK_CHANGE_PK)
        
        if len(batch['rank_change']) > 0:
            insert_rows_copy(conn, f'rank_change', batch['rank_change'], RANK_CHANGE_COLS, RANK_CHANGE_PK)
        
        if len(batch['qualifier_change']) > 0:
            insert_rows_copy(conn, f'qualifier_change', batch['qualifier_change'], QUALIFIER_CHANGE_COLS, QUALIFIER_CHANGE_PK)
        
        if len(batch['reference_change']) > 0:
            insert_rows_copy(conn, f'reference_change', batch['reference_change'], REFERENCE_CHANGE_COLS, REFERENCE_CHANGE_PK)
        
        if len(batch['datatype_metadata_change']) > 0:
            insert_rows_copy(conn, f'datatype_metadata_change', batch['datatype_metadata_change'], DATATYPE_METADATA_CHANGE_COLS, DATATYPE_METADATA_CHANGE_PK)

    except Exception as e:
        print(f'There was an error when batch inserting revisions and changes: {e}', flush=True)
        print(traceback.format_exc(), flush=True)
        raise e


def db_writer(num_workers, results_queue: queue.Queue):

    log_dir = Path('logs')
    log_dir.mkdir(exist_ok=True)
    pid = os.getpid()
    log_file = open(f'logs/db_writer_{pid}.log', 'w', buffering=1)  # Line buffered
    
    def log(msg):
        """Helper to write to log file"""
        log_file.write(f"{msg}\n")
        log_file.flush()

    log(f"[DB_WRITER] Starting - Num workers: {num_workers}")

    load_dotenv(".env")
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

    log(f"[DB_WRITER] Database connection established")

    batch = {
        'revision': [],
        'value_change': [],
        'sitelink_change': [],
        'rank_change': [],
        'datatype_metadata_change': [],
        'qualifier_change': [],
        'reference_change': []
    }

    workers_finished = 0
    last_write = time.time()

    last_log = time.time()

    try:
        while workers_finished < num_workers:
            try:
                if time.time() - last_log > 30:
                    log(f"[DB_WRITER] Still waiting... {workers_finished}/{num_workers} workers finished")
                    log(f"[DB_WRITER] results_queue size: {str(results_queue.qsize())}")
                    
                    last_log = time.time()

                result = results_queue.get(timeout=5)
                
                if result is None:
                    # Worker finished
                    workers_finished += 1
                    log(f"[DB_WRITER] Worker finished, total finished: {workers_finished}/{num_workers}")
                    continue
                
                for table_name in batch.keys():
                    batch[table_name].extend(result.get(table_name, []))
                
                current_batch_size = len(batch['revision'])
                time_since_write = time.time() - last_write
                if len(batch['revision']) >= BATCH_SIZE or (time_since_write > 20 and current_batch_size > 0):
                    
                    batch_insert(conn, batch)
                    # Clear this batch
                    for table in batch:
                        batch[table] = []

                    last_write = time.time()
                
            except queue.Empty:
                log(f"[DB_WRITER] Queue empty timeout - flushing batches")
                if any(len(v) > 0 for v in batch.values()):
                    batch_insert(conn, batch)
                    for table in batch:
                        batch[table] = []
    
        if any(len(v) > 0 for v in batch.values()):
            batch_insert(conn, batch)
        
        log(f"[DB_WRITER] Completed successfully")

    except Exception as e:
        log(f'Error in DB writer: {e}')
        log(traceback.format_exc())
        raise e
    finally:
        log(f"[DB_WRITER] Closing connection")
        try:
            conn.close()
        except:
            pass
        log(f"[DB_WRITER] Exiting")
        log_file.close()