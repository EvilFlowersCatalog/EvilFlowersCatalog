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


def related_sections(section: str, entries: Iterable[str], limit: int = 5) -> List[dict]:
    """Sections of other publications that mention the same concepts or are `SIMILAR_TO`, best first."""
    if not age.available():
        return []
    entries = list(entries)
    shared = age.cypher(
        "MATCH (s:Section {key: $key})-[:MENTIONS]->(c:Concept)<-[:MENTIONS]-(o:Section) "
        "WHERE o.entry_id <> s.entry_id AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, count(DISTINCT c) AS score "
        "ORDER BY score DESC LIMIT $limit",
        {"key": section, "entries": entries, "limit": limit},
        _SECTION_COLUMNS,
    )
    similar = age.cypher(
        "MATCH (s:Section {key: $key})-[r:SIMILAR_TO]-(o:Section) "
        "WHERE o.entry_id <> s.entry_id AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, r.weight "
        "ORDER BY r.weight DESC LIMIT $limit",
        {"key": section, "entries": entries, "limit": limit},
        _SECTION_COLUMNS,
    )
    # A shared concept counts 3; a SIMILAR_TO edge 3 x its centred cosine (0.55-1, #151), so a near-identical
    # section weighs about one shared concept. Both count.
    scored: dict = {}
    for item in _sections(shared):
        scored[item["key"]] = {**item, "score": item["score"] * SHARED_CONCEPT, "via": "concepts"}
    for item in _sections(similar):
        weight = round(float(item["score"]) * SIMILARITY, 4)
        if item["key"] in scored:
            scored[item["key"]]["score"] += weight
            scored[item["key"]]["via"] = "concepts+similar"
        else:
            scored[item["key"]] = {**item, "score": weight, "via": "similar"}
    return sorted(scored.values(), key=lambda item: (-item["score"], item["key"]))[:limit]


def prerequisites(section: str, entries: Iterable[str], limit: int = 3) -> List[dict]:
    """Sections that define a concept this one only discusses: what to read before it.

    Within one book, earlier sections come first. A later section of the same book is returned only after
    them, with `later: True`, for the reader to be told it is "defined later in this book" (#153).
    """
    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (s:Section {key: $key})-[m:MENTIONS]->(c:Concept)<-[d:MENTIONS]-(o:Section) "
        "WHERE m.role = 'discusses' AND d.role = 'defines' AND o.key <> s.key AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, count(DISTINCT c) AS score, "
        "o.entry_id = s.entry_id AND o.seq > s.seq AS later",
        {"key": section, "entries": list(entries)},
        _SECTION_COLUMNS + ("later",),
    )
    found = [{**_sections([row[:6]])[0], "later": bool(row[6])} for row in rows]
    found.sort(key=lambda item: (item["later"], -item["score"], item["key"]))
    return found[:limit]


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
