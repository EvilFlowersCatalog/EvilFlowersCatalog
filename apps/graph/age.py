"""Apache AGE in the catalog's own PostgreSQL (IP-030 §4, Q7).

The graph `library` is a derived projection of relational tables (`apps.graph.projection`), never the source of
truth and never the access check: every query returns entry ids, which callers filter with `EntryFilter`.

How a query runs, as verified against evilflowers-postgresql 18.6 with AGE (2026-11-13):

- `LOAD 'age'` and `SET LOCAL search_path = ag_catalog, "$user", public` inside one transaction: AGE's
  functions and operator classes resolve through the search path (`create_graph` fails without it, "operator
  class graphid_ops does not exist"). `SET LOCAL` ends with the transaction, so a pooled connection (PgBouncer in
  transaction mode) is handed back unchanged. Nothing is preloaded server-wide.
- Parameters go in as the third argument of `cypher()`, which AGE accepts only as a bound parameter ("third
  argument of cypher function must be a parameter"). Django's psycopg cursor binds on the client, so the query
  runs on a server-binding `psycopg.Cursor` over the same connection. Section headings and generated concept
  terms are therefore never part of the query text.
- Values come back as `agtype` text, which is JSON for scalars, lists and maps.

Without the extension (a development database on plain PostgreSQL) `available()` is False and every caller
degrades: no graph relation in `follow`, no concepts in `library_overview`.
"""

import json
import logging
import time
from typing import Iterable, List, Optional, Sequence

from django.db import connection, transaction

logger = logging.getLogger(__name__)

GRAPH = "library"

#: Seconds `available()` keeps its answer.
_AVAILABLE_TTL = 60
_available_cache: dict = {}


class GraphUnavailable(Exception):
    pass


def extension_installable(cursor) -> bool:
    cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'age'")
    return cursor.fetchone() is not None


def available() -> bool:
    """Whether the extension is installed and the graph exists (cached for a minute per process)."""
    now = time.monotonic()
    cached = _available_cache.get(connection.alias)
    if cached and now - cached[1] < _AVAILABLE_TTL:
        return cached[0]
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'")
            installed = cursor.fetchone() is not None
            exists = False
            if installed:
                cursor.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", [GRAPH])
                exists = cursor.fetchone() is not None
    except Exception:
        logger.exception("Could not check for the AGE graph")
        installed = exists = False
    _available_cache[connection.alias] = (installed and exists, now)
    return installed and exists


def forget() -> None:
    """Drop the cached `available()` answer (after creating or dropping the graph, and in tests)."""
    _available_cache.clear()


def _prepare(cursor) -> None:
    cursor.execute("LOAD 'age'")
    cursor.execute('SET LOCAL search_path = ag_catalog, "$user", public')


def _parse(value):
    if value is None:
        return None
    text = str(value)
    for suffix in ("::vertex", "::edge", "::path"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    try:
        return json.loads(text)
    except ValueError:
        return text


def cypher(query: str, params: Optional[dict] = None, columns: Sequence[str] = ("value",)) -> List[tuple]:
    """Run one Cypher statement on graph `library`; rows of parsed values.

    `query` is code, never data: every value goes through `params` (`$name` in the query).
    """
    import psycopg

    if "$$" in query or "%" in query:
        raise ValueError("A Cypher query may not contain $$ or %")
    returned = ", ".join(f"{column} ag_catalog.agtype" for column in columns)
    sql = f"SELECT * FROM ag_catalog.cypher('{GRAPH}', $$ {query} $$"
    arguments: list = []
    if params is not None:
        sql += ", %s"
        arguments.append(json.dumps(params, ensure_ascii=False, default=str))
    sql += f") AS ({returned})"

    with transaction.atomic():
        connection.ensure_connection()
        with psycopg.Cursor(connection.connection) as cursor:
            _prepare(cursor)
            cursor.execute(sql, arguments or None)
            rows = cursor.fetchall() if cursor.description else []
    return [tuple(_parse(value) for value in row) for row in rows]


def execute_many(statements: Iterable[tuple]) -> None:
    """Several `(query, params)` statements in one transaction (a projection is all or nothing)."""
    with transaction.atomic():
        for query, params in statements:
            cypher(query, params)


def create_graph(cursor=None) -> bool:
    """Create the graph if missing; False when AGE is not installed."""
    import psycopg

    with transaction.atomic():
        connection.ensure_connection()
        with psycopg.Cursor(connection.connection) as raw:
            raw.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'")
            if raw.fetchone() is None:
                return False
            _prepare(raw)
            raw.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", [GRAPH])
            if raw.fetchone() is None:
                raw.execute("SELECT ag_catalog.create_graph(%s)", [GRAPH])
    forget()
    return True


def drop_graph() -> None:
    import psycopg

    with transaction.atomic():
        connection.ensure_connection()
        with psycopg.Cursor(connection.connection) as raw:
            _prepare(raw)
            raw.execute("SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", [GRAPH])
            if raw.fetchone() is not None:
                raw.execute("SELECT ag_catalog.drop_graph(%s, true)", [GRAPH])
    forget()
