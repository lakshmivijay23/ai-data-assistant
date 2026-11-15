"""Safety layer: validate SQL, run it read-only with a timeout and row cap, scrub text.

Defence in depth (each layer works even if the others fail):
 1. Data layer  : phone/email are stored only in restricted.players; the assistant queries the
                  `players` VIEW, which has no such columns (see build_db.py).
 2. Allowlist   : DuckDB has no GRANTs, so this plays their role. Every table/view/function a
                  query references must be on the allowlist, checked on DuckDB's own parse
                  tree: restricted.*, information_schema, query_table('...'), files and
                  anything that cannot be parsed are refused before the query runs.
                  (On ClickHouse: GRANT SELECT on the view only; this becomes a second line.)
 3. Validator   : exactly ONE statement, and it must be a SELECT; PII words are rejected.
 4. Connection  : read_only=True, external access disabled and the configuration locked, so
                  the ENGINE refuses writes, ATTACH, read_csv and SET to undo any of this.
 5. Limits      : timeout (query is interrupted) and a hard row cap.
 6. Scrubber    : text that looks like an email or phone number is masked, both in query
                  results and in the user's question (before it reaches the AI or the log).
"""
import json
import re
import threading
import time
from dataclasses import dataclass

import duckdb

from .build_db import ASSISTANT_OBJECTS
from .config import DB_PATH, MAX_ROWS, QUERY_TIMEOUT_SECONDS

# Table functions the assistant may use (pure generators; none of them can read a table).
ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})

PII_WORDS = re.compile(r"\b(phone|phones|email|emails|e_mail|mobile|msisdn|contact_number)\b", re.I)
# Bounded repeats ({1,64} etc.) keep matching linear. An unbounded `[\w.+-]+@` re-scans the rest
# of the string from every position (quadratic), which froze the server on one long text value.
EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}")
MAX_CELL_CHARS = 1000      # longer text values in a result are cut (also bounds the scrubber's work)
# Phone-shaped text: +<country code...>, or 10-13 digits in a row, or 5-5 grouping.
# Deliberately does NOT match dates/timestamps like 2026-10-31 or 2026-10-31 23:47:00.
PHONE_RE = re.compile(r"(?<![\d-])(?:\+\d[\d ()-]{8,14}\d|\d{10,13}|\d{5}[ -]\d{5})(?![\d-])")


class UnsafeQuery(Exception):
    """Raised when SQL is rejected for safety reasons. Never retried."""


class BadSql(Exception):
    """The model's reply is not valid SQL (empty, prose around it, syntax error).
    Not a safety problem, so the model gets one chance to correct it."""


@dataclass
class QueryResult:
    columns: list
    rows: list
    truncated: bool
    elapsed_ms: int


def validate_sql(sql: str) -> str:
    """Return cleaned SQL, or raise UnsafeQuery."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise BadSql("The model returned no SQL.")
    if PII_WORDS.search(sql):
        raise UnsafeQuery("Personal data (phone/email) is not available.")
    try:
        statements = duckdb.extract_statements(sql)   # DuckDB's own parser, not regex
    except Exception as e:
        raise BadSql(f"Could not parse SQL: {e}")
    if len(statements) != 1:
        raise UnsafeQuery("Only a single statement is allowed.")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise UnsafeQuery("Only SELECT queries are allowed.")
    check_objects(sql)
    return sql


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def check_objects(sql: str) -> None:
    """Reject the query unless every table/view/table-function it touches is allowlisted.
    Uses DuckDB's own parser (json_serialize_sql), so aliases, sub-queries, CTEs and
    quoting tricks are all seen exactly as the engine will see them. Fails closed."""
    try:
        with duckdb.connect() as parser:      # empty in-memory DB: only parses, never runs the SQL
            tree = json.loads(parser.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    except Exception as e:
        raise UnsafeQuery(f"Could not analyse SQL: {e}")
    if tree.get("error"):
        raise UnsafeQuery("Could not analyse SQL.")
    nodes = list(_walk(tree))
    cte_names = {item["key"].lower() for n in nodes if isinstance(n.get("cte_map"), dict)
                 for item in n["cte_map"].get("map", [])}
    for n in nodes:
        kind = n.get("type")
        if kind == "BASE_TABLE":
            schema, catalog = n.get("schema_name", ""), n.get("catalog_name", "")
            name = n.get("table_name", "").lower()
            if catalog or schema.lower() not in ("", "main"):
                raise UnsafeQuery(f"Access to '{schema or catalog}.{name}' is not allowed.")
            if name not in ASSISTANT_OBJECTS and not (not schema and name in cte_names):
                raise UnsafeQuery(f"Table '{name}' is not available to the assistant.")
        elif kind == "TABLE_FUNCTION":
            fn = (n.get("function") or {}).get("function_name", "").lower()
            if fn not in ALLOWED_TABLE_FUNCTIONS:
                raise UnsafeQuery(f"Function '{fn}' is not allowed.")


def scrub_text(text: str) -> str:
    """Mask anything shaped like an email address or phone number."""
    return PHONE_RE.sub("[REDACTED]", EMAIL_RE.sub("[REDACTED]", text))


def _scrub(value):
    if not isinstance(value, str):
        return value
    if len(value) > MAX_CELL_CHARS:                 # cut first, so scrubbing work stays bounded
        value = value[:MAX_CELL_CHARS] + "…[truncated]"
    return scrub_text(value)


def connect_readonly(db_path=DB_PATH):
    """The ONLY way the project opens the warehouse. DuckDB requires every connection to the
    same file in one process to use identical settings, so everything goes through here."""
    return duckdb.connect(str(db_path), read_only=True, config={
        "enable_external_access": False,   # no read_csv('/etc/passwd'), no COPY TO, no ATTACH
        "lock_configuration": True,        # settings above can no longer be changed
        "memory_limit": "1GB",
        "threads": 2,
    })


def run_query(sql: str, db_path=DB_PATH, timeout_s: float = QUERY_TIMEOUT_SECONDS,
              max_rows: int = MAX_ROWS) -> QueryResult:
    sql = validate_sql(sql)
    con = connect_readonly(db_path)
    timer = threading.Timer(timeout_s, con.interrupt)   # kills the query after timeout_s
    start = time.perf_counter()
    timer.start()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)               # fetch one extra to detect truncation
    except duckdb.InterruptException:
        raise UnsafeQuery(f"Query took longer than {timeout_s:.0f}s and was stopped.")
    finally:
        timer.cancel()
        con.close()
    truncated = len(rows) > max_rows
    rows = [tuple(_scrub(v) for v in r) for r in rows[:max_rows]]
    return QueryResult(columns, rows, truncated, int((time.perf_counter() - start) * 1000))
