"""
From the reader's words to filtered, ranked and highlighted entries.

The tsquery is the same for every row (so the GIN index on `search_vector` applies): the
`websearch_to_tsquery` of the text in every available configuration, OR-ed, plus a prefix term per
word (`vzdelávanie` -> `vzdeláva:*`) as the inflection fallback for languages without a dictionary.
`websearch_to_tsquery` gives readers quotes, `-word` and `or`.

Levels, tried in order by `EntryFilter` until one returns entries:

1. strict: every word has to match (AND).
2. relaxed: any word may match (OR).
3. fuzzy: trigram word similarity on the title and author names (typos).
"""

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Optional

from django.conf import settings
from django.contrib.postgres.search import SearchQueryField, TrigramWordSimilarity
from django.db import connection
from django.db.models import (
    BooleanField,
    Case,
    F,
    FloatField,
    Func,
    OuterRef,
    QuerySet,
    Subquery,
    TextField,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Cast, Coalesce, Concat, Greatest

from apps.core.search.ddl import SIMPLE_CONFIG
from apps.core.search.index import available_configs

WORD = re.compile(r"[^\W_]+")
# websearch syntax: a quoted phrase, a negated word or `or`
OPERATORS = re.compile(r"\"|(?:^|\s)-\w|(?:^|\s)or(?:\s|$)", re.IGNORECASE)

TITLE_PHRASE_BOOST = 0.5
POPULARITY_WEIGHT = 0.01
MIN_PREFIX_LENGTH = 5

HEADLINE_OPTIONS = "StartSel=<mark>, StopSel=</mark>"
TITLE_HEADLINE_OPTIONS = f"HighlightAll=true, {HEADLINE_OPTIONS}"
SUMMARY_HEADLINE_OPTIONS = f'MaxFragments=2, MinWords=6, MaxWords=24, FragmentDelimiter=" … ", {HEADLINE_OPTIONS}'


class Level(IntEnum):
    STRICT = 0
    RELAXED = 1
    FUZZY = 2


def words(text: str) -> list[str]:
    return WORD.findall((text or "").lower())


def prefix_term(word: str) -> str:
    """`vzdelávanie` -> `vzdeláva:*`: drop up to three trailing letters (case endings), keep at least four."""
    if len(word) < MIN_PREFIX_LENGTH:
        return word
    return f"{word[: max(4, len(word) - 3)]}:*"


def trigram_threshold() -> float:
    return getattr(settings, "EVILFLOWERS_SEARCH_TRIGRAM_THRESHOLD", 0.45)


@dataclass(frozen=True)
class TsQuery:
    """A tsquery as SQL; `expression()` embeds it in ORM queries, `sql`/`params` in raw SQL."""

    sql: str
    params: tuple

    def expression(self) -> RawSQL:
        return RawSQL(self.sql, self.params, output_field=SearchQueryField())


def build_tsquery(text: str, level: Level = Level.STRICT, *, prefix_last: bool = False) -> Optional[TsQuery]:
    """
    `prefix_last` (type-ahead) also matches the last, still-being-typed word as a prefix.
    Returns None when the text has no words.
    """
    tokens = words(text)
    if not tokens:
        return None

    if level == Level.STRICT:
        source, joiner = text, " & "
        plain = not OPERATORS.search(text)
    else:
        source, joiner = " or ".join(tokens), " | "
        plain = True

    parts, params = [], []
    for config in available_configs():
        parts.append("websearch_to_tsquery(%s::regconfig, %s)")
        params += [config, source]

    if plain:
        terms = [prefix_term(token) for token in tokens]
        if prefix_last and not terms[-1].endswith(":*"):
            terms[-1] = f"{terms[-1]}:*"
        parts.append("to_tsquery(%s::regconfig, %s)")
        params += [SIMPLE_CONFIG, joiner.join(terms)]

    return TsQuery(f"({' || '.join(parts)})", tuple(params))


class Matches(Func):
    template = "%(expressions)s"
    arg_joiner = " @@ "
    output_field = BooleanField()


class Unaccent(Func):
    """The IMMUTABLE `f_unaccent` wrapper (migration 0046) — usable in index expressions."""

    function = "f_unaccent"
    output_field = TextField()


def rank_expression(tsquery: TsQuery, text: str):
    """`ts_rank_cd` (cover density, normalised to 0..1) + exact title phrase + a little popularity."""
    return (
        Func(F("search_vector"), tsquery.expression(), Value(32), function="ts_rank_cd", output_field=FloatField())
        + Case(
            When(title__unaccent__icontains=text.strip(), then=Value(TITLE_PHRASE_BOOST)),
            default=Value(0.0),
            output_field=FloatField(),
        )
        + Func(Cast(F("popularity"), FloatField()) + Value(1.0), function="ln", output_field=FloatField())
        * Value(POPULARITY_WEIGHT)
    )


def fuzzy_score(text: str):
    """Best trigram word similarity of the text against the title or any author name (0..1)."""
    from apps.core.models import EntryAuthor

    needle = Unaccent(Value(text.strip()))
    author = Subquery(
        EntryAuthor.objects.filter(entry_id=OuterRef("pk"))
        .annotate(
            similarity=TrigramWordSimilarity(
                needle, Unaccent(Concat(F("author__name"), Value(" "), F("author__surname")))
            )
        )
        .order_by("-similarity")
        .values("similarity")[:1],
        output_field=FloatField(),
    )
    return Greatest(TrigramWordSimilarity(needle, Unaccent(F("title"))), Coalesce(author, Value(0.0)))


@dataclass
class SearchState:
    """One free-text search: the words, the level that produced the results and the suggestion."""

    text: str
    level: Level = Level.STRICT
    did_you_mean: Optional[str] = None
    #: type-ahead: the last (unfinished) word also matches as a prefix
    prefix_last: bool = False
    _tsqueries: dict = field(default_factory=dict, repr=False)

    @property
    def relaxed(self) -> bool:
        return self.level > Level.STRICT

    @property
    def fallback(self) -> Optional[str]:
        return {Level.STRICT: None, Level.RELAXED: "any_word", Level.FUZZY: "similar"}[self.level]

    def tsquery(self) -> Optional[TsQuery]:
        """The tsquery used for ranking and highlights (fuzzy results are highlighted with the relaxed one)."""
        level = min(self.level, Level.RELAXED)
        if level not in self._tsqueries:
            self._tsqueries[level] = build_tsquery(self.text, level, prefix_last=self.prefix_last)
        return self._tsqueries[level]

    def apply(self, qs: QuerySet) -> QuerySet:
        if not words(self.text):
            return qs.none()

        if self.level == Level.FUZZY:
            return qs.annotate(relevance_score=fuzzy_score(self.text)).filter(relevance_score__gte=trigram_threshold())

        tsquery = self.tsquery()
        return qs.annotate(relevance_score=rank_expression(tsquery, self.text)).filter(
            Matches(F("search_vector"), tsquery.expression())
        )


def _decode_and_escape(column: str) -> str:
    """SQL: strip tags, decode the common entities, then HTML-escape (headlines are inserted as HTML)."""
    sql = f"regexp_replace({column}, '<[^>]*>', ' ', 'g')"
    for entity, char in (("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "''"), ("&lt;", "<"), ("&gt;", ">")):
        sql = f"replace({sql}, '{entity}', '{char}')"
    sql = f"replace({sql}, '&amp;', '&')"
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        sql = f"replace({sql}, '{char}', '{entity}')"
    return sql


def _vector(config: str, text: str) -> str:
    return f"(to_tsvector({config}, {text}) || to_tsvector('{SIMPLE_CONFIG}', {text}))"


DESCRIBE_SQL = f"""
SELECT
    e.id,
    ts_headline(s.cfg, {_decode_and_escape('e.title')}, s.q, %s) AS title_headline,
    CASE WHEN {_vector('s.cfg', "COALESCE(regexp_replace(e.summary, '<[^>]*>', ' ', 'g'), '')")} @@ s.q
        THEN ts_headline(s.cfg, {_decode_and_escape("COALESCE(e.summary, '')")}, s.q, %s)
    END AS summary_headline,
    {_vector('s.cfg', 'e.title')} @@ s.q AS matched_title,
    EXISTS (
        SELECT 1 FROM entry_authors ea JOIN authors au ON au.id = ea.author_id
        WHERE ea.entry_id = e.id AND {_vector('s.cfg', "concat_ws(' ', au.name, au.surname)")} @@ s.q
    ) AS matched_author,
    EXISTS (
        SELECT 1 FROM entry_categories ec JOIN categories c ON c.id = ec.category_id
        WHERE ec.entry_id = e.id
          AND {_vector('s.cfg', "concat_ws(' ', c.term, jsonb_path_query_array(c.label, '$.*')::text)")} @@ s.q
    ) AS matched_category,
    {_vector('s.cfg', "COALESCE(regexp_replace(e.summary, '<[^>]*>', ' ', 'g'), '')")} @@ s.q AS matched_summary
FROM entries e
CROSS JOIN LATERAL (
    SELECT COALESCE(e.search_config, '{SIMPLE_CONFIG}')::regconfig AS cfg, {{tsquery}} AS q
) AS s
WHERE e.id = ANY(%s)
"""


def describe_hits(state: SearchState, entries: Iterable) -> dict:
    """
    Per entry of one result page: `{score, matched: [...], highlights: {title, summary}}`.
    Highlights are HTML-escaped text with `<mark>` around the matched words.
    """
    entries = list(entries)
    tsquery = state.tsquery()
    if not entries or tsquery is None:
        return {}

    sql = DESCRIBE_SQL.replace("{tsquery}", tsquery.sql)
    params = [TITLE_HEADLINE_OPTIONS, SUMMARY_HEADLINE_OPTIONS, *tsquery.params, [entry.pk for entry in entries]]
    # The lateral select holds the tsquery: its params come after the headline options and before the ids.
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = {row[0]: row for row in cursor.fetchall()}

    result = {}
    for entry in entries:
        row = rows.get(entry.pk)
        matched, highlights = [], {"title": None, "summary": None}
        if row is not None:
            _, title_headline, summary_headline, *flags = row
            matched = [name for name, hit in zip(("title", "author", "category", "summary"), flags) if hit]
            highlights = {
                "title": title_headline if "title" in matched else None,
                "summary": summary_headline,
            }
        score = getattr(entry, "relevance_score", None)
        result[entry.pk] = {
            "score": round(float(score), 6) if score is not None else None,
            "matched": matched,
            "highlights": highlights,
        }
    return result


SUGGEST_WORDS_SQL = """
SELECT word
FROM (
    SELECT DISTINCT lower(w) AS word
    FROM entries e,
         regexp_split_to_table(
             concat_ws(' ', e.title, (
                 SELECT string_agg(concat_ws(' ', au.name, au.surname), ' ')
                 FROM entry_authors ea JOIN authors au ON au.id = ea.author_id
                 WHERE ea.entry_id = e.id
             )),
             '[^[:alnum:]]+'
         ) AS w
    WHERE e.id IN ({entries}) AND length(w) >= 3
) AS lexicon
WHERE similarity(f_unaccent(word), f_unaccent(%s)) >= %s
ORDER BY similarity(f_unaccent(word), f_unaccent(%s)) DESC, word
LIMIT 1
"""


def did_you_mean(text: str, readable: QuerySet) -> Optional[str]:
    """
    The query with each unknown word replaced by the most similar title word or author name among
    the entries the reader may see (`readable`). None when nothing better is found.
    """
    tokens = words(text)
    if not tokens:
        return None

    subquery, subquery_params = readable.order_by().values("pk").query.sql_with_params()
    sql = SUGGEST_WORDS_SQL.replace("{entries}", subquery)
    threshold = getattr(settings, "EVILFLOWERS_SEARCH_SUGGEST_THRESHOLD", 0.3)

    corrected, changed = [], False
    with connection.cursor() as cursor:
        for token in tokens:
            if len(token) < 3 or token.isdigit():
                corrected.append(token)
                continue
            cursor.execute(sql, [*subquery_params, token, threshold, token])
            row = cursor.fetchone()
            replacement = row[0] if row else token
            if row and _fold(replacement) != _fold(token):
                changed = True
            corrected.append(replacement if row else token)

    return " ".join(corrected) if changed else None


def _fold(word: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c)).lower()


__all__ = [
    "Level",
    "SearchState",
    "TsQuery",
    "build_tsquery",
    "describe_hits",
    "did_you_mean",
    "prefix_term",
    "words",
]
