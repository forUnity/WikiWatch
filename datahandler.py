from __future__ import annotations

from io import StringIO
from typing import Iterable, List, Tuple, Any, Final, Union
from enum import Enum
from uuid import uuid4
import time

from dotenv import load_dotenv
import os
import subprocess
import getpass
import psycopg2
import pandas
import logging

import duckdb

from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import Enum as SAEnum, Integer, BigInteger, String, create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import mapped_column, Mapped
from sqlalchemy import ForeignKey
import tqdm
from sqlalchemy import DateTime, func
from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError as SAOperationalError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
from sqlalchemy import text
import uuid
import pandas as pd
from utils.memory_tracker import memusage, rss_memusage
import logging 
import sys


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

log = logging.getLogger(__name__)


SCHEMA_VALUE_CHANGE_TABLE_NAME : Final[str] = "value_change"
SCHEMA_REFERENCE_CHANGE_TABLE_NAME : Final[str] = "reference_change"
SCHEMA_SITELINK_CHANGE_TABLE_NAME : Final[str] = "sitelink_change"
SCHEMA_REVISION_TABLE_NAME : Final[str] = "revision"

CACHE_TABLE_FLOAT = "cache_float"

POSTGRES_DB_NAME : Final[str] = "postgres_db"
DB_CONNECTION_RETRY_SLEEP_SECONDS: Final[int] = 10 * 60


def _is_db_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError, SAOperationalError, DisconnectionError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True

    message_parts = [str(exc)]
    if getattr(exc, "__cause__", None) is not None:
        message_parts.append(str(exc.__cause__))
    if getattr(exc, "__context__", None) is not None:
        message_parts.append(str(exc.__context__))

    message = " ".join(message_parts).lower()
    return any(
        fragment in message
        for fragment in (
            "connection refused",
            "could not connect",
            "connection to server was lost",
            "server closed the connection",
            "terminating connection",
            "database system is starting up",
            "database system is in recovery mode",
            "broken pipe",
            "connection reset",
            "server not available",
            "connection error",
        )
    )


def _get_git_commit_hash() -> str | None:
    v = os.environ.get("GIT_COMMIT_HASH")
    if v:
        return v
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def _get_db_host() -> str:
    # cxnode format: cxNN
    cxnode = subprocess.check_output(["squeue", "--name=wikiwatch-db", "--noheader", "--format=%N"], stderr=subprocess.DEVNULL).decode().strip()
    ip = "10.130.31." + cxnode[2:]  # Extract the IP address from the cxnode format
    return ip


#the datahandler needs state -> global class
class DataHandler:
    def _create_run(self, engine) -> int:
        """
        If running on SLURM: re-use the SLURM job id as run id (so restarts of same job share run).
        Otherwise: create a new Run row and return its autoincrement id.
        """
        slurm_job_id = os.environ.get("SLURM_JOB_ID")
        user = getpass.getuser()

        def _do_create_run():
            with Session(engine) as session:
                if slurm_job_id is not None:
                    # Try to use SLURM job id as the primary key value.
                    # This requires that Run.id is NOT always auto-assigned by Postgres; explicit ids are allowed.
                    rid = int(slurm_job_id)

                    existing = session.execute(select(Runs.id).where(Runs.id == rid)).scalar_one_or_none()
                    if existing is not None:
                        return existing

                    r = Runs(
                        id=rid,
                        user=f"{user}@slurm:{slurm_job_id}",
                        git_commit_hash=_get_git_commit_hash(),
                    )
                    session.add(r)
                    session.commit()
                    return r.id

                # Not on slurm -> create a fresh run row (autoincrement id)
                r = Runs(
                    user=f"{user}@local:{uuid.uuid4().hex[:8]}",
                    git_commit_hash=_get_git_commit_hash(),
                )
                session.add(r)
                session.commit()
                return r.id

        return self._retry_db_operation("create run", _do_create_run, on_retry=engine.dispose)

    def _retry_db_operation(self, operation_name: str, operation, on_retry=None):
        while True:
            try:
                return operation()
            except Exception as exc:
                if not _is_db_connection_error(exc):
                    raise

                print(f"{operation_name}: database connection error ({exc}); retrying in 10 minutes...")
                logging.warning("%s: database connection error (%s); retrying in 10 minutes...", operation_name, exc)
                self.connect_db()

                if on_retry is not None:
                    on_retry()
                time.sleep(DB_CONNECTION_RETRY_SLEEP_SECONDS)

    def connect_db(self):
        load_dotenv(".env")
        self.DB_USER = os.environ.get("DB_USER")
        self.DB_PASS = os.environ.get("DB_PASS")
        self.DB_NAME = os.environ.get("DB_NAME")
        self.DB_HOST = _get_db_host()
        self.DB_PORT = os.environ.get("DB_PORT")

        self.duckdb.execute(f"DETACH {POSTGRES_DB_NAME}")
        self.duckdb.execute(f"""ATTACH 'dbname={self.DB_NAME} user={self.DB_USER} hostaddr={self.DB_HOST} port={self.DB_PORT} password={self.DB_PASS}'
                                  AS {POSTGRES_DB_NAME} (TYPE postgres, READ_ONLY FALSE);""")

        self.engine = create_engine(f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}")

    def __init__(self, wipe_metrics_table: bool = False):
        #this in-memory database acts as a cache that holds the Revisions for the current timestep. These can be queried via SQL
        self.duckdb = duckdb.connect(database=":memory:")
        # load .env for Postgres
        load_dotenv(".env")
        self.DB_USER = os.environ.get("DB_USER")
        self.DB_PASS = os.environ.get("DB_PASS")
        self.DB_NAME = os.environ.get("DB_NAME")
        self.DB_HOST = _get_db_host()
        self.DB_PORT = os.environ.get("DB_PORT")
        #attach Postgres to DuckDB to load as read-only in memory data
        self._retry_db_operation("duckdb postgres extension install", lambda: self.duckdb.execute(f"""INSTALL postgres; LOAD postgres;"""))
        self._retry_db_operation(
            "duckdb postgres attach",
            lambda: self.duckdb.execute(f"""ATTACH 'dbname={self.DB_NAME} user={self.DB_USER} hostaddr={self.DB_HOST} port={self.DB_PORT} password={self.DB_PASS}'
                              AS {POSTGRES_DB_NAME} (TYPE postgres, READ_ONLY FALSE);""")
        )
        
        # DuckDB progress printing
        # self.duckdb.execute("SET enable_progress_bar = true;")
        # self.duckdb.execute("SET enable_progress_bar_print = true;")
        # self.duckdb.execute("SET progress_bar_time = 2000;")  # time before showing progress in ms

                # DuckDB progress printing
                # self.duckdb.execute("SET enable_progress_bar = true;")
                # self.duckdb.execute("SET enable_progress_bar_print = true;")
                # self.duckdb.execute("SET progress_bar_time = 2000;")  # time before showing progress in ms

        #SQAlchemy
        self.engine = create_engine(f"postgresql+psycopg2://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}")
        # Create ORM tables before we query or clear them. If the tables do not exist yet,
        # SQLAlchemy will create them in the Postgres database so subsequent queries
        # (like load_metric_name_to_id_map) won't fail with "relation 'metrics' does not exist".
        self._retry_db_operation("create ORM tables", lambda: SABase.metadata.create_all(self.engine), on_retry=self.engine.dispose)
        self.metric_name_to_id : dict[str, int] = self._retry_db_operation(
            "load metric name map",
            lambda: self.load_metric_name_to_id_map(self.engine),
            on_retry=self.engine.dispose,
        )
        # Optionally clear metric rows (deletes rows, does not drop tables)
        if wipe_metrics_table:
            self._retry_db_operation(
                "wipe metric tables",
                lambda: self.remove_everything_from_metric_tables(self.engine),
                on_retry=self.engine.dispose,
            )
            self.metric_name_to_id = self._retry_db_operation(
                "reload metric name map",
                lambda: self.load_metric_name_to_id_map(self.engine),
                on_retry=self.engine.dispose,
            )

        self.run_id = self._retry_db_operation("create run row", lambda: self._create_run(self.engine), on_retry=self.engine.dispose)
    #region Postgres Wikidata-Database Querying
    
    def query_postgres(self, query: str, params=None) -> List[Tuple[Any, ...]]:
        load_dotenv(".env")
        DB_USER = os.environ.get("DB_USER")
        DB_PASS = os.environ.get("DB_PASS")
        DB_NAME = os.environ.get("DB_NAME")
        DB_HOST = _get_db_host()
        DB_PORT = os.environ.get("DB_PORT")
        def _do_query():
            with psycopg2.connect(
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASS,
                host=DB_HOST,
                port=DB_PORT,
                connect_timeout=30,
                gssencmode='disable'
            ) as db:
                with db.cursor() as cur:
                    cur.execute(query, params)
                    if cur.description:  # query returned rows
                        return cur.fetchall()
                    return []

        try:
            return self._retry_db_operation("Postgres query", _do_query)
        except Exception as e:
            print(f"Failed: {e}")
            return []


    def quick_write(self, table_name: str, rows: Iterable[Tuple[Any, ...]]):
        load_dotenv(".env")
        DB_USER = os.environ.get("DB_USER")
        DB_PASS = os.environ.get("DB_PASS")
        DB_NAME = os.environ.get("DB_NAME")
        DB_HOST = _get_db_host()
        DB_PORT = os.environ.get("DB_PORT")
        
        rows = list(rows)

        def _do_write():
            with (
                psycopg2.connect(
                    dbname=DB_NAME,
                    user=DB_USER,
                    password=DB_PASS,
                    host=DB_HOST,
                    port=DB_PORT,
                    connect_timeout=30,
                    gssencmode='disable'
                ) as conn,
                conn.cursor() as cursor,
                StringIO() as buffer
            ):
                temp_table = "temp_" + str(uuid4()).replace("-", "_")
                cursor.execute(f"""
                    CREATE TEMP TABLE {temp_table} 
                    (LIKE {table_name} INCLUDING DEFAULTS)
                    ON COMMIT DROP
                """)
                
                for row in tqdm.tqdm(rows, "(QuickWrite) Writing"):
                    line_items = []
                    for val in row:
                        if val is None:
                            line_items.append('\\N')
                        elif val == '':
                            line_items.append('')
                        else:
                            val_str = str(val)
                            val_str = val_str.replace('\\', '\\\\')
                            val_str = val_str.replace('\t', '\\t')
                            val_str = val_str.replace('\n', '\\n')
                            val_str = val_str.replace('\r', '\\r')
                            line_items.append(val_str)
                    buffer.write('\t'.join(line_items) + '\n')
            
                buffer.seek(0)
                cursor.copy_expert(f"COPY {temp_table} FROM STDIN", buffer)
                
                # DO NOTHING on conflict
                insert_query = f"""
                    INSERT INTO {table_name}
                    SELECT * FROM {temp_table}
                """
                cursor.execute(insert_query)
                conn.commit()

        try:
            self._retry_db_operation(f"QuickWrite {table_name}", _do_write)
        except Exception as e:
            print(f"Error: datahandler.py/quick_write(): COPY write failed for {table_name}: {e}")
            raise
        
    def test_duck_cache(self) -> bool:
        self._retry_db_operation(
            "duckdb test cache drop",
            lambda: self.duckdb.execute(f"DROP TABLE IF EXISTS {SCHEMA_VALUE_CHANGE_TABLE_NAME};")
        )
        self._retry_db_operation(
            "duckdb test cache load",
            lambda: self.duckdb.execute(f"""CREATE TABLE {SCHEMA_VALUE_CHANGE_TABLE_NAME}
                             AS SELECT * FROM {POSTGRES_DB_NAME}.{SCHEMA_VALUE_CHANGE_TABLE_NAME}
                             ORDER BY timestamp
                             LIMIT 10;""")
        )

        results_posgres = self.query_postgres(f"SELECT * FROM {SCHEMA_VALUE_CHANGE_TABLE_NAME} ORDER BY timestamp LIMIT 10;")
        print("Postgres query results:-------------------------------------------")
        print(results_posgres)

        results_duckdb = self.query_duckdb(f"SELECT * FROM {SCHEMA_VALUE_CHANGE_TABLE_NAME} ORDER BY timestamp LIMIT 10;")
        print("DuckDB query results:-------------------------------------------")
        print(results_duckdb)

        assert results_posgres == results_duckdb, "Results from Postgres and DuckDB do not match!"
        return True


    def get_current_start_timestamp(self):
        return self.current_start_timestep
    def get_current_end_timestamp(self):
        return self.current_end_timestep
    
    def load_next_timestep(self, start_timestep: datetime, end_timestep: datetime, less_entities: bool):
        self.current_start_timestep = start_timestep
        self.current_end_timestep = end_timestep

        load_revision_join_tables = [SCHEMA_VALUE_CHANGE_TABLE_NAME, SCHEMA_REFERENCE_CHANGE_TABLE_NAME, SCHEMA_SITELINK_CHANGE_TABLE_NAME, SCHEMA_REVISION_TABLE_NAME]

        for table_name in load_revision_join_tables:
            #clear database in duckdb for next timestep
            self._retry_db_operation("duckdb drop timestep table", lambda: self.duckdb.execute(f"DROP TABLE IF EXISTS {table_name};"))
            # Use postgres_query() so the WHERE clause is executed *inside* PostgreSQL,
            # which allows Postgres to use its indexes on the timestamp column.
            # The previous approach (SELECT … FROM postgres_db.table WHERE …) used
            # DuckDB's postgres_scan, which transfers the ENTIRE table first and
            # filters only afterwards in DuckDB — bypassing all Postgres indexes.
            if less_entities:
                pg_sql = f"SELECT * FROM {table_name} WHERE timestamp >= '{start_timestep}' AND timestamp < '{end_timestep}' AND entity_id::text LIKE '%0'"
            else:
                pg_sql = f"SELECT * FROM {table_name} WHERE timestamp >= '{start_timestep}' AND timestamp < '{end_timestep}'"
            pg_sql_escaped = pg_sql.replace("'", "''")
            self._retry_db_operation(
                "duckdb load timestep table",
                lambda: self.duckdb.execute(f"""CREATE TABLE {table_name}
                                 AS SELECT * FROM postgres_query('{POSTGRES_DB_NAME}', '{pg_sql_escaped}')
                                 ;""")
            )
            
        df = self.duckdb.execute("PRAGMA database_size").fetchdf()
        log.info(f"DuckDB memory logs:\n{df}")

    def query_duckdb(self, query: str, params: tuple = ()) -> List[Tuple[Any, ...]]:
        try:
            return self._retry_db_operation("DuckDB query", lambda: self.duckdb.execute(query, params).fetchall())
        except Exception as e:
            print(f"Failed to execute DuckDB query: {e}")
            return []
        
    # return as pandas dataframe
    def query_duckdb_df(self, query: str,  params: tuple = ()) -> pandas.DataFrame:
        try:
            return self._retry_db_operation("DuckDB query", lambda: self.duckdb.execute(query, params).df())
        except Exception as e:
            print(f"Failed to execute DuckDB query: {e}")
            return pandas.DataFrame()

    #endregion Postgres Wikidata-Database Querying

    #region Save and Load Cache
    
    # Currently per metric name it is possible to save either a df or a dict and either an int or a float
    # If more caching is needed, this has to be changed in the future.
    
    def reset_cache(self):
        self.query_postgres(f"DROP TABLE IF EXISTS {CACHE_TABLE_FLOAT}")
        # Other tabels are overwirten when the next cache is saved
    
    def save_timestep_config(self, endtimestep: datetime, timestep_length: timedelta):
        table_name = "cache_timestep_config"
        self.query_postgres(f"DROP TABLE IF EXISTS {table_name}")
        self.query_postgres(f"CREATE TABLE {table_name} (endtimestep timestamp with time zone, timestep_length INTERVAL)") 
        self.query_postgres(f"INSERT INTO {table_name} (endtimestep, timestep_length) VALUES (%s, %s)", (endtimestep, timestep_length))  
    
    def get_timestep_config(self) -> Tuple[datetime, timedelta]:
        table_name = "cache_timestep_config"
        result = self.query_postgres(f"SELECT endtimestep, timestep_length FROM {table_name} LIMIT 1")
        return result[0]
    
    def save_cache_df(self, metric_name: str, cache: pd.DataFrame):
        table_name = "cache_" + metric_name
        self._retry_db_operation(
            f"save cache dataframe {table_name}",
            lambda: cache.to_sql(table_name, self.engine, if_exists="replace", index=False),
            on_retry=self.engine.dispose,
        )
    
    def get_cache_df(self, metric_name: str) -> pd.DataFrame:
        table_name = "cache_" + metric_name
        return self._retry_db_operation(
            f"read cache dataframe {table_name}",
            lambda: pd.read_sql_table(table_name, self.engine),
            on_retry=self.engine.dispose,
        )
    
    def save_cache_dict(self, metric_name: str, cache: dict, columns: str):
        table_name = "cache_" + metric_name
        self.query_postgres(f"DROP TABLE IF EXISTS {table_name}")
        self.query_postgres(f"CREATE TABLE {table_name} ({columns})")
        rows = [(*(k if isinstance(k, tuple) else (k,)), *(v if isinstance(v, tuple) else (v,))) for k, v in cache.items()]
        self.quick_write(table_name, rows)

    def save_cache_rows(self, metric_name: str, rows: list[tuple], columns: str):
        table_name = "cache_" + metric_name
        self.query_postgres(f"DROP TABLE IF EXISTS {table_name}")
        self.query_postgres(f"CREATE TABLE {table_name} ({columns})")
        self.quick_write(table_name, rows)
    
    def get_cache_dict(self, metric_name: str, key_length: int = 1) -> dict:
        table_name = "cache_" + metric_name
        rows = self.query_postgres(f"SELECT * FROM {table_name}")
        result = {
            (t[0] if key_length == 1 else t[:key_length]):
            (t[key_length] if len(t[key_length:]) == 1 else t[key_length:])
            for t in rows
        }
        return result

    def save_cache_float(self, metric_name: str, cache: float):
        self.query_postgres(
            f"""
            CREATE TABLE IF NOT EXISTS {CACHE_TABLE_FLOAT} (
                metric_name VARCHAR(100) PRIMARY KEY,
                value FLOAT
            )
            """
        )

        self.query_postgres(
            f"""
            INSERT INTO {CACHE_TABLE_FLOAT} (metric_name, value)
            VALUES ('{metric_name}', {cache})
            ON CONFLICT (metric_name)
            DO UPDATE SET value = EXCLUDED.value
            """
        )
    
    def get_cache_float(self, metric_name: str) -> float:
        temp = self.query_postgres(f"SELECT value FROM {CACHE_TABLE_FLOAT} WHERE metric_name='{metric_name}'")
        if not temp:
            return 0.0
        return temp[0][0]
    
    def save_cache_int(self, metric_name: str, cache: int):
        self.save_cache_float(metric_name, float(cache))
    
    def get_cache_int(self, metric_name: str) -> int:
        return int(self.get_cache_float(metric_name))
        
    #endregion Save and Load Cache

    #region Metric Write to Database
    
    def _upsert(self, row_obj: DeclarativeBase, pk_cols: list[str]):
        """
        Insert-or-update based on pk_cols.
        Overwrites non-PK columns (e.g., 'value') when a duplicate key exists.
        """
        table = row_obj.__class__.__table__
        data = {c.name: getattr(row_obj, c.name) for c in table.columns}

        update_cols = {k: v for k, v in data.items() if k not in pk_cols}

        stmt = insert(table).values(**data).on_conflict_do_update(
            index_elements=pk_cols,
            set_=update_cols,
        )

        def _do_upsert():
            with Session(self.engine) as session:
                session.execute(stmt)
                session.commit()

        self._retry_db_operation("upsert metric row", _do_upsert, on_retry=self.engine.dispose)
            
    def write_single_change(self, addition : DeclarativeBase):
        # Depricated because of _upsert()
        #TODO: could batch these writes here for performance
        def _do_write_single_change():
            with Session(self.engine) as session:
                session.add(addition)
                session.commit()

        self._retry_db_operation("write single change", _do_write_single_change, on_retry=self.engine.dispose)

    class SeriesWriter():
        def __init__(self, datahandler: DataHandler, metric_name: str, table: Union[type[MetricOnStatementValueFloat], type[MetricOnClassAndPropertyValueFloat], type[MetricOnEntitySchemaAndPropertyValueFloat], type[MetricOnEntitySchemaValueFloat], type[MetricOnPropertyValueFloat], type[MetricOnEntityValueBigint], type[MetricOnClassValueFloat], type[MetricValueFloat]]) -> None:
            self.datahandler = datahandler
            self.table_name = table.__tablename__
            self.metric_id = datahandler.register_metric(metric_name, table.metric_type, Dimension.default, self.table_name)
            self.timestamp = datahandler.current_end_timestep
            self.rows: list[tuple] = []

        def add(
            self,
            *args
        ):
            # Order of fields in DB:
            # - run_id
            # - metric_id
            # - timestamp
            # - other params
            # - value
            self.rows.append((self.datahandler.run_id, self.metric_id, self.timestamp, *args))

        def write_all(self):
            if len(self.rows) == 0:
                return
            self.datahandler.quick_write(self.table_name, self.rows)

    def write_global_metric_value(self, metric_name: str, value: float):
        if not isinstance(value, float):
            raise ValueError(f"Metric value for {metric_name} is not float: {value} and other types are not implemented yet.")

        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricValueFloat.__tablename__)
        metric_value = MetricValueFloat(
            run_id = self.run_id,
            metric_id = metric_id,
            timestamp = self.current_end_timestep,
            value = value
        )

        self._upsert(metric_value, pk_cols=["run_id", "metric_id", "timestamp"])
    
    def write_class_metric_value(self, metric_name: str, class_id: int, value: float):
        if not isinstance(value, float):
            raise ValueError(f"Metric value for {metric_name} is not float: {value} and other types are not implemented yet.")
        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricOnClassValueFloat.__tablename__)
        metric_value = MetricOnClassValueFloat(
            run_id=self.run_id,
            metric_id = metric_id,
            timestamp = self.current_end_timestep,
            class_id = class_id,
            value = value
        )
        self._upsert(metric_value, pk_cols=["run_id", "metric_id", "timestamp", "class_id"])

    def write_property_metric_value(self, metric_name: str, property_id: int, value: float):
        if not isinstance(value, float):
            raise ValueError(f"Metric value for {metric_name} is not float: {value} and other types are not implemented yet.")
        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricOnPropertyValueFloat.__tablename__)
        metric_value = MetricOnPropertyValueFloat(
            run_id=self.run_id,
            metric_id = metric_id,
            timestamp = self.current_end_timestep,
            property_id = property_id,
            value = value
        )
        self._upsert(metric_value, pk_cols=["run_id", "metric_id", "timestamp", "property_id"])

    def write_class_and_property_metric_value(self, metric_name: str, class_id: int, property_id: int, value: float):
        if not isinstance(value, float):
            raise ValueError(f"Metric value for {metric_name} is not float: {value} and other types are not implemented yet.")
        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricOnClassAndPropertyValueFloat.__tablename__)
        metric_value = MetricOnClassAndPropertyValueFloat(
            run_id=self.run_id,
            metric_id = metric_id,
            timestamp = self.current_end_timestep,
            class_id = class_id,
            property_id = property_id,
            value = value
        )
        self._upsert(metric_value, pk_cols=["run_id", "metric_id", "timestamp", "class_id", "property_id"])
        
    def write_statement_metric_value_dataframe(self, metric_name : str, df: pd.DataFrame):
        df_temp = df.reset_index()
        df_temp["run_id"] = self.run_id
        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricOnStatementValueFloat.__tablename__)
        df_temp["metric_id"] = metric_id
        df_temp["timestamp"] = self.current_end_timestep
        
        stmt = insert(MetricOnStatementValueFloat).values(df_temp.to_dict(orient="records"))

        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "metric_id", "timestamp", "entity_id", "property_id", "value_id"],
            set_={"value": stmt.excluded["value"]}
        )

        def _do_write_statement_values():
            with self.engine.begin() as conn:
                conn.execute(stmt)

        self._retry_db_operation(
            f"write statement metric values for {metric_name}",
            _do_write_statement_values,
            on_retry=self.engine.dispose,
        )

    def write_statement_metric_value(self, metric_name : str, entity_id: int, property_id: int, value_id: str, value : float):
        if not isinstance(value, float):
            raise ValueError(f"Metric value for {metric_name} is not float: {value} and other types are not implemented yet.")
        metric_id = self.register_metric(metric_name, Type.type_float, Dimension.default, MetricOnStatementValueFloat.__tablename__)
        metric_value = MetricOnStatementValueFloat(
            run_id=self.run_id,
            metric_id = metric_id,
            timestamp = self.current_end_timestep,
            entity_id = entity_id,
            property_id = property_id,
            value_id = value_id,
            value = value
        )
        self._upsert(metric_value, pk_cols=["run_id", "metric_id", "timestamp", "entity_id", "property_id", "value_id"])


    def remove_everything_from_metric_tables(self, engine):
        def _do_remove():
            with Session(engine) as session:
                session.query(MetricOnStatementValueFloat).delete()
                session.query(MetricOnClassAndPropertyValueFloat).delete()
                session.query(MetricOnPropertyValueFloat).delete()
                session.query(MetricOnEntityValueBigint).delete()
                session.query(MetricOnClassValueFloat).delete()
                session.query(MetricOnEntitySchemaAndPropertyValueFloat).delete()
                session.query(MetricOnEntitySchemaValueFloat).delete()
                session.query(MetricValueFloat).delete()
                session.query(MetricMetaInfo).delete()
                session.query(Runs).delete()
                session.commit()

        self._retry_db_operation("remove metric tables", _do_remove, on_retry=engine.dispose)
    
    def load_metric_name_to_id_map(self, engine) -> dict[str, int]:
        metric_name_to_id : dict[str, int] = dict()
        def _do_load():
            with Session(engine) as session:
                results = session.query(MetricMetaInfo).all()
                for r in results:
                    metric_name_to_id[r.name] = r.id
            return metric_name_to_id

        return self._retry_db_operation("load metric name map", _do_load, on_retry=engine.dispose)

    def register_metric(self, metric_name: str, type: Type, dimension: Dimension, table_name: str) -> int:
        #in init
        if metric_name in self.metric_name_to_id:
            # print("Returning cached metric id for", metric_name)
            return self.metric_name_to_id[metric_name]
        #register metric in DB
        def _do_register():
            with Session(self.engine) as session:
                metric_meta = MetricMetaInfo(
                    name = metric_name,
                    type = type,
                    dimension = dimension,
                    table_name = table_name
                )
                session.add(metric_meta)
                session.commit()
                metric_id = metric_meta.id
                self.metric_name_to_id[metric_name] = metric_id
                logging.info(f"Registered new metric: '{metric_name}' with ID: {metric_id}")
                return metric_id

        return self._retry_db_operation("register metric", _do_register, on_retry=self.engine.dispose)
        
    #NOTE: at some point we could do this also and expose the DeclarativeBase models to the actual Metric classes?
    # def commit_metric(self, metric : Metric):
    #     engine = self.create_sqlalchemy_engine()
    #     with Session(engine) as session:
    #         session.add(metric)
    #         session.commit()

    def write_metric_on_string(self, metric_name: str, df: pandas.DataFrame, key_column: str, value_column: str):
        if df.empty:
            return

        series_writer = DataHandler.SeriesWriter(self, metric_name, MetricOnString)

        # Use the shared batched writer path to keep behavior consistent with other metric tables.
        for key_string, value in df[[key_column, value_column]].itertuples(index=False, name=None):
            series_writer.add(key_string, value)

        series_writer.write_all()


class Dimension(str, Enum):
    default = "default"

class Type(str, Enum):
    type_float = "float"
    type_int = "int"
    type_interval = "interval"
    type_bigint = "bigint"
    type_datetime = "datetime"

class SABase(DeclarativeBase):
    pass

class MetricMetaInfo(SABase):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    type: Mapped[Type] = mapped_column(SAEnum(Type), nullable=True)
    dimension: Mapped[Dimension] = mapped_column(SAEnum(Dimension), nullable=True)
    table_name: Mapped[str] = mapped_column(String(255), nullable=False)

class Runs(SABase):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git_commit_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

class MetricValueFloat(SABase):
    __tablename__ = "metric_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    value: Mapped[float] = mapped_column() 

    # metric_meta: Mapped[MetricMetaInfo]# = relationship()

class MetricOnEntityValueBigint(SABase):
    __tablename__ = "metric_on_entity_value_bigint"
    metric_type: Type = Type.type_bigint
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


class MetricOnClassValueFloat(SABase):
    __tablename__ = "metric_on_class_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    class_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[float] = mapped_column() 


class MetricOnEntitySchemaValueFloat(SABase):
    __tablename__ = "metric_on_entity_schema_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    entity_schema_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[float] = mapped_column() 


class MetricOnPropertyValueFloat(SABase):
    __tablename__ = "metric_on_property_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[float] = mapped_column() 


class MetricOnClassAndPropertyValueFloat(SABase):
    __tablename__ = "metric_on_class_and_property_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    class_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[float] = mapped_column() 


class MetricOnEntitySchemaAndPropertyValueFloat(SABase):
    __tablename__ = "metric_on_entity_schema_and_property_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    entity_schema_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[float] = mapped_column() 


class MetricOnStatementValueFloat(SABase):
    __tablename__ = "metric_on_statement_value_float"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(primary_key=True)
    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value_id: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float] = mapped_column() 

class MetricOnString(SABase):
    __tablename__ = "metric_on_string"
    metric_type: Type = Type.type_float
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("metrics.id"), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(primary_key=True)
    key_string : Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[float] = mapped_column()

#endregion Metric Write to Database


if __name__ == "__main__":
    db = DataHandler()
    # assert db.test_duck_cache()
