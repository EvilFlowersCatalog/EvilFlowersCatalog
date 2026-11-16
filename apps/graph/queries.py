"""Read-only questions to the `library` graph (IP-030 §4).

Every function takes the ids of the entries the caller may see (`visible_entry_ids`) and passes them into the
query, so a hidden publication never comes back; callers still resolve what they show through `EntryFilter`.
"""

from typing import Iterable, List, Optional

from apps.graph import age


def visible_entry_ids(request, scope=None) -> List[str]:
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry

    queryset = EntryFilter(scope or {}, queryset=Entry.objects.all(), request=request).qs
    return [str(pk) for pk in queryset.values_list("pk", flat=True).distinct()]


def _sections(rows) -> List[dict]:
    return [
        {"key": key, "entry_id": entry_id, "run_id": run_id, "title": title, "first_page": first, "score": score}
        for key, entry_id, run_id, title, first, score in rows
    ]


_SECTION_COLUMNS = ("key", "entry_id", "run_id", "title", "first_page", "score")

#: `related_sections` scores: per shared concept, and per unit of SIMILAR_TO weight.
SHARED_CONCEPT = 3
SIMILARITY = 3


#: #203: candidates fetched per query beyond `limit`, so pairs the decider hid still leave `limit` results.
SPARE = 10


def related_sections(section: str, entries: Iterable[str], limit: int = 5) -> List[dict]:
    """Sections of other publications that mention the same concepts or are `SIMILAR_TO`, best first."""
    if not age.available():
        return []
    entries = list(entries)
    # #203: more candidates than asked for; hidden pairs are dropped before truncating to `limit`.
    fetch = limit * 2 + SPARE
    shared = age.cypher(
        "MATCH (s:Section {key: $key})-[:MENTIONS]->(c:Concept)<-[:MENTIONS]-(o:Section) "
        "WHERE o.entry_id <> s.entry_id AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, count(DISTINCT c) AS score "
        "ORDER BY score DESC LIMIT $limit",
        {"key": section, "entries": entries, "limit": fetch},
        _SECTION_COLUMNS,
    )
    similar = age.cypher(
        "MATCH (s:Section {key: $key})-[r:SIMILAR_TO]-(o:Section) "
        "WHERE o.entry_id <> s.entry_id AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, r.weight "
        "ORDER BY r.weight DESC LIMIT $limit",
        {"key": section, "entries": entries, "limit": fetch},
        _SECTION_COLUMNS,
    )
    return rank_related(section, _sections(shared), _sections(similar), limit)


def rank_related(section: str, shared: List[dict], similar: List[dict], limit: int) -> List[dict]:
    """Score, attach the decider's relation, drop hidden pairs, then truncate to `limit` (#203)."""
    from apps.decider.edges import relation_factor

    # A shared concept counts 3; a SIMILAR_TO edge 3 x its centred cosine (0.55-1, #151), so a near-identical
    # section weighs about one shared concept. Both count.
    scored: dict = {}
    for item in shared:
        scored[item["key"]] = {**item, "score": item["score"] * SHARED_CONCEPT, "similar": 0.0, "via": "concepts"}
    for item in similar:
        weight = float(item["score"]) * SIMILARITY
        if item["key"] in scored:
            scored[item["key"]]["similar"] = weight
            scored[item["key"]]["via"] = "concepts+similar"
        else:
            scored[item["key"]] = {**item, "score": 0.0, "similar": weight, "via": "similar"}
    items = []
    for item in _with_relations(section, list(scored.values())):
        # #203: the SIMILAR_TO part is weighted by the relation once its p reaches the `edge` threshold; without
        # thresholds (or weights) the factor is 1 and the order is the one before #203.
        factor = relation_factor(item.pop("_relation", None))
        similar_part = item.pop("similar")
        items.append({**item, "score": round(item["score"] + similar_part * factor, 4)})
    items.sort(key=lambda item: (-item["score"], item["key"]))
    return items[:limit]


def _with_relations(section: str, items: List[dict]) -> List[dict]:
    """#190: the decider's relation of each item to `section`, as its reader sees it, and its p; a pair the
    decider hid is left out even before the graph is projected again. `_relation` carries the stored row."""
    from apps.decider.edges import lookup, perspective
    from apps.decider.models import Band

    relations = lookup((section, item["key"]) for item in items)
    found = []
    for item in items:
        relation = relations.get(frozenset((section, item["key"])))
        if relation is not None and relation.band == Band.HIDE:
            continue
        found.append(
            {
                **item,
                "relation": perspective(relation, section) if relation is not None else None,
                "relation_p": relation.p if relation is not None else None,
                "_relation": relation,
            }
        )
    return found


def reading_path(section: str, entries: Iterable[str], depth: int = 2, limit: int = 10) -> List[dict]:
    """#203: sections the decider said come before this one (`PREREQUISITE_OF`), up to `depth` steps back.

    Nearest first (`depth` 1 before 2), then by the edge's p. Only typed pairs the decider did not hide are in the
    graph (`apps.graph.projection._similar_sections`), so nothing else is needed here.
    """
    if not age.available():
        return []
    entries = list(entries)
    found: dict = {}
    for steps in range(1, max(1, min(depth, 3)) + 1):
        rows = age.cypher(_path_query(steps), {"key": section, "entries": entries}, _SECTION_COLUMNS)
        for item in _sections(rows):
            if item["key"] == section or item["key"] in found:
                continue
            found[item["key"]] = {**item, "score": float(item["score"] or 0.0), "depth": steps}
    ordered = sorted(found.values(), key=lambda item: (item["depth"], -item["score"], item["key"]))
    return ordered[:limit]


def _path_query(steps: int) -> str:
    """`(o)-[:PREREQUISITE_OF]->…->(s)` with `steps` edges; the score is the p of the edge leaving `o`."""
    middle = "".join("(:Section)-[:PREREQUISITE_OF]->" for _ in range(steps - 1))
    return (
        f"MATCH (o:Section)-[r:PREREQUISITE_OF]->{middle}(s:Section {{key: $key}}) "
        "WHERE o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, r.p"
    )


def examples(section: str, entries: Iterable[str], limit: int = 10) -> List[dict]:
    """#203: sections the decider said are examples of this one (`(o)-[:EXAMPLE_OF]->(s)`), most certain first."""
    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (o:Section)-[r:EXAMPLE_OF]->(s:Section {key: $key}) WHERE o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, r.p",
        {"key": section, "entries": list(entries)},
        _SECTION_COLUMNS,
    )
    found = [{**item, "score": float(item["score"] or 0.0)} for item in _sections(rows)]
    found.sort(key=lambda item: (-item["score"], item["key"]))
    return found[:limit]


def prerequisites(section: str, entries: Iterable[str], limit: int = 3) -> List[dict]:
    """Sections that define a concept this one only discusses: what to read before it.

    Within one book, earlier sections come first. A later section of the same book is returned only after
    them, with `later: True`, for the reader to be told it is "defined later in this book" (#153).

    #203: sections the decider typed as prerequisites (`reading_path`, `via: relation`) come before the ones found
    through concepts (`via: concepts`); without typed edges the result is the one before #203.
    """
    if not age.available():
        return []
    entries = list(entries)
    typed = [
        {**item, "later": False, "via": "relation"} for item in reading_path(section, entries, depth=2, limit=limit)
    ]
    seen = {item["key"] for item in typed}
    rows = age.cypher(
        "MATCH (s:Section {key: $key})-[m:MENTIONS]->(c:Concept)<-[d:MENTIONS]-(o:Section) "
        "WHERE m.role = 'discusses' AND d.role = 'defines' AND o.key <> s.key AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, count(DISTINCT c) AS score, "
        "o.entry_id = s.entry_id AND o.seq > s.seq AS later",
        {"key": section, "entries": entries},
        _SECTION_COLUMNS + ("later",),
    )
    found = [{**_sections([row[:6]])[0], "later": bool(row[6]), "via": "concepts"} for row in rows]
    found = [item for item in found if item["key"] not in seen]
    found.sort(key=lambda item: (item["later"], -item["score"], item["key"]))
    return (typed + found)[:limit]


def top_concepts(entries: Iterable[str], limit: int = 10) -> List[dict]:
    """Concepts that span the most subjects (then publications), for "what is the library about".

    A concept is one term in one field (`Concept.subject`, the UDC main class), so the same word in two fields
    comes back twice, each with its own `field`.
    """
    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (e:Entry)-[:HAS_SECTION]->(:Section)-[:MENTIONS]->(c:Concept) WHERE e.id IN $entries "
        "OPTIONAL MATCH (e)-[:FILED_UNDER]->(j:Subject) "
        "RETURN c.key, c.term, c.subject, count(DISTINCT j) AS subjects, count(DISTINCT e) AS publications "
        "ORDER BY subjects DESC, publications DESC, c.key LIMIT $limit",
        {"entries": list(entries), "limit": limit},
        ("key", "term", "field", "subjects", "publications"),
    )
    return [
        {"key": key, "term": term, "field": field or None, "subjects": subjects, "publications": publications}
        for key, term, field, subjects, publications in rows
    ]


def section_entry(section: str) -> Optional[str]:
    """The entry a section key belongs to, for the caller to check before answering about it (#152)."""
    if not age.available():
        return None
    rows = age.cypher("MATCH (s:Section {key: $key}) RETURN s.entry_id LIMIT 1", {"key": section}, ("entry_id",))
    return str(rows[0][0]) if rows else None


def search_concepts(query: str, entries: Iterable[str], limit: int = 20) -> List[dict]:
    """Concepts the caller's publications mention whose key contains the folded `query`, most used first.

    An empty query lists the most mentioned. A concept mentioned only by hidden publications never appears.
    """
    from apps.graph.projection import concept_key

    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (s:Section)-[m:MENTIONS]->(c:Concept) WHERE s.entry_id IN $entries "
        "AND ($q = '' OR c.key CONTAINS $q) "
        "RETURN c.key, c.term, c.subject, count(DISTINCT s.entry_id) AS publications, count(DISTINCT s) AS sections "
        "ORDER BY publications DESC, sections DESC, c.key LIMIT $limit",
        {"entries": list(entries), "q": concept_key(query or ""), "limit": limit},
        ("key", "term", "field", "publications", "sections"),
    )
    return [
        {"key": key, "term": term, "field": field or None, "publications": publications, "sections": sections}
        for key, term, field, publications, sections in rows
    ]


def concept(key: str, entries: Iterable[str]) -> Optional[dict]:
    """A concept with the caller's sections that define or discuss it; None when they mention it nowhere."""
    if not age.available():
        return None
    rows = age.cypher(
        "MATCH (s:Section)-[m:MENTIONS]->(c:Concept {key: $key}) WHERE s.entry_id IN $entries "
        "RETURN c.term, c.subject, s.key, s.entry_id, s.title, s.first_page, s.seq, m.role",
        {"key": key, "entries": list(entries)},
        ("term", "field", "key", "entry_id", "title", "first_page", "seq", "role"),
    )
    if not rows:
        return None
    sections = {}
    for _term, _field, section, entry_id, title, first_page, seq, role in rows:
        known = sections.get(section)
        # A section may both define and discuss a term (summary and study mode): defining wins.
        if known is None or role == "defines":
            sections[section] = {
                "key": section,
                "entry_id": entry_id,
                "title": title,
                "first_page": first_page,
                "seq": seq,
                "role": role,
            }
    ordered = sorted(
        sections.values(), key=lambda item: (item["role"] != "defines", str(item["entry_id"]), item["seq"])
    )
    return {"key": key, "term": rows[0][0], "field": rows[0][1] or None, "sections": ordered}


def overview_counts(entries: Iterable[str]) -> dict:
    """Publications, sections, concepts and SIMILAR_TO edges of the caller's publications in the graph."""
    if not age.available():
        return {"publications": 0, "sections": 0, "concepts": 0, "similar": 0}
    params = {"entries": list(entries)}

    def one(query: str) -> int:
        rows = age.cypher(query, params)
        return int(rows[0][0]) if rows else 0

    return {
        "publications": one("MATCH (e:Entry) WHERE e.id IN $entries RETURN count(e)"),
        "sections": one("MATCH (s:Section) WHERE s.entry_id IN $entries RETURN count(s)"),
        "concepts": one(
            "MATCH (s:Section)-[:MENTIONS]->(c:Concept) WHERE s.entry_id IN $entries RETURN count(DISTINCT c)"
        ),
        # Both ends visible: an edge to a hidden section is not counted.
        "similar": one(
            "MATCH (a:Section)-[r:SIMILAR_TO]->(b:Section) WHERE a.entry_id IN $entries AND b.entry_id IN $entries "
            "RETURN count(r)"
        ),
    }
