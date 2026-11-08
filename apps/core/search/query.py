"""
From the reader's words to filtered, ranked and highlighted entries.

The tsquery is the same for every row (so the GIN index on `search_vector` applies): the
`websearch_to_tsquery` of the text in every available configuration, OR-ed, plus a prefix term per
word (`vzdelávanie` -> `vzdeláva:*`) as the inflection fallback for languages without a dictionary.
`websearch_to_tsquery` gives readers quotes, `-word` and `or`.

Levels, tried in order by `EntryFilter` until the words match something:

1. strict: every word has to match (AND).
2. relaxed: any word may match (OR).
3. fuzzy: trigram word similarity on the title and author names (typos).

IP-021 adds two things on top of the matched set:

- BM25 (pg_search, `bm25_scores`) orders what the tsvector matched, when the index exists;
- the vector channel (e5 vectors from `tei-embed`) adds the entries closest in meaning. Both are fused
  by weighted Reciprocal Rank Fusion (words 1.0, meaning `EVILFLOWERS_SEARCH_VECTOR_WEIGHT`).
"""

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Optional

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
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
    UUIDField,
    Value,
    When,
)
from pgvector import HalfVector
from pgvector.django import CosineDistance
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


NEGATED = re.compile(r"(?:^|\s)-([^\W_]+)")


def words(text: str) -> list[str]:
    return WORD.findall((text or "").lower())


def negated_words(text: str) -> list[str]:
    """Words excluded with `-word`."""
    return [word.lower() for word in NEGATED.findall(text or "")]


def positive_words(text: str) -> list[str]:
    """The words a result should contain (everything but `-word`)."""
    negated = set(negated_words(text))
    return [word for word in words(text) if word not in negated]


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
    tokens = positive_words(text)
    if not tokens:
        return None

    if level == Level.STRICT:
        source, joiner = text, " & "
        plain = not OPERATORS.search(text)
    else:
        source, joiner = " or ".join(tokens), " | "
        plain = True

    configs = available_configs()
    parts, params = [], []
    for config in configs:
        parts.append("websearch_to_tsquery(%s::regconfig, %s)")
        params += [config, source]

    if plain:
        terms = [prefix_term(token) for token in tokens]
        if prefix_last and not terms[-1].endswith(":*"):
            terms[-1] = f"{terms[-1]}:*"
        parts.append("to_tsquery(%s::regconfig, %s)")
        params += [SIMPLE_CONFIG, joiner.join(terms)]

    sql = f"({' || '.join(parts)})"

    # `-word` excludes the word in every configuration. Inside each OR-ed websearch query it is only
    # negated in that configuration, so a stemmer that splits it differently would let it back in.
    negated = negated_words(text)
    if negated:
        exclusions = []
        for config in configs:
            exclusions.append("to_tsquery(%s::regconfig, %s)")
            params += [config, " | ".join(negated)]
        sql = f"({sql} && !!({' || '.join(exclusions)}))"

    return TsQuery(sql, tuple(params))


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


class Channel:
    LEXICAL = "lexical"
    VECTOR = "vector"
    BOTH = (LEXICAL, VECTOR)


class ScoreOf(Func):
    """`relevance_score` of a row from parallel `ids`/`scores` arrays computed in Python."""

    output_field = FloatField()

    def __init__(self, ids: list, scores: list):
        super().__init__(
            Value(ids, output_field=ArrayField(UUIDField())),
            Value(scores, output_field=ArrayField(FloatField())),
            F("pk"),
        )

    def as_sql(self, compiler, connection, **extra):
        parts, params = [], []
        for expression in self.source_expressions:
            sql, expression_params = compiler.compile(expression)
            parts.append(sql)
            params.extend(expression_params)
        ids, scores, pk = parts
        return (
            f"(SELECT s.score FROM unnest({ids}::uuid[], {scores}::float8[]) AS s(id, score) WHERE s.id = {pk})",
            params,
        )


def rrf(rankings: list[list], k: int, weights: Optional[list] = None) -> list[tuple]:
    """
    Weighted Reciprocal Rank Fusion: `Σ weight / (k + rank)` over the ranked id lists; best first,
    ties by first appearance.
    """
    weights = weights or [1.0] * len(rankings)
    scores: dict = {}
    order: dict = {}
    for ranking, weight in zip(rankings, weights):
        for rank, entry_id in enumerate(ranking, start=1):
            scores[entry_id] = scores.get(entry_id, 0.0) + weight / (k + rank)
            order.setdefault(entry_id, len(order))
    return sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))


def bm25_scores(text: str, entry_ids: list) -> dict:
    """pg_search BM25 scores of the given entries for the words of `text` (title and names weigh most)."""
    from apps.core.search.ddl import BM25_FIELDS, bm25_alias

    tokens = " ".join(positive_words(text))
    if not tokens or not entry_ids:
        return {}
    clauses, params = [], []
    boosts = {"title": 3, "search_authors": 2, "search_subjects": 2, "publisher": 1, "search_summary": 1}
    for field_name, languages in BM25_FIELDS.items():
        targets = [f"e.{field_name}"] + [
            f"e.{field_name}::pdb.alias('{bm25_alias(field_name, language)}')" for language in languages
        ]
        for target in targets:
            clauses.append(f"{target} ||| %s::pdb.boost({boosts[field_name]})")
            params.append(tokens)
    sql = "SELECT e.id, pdb.score(e.id) FROM entries e " f"WHERE e.id = ANY(%s::uuid[]) AND ({' OR '.join(clauses)})"
    with connection.cursor() as cursor:
        cursor.execute(sql, [[str(entry_id) for entry_id in entry_ids], *params])
        return {row[0]: float(row[1]) for row in cursor.fetchall()}


@dataclass
class SearchState:
    """
    One free-text search: the words, the level that produced the results, the suggestion and which
    channels found what (IP-021).

    `apply` ranks two channels and fuses them with Reciprocal Rank Fusion:

    - lexical: the IP-020 tsvector match (the level decides strict / any word / similar), ordered by
      pg_search BM25 when the index exists, else by `ts_rank_cd`;
    - vector: entries whose e5 vector is close to the query's (TEI), when embeddings are configured.

    A single channel is ranked the same way, so `relevance_score` is always an RRF score.
    """

    text: str
    level: Level = Level.STRICT
    did_you_mean: Optional[str] = None
    #: type-ahead: the last (unfinished) word also matches as a prefix
    prefix_last: bool = False
    #: `Channel.BOTH`, or one channel (OPDS `mode=keyword` / `mode=semantic`)
    channels: tuple = Channel.BOTH
    #: what the last recorded `apply` found, per channel
    lexical_ids: set = field(default_factory=set)
    vector_ids: set = field(default_factory=set)
    #: True when the vector channel was asked for but TEI did not answer
    degraded: bool = False
    _tsqueries: dict = field(default_factory=dict, repr=False)
    _vector: Optional[list] = field(default=None, repr=False)
    _vector_resolved: bool = field(default=False, repr=False)

    @property
    def relaxed(self) -> bool:
        return self.level > Level.STRICT or self.semantic_only

    @property
    def semantic_only(self) -> bool:
        """Nothing matched the words; every result is a semantic neighbour."""
        return Channel.LEXICAL in self.channels and not self.lexical_ids and bool(self.vector_ids)

    @property
    def fallback(self) -> Optional[str]:
        if self.semantic_only and self.level == Level.STRICT:
            return "semantic"
        return {Level.STRICT: None, Level.RELAXED: "any_word", Level.FUZZY: "similar"}[self.level]

    @property
    def mode(self) -> str:
        """`hybrid`, `semantic` (vectors only), `bm25` or `fulltext` (`ts_rank_cd`)."""
        from apps.core.search.index import bm25_available

        vectors = Channel.VECTOR in self.channels and self.query_vector() is not None
        if Channel.LEXICAL not in self.channels:
            return "semantic"
        if vectors:
            return "hybrid"
        return "bm25" if bm25_available() else "fulltext"

    def tsquery(self) -> Optional[TsQuery]:
        """The tsquery used for ranking and highlights (fuzzy results are highlighted with the relaxed one)."""
        level = min(self.level, Level.RELAXED)
        if level not in self._tsqueries:
            self._tsqueries[level] = build_tsquery(self.text, level, prefix_last=self.prefix_last)
        return self._tsqueries[level]

    def query_vector(self) -> Optional[list]:
        """The query embedded once per search (None: TEI unconfigured or down)."""
        if not self._vector_resolved:
            from apps.core.search import embeddings

            self._vector_resolved = True
            if Channel.VECTOR in self.channels and embeddings.enabled():
                self._vector = embeddings.try_embed_query(self.text, "entries")
                self.degraded = self._vector is None
        return self._vector

    def lexical_ranking(self, qs: QuerySet, ordered: bool = True) -> list:
        """`ordered=False` (facet counts) skips the BM25 query: only membership matters there."""
        if self.level == Level.FUZZY:
            rows = (
                qs.annotate(_lexical=fuzzy_score(self.text))
                .filter(_lexical__gte=trigram_threshold())
                .order_by("-_lexical", "-popularity", "-pk")
                .values_list("pk", flat=True)
            )
            return list(rows)

        from apps.core.search.index import bm25_available

        tsquery = self.tsquery()
        rows = list(
            qs.filter(Matches(F("search_vector"), tsquery.expression()))
            .annotate(_lexical=rank_expression(tsquery, self.text))
            .order_by("-_lexical", "-popularity", "-pk")
            .values_list("pk", flat=True)
        )
        if ordered and rows and bm25_available():
            # BM25 orders what the tsvector matched; matches BM25 cannot see (prefix, hunspell lemma)
            # keep their `ts_rank_cd` order behind them.
            scores = bm25_scores(self.text, rows)
            position = {entry_id: index for index, entry_id in enumerate(rows)}
            rows.sort(key=lambda entry_id: (-scores.get(entry_id, float("-inf")), position[entry_id]))
        return rows

    def vector_ranking(self, qs: QuerySet) -> list:
        vector = self.query_vector()
        if vector is None:
            return []
        from apps.core.search import embeddings

        try:
            model = embeddings.model_id()
        except embeddings.EmbeddingError:
            # The model is read from TEI's /info when not pinned; TEI down means words only.
            self._vector, self.degraded = None, True
            return []
        min_score = getattr(settings, "EVILFLOWERS_SEARCH_VECTOR_MIN_SCORE", 0.8)
        limit = getattr(settings, "EVILFLOWERS_SEARCH_VECTOR_CANDIDATES", 100)
        rows = (
            qs.filter(embedding__isnull=False, embedding_model=model)
            .annotate(_distance=CosineDistance("embedding", HalfVector(vector)))
            .filter(_distance__lte=1 - min_score)
            .order_by("_distance", "-pk")
            .values_list("pk", flat=True)[:limit]
        )
        return list(rows)

    def apply(self, qs: QuerySet, record: bool = True) -> QuerySet:
        """
        The entries of `qs` the search finds, annotated with `relevance_score`. `record` keeps what each
        channel found (the main result; facets apply the same search to other subsets without it).
        """
        nothing = qs.none().annotate(relevance_score=Value(0.0, output_field=FloatField()))
        if not words(self.text):
            return nothing

        lexical = self.lexical_ranking(qs, ordered=record) if Channel.LEXICAL in self.channels else []
        semantic = self.vector_ranking(qs) if Channel.VECTOR in self.channels else []
        if record:
            self.lexical_ids, self.vector_ids = set(lexical), set(semantic)

        # The words count more than the meaning: an exact match in the reader's language stays ahead of a
        # translation that is merely close (measured on the golden set, IP-021).
        vector_weight = getattr(settings, "EVILFLOWERS_SEARCH_VECTOR_WEIGHT", 0.5)
        channels = [(ranking, weight) for ranking, weight in ((lexical, 1.0), (semantic, vector_weight)) if ranking]
        fused = rrf([ranking for ranking, _ in channels], settings.EVILFLOWERS_SEARCH_RRF_K, [w for _, w in channels])
        if not fused:
            return nothing
        ids = [str(entry_id) for entry_id, _score in fused]
        scores = [round(score, 8) for _entry_id, score in fused]
        return qs.filter(pk__in=ids).annotate(relevance_score=ScoreOf(ids, scores))


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


def _headline(text: str, options: str) -> str:
    """
    `ts_headline` in the entry's configuration, or in the accent-folding `evilflowers_simple` when that
    marks nothing: a hunspell configuration does not fold accents, so `vzdelavanie` would stay unmarked.
    """
    entry = f"ts_headline(s.cfg, {text}, s.q, '{options}')"
    folded = f"ts_headline('{SIMPLE_CONFIG}', {text}, s.q, '{options}')"
    return f"(SELECT CASE WHEN strpos(h.v, '<mark>') > 0 THEN h.v ELSE {folded} END FROM (SELECT {entry} AS v) AS h)"


DESCRIBE_SQL = f"""
SELECT
    e.id,
    {_headline(_decode_and_escape('e.title'), TITLE_HEADLINE_OPTIONS)} AS title_headline,
    CASE WHEN {_vector('s.cfg', "COALESCE(regexp_replace(e.summary, '<[^>]*>', ' ', 'g'), '')")} @@ s.q
        THEN {_headline(_decode_and_escape("COALESCE(e.summary, '')"), SUMMARY_HEADLINE_OPTIONS)}
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
    # The lateral select holds the tsquery: its params come before the ids (headline options are literals).
    params = [*tsquery.params, [entry.pk for entry in entries]]
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
        if entry.pk in state.vector_ids:
            # Close in meaning (IP-021), whether or not the words matched too.
            matched.append("semantic")
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
