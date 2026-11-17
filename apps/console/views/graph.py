"""IP-035: the knowledge-graph explorer (`console/graph/`), a Cytoscape.js page over JSON neighbourhoods.

A node id is `<kind>:<key>`: `entry:<uuid>`, `section:<section key>`, `concept:<concept key>`,
`category:<uuid>`, `author:<uuid>`. The page searches for a seed, then asks `console/api/graph/expand/` for
the neighbourhood of one node at a time and merges it into the canvas.

Where the neighbourhood comes from. The `library` AGE graph is a *projection* of relational tables
(`apps.graph.projection`): categories and authors, the outline's sections (`chapter_map`), the cross-book
`SectionSimilarity` pairs, the decider's `SectionRelation`/`MentionRelation` answers, the summaries'
concepts and the reference lists' `CITES`. The explorer reads those tables directly, so it works on a
deployment without AGE and shows what the projection is made of, including what it hid (`band: hide`,
drawn dashed on request). Only concept questions use AGE when it is there (`queries.search_concepts`,
`queries.concept`): the summaries are the relational fallback. Every payload names its `source`.

The admin is superuser-only, so nothing is filtered by catalog ACLs; the entry-level similarity edges come
from the same cached library map as `GET /api/v1/entries/map` (`apps.graph.views.entry_neighbours`).
"""

from typing import Dict, Iterable, List, Optional, Tuple

from django.db.models import Q
from django.urls import NoReverseMatch, path, reverse
from django.utils.translation import gettext_lazy as _

from apps.console.views import ApiError, ConsoleApi, ConsolePage, bounded_int

MODULE = "knowledge"
#: Nodes one expansion adds at most, so a hub (a big category) cannot freeze the canvas.
EXPAND_LIMIT = 60
SEARCH_LIMIT = 12

#: Edge types, with the label the page shows; the page's filter lists them in this order.
EDGE_TYPES = {
    "HAS_SECTION": _("has section"),
    "CHAPTER_LINK": _("chapters linked (similar paragraphs)"),
    "SIMILAR_TO": _("similar (vectors)"),
    "SAME_IDEA_AS": _("same idea (decider)"),
    "PREREQUISITE_OF": _("prerequisite of (decider)"),
    "EXAMPLE_OF": _("example of (decider)"),
    "LOOSE": _("loosely related (decider)"),
    "UNRELATED": _("unrelated (decider)"),
    "MENTIONS": _("mentions concept"),
    "FILED_UNDER": _("filed under"),
    "SUGGESTED": _("suggested subject (decider)"),
    "WRITTEN_BY": _("written by"),
    "RELATED": _("related entry"),
    "CITES": _("cites"),
}

#: `SectionRelation.relation` → edge type. `pre` and `example` are directed (A → B as stored).
SECTION_RELATION_EDGES = {
    "same": "SAME_IDEA_AS",
    "pre": "PREREQUISITE_OF",
    "example": "EXAMPLE_OF",
    "loose": "LOOSE",
    "none": "UNRELATED",
}


def _admin_url(model, pk) -> Optional[str]:
    try:
        return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk])
    except NoReverseMatch:
        return None


class Graph:
    """Nodes and edges collected for one answer; de-duplicated by id."""

    def __init__(self):
        self.nodes: Dict[str, dict] = {}
        self.edges: Dict[str, dict] = {}
        self.truncated = False

    def node(self, node_id: str, kind: str, label: str, **data) -> str:
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, "kind": kind, "label": (label or node_id)[:160], **data}
        return node_id

    def edge(self, source: str, target: str, edge_type: str, p: Optional[float] = None, **data) -> None:
        if source == target:
            return
        edge_id = f"{edge_type}|{source}|{target}"
        if edge_id in self.edges:
            return
        self.edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "type": edge_type,
            "p": round(float(p), 4) if p is not None else None,
            **data,
        }

    def payload(self, **extra) -> dict:
        return {
            "nodes": list(self.nodes.values()),
            "edges": [
                edge for edge in self.edges.values() if edge["source"] in self.nodes and edge["target"] in self.nodes
            ],
            "truncated": self.truncated,
            **extra,
        }


# --- nodes -------------------------------------------------------------------------------------------------


def entry_node(graph: Graph, entry) -> str:
    from apps.core.models import Entry

    return graph.node(
        f"entry:{entry.pk}",
        "entry",
        entry.title,
        catalog=str(entry.catalog_id) if getattr(entry, "catalog_id", None) else None,
        url=_admin_url(Entry, entry.pk),
    )


def section_node(graph: Graph, key: str, title: Optional[str], entry_id=None, page=None) -> str:
    return graph.node(
        f"section:{key}",
        "section",
        title or key,
        entry=str(entry_id) if entry_id else None,
        page=page,
    )


def concept_node(graph: Graph, key: str, term: Optional[str] = None, field: Optional[str] = None) -> str:
    return graph.node(f"concept:{key}", "concept", term or key.split("@")[0], field=field)


def category_node(graph: Graph, category) -> str:
    from apps.core.models import Category

    return graph.node(
        f"category:{category.pk}",
        "category",
        category.term,
        scheme=category.scheme,
        url=_admin_url(Category, category.pk),
    )


def author_node(graph: Graph, author) -> str:
    from apps.core.models import Author

    return graph.node(
        f"author:{author.pk}",
        "author",
        f"{author.name} {author.surname}".strip(),
        url=_admin_url(Author, author.pk),
    )


# --- expansions --------------------------------------------------------------------------------------------


def _section_headings(entry_id) -> Tuple[Dict[str, dict], List[dict]]:
    """`({key: section}, links)` of an entry's outline (the active run), through the REST chapter map."""
    from apps.graph.views import chapter_map

    _etag, payload = chapter_map(entry_id, readable=True)
    return {section["key"]: section for section in payload["sections"]}, payload["links"]


def expand_entry(graph: Graph, request, entry_id: str, limit: int) -> None:
    from apps.core.models import Entry, EntryAuthor
    from apps.decider.models import SubjectSuggestion
    from apps.extraction.models import Bibitem

    entry = Entry.objects.filter(pk=entry_id).only("pk", "title", "catalog_id").first()
    if entry is None:
        raise ApiError("No such entry.", status=404)
    me = entry_node(graph, entry)
    budget = [limit]

    def room() -> bool:
        if budget[0] <= 0:
            graph.truncated = True
            return False
        budget[0] -= 1
        return True

    for category in entry.categories.all().order_by("term"):
        if room():
            graph.edge(me, category_node(graph, category), "FILED_UNDER")
    for suggestion in SubjectSuggestion.objects.filter(
        entry=entry, status=SubjectSuggestion.Status.PENDING
    ).select_related("category"):
        if room():
            graph.edge(me, category_node(graph, suggestion.category), "SUGGESTED", p=suggestion.p, status="pending")
    for link in EntryAuthor.objects.filter(entry=entry).select_related("author").order_by("position"):
        if room():
            graph.edge(me, author_node(graph, link.author), "WRITTEN_BY")
    for other in entry.related_entries.all().only("pk", "title", "catalog_id")[:limit]:
        if room():
            graph.edge(me, entry_node(graph, other), "RELATED")

    # Entry-level similarity: the library map's mutual kNN (cached with the map).
    from apps.graph.views import entry_neighbours

    neighbours = entry_neighbours(request, {}).get(str(entry.pk), [])
    titles = {
        str(pk): title
        for pk, title in Entry.objects.filter(pk__in=[pk for pk, _ in neighbours]).values_list("pk", "title")
    }
    for other_id, score in neighbours:
        if room():
            other = graph.node(
                f"entry:{other_id}", "entry", titles.get(str(other_id), str(other_id)), url=_admin_url(Entry, other_id)
            )
            graph.edge(me, other, "SIMILAR_TO", p=score, level="entry")

    # CITES (#178): the reference lists of the entry's active runs, both directions.
    cited = (
        Bibitem.objects.filter(run__is_active=True, run__acquisition__entry=entry, entry__isnull=False)
        .values_list("entry_id", "entry__title")
        .distinct()
    )
    for other_id, title in cited[:limit]:
        if room():
            graph.edge(me, graph.node(f"entry:{other_id}", "entry", title, url=_admin_url(Entry, other_id)), "CITES")
    citing = (
        Bibitem.objects.filter(run__is_active=True, entry=entry)
        .values_list("run__acquisition__entry_id", "run__acquisition__entry__title")
        .distinct()
    )
    for other_id, title in citing[:limit]:
        if room():
            graph.edge(graph.node(f"entry:{other_id}", "entry", title, url=_admin_url(Entry, other_id)), me, "CITES")

    # The book's own outline, and which of its chapters share similar paragraphs.
    sections, links = _section_headings(entry.pk)
    for key, section in list(sections.items()):
        if room():
            graph.edge(
                me, section_node(graph, key, section.get("title"), entry.pk, section.get("first_page")), "HAS_SECTION"
            )
    for link in links:
        graph.edge(f"section:{link['source']}", f"section:{link['target']}", "CHAPTER_LINK", links=link["links"])


def _section_entry(key: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """`(entry_id, heading, page)` of a section key, from its `Locator` (#171): keys survive re-extraction."""
    from apps.extraction.models import Locator

    row = (
        Locator.objects.filter(key=key, kind=Locator.Kind.SECTION)
        .values_list("entry_id", "quote_exact", "source_page")
        .first()
    )
    if row is None:
        return None, None, None
    return str(row[0]), row[1], row[2]


def expand_section(graph: Graph, request, key: str, limit: int, include_hidden: bool) -> str:
    from apps.core.models import Entry
    from apps.decider.models import Band, MentionRelation, SectionRelation
    from apps.extraction.models import ExtractionRun, SectionSimilarity
    from apps.graph.projection import section_key

    entry_id, quote, page = _section_entry(key)
    headings: Dict[str, dict] = {}
    if entry_id:
        headings, _links = _section_headings(entry_id)
    heading = (headings.get(key) or {}).get("title") or quote
    me = section_node(graph, key, heading, entry_id, (headings.get(key) or {}).get("first_page") or page)
    if entry_id:
        entry = Entry.objects.filter(pk=entry_id).only("pk", "title", "catalog_id").first()
        if entry is not None:
            graph.edge(entry_node(graph, entry), me, "HAS_SECTION")

    budget = [limit]

    def room() -> bool:
        if budget[0] <= 0:
            graph.truncated = True
            return False
        budget[0] -= 1
        return True

    # The decider's typed pairs (#190), read A → B as stored.
    relations = SectionRelation.objects.filter(Q(a_key=key) | Q(b_key=key)).order_by("-p")
    if not include_hidden:
        relations = relations.exclude(band=Band.HIDE)
    typed = {}
    for relation in relations[: limit * 2]:
        other = relation.b_key if relation.a_key == key else relation.a_key
        typed[other] = relation
    for other, relation in typed.items():
        if not room():
            break
        other_entry, other_quote, other_page = _section_entry(other)
        section_node(graph, other, other_quote, other_entry, other_page)
        graph.edge(
            f"section:{relation.a_key}",
            f"section:{relation.b_key}",
            SECTION_RELATION_EDGES.get(relation.relation, "LOOSE"),
            p=relation.p,
            band=relation.band,
            model=relation.decider_model,
        )

    # Cross-book similarity of section centroids (#151), the projection's SIMILAR_TO.
    if entry_id and heading:
        run = (
            ExtractionRun.objects.filter(acquisition__entry_id=entry_id, is_active=True)
            .order_by("-finished_at")
            .first()
        )
        if run is not None:
            rows = (
                SectionSimilarity.objects.filter(
                    Q(source=run, source_section=heading) | Q(target=run, target_section=heading),
                    source__is_active=True,
                    target__is_active=True,
                )
                .values_list(
                    "source__acquisition__entry_id",
                    "source_section",
                    "target__acquisition__entry_id",
                    "target_section",
                    "score",
                )
                .order_by("-score")
            )
            for source_entry, source, target_entry, target, score in rows[: limit * 2]:
                if str(source_entry) == entry_id and source == heading:
                    other_entry, other_heading = target_entry, target
                else:
                    other_entry, other_heading = source_entry, source
                other = section_key(other_entry, other_heading)
                if other in typed:
                    continue
                if not room():
                    break
                section_node(graph, other, other_heading, other_entry)
                graph.edge(me, f"section:{other}", "SIMILAR_TO", p=score, level="section")

    # Concepts its summary names (#206 typing from MentionRelation when the decider asked).
    mentions = {
        relation.concept_key: relation
        for relation in MentionRelation.objects.filter(section_key=key)
        if include_hidden or relation.band != Band.HIDE
    }
    for concept in _summary_concepts(entry_id, heading):
        relation = mentions.pop(concept["key"], None)
        if relation is None and concept["key"] in _hidden_mentions(key) and not include_hidden:
            continue
        if not room():
            break
        node = concept_node(graph, concept["key"], concept["term"], concept["subject"] or None)
        graph.edge(
            me,
            node,
            "MENTIONS",
            p=relation.p if relation else None,
            role=concept["role"],
            kind=relation.relation if relation else None,
        )
    for concept_key_, relation in mentions.items():
        if not room():
            break
        graph.edge(me, concept_node(graph, concept_key_), "MENTIONS", p=relation.p, kind=relation.relation)
    return me


def _hidden_mentions(key: str) -> set:
    from apps.decider.models import Band, MentionRelation

    return set(MentionRelation.objects.filter(section_key=key, band=Band.HIDE).values_list("concept_key", flat=True))


def _summary_concepts(entry_id, heading) -> List[dict]:
    """The concepts of a section's summary, keyed as the projection keys them (`project_concepts`)."""
    from apps.assistant.models import SectionSummary
    from apps.core.models import Entry
    from apps.graph.projection import concept_key, concept_subject

    if not entry_id or not heading:
        return []
    summary = SectionSummary.objects.filter(entry_id=entry_id, section=heading).only("concepts", "entry_id").first()
    if summary is None:
        return []
    subject = concept_subject(Entry.objects.get(pk=entry_id))
    found, seen = [], set()
    for item in summary.concepts or []:
        if not isinstance(item, dict) or not item.get("term"):
            continue
        key = concept_key(str(item["term"]), subject)
        if not key or key in seen:
            continue
        seen.add(key)
        role = "defines" if item.get("role") == "defines" else "discusses"
        found.append({"key": key, "term": str(item["term"])[:120], "subject": subject, "role": role})
    return found


def _all_entry_ids() -> List[str]:
    from apps.core.models import Entry

    return [str(pk) for pk in Entry.objects.values_list("pk", flat=True)]


def _age_available() -> bool:
    try:
        from apps.graph import age

        return age.available()
    except Exception:  # pragma: no cover - a broken extension must not take the page down
        return False


def expand_concept(graph: Graph, key: str, limit: int) -> str:
    """Sections that mention a concept: AGE (`queries.concept`) when it is there, else the summaries."""
    from apps.core.models import Entry

    if _age_available():
        from apps.graph import queries

        found = queries.concept(key, _all_entry_ids())
        source = "age"
        if found is None:
            raise ApiError("No section mentions this concept.", status=404)
        me = concept_node(graph, key, found["term"], found.get("field"))
        sections = found["sections"]
    else:
        source = "relational"
        sections, term = _concept_sections_relational(key, limit * 2)
        if not sections:
            raise ApiError("No section mentions this concept.", status=404)
        me = concept_node(graph, key, term)
    titles = {
        str(pk): title
        for pk, title in Entry.objects.filter(pk__in={str(section["entry_id"]) for section in sections}).values_list(
            "pk", "title"
        )
    }
    for count, section in enumerate(sections):
        if count >= limit:
            graph.truncated = True
            break
        node = section_node(
            graph, section["key"], section.get("title"), section["entry_id"], section.get("first_page")
        )
        graph.edge(node, me, "MENTIONS", role=section.get("role"))
        entry_title = titles.get(str(section["entry_id"]))
        if entry_title:
            graph.edge(
                graph.node(
                    f"entry:{section['entry_id']}", "entry", entry_title, url=_admin_url(Entry, section["entry_id"])
                ),
                node,
                "HAS_SECTION",
            )
    return source


def _concept_sections_relational(key: str, limit: int) -> Tuple[List[dict], Optional[str]]:
    """Scan the summaries for a concept key (hundreds to a few thousand rows; JSON, so no index helps)."""
    from apps.assistant.models import SectionSummary
    from apps.core.models import Entry
    from apps.graph.projection import concept_key, concept_subject, section_key

    subjects: Dict[str, str] = {}
    found: List[dict] = []
    term = None
    folded, _, scope = key.partition("@")
    # The search lists unscoped terms (`_search_concepts_relational`): an unscoped key matches in every field.
    scoped = bool(scope)
    for summary in SectionSummary.objects.only("entry_id", "section", "first_page", "concepts").iterator():
        candidates = [item for item in (summary.concepts or []) if isinstance(item, dict) and item.get("term")]
        if not any(concept_key(str(item["term"])) == folded for item in candidates):
            continue
        entry_id = str(summary.entry_id)
        if entry_id not in subjects:
            subjects[entry_id] = concept_subject(Entry.objects.get(pk=entry_id))
        for item in candidates:
            item_key = concept_key(str(item["term"]), subjects[entry_id]) if scoped else concept_key(str(item["term"]))
            if item_key == key:
                term = term or str(item["term"])
                found.append(
                    {
                        "key": section_key(entry_id, summary.section),
                        "entry_id": entry_id,
                        "title": summary.section,
                        "first_page": summary.first_page,
                        "role": "defines" if item.get("role") == "defines" else "discusses",
                    }
                )
                break
        if len(found) >= limit:
            break
    return found, term


def expand_category(graph: Graph, category_id: str, limit: int) -> None:
    from apps.core.models import Category, Entry

    category = Category.objects.filter(pk=category_id).first()
    if category is None:
        raise ApiError("No such category.", status=404)
    me = category_node(graph, category)
    entries = (
        Entry.objects.filter(categories=category).only("pk", "title", "catalog_id").order_by("-popularity", "title")
    )
    for count, entry in enumerate(entries[: limit + 1]):
        if count >= limit:
            graph.truncated = True
            break
        graph.edge(entry_node(graph, entry), me, "FILED_UNDER")


def expand_author(graph: Graph, author_id: str, limit: int) -> None:
    from apps.core.models import Author, Entry

    author = Author.objects.filter(pk=author_id).first()
    if author is None:
        raise ApiError("No such author.", status=404)
    me = author_node(graph, author)
    entries = Entry.objects.filter(authors=author).only("pk", "title", "catalog_id").order_by("title")
    for count, entry in enumerate(entries[: limit + 1]):
        if count >= limit:
            graph.truncated = True
            break
        graph.edge(entry_node(graph, entry), me, "WRITTEN_BY")


# --- views -------------------------------------------------------------------------------------------------


class GraphPage(ConsolePage):
    template_name = "console/graph.html"
    module = MODULE
    title = _("Knowledge graph")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["edge_types"] = [{"key": key, "label": str(label)} for key, label in EDGE_TYPES.items()]
        context["age_available"] = _age_available()
        context["seed"] = self.request.GET.get("node", "")
        return context


class GraphSearch(ConsoleApi):
    """Seeds by name: entries (title), categories (term), authors, concepts (AGE, else the summaries)."""

    module = MODULE

    def get_data(self, request):
        from apps.core.models import Author, Category, Entry

        query = (request.GET.get("q") or "").strip()
        if len(query) < 2:
            return {"query": query, "results": [], "source": "relational"}
        graph = Graph()
        for entry in (
            Entry.objects.filter(title__icontains=query)
            .only("pk", "title", "catalog_id")
            .order_by("title")[:SEARCH_LIMIT]
        ):
            entry_node(graph, entry)
        for category in Category.objects.filter(term__icontains=query).order_by("term")[:SEARCH_LIMIT]:
            category_node(graph, category)
        for author in Author.objects.filter(Q(surname__icontains=query) | Q(name__icontains=query))[:SEARCH_LIMIT]:
            author_node(graph, author)
        source = "relational"
        if _age_available():
            from apps.graph import queries

            source = "age"
            for concept in queries.search_concepts(query, _all_entry_ids(), limit=SEARCH_LIMIT):
                concept_node(graph, concept["key"], concept["term"], concept.get("field"))
        else:
            for concept in _search_concepts_relational(query, SEARCH_LIMIT):
                concept_node(graph, concept["key"], concept["term"], concept["subject"] or None)
        return {"query": query, "results": list(graph.nodes.values()), "source": source}


def _search_concepts_relational(query: str, limit: int) -> List[dict]:
    from apps.assistant.models import SectionSummary
    from apps.graph.projection import concept_key

    folded = concept_key(query)
    found: Dict[str, dict] = {}
    if not folded:
        return []
    for concepts in SectionSummary.objects.values_list("concepts", flat=True).iterator():
        for item in concepts or []:
            if not isinstance(item, dict) or not item.get("term"):
                continue
            key = concept_key(str(item["term"]))
            if folded in key and key not in found:
                # Unscoped here: the search lists terms; expanding a section shows the scoped concept.
                found[key] = {"key": key, "term": str(item["term"])[:120], "subject": ""}
                if len(found) >= limit:
                    return list(found.values())
    return list(found.values())


class GraphExpand(ConsoleApi):
    """`?node=<kind>:<key>` → the node's neighbourhood (at most `limit` new nodes, default 60)."""

    module = MODULE

    def get_data(self, request):
        node = (request.GET.get("node") or "").strip()
        kind, _, key = node.partition(":")
        if not key:
            raise ApiError("`node` must be `<kind>:<key>` (entry, section, concept, category, author).")
        limit = bounded_int(request.GET.get("limit"), EXPAND_LIMIT, 1, 200)
        include_hidden = request.GET.get("hidden") in ("1", "true")
        graph = Graph()
        source = "relational"
        if kind == "entry":
            expand_entry(graph, request, _checked_uuid(key), limit)
        elif kind == "section":
            expand_section(graph, request, key, limit, include_hidden)
        elif kind == "concept":
            source = expand_concept(graph, key, limit)
        elif kind == "category":
            expand_category(graph, _checked_uuid(key), limit)
        elif kind == "author":
            expand_author(graph, _checked_uuid(key), limit)
        else:
            raise ApiError(f"Unknown node kind '{kind}'.")
        return graph.payload(node=node, source=source, age_available=_age_available())


def _checked_uuid(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ApiError("Not a valid id.", status=404)


class GraphOverview(ConsoleApi):
    """Counts for the page header: what the relational tables behind the graph hold (planner estimates)."""

    module = MODULE

    def get_data(self, request):
        from apps.console.options import estimated_count
        from apps.core.models import Category, Entry
        from apps.decider.models import MentionRelation, SectionRelation
        from apps.extraction.models import Locator, SectionSimilarity

        def count(model):
            estimate = estimated_count(model)
            return estimate if estimate >= 10_000 else model.objects.count()

        sections = Locator.objects.filter(kind=Locator.Kind.SECTION)
        return {
            "entries": count(Entry),
            "categories": count(Category),
            "sections": sections.count() if estimated_count(Locator) < 1_000_000 else None,
            "similar_sections": count(SectionSimilarity),
            "typed_relations": count(SectionRelation),
            "mentions": count(MentionRelation),
            "age_available": _age_available(),
            "edge_types": [{"key": key, "label": str(label)} for key, label in EDGE_TYPES.items()],
        }


def urlpatterns(site):
    return [
        path("console/graph/", site.admin_view(GraphPage.as_view(site=site)), name="console_graph"),
        path("console/api/graph/search/", site.admin_view(GraphSearch.as_view()), name="console_graph_search"),
        path("console/api/graph/expand/", site.admin_view(GraphExpand.as_view()), name="console_graph_expand"),
        path("console/api/graph/overview/", site.admin_view(GraphOverview.as_view()), name="console_graph_overview"),
    ]
