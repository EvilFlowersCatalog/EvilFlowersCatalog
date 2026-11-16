"""Cross-book similarity from the vectors the catalog already stores (#151).

text-service links `similar` paragraphs only inside one run, so no `similar` link joins two publications and
the graph could never draw `SIMILAR_TO` from them. Two measures are computed here instead:

**Sections** (`SectionSimilarity`, projected as `Section-[SIMILAR_TO {weight}]-Section`). A section's vector is
the mean of its paragraph embeddings. Raw cosine between two of them is 0.91-0.94 for nearly any pair (e5
vectors share a large common component), so a mean is subtracted first (`_centre`); two sections of different
runs are similar when that centred cosine is at least `MIN_SCORE`. Front and back matter (contents, index,
references ...) matched itself across every book and is left out (`is_topical`), as are sections of fewer than
`MIN_PARAGRAPHS` paragraphs.

The mean is taken **per language** of the publication. With one corpus mean, books in a minority language kept
their shared language component: on dev (2026-11-14, 149 English and 14 German books) every section of the
German *Alterssurvey* had pairs, up to 263 per section, 12.5 K with the *Freiwilligensurvey* alone. Centred per
language, it has 104 pairs over 33 of 176 sections (at most 13 per section; the two surveys share the institute's
chapter template, so "Datengrundlage" with "Datengrundlage" is right), and *Elements of Robotics* 37 pairs over
21 of 151 sections (PID control with PID control 0.71, state machines 0.64, Bayes' rule 0.62). A language with
fewer than `MIN_LANGUAGE_BOOKS` books, or none recorded, is centred by the mean of all.

The rule is a threshold, not top-k, so a pair does not depend on which run came first; only the means move a
little as books arrive. `compute_run` replaces one run's pairs, `rebuild` recomputes every pair with one set of
means (`link_extraction --steps similarity` without `--entry`). Both hold a transaction-level advisory lock, so
two runs activated at once never store their pair twice.

**Publications** (`entry_neighbours`). Raw cosine between publication vectors (`ExtractionRun.embedding`) is
0.93-0.997 for every pair on dev, so a few generalist books become everyone's neighbour. Centred (per language,
as above), and kept only when both are among each other's `ENTRY_NEIGHBOURS` nearest (mutual kNN), the pairs are
clean. Computed on the publications a caller may see, so a hidden one never shapes a visible pair.

Vectors of two embedding models are never compared.
"""

import logging
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from django.db import connection, transaction
from django.db.models import Q

logger = logging.getLogger(__name__)

#: Centred cosine of two section vectors from which they are similar.
MIN_SCORE = 0.55

#: Paragraphs a section needs before its mean says something.
MIN_PARAGRAPHS = 3

#: Books a language needs to be centred by its own mean.
MIN_LANGUAGE_BOOKS = 5

#: Rows of the similarity matrix computed at once.
BLOCK = 1024

#: Mutual nearest publications kept per publication.
ENTRY_NEIGHBOURS = 4

#: Serialises every writer of `extraction_section_similarity` (`pg_advisory_xact_lock`).
LOCK_KEY = 151_001

#: Headings of front and back matter, which match each other across every book (English, Slovak, Czech, German).
NON_TOPICAL = re.compile(
    r"^\W*(?:[\dIVXivx]+(?:\.\d+)*\.?\s+)?(?:"
    r"(?:table\s+of\s+)?contents|index|(?:subject|author|name)\s+index|references?|bibliography|"
    r"further\s+reading|acknowledge?ments?|preface|foreword|about\s+the\s+(?:authors?|editors?|contributors)|"
    r"(?:notes\s+on\s+)?contributors|list\s+of\s+\w+|abbreviations|glossary|"
    r"obsah|literat[úu]ra|pou[žz]it[áa]\s+literat[úu]ra|zoznam\s+\w+|seznam\s+\w+|register|predhovor|"
    r"p[řr]edmluva|pod[aě]kovanie|pod[ěe]kov[áa]n[íi]|o\s+autoroch|o\s+autorech|"
    r"inhaltsverzeichnis|inhalt|literaturverzeichnis|literatur|vorwort|danksagung|"
    r"abk[üu]rzungen|abk[üu]rzungsverzeichnis|stichwortverzeichnis|sachregister|"
    r"(?:die\s+)?autorinnen\s+und\s+autoren|(?:die\s+)?autoren|(?:die\s+)?herausgeber(?:innen)?"
    # The whole heading: "Reference Architecture for ..." is a topic, "References" is not.
    r")\W*$",
    re.IGNORECASE,
)


def is_topical(title: str) -> bool:
    return bool(title) and not NON_TOPICAL.match(title.strip())


def embedding_model(run) -> str:
    return ((run.provenance or {}).get("paragraphs") or {}).get("embedding_model") or ""


def _centre(matrix: np.ndarray, languages: Sequence, books: Sequence) -> np.ndarray:
    """Rows minus the mean of their language (the mean of all for a small or unknown one), at unit length.

    `languages[i]` and `books[i]` belong to row i; a language counts its distinct books. A zero row stays zero.
    """
    centred = matrix - matrix.mean(axis=0, keepdims=True)
    languages = np.asarray([str(language) if language is not None else "" for language in languages])
    sizes = Counter(language for language, _book in set(zip(languages.tolist(), [str(b) for b in books])))
    for language, size in sizes.items():
        if not language or size < MIN_LANGUAGE_BOOKS:
            continue
        rows = languages == language
        centred[rows] = matrix[rows] - matrix[rows].mean(axis=0, keepdims=True)
    norms = np.linalg.norm(centred, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return centred / norms


def section_vectors(model: str) -> Tuple[List[tuple], np.ndarray]:
    """`([(run_id, section), ...], centred unit vectors)` of the topical sections of every active run of `model`."""
    import psycopg
    from pgvector.psycopg.halfvec import register_halfvec_info
    from psycopg.types import TypeInfo

    sql = (
        "SELECT p.run_id, e.language_id, p.section, AVG(p.embedding) FROM extraction_paragraph p "
        "JOIN extraction_run r ON r.id = p.run_id "
        "JOIN acquisitions a ON a.id = r.acquisition_id "
        "JOIN entries e ON e.id = a.entry_id "
        "WHERE r.is_active AND p.section IS NOT NULL AND p.embedding IS NOT NULL "
        # #208: hidden paragraphs (reference lists, ToC, ...) inflate cosine between unrelated books.
        "AND (p.band IS NULL OR p.band <> 'hide') "
        "AND COALESCE(r.provenance->'paragraphs'->>'embedding_model', '') = %s "
        "GROUP BY p.run_id, e.language_id, p.section HAVING count(*) >= %s ORDER BY p.run_id, p.section"
    )
    with transaction.atomic():
        connection.ensure_connection()
        # Binary halfvecs on a cursor of its own: ~35 MB for 16 K sections instead of ~170 MB of text, and the
        # loader is registered on this cursor only, never on Django's connection.
        with psycopg.Cursor(connection.connection) as cursor:
            info = TypeInfo.fetch(connection.connection, "halfvec")
            if info is None:
                return [], np.zeros((0, 0), dtype=np.float32)
            register_halfvec_info(cursor, info)
            cursor.execute(sql, [model, MIN_PARAGRAPHS], binary=True)
            rows = cursor.fetchall()
    keys, languages, vectors = [], [], []
    for run_id, language, section, vector in rows:
        if vector is not None and is_topical(section):
            keys.append((run_id, section))
            languages.append(language)
            vectors.append(vector.to_numpy().astype(np.float32))
    if not vectors:
        return [], np.zeros((0, 0), dtype=np.float32)
    return keys, _centre(np.vstack(vectors), languages, [key[0] for key in keys])


def _pairs(keys: Sequence[tuple], matrix: np.ndarray, rows: Sequence[int], columns: Sequence[int]):
    """`(i, j, score)` of rows x columns at or above `MIN_SCORE`, in different runs."""
    columns = np.asarray(columns, dtype=np.int64)
    if not len(columns):
        return
    runs = np.array([str(key[0]) for key in keys])
    for start in range(0, len(rows), BLOCK):
        block = np.asarray(rows[start : start + BLOCK], dtype=np.int64)
        scores = matrix[block] @ matrix[columns].T
        scores[runs[block][:, None] == runs[columns][None, :]] = -1.0
        for i, j in np.argwhere(scores >= MIN_SCORE):
            yield int(block[i]), int(columns[j]), float(scores[i, j])


def _store(keys: Sequence[tuple], pairs: Iterable[tuple]) -> int:
    from apps.extraction.models import SectionSimilarity

    rows = [
        SectionSimilarity(
            source_id=keys[i][0],
            source_section=keys[i][1],
            target_id=keys[j][0],
            target_section=keys[j][1],
            score=round(score, 4),
        )
        for i, j, score in pairs
    ]
    SectionSimilarity.objects.bulk_create(rows, batch_size=2000)
    return len(rows)


def _lock() -> None:
    """Wait for any other writer of the table; released when the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])


def compute_run(run) -> int:
    """Replace the pairs one run has with the sections of every other active run. Returns how many."""
    from apps.extraction.models import SectionSimilarity

    with transaction.atomic():
        # Before reading: a run activated at the same time either sees this one's rows to replace, or waits.
        _lock()
        keys, matrix = section_vectors(embedding_model(run))
        mine = [index for index, key in enumerate(keys) if key[0] == run.pk]
        others = [index for index, key in enumerate(keys) if key[0] != run.pk]
        SectionSimilarity.objects.filter(Q(source=run) | Q(target=run)).delete()
        return _store(keys, _pairs(keys, matrix, mine, others))


def rebuild() -> int:
    """Every pair again, each embedding model with one set of means. Returns how many."""
    from apps.extraction.models import ExtractionRun, SectionSimilarity

    models = {embedding_model(run) for run in ExtractionRun.objects.filter(is_active=True).only("provenance")}
    total = 0
    with transaction.atomic():
        _lock()
        SectionSimilarity.objects.all().delete()
        for model in sorted(models):
            keys, matrix = section_vectors(model)
            for start in range(0, len(keys), BLOCK):
                rows = list(range(start, min(start + BLOCK, len(keys))))
                # Each unordered pair once: columns after the block's first row, the rest filtered below.
                pairs = ((i, j, score) for i, j, score in _pairs(keys, matrix, rows, range(start, len(keys))) if j > i)
                total += _store(keys, pairs)
    logger.info("Section similarity: %s pairs", total)
    return total


def entry_neighbours(vectors: Dict, k: int = ENTRY_NEIGHBOURS, languages: Optional[Dict] = None) -> List[tuple]:
    """`[(entry_a, entry_b, score)]`: publications among each other's `k` nearest, by centred cosine.

    `vectors` is `{entry_id: vector}` of one embedding model, `languages` `{entry_id: language}` (centring per
    language, see the module). Each pair once, best first; deterministic for the same input.
    """
    ids = sorted(vectors, key=str)
    if len(ids) < 2:
        return []
    languages = languages or {}
    matrix = _centre(
        np.vstack([np.asarray(vectors[entry_id], dtype=np.float32) for entry_id in ids]),
        [languages.get(entry_id) for entry_id in ids],
        ids,
    )
    scores = matrix @ matrix.T
    np.fill_diagonal(scores, -np.inf)
    k = min(k, len(ids) - 1)
    # Ties broken by index (the sorted ids), so the result never depends on the order of `vectors`.
    nearest = [set(np.argsort(-row, kind="stable")[:k].tolist()) for row in scores]
    # Nearest is not near: a pair pointing away from each other once centred (two books on unrelated subjects in
    # a small library) is no edge.
    pairs = [
        (ids[i], ids[j], round(float(scores[i, j]), 4))
        for i in range(len(ids))
        for j in nearest[i]
        if i < j and i in nearest[j] and scores[i, j] > 0
    ]
    return sorted(pairs, key=lambda pair: (-pair[2], str(pair[0]), str(pair[1])))
