"""
Maintains `entries.search_vector` and `entries.search_config`.

One UPDATE recomputes the vector of any set of entries from the row and its relations:

- weight A: title
- weight B: author names, category terms and labels (every translation)
- weight C: publisher and summary (HTML tags stripped)

Each part is indexed twice, `to_tsvector(<entry config>, ...) || to_tsvector('evilflowers_simple', ...)`:
stemmed for the entry's language and exact, so an entry tagged with the wrong language still
matches the words it contains. Signals (`apps.core.search.signals`) call `refresh_search_vectors`
whenever an entry, its authors or its categories change.
"""

import json
from typing import Iterable, Optional

from django.db import connection as default_connection

from apps.core.search.ddl import SIMPLE_CONFIG

# Entry language (ISO 639-1) -> preferred configuration; used only when it exists in the database.
LANGUAGE_CONFIGS = {
    "en": "evilflowers_english",
    "de": "evilflowers_german",
    "hu": "evilflowers_hungarian",
    "sk": "evilflowers_slovak",
    "cs": "evilflowers_czech",
}

_available: Optional[list[str]] = None
_bm25: Optional[bool] = None


def reset_config_cache() -> None:
    global _available, _bm25
    _available = None
    _bm25 = None


def bm25_available(connection=None) -> bool:
    """The BM25 index on `entries` exists (pg_search installed and indexed; cached per process)."""
    global _bm25
    if _bm25 is None:
        from apps.core.search.ddl import BM25_INDEX

        connection = connection or default_connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", [BM25_INDEX])
            _bm25 = cursor.fetchone() is not None
    return _bm25


def available_configs(connection=None) -> list[str]:
    """The `evilflowers_*` configurations present in the database (cached per process)."""
    global _available
    if _available is None:
        connection = connection or default_connection
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT cfgname FROM pg_ts_config WHERE cfgname LIKE %s ORDER BY cfgname", ["evilflowers\\_%"]
            )
            _available = [row[0] for row in cursor.fetchall()]
    return _available


def language_configs(connection=None) -> dict[str, str]:
    """Language -> configuration, restricted to configurations that exist."""
    available = set(available_configs(connection))
    return {code: config for code, config in LANGUAGE_CONFIGS.items() if config in available}


def _vector(part: str) -> str:
    return f"(to_tsvector(s.cfg::regconfig, {part}) || to_tsvector('{SIMPLE_CONFIG}', {part}))"


REFRESH_SQL = f"""
UPDATE entries AS e
SET search_config = s.cfg,
    search_authors = NULLIF(s.authors, ''),
    search_subjects = NULLIF(s.subjects, ''),
    search_summary = NULLIF(s.summary, ''),
    search_vector = setweight({_vector('s.a')}, 'A')
                 || setweight({_vector('s.b')}, 'B')
                 || setweight({_vector('s.c')}, 'C')
FROM (
    SELECT t.*,
           t.title AS a,
           concat_ws(' ', t.authors, t.subjects) AS b,
           concat_ws(' ', t.publisher, t.summary) AS c
    FROM (
        SELECT
            x.id,
            COALESCE(%(configs)s::jsonb ->> l.alpha2, '{SIMPLE_CONFIG}') AS cfg,
            COALESCE(x.title, '') AS title,
            x.publisher,
            COALESCE((
                SELECT string_agg(concat_ws(' ', au.name, au.surname), ' ' ORDER BY ea.position)
                FROM entry_authors ea JOIN authors au ON au.id = ea.author_id
                WHERE ea.entry_id = x.id
            ), '') AS authors,
            COALESCE((
                SELECT string_agg(
                    concat_ws(
                        ' ',
                        c.term,
                        CASE
                            WHEN jsonb_typeof(c.label) = 'object'
                                THEN (SELECT string_agg(t.value, ' ') FROM jsonb_each_text(c.label) AS t)
                            ELSE c.label #>> '{{}}'
                        END
                    ),
                    ' '
                )
                FROM entry_categories ec JOIN categories c ON c.id = ec.category_id
                WHERE ec.entry_id = x.id
            ), '') AS subjects,
            COALESCE(btrim(regexp_replace(x.summary, '<[^>]*>', ' ', 'g')), '') AS summary
        FROM entries x
        LEFT JOIN languages l ON l.id = x.language_id
        WHERE %(all)s OR x.id = ANY(%(ids)s)
    ) AS t
) AS s
WHERE e.id = s.id
"""


def refresh_search_vectors(entry_ids: Optional[Iterable] = None, connection=None) -> int:
    """Recompute the search vector of the given entries (all entries when `entry_ids` is None)."""
    ids = None if entry_ids is None else [entry_id for entry_id in entry_ids if entry_id is not None]
    if ids is not None and not ids:
        return 0

    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute(
            REFRESH_SQL,
            {
                "configs": json.dumps(language_configs(connection)),
                "all": ids is None,
                "ids": ids or [],
            },
        )
        return cursor.rowcount


__all__ = [
    "LANGUAGE_CONFIGS",
    "available_configs",
    "bm25_available",
    "language_configs",
    "refresh_search_vectors",
    "reset_config_cache",
]
