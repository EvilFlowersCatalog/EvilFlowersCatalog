# OPDS 2.0 Search Modes

> IP-020 (full text, facets) and IP-021 (BM25, semantic, hybrid). Every mode runs in the catalog's
> PostgreSQL; the evilflowers-search-service is gone.

```
GET /opds/v2/{catalog}/search?query=<query>&mode=<mode>
```

The entry filters (`language_code`, `category_id`, `author_id`, `feed_id`, `published_at__gte/lte`, ...)
apply in every mode, and the feed carries OPDS 2.0 `facets` with `numberOfItems`.

## Modes

### `mode=catalog` (default) — hybrid

- **Words:** the full-text index (`entries.search_vector`: title, authors, categories, publisher, summary;
  stemmed per language, accent-free, Slovak/Czech lemmatised with hunspell), ordered by **BM25**
  (ParadeDB `pg_search`) where the extension is installed, else by `ts_rank_cd`. When no entry has every
  word, any word, then similar titles/authors (typos).
- **Meaning:** the entries whose `multilingual-e5-large-instruct` vector (title, authors, categories,
  summary) is closest to the query's, from the shared `tei-embed`.
- Both lists are fused by weighted Reciprocal Rank Fusion (words 1.0, meaning 0.5), so an exact match in
  the reader's language stays first and a translation or a paraphrase follows.

Without embeddings configured, or when TEI does not answer, the search is words only.

```http
GET /opds/v2/stu/search?query=umelá+inteligencia
```

### `mode=keyword`

Words only (BM25 / full text, with the same fallbacks).

### `mode=semantic`

Meaning only: entries above the similarity floor, closest first. Empty when embeddings are not
configured.

## Catalog scoping

Every mode searches `Entry.objects.filter(catalog=…)` through `EntryFilter`, so the catalog access
control (public catalogs for anonymous readers, assigned catalogs otherwise) applies before ranking.

## Configuration

```sh
EVILFLOWERS_EMBEDDING_URL=http://tei-embed.shared.svc.cluster.local   # empty: words only
EVILFLOWERS_EMBEDDING_API_KEY=…                                       # 1Password ELVIRA/stu.inference.tei
EVILFLOWERS_SEARCH_VECTOR_MIN_SCORE=0.8    # cosine similarity floor of the meaning channel
EVILFLOWERS_SEARCH_VECTOR_WEIGHT=0.5       # weight of the meaning channel in the fusion
EVILFLOWERS_SEARCH_VECTOR_CANDIDATES=100   # entries the meaning channel contributes at most
EVILFLOWERS_SEARCH_RRF_K=60
```

## Operator commands

```sh
# Text search configurations (after the Slovak/Czech dictionaries arrive), BM25 index, lexical index.
python manage.py reindex_search

# Entry vectors through tei-embed (all entries, or only those missing a vector of the current model).
python manage.py embed_entries --missing
```

New and edited entries are embedded by a Celery task after the change commits.
