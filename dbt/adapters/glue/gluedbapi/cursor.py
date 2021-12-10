import uuid
import textwrap
import json
from dbt.contracts.connection import AdapterResponse
from dbt import exceptions as dbterrors
from dbt.adapters.glue.gluedbapi.commons import GlueStatement
from dbt.logger import GLOBAL_LOGGER as logger


class GlueCursorState:
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    AVAILABLE = "AVAILABLE"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class GlueCursor:
    def __init__(self, connection):
        self.name = str(uuid.uuid4())
        self._connection = connection
        self.state = None
        self._is_running = False
        self.statement_id = None
        self.code = None
        self.sql = None
        self.response = None
        self._closed = False

    @property
    def connection(self):
        return self._connection

    @property
    def rowcount(self):
        if self.response:
            return self.response.get("rowcount")

    def _pre(self):
        self._it = None
        self._is_running = True
        self.response = None

    def _post(self):
        self._it = None
        self._is_running = False

    @classmethod
    def remove_comments_header(cls, sql: str):
        logger.debug("GlueCursor remove_comments_header called")
        if sql[0:2] == "/*":
            end = sql.index("*/\n")
            return sql[end + 3:]
        return sql

    def execute(self, sql, bindings=None):
        logger.debug("GlueCursor execute called")
        if self.closed:
            raise Exception("CursorClosed")
        if self._is_running:
            raise dbterrors.InternalException("CursorAlreadyRunning")
        self.sql = GlueCursor.remove_comments_header(sql)

        self._pre()
        if "--pyspark" in self.sql:
            self.code = textwrap.dedent(self.sql.replace("--pyspark", ""))
        else:
            self.code = f"SqlWrapper2.execute('''{self.sql}''')"
        self.statement = GlueStatement(
            client=self.connection.client,
            session_id=self.connection.session_id,
            code=self.code
        )
        logger.debug("client : " + self.code)
        response = self.statement.execute()
        logger.debug(response)
        self.state = response.get("Statement", {}).get("State", GlueCursorState.WAITING)
        if self.state == GlueCursorState.AVAILABLE:
            self._post()
            output = response.get("Statement", {}).get("Output", {})
            status = output.get("Status")
            if status == "ok":
                try:
                    self.response = json.loads(output.get("Data", {}).get("TextPlain", None).strip())
                except Exception as ex:
                    chunks = output.get("Data", {}).get("TextPlain", None).strip().split('\n')
                    try:
                        self.response = json.loads(chunks[0])
                    except Exception as ex:
                        logger.debug("Could not parse " + json.loads(chunks[0]), ex)
            else:
                if output.get('ErrorValue').find("is not a view"):
                    logger.debug(f"Glue returned `{status}` for statement {self.statement_id} for code {self.code}, {output.get('ErrorName')}: {output.get('ErrorValue')}")
                else:
                    raise dbterrors.DatabaseException(
                        msg=f"Glue returned `{status}` for statement {self.statement_id} for code {self.code}, {output.get('ErrorName')}: {output.get('ErrorValue')}")
        if self.state == GlueCursorState.ERROR:
            self._post()
            self.state = "error"
            raise dbterrors.InternalException
        if self.state in [GlueCursorState.CANCELLED, GlueCursorState.CANCELLING]:
            self._post()
            raise dbterrors.DatabaseException(
                msg=f"Statement {self.connection.session_id}.{self.statement_id} cancelled.")

        return self.response

    @property
    def columns(self):
        if self.response:
            return [column.get("name") for column in self.response.get("description")]

    def fetchall(self):
        logger.debug("GlueCursor fetchall called")
        if self.closed:
            raise Exception("CursorClosed")

        if self.response:
            records = []
            for item in self.response.get("results", []):
                record = []
                for column in self.columns:
                    record.append(item.get("data", {}).get(column, None))
                records.append(record)

            return records

    def fetchone(self):
        logger.debug("GlueCursor fetchone called")
        if self.closed:
            raise Exception("CursorClosed")
        if self.response:
            if not self._it:
                self._it = 0
            try:
                record = []
                item = self.response.get("results")[self._it]
                for column in self.columns:
                    record.append(item.get("data", {}).get(column, None))
                self._it = self._it + 1
                return record
            except Exception:
                self._it = None
                return None

    def __iter__(self):
        return self

    def __next__(self):
        item = self.fetchone()
        if not item:
            raise StopIteration
        return item

    def description(self):
        logger.debug("GlueCursor get_columns_in_relation called")
        if self.response:
            return [[c["name"], c["type"]] for c in self.response.get("description", [])]

    def get_response(self) -> AdapterResponse:
        logger.debug("GlueCursor get_columns_in_relation called")
        if self.statement:
            r = self.statement._get_statement()
            return AdapterResponse(
                _message=f'r["State"]',
                code=self.sql,
                **r
            )

    def close(self):
        logger.debug("GlueCursor get_columns_in_relation called")
        if self._closed:
            raise Exception("CursorAlreadyClosed")
        self._closed = True

    @property
    def closed(self):
        return self._closed


class GlueDictCursor(GlueCursor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def fetchone(self):
        logger.debug("GlueDictCursor fetchone called")
        item = super().fetchone()
        if not item:
            return None
        data = {}
        for i, c in enumerate(self.columns):
            data[c] = item[i]
        return data

    def fetchall(self):
        logger.debug("GlueDictCursor fetchall called")
        array_records = super().fetchall()
        dict_records = []
        for array_item in array_records:
            dict_record = {}
            for i, c in enumerate(self.columns):
                dict_record[c] = array_item[i]
            dict_records.append(dict_record)
        return dict_records