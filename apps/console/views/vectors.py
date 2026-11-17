"""IP-035: the vector explorer (`console/vectors/`): what the embeddings look like and what they find.

Four panels, each its own JSON endpoint:

* **map** — the entries' vectors projected to 2-D with PCA (numpy SVD, server-side), coloured by catalog,
  language or category. `source=entry` is the search embedding (`Entry.embedding`, IP-021);
  `source=run` the document vector of the active extraction run (`ExtractionRun.embedding`), the one the
  library map's edges come from (#151). Vectors of two models are never mixed: the map takes the model most
  rows were embedded with and says how many it left out. Above `MAP_LIMIT` rows a deterministic sample
  (`md5(id)` order) is drawn. Cached per fingerprint (model, row count, last change).
* **neighbours** — top-k by cosine distance (pgvector `<=>`, HNSW where indexed), same model only;
* **coverage** — per vector column: rows, rows with a vector, the models. Tables past `SAMPLE_ABOVE`
  rows (paragraphs: 400 000, 2.6 GB) are measured on a `TABLESAMPLE SYSTEM` sample scaled by the planner's
  row estimate, and say so (`estimated`), so the page never scans them;
* **passages** — semantic + word search over paragraphs through the same `search_passages` the REST API
  and the assistant use (`apps.extraction.discovery`): its query-embedding budget, its degradation to
  words when tei-embed is unconfigured or down.
"""

import hashlib
from collections import Counter
from typing import Dict, List, Optional

from django.core.cache import cache
from django.db import connection
from django.db.models import Max
from django.db.models.expressions import RawSQL
from django.urls import NoReverseMatch, path, reverse
from django.utils.translation import gettext_lazy as _

from apps.console.options import estimated_count
from apps.console.views import ApiError, ConsoleApi, ConsolePage, bounded_int

MODULE = "library"
#: Points the map shows at most; beyond, a deterministic sample.
MAP_LIMIT = 5000
MAP_CACHE_SECONDS = 6 * 3600
#: Above this many (estimated) rows a coverage figure comes from a sample.
SAMPLE_ABOVE = 50_000
#: Rows a coverage sample aims at.
SAMPLE_ROWS = 20_000
COLOURS = ("catalog", "language", "category")
SOURCES = ("entry", "run")


def _entry_url(entry_id) -> Optional[str]:
    try:
        return reverse("admin:core_entry_change", args=[entry_id])
    except NoReverseMatch:
        return None


# --- map ---------------------------------------------------------------------------------------------------


def pca_2d(matrix):
    """Rows of `matrix` projected on its first two principal components; `(points, explained ratio)`.

    Centred, then SVD: the right singular vectors are the components. With fewer than 2 rows (or no variance)
    the points sit at the origin.
    """
    import numpy as np

    data = np.asarray(matrix, dtype=np.float64)
    if data.ndim != 2 or data.shape[0] == 0:
        return np.zeros((0, 2)), [0.0, 0.0]
    centred = data - data.mean(axis=0, keepdims=True)
    if data.shape[0] < 2 or not np.any(centred):
        return np.zeros((data.shape[0], 2)), [0.0, 0.0]
    _u, singular, vt = np.linalg.svd(centred, full_matrices=False)
    components = vt[:2]
    if components.shape[0] < 2:
        components = np.vstack([components, np.zeros((2 - components.shape[0], data.shape[1]))])
    projected = centred @ components.T
    variance = singular**2
    total = float(variance.sum()) or 1.0
    explained = [float(variance[i] / total) if i < len(variance) else 0.0 for i in range(2)]
    return projected, explained


def _run_model(provenance) -> str:
    return ((provenance or {}).get("paragraphs") or {}).get("embedding_model") or ""


def vector_map(source: str, colour: str) -> dict:
    from apps.core.models import Entry
    from apps.extraction.models import ExtractionRun

    if source == "entry":
        counts = Counter()
        for (model,) in Entry.objects.filter(embedding__isnull=False).values_list("embedding_model"):
            counts[model or ""] += 1
        model = counts.most_common(1)[0][0] if counts else ""
        queryset = Entry.objects.filter(embedding__isnull=False, embedding_model=model)
        total = counts[model] if counts else 0
        excluded = sum(counts.values()) - total
        changed = queryset.aggregate(at=Max("updated_at"))["at"]
    else:
        counts = Counter()
        for (provenance,) in ExtractionRun.objects.filter(is_active=True, embedding__isnull=False).values_list(
            "provenance"
        ):
            counts[_run_model(provenance)] += 1
        model = counts.most_common(1)[0][0] if counts else ""
        run_ids = [
            pk
            for pk, provenance in ExtractionRun.objects.filter(is_active=True, embedding__isnull=False).values_list(
                "pk", "provenance"
            )
            if _run_model(provenance) == model
        ]
        queryset = ExtractionRun.objects.filter(pk__in=run_ids)
        total = len(run_ids)
        excluded = sum(counts.values()) - total
        changed = queryset.aggregate(at=Max("finished_at"))["at"]

    fingerprint = hashlib.sha1(f"{source}:{colour}:{model}:{total}:{changed}:{MAP_LIMIT}".encode()).hexdigest()
    key = f"console:vectors:map:{fingerprint}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    sampled = total > MAP_LIMIT
    ordered = queryset.order_by(RawSQL("md5(id::text)", [])) if sampled else queryset.order_by("pk")
    if source == "entry":
        rows = list(ordered.values_list("pk", "pk", "embedding")[:MAP_LIMIT])
    else:
        rows = list(ordered.values_list("pk", "acquisition__entry_id", "embedding")[:MAP_LIMIT])

    entry_ids = [entry_id for _pk, entry_id, _vector in rows]
    info = _entry_info(entry_ids, colour)
    vectors = [vector.to_numpy() if hasattr(vector, "to_numpy") else vector for _pk, _entry, vector in rows]
    projected, explained = pca_2d(vectors)
    points = []
    groups: Counter = Counter()
    labels: Dict[str, str] = {}
    for (pk, entry_id, _vector), (x, y) in zip(rows, projected.tolist() if len(rows) else []):
        meta = info.get(str(entry_id), {})
        group, group_label = meta.get("group") or ("", "—")
        groups[group] += 1
        labels[group] = group_label
        points.append(
            {
                "id": str(pk),
                "entry_id": str(entry_id),
                "title": meta.get("title") or str(entry_id),
                "x": round(float(x), 5),
                "y": round(float(y), 5),
                "group": group,
            }
        )
    payload = {
        "source": source,
        "colour": colour,
        "model": model,
        "total": total,
        "excluded_other_models": excluded,
        "shown": len(points),
        "sampled": sampled,
        "explained_variance": [round(value, 4) for value in explained],
        "groups": [{"key": group, "label": labels[group], "count": count} for group, count in groups.most_common()],
        "points": points,
    }
    cache.set(key, payload, MAP_CACHE_SECONDS)
    return payload


def _entry_info(entry_ids, colour: str) -> Dict[str, dict]:
    """Title and colour group of each entry: `(group key, group label)`."""
    from apps.core.models import Entry

    queryset = Entry.objects.filter(pk__in=set(entry_ids)).only(
        "pk", "title", "catalog_id", "language_id", "catalog__title", "language__name"
    )
    queryset = queryset.select_related("catalog", "language")
    if colour == "category":
        queryset = queryset.prefetch_related("categories")
    info = {}
    for entry in queryset:
        if colour == "language":
            group = (str(entry.language_id or ""), entry.language.name if entry.language_id else "—")
        elif colour == "category":
            terms = sorted(category.term for category in entry.categories.all())
            group = (terms[0], terms[0]) if terms else ("", "—")
        else:
            group = (str(entry.catalog_id or ""), entry.catalog.title if entry.catalog_id else "—")
        info[str(entry.pk)] = {"title": entry.title, "group": group}
    return info


# --- neighbours --------------------------------------------------------------------------------------------


def neighbours(source: str, entry_id: str, k: int) -> dict:
    from pgvector.django import CosineDistance

    from apps.core.models import Entry
    from apps.extraction.models import ExtractionRun

    entry = Entry.objects.filter(pk=entry_id).only("pk", "title", "embedding", "embedding_model").first()
    if entry is None:
        raise ApiError("No such entry.", status=404)
    if source == "entry":
        if entry.embedding is None:
            raise ApiError("This entry has no search embedding yet.", status=404)
        rows = (
            Entry.objects.filter(embedding__isnull=False, embedding_model=entry.embedding_model)
            .exclude(pk=entry.pk)
            .annotate(distance=CosineDistance("embedding", entry.embedding))
            .order_by("distance")
            .values_list("pk", "title", "distance")[:k]
        )
        model = entry.embedding_model
        items = [(pk, title, distance) for pk, title, distance in rows]
    else:
        run = (
            ExtractionRun.objects.filter(acquisition__entry=entry, is_active=True, embedding__isnull=False)
            .order_by("-finished_at")
            .only("pk", "embedding", "provenance")
            .first()
        )
        if run is None:
            raise ApiError("This entry has no embedded extraction run.", status=404)
        model = _run_model(run.provenance)
        candidates = (
            ExtractionRun.objects.filter(is_active=True, embedding__isnull=False)
            .exclude(acquisition__entry=entry)
            .annotate(distance=CosineDistance("embedding", run.embedding))
            .order_by("distance")
            .values_list("acquisition__entry_id", "acquisition__entry__title", "distance", "provenance")[: k * 3]
        )
        seen, items = set(), []
        for other, title, distance, provenance in candidates:
            if other in seen or _run_model(provenance) != model:
                continue
            seen.add(other)
            items.append((other, title, distance))
            if len(items) >= k:
                break
    return {
        "source": source,
        "model": model,
        "entry": {"id": str(entry.pk), "title": entry.title, "url": _entry_url(entry.pk)},
        "items": [
            {
                "id": str(pk),
                "title": title,
                "distance": round(float(distance), 5),
                "similarity": round(1 - float(distance), 5),
                "url": _entry_url(pk),
            }
            for pk, title, distance in items
        ],
    }


# --- coverage ----------------------------------------------------------------------------------------------


def _coverage_specs():
    from apps.core.models import Entry
    from apps.extraction.models import ExtractionRun, Paragraph, Region

    return [
        (Entry, "embedding", "embedding_model", _("Entries (search)")),
        (ExtractionRun, "embedding", None, _("Extraction runs (document)")),
        (Region, "embedding", "embedding_model", _("Regions (figures, tables)")),
        (Paragraph, "embedding", None, _("Paragraphs")),
    ]


def coverage() -> List[dict]:
    result = []
    for model, field_name, model_field, label in _coverage_specs():
        field = model._meta.get_field(field_name)
        table = connection.ops.quote_name(model._meta.db_table)
        column = connection.ops.quote_name(field.column)
        estimate = estimated_count(model)
        estimated = estimate >= SAMPLE_ABOVE
        sample = ""
        scale = 1.0
        if estimated:
            percent = max(0.05, min(100.0, SAMPLE_ROWS * 100.0 / estimate))
            sample = f" TABLESAMPLE SYSTEM ({percent:.4f})"
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*), count({column}) FROM {table}{sample}")
            rows, with_vector = cursor.fetchone()
            models_found: List[dict] = []
            if model_field:
                model_column = connection.ops.quote_name(model._meta.get_field(model_field).column)
                cursor.execute(
                    f"SELECT {model_column}, count(*) FROM {table}{sample} WHERE {column} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
                )
                models_found = [{"model": name or "", "rows": count} for name, count in cursor.fetchall()]
        if estimated and rows:
            scale = estimate / rows
            with_vector = int(round(with_vector * scale))
            rows = estimate
            for item in models_found:
                item["rows"] = int(round(item["rows"] * scale))
        result.append(
            {
                "model": model._meta.label,
                "label": str(label),
                "dimensions": getattr(field, "dimensions", None),
                "rows": int(rows),
                "with_vector": int(with_vector),
                "ratio": (with_vector / rows) if rows else None,
                "estimated": estimated,
                "models": models_found,
            }
        )
    return result


# --- views -------------------------------------------------------------------------------------------------


class VectorsPage(ConsolePage):
    template_name = "console/vectors.html"
    module = MODULE
    title = _("Vectors")

    def get_context_data(self, **kwargs):
        from apps.core.search import embeddings

        context = super().get_context_data(**kwargs)
        context["embedding_enabled"] = embeddings.enabled()
        context["map_limit"] = MAP_LIMIT
        return context


class VectorsMap(ConsoleApi):
    module = MODULE

    def get_data(self, request):
        source = request.GET.get("source", "entry")
        colour = request.GET.get("colour", "catalog")
        if source not in SOURCES:
            raise ApiError(f"`source` is one of {', '.join(SOURCES)}.")
        if colour not in COLOURS:
            raise ApiError(f"`colour` is one of {', '.join(COLOURS)}.")
        return vector_map(source, colour)


class VectorsNeighbours(ConsoleApi):
    module = MODULE

    def get_data(self, request):
        source = request.GET.get("source", "entry")
        if source not in SOURCES:
            raise ApiError(f"`source` is one of {', '.join(SOURCES)}.")
        entry_id = (request.GET.get("entry") or "").strip()
        import uuid

        try:
            entry_id = str(uuid.UUID(entry_id))
        except ValueError:
            raise ApiError("`entry` must be an entry id.")
        return neighbours(source, entry_id, bounded_int(request.GET.get("k"), 10, 1, 50))


class VectorsEntrySearch(ConsoleApi):
    """Entries by title for the neighbour picker, with whether each has a search vector."""

    module = MODULE

    def get_data(self, request):
        from apps.core.models import Entry

        query = (request.GET.get("q") or "").strip()
        if len(query) < 2:
            return {"results": []}
        rows = (
            Entry.objects.filter(title__icontains=query)
            .order_by("title")
            .values_list("pk", "title", "embedding_model")[:15]
        )
        return {"results": [{"id": str(pk), "title": title, "model": model or None} for pk, title, model in rows]}


class VectorsCoverage(ConsoleApi):
    module = MODULE

    def get_data(self, request):
        from apps.core.search import embeddings

        return {
            "items": coverage(),
            "embedding_service": {"enabled": embeddings.enabled(), "model": embeddings.known_model_id()},
        }


class VectorsPassages(ConsoleApi):
    """Paragraphs by meaning and words, through the REST/assistant `search_passages` (its budget, its fallback)."""

    module = MODULE

    def get_data(self, request):
        from apps.core.models import Entry
        from apps.extraction.discovery import search_passages

        query = (request.GET.get("q") or "").strip()
        if len(query) < 3:
            return {"query": query, "degraded": False, "indexed": 0, "items": []}
        try:
            result = search_passages(request, query, limit=bounded_int(request.GET.get("limit"), 10, 1, 30))
        except Exception as error:  # the search depends on extraction tables and TEI; report, don't 500
            raise ApiError(f"Passage search failed: {error}", status=502)
        payload = result.model_dump(mode="json")
        titles = {
            str(pk): title
            for pk, title in Entry.objects.filter(pk__in=[item["entry_id"] for item in payload["items"]]).values_list(
                "pk", "title"
            )
        }
        for item in payload["items"]:
            item["title"] = titles.get(item["entry_id"])
            item["url"] = _entry_url(item["entry_id"])
        return payload


def urlpatterns(site):
    return [
        path("console/vectors/", site.admin_view(VectorsPage.as_view(site=site)), name="console_vectors"),
        path("console/api/vectors/map/", site.admin_view(VectorsMap.as_view()), name="console_vectors_map"),
        path(
            "console/api/vectors/neighbours/",
            site.admin_view(VectorsNeighbours.as_view()),
            name="console_vectors_neighbours",
        ),
        path(
            "console/api/vectors/entries/",
            site.admin_view(VectorsEntrySearch.as_view()),
            name="console_vectors_entries",
        ),
        path(
            "console/api/vectors/coverage/",
            site.admin_view(VectorsCoverage.as_view()),
            name="console_vectors_coverage",
        ),
        path(
            "console/api/vectors/passages/",
            site.admin_view(VectorsPassages.as_view()),
            name="console_vectors_passages",
        ),
    ]
