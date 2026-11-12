"""
Database objects the search index needs, created idempotently.

Every configuration is prefixed `evilflowers_` so it never collides with the core ones:

- `evilflowers_simple`: no stemming, accents folded (`unaccent` runs before `simple`). Every entry is
  indexed with it as well, so an exact word matches whatever language the entry is tagged with.
- `evilflowers_english` / `_german` / `_hungarian`: accents folded, then the core snowball stemmer.
- `evilflowers_slovak` / `_czech`: hunspell lemmatisation. PostgreSQL ships no Slovak or Czech
  dictionary; the image adds the files (`sk_sk.dict/.affix`, `slovak.stop`, ... in `tsearch_data`,
  evilflowers-postgresql#1). Where the files are missing the configuration is skipped, entries in that
  language use `evilflowers_simple`, and `manage.py reindex_search` creates it once the files arrive.

Accents are folded inside the configurations (not by wrapping the text in `unaccent()`), so
`ts_headline` can highlight the original, accented text.
"""

import logging

from django.db import DatabaseError, connection as default_connection, transaction

logger = logging.getLogger(__name__)

SIMPLE_CONFIG = "evilflowers_simple"

# configuration -> snowball dictionary
SNOWBALL_CONFIGS = {
    "evilflowers_english": "english_stem",
    "evilflowers_german": "german_stem",
    "evilflowers_hungarian": "hungarian_stem",
}

# configuration -> (dictionary name, dict/affix file stem, stopword file stem)
HUNSPELL_CONFIGS = {
    "evilflowers_slovak": ("slovak_hunspell", "sk_sk", "slovak"),
    "evilflowers_czech": ("czech_hunspell", "cs_cz", "czech"),
}


def _stop_dictionary(hunspell: str) -> str:
    """`slovak_hunspell` -> `slovak_stop` (evilflowers-postgresql README naming)."""
    return hunspell.replace("_hunspell", "_stop")


# Token types that can carry non-ASCII letters; ASCII token types need no unaccent.
ACCENTED_TOKENS = "word, hword, hword_part"
ALL_WORD_TOKENS = "asciiword, word, hword, hword_part, asciihword, hword_asciipart"

UNACCENT_FUNCTION = """
CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
    AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$
"""


def _config_exists(cursor, name: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_ts_config WHERE cfgname = %s", [name])
    return cursor.fetchone() is not None


def _dictionary_exists(cursor, name: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_ts_dict WHERE dictname = %s", [name])
    return cursor.fetchone() is not None


def ensure_search_objects(connection=None) -> list[str]:
    """
    Create the unaccent wrapper and every text search configuration that can be created here.
    Returns the names of the `evilflowers_*` configurations present afterwards.
    """
    from apps.core.search import index

    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute(UNACCENT_FUNCTION)

        if not _config_exists(cursor, SIMPLE_CONFIG):
            cursor.execute(f"CREATE TEXT SEARCH CONFIGURATION {SIMPLE_CONFIG} (COPY = simple)")
            cursor.execute(
                f"ALTER TEXT SEARCH CONFIGURATION {SIMPLE_CONFIG} "
                f"ALTER MAPPING FOR {ACCENTED_TOKENS} WITH unaccent, simple"
            )

        for name, stemmer in SNOWBALL_CONFIGS.items():
            if _config_exists(cursor, name):
                continue
            source = stemmer.removesuffix("_stem")
            cursor.execute(f"CREATE TEXT SEARCH CONFIGURATION {name} (COPY = {source})")
            cursor.execute(
                f"ALTER TEXT SEARCH CONFIGURATION {name} ALTER MAPPING FOR {ACCENTED_TOKENS} WITH unaccent, {stemmer}"
            )

        for name, (dictionary, files, stopwords) in HUNSPELL_CONFIGS.items():
            if _config_exists(cursor, name):
                continue
            # CREATE DICTIONARY reads the files; without them it fails. The atomic block (a
            # savepoint inside a migration) keeps the surrounding transaction usable.
            # The ispell template checks its stop list against the *lemma* (Slovak `je` → `jesť`),
            # so a `simple` dictionary with `Accept = false` drops stop words as written first.
            stop = _stop_dictionary(dictionary)
            try:
                with transaction.atomic(using=connection.alias):
                    if not _dictionary_exists(cursor, dictionary):
                        cursor.execute(
                            f"CREATE TEXT SEARCH DICTIONARY {dictionary} "
                            f"(TEMPLATE = ispell, DictFile = {files}, AffFile = {files}, StopWords = {stopwords})"
                        )
                    if not _dictionary_exists(cursor, stop):
                        cursor.execute(
                            f"CREATE TEXT SEARCH DICTIONARY {stop} "
                            f"(TEMPLATE = simple, StopWords = {stopwords}, Accept = false)"
                        )
                    cursor.execute(f"CREATE TEXT SEARCH CONFIGURATION {name} (COPY = simple)")
                    cursor.execute(
                        f"ALTER TEXT SEARCH CONFIGURATION {name} "
                        f"ALTER MAPPING FOR {ALL_WORD_TOKENS} WITH {stop}, {dictionary}, simple"
                    )
            except DatabaseError as error:
                logger.warning("Text search configuration %s not created (%s); using %s", name, error, SIMPLE_CONFIG)

    index.reset_config_cache()
    return index.available_configs(connection)


BM25_INDEX = "entries_bm25"

# BM25 fields (IP-021): every text is indexed accent-folded as written, and the longer texts also
# stemmed in English, German and Czech (pg_search has no Slovak stemmer; Czech is close enough to
# match Slovak inflection, measured on the golden set). Aliases name the extra tokenizations.
BM25_FIELDS = {
    "title": ("en", "de", "cs"),
    "search_authors": (),
    "search_subjects": ("en", "cs"),
    "publisher": (),
    "search_summary": ("en", "de", "cs"),
}
BM25_STEMMERS = {"en": "english", "de": "german", "cs": "czech"}


def bm25_alias(field: str, language: str) -> str:
    return f"{field}_{language}"


def _bm25_index_sql() -> str:
    columns = ["id"]
    for field, languages in BM25_FIELDS.items():
        columns.append(f"({field}::pdb.simple('ascii_folding=true'))")
        for language in languages:
            columns.append(
                f"({field}::pdb.simple('stemmer={BM25_STEMMERS[language]}', 'ascii_folding=true', "
                f"'alias={bm25_alias(field, language)}'))"
            )
    return f"CREATE INDEX IF NOT EXISTS {BM25_INDEX} ON entries USING bm25 ({', '.join(columns)})"


def ensure_bm25(connection=None) -> bool:
    """
    `CREATE EXTENSION pg_search` (needs a superuser: the catalog's role today, Ansible once roles are
    split) and the BM25 index on `entries`. Returns whether BM25 search is available; without the
    extension the catalog keeps ranking with `ts_rank_cd`.
    """
    from apps.core.search import index

    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'")
        if cursor.fetchone() is None:
            logger.warning("pg_search is not installed in this PostgreSQL; search ranks without BM25")
            index.reset_config_cache()
            return False
        try:
            with transaction.atomic(using=connection.alias):
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
                cursor.execute(_bm25_index_sql())
        except DatabaseError as error:
            logger.warning("BM25 index not created (%s); search ranks without BM25", error)
    index.reset_config_cache()
    return index.bm25_available(connection)


def drop_bm25(connection=None) -> None:
    connection = connection or default_connection
    with connection.cursor() as cursor:
        cursor.execute(f"DROP INDEX IF EXISTS {BM25_INDEX}")


def drop_search_objects(connection=None) -> None:
    connection = connection or default_connection
    with connection.cursor() as cursor:
        for name in [SIMPLE_CONFIG, *SNOWBALL_CONFIGS, *HUNSPELL_CONFIGS]:
            cursor.execute(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS {name}")
        for dictionary, _files, _stopwords in HUNSPELL_CONFIGS.values():
            cursor.execute(f"DROP TEXT SEARCH DICTIONARY IF EXISTS {dictionary}")
            cursor.execute(f"DROP TEXT SEARCH DICTIONARY IF EXISTS {_stop_dictionary(dictionary)}")
        cursor.execute("DROP FUNCTION IF EXISTS f_unaccent(text)")
