"""Read-only questions to the `library` graph (IP-030 §4).

Every function takes the ids of the entries the caller may see (`visible_entry_ids`) and passes them into the
query, so a hidden publication never comes back; callers still resolve what they show through `EntryFilter`.
"""

from typing import Iterable, List

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
    # A shared concept outweighs a few similar paragraphs; both count.
    scored: dict = {}
    for item in _sections(shared):
        scored[item["key"]] = {**item, "score": item["score"] * 3, "via": "concepts"}
    for item in _sections(similar):
        if item["key"] in scored:
            scored[item["key"]]["score"] += item["score"]
            scored[item["key"]]["via"] = "concepts+similar"
        else:
            scored[item["key"]] = {**item, "via": "similar"}
    return sorted(scored.values(), key=lambda item: -item["score"])[:limit]


def prerequisites(section: str, entries: Iterable[str], limit: int = 3) -> List[dict]:
    """Sections that define a concept this one only discusses: what to read before it."""
    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (s:Section {key: $key})-[m:MENTIONS]->(c:Concept)<-[d:MENTIONS]-(o:Section) "
        "WHERE m.role = 'discusses' AND d.role = 'defines' AND o.key <> s.key AND o.entry_id IN $entries "
        "RETURN o.key, o.entry_id, o.run_id, o.title, o.first_page, count(DISTINCT c) AS score "
        "ORDER BY score DESC LIMIT $limit",
        {"key": section, "entries": list(entries), "limit": limit},
        _SECTION_COLUMNS,
    )
    return _sections(rows)


def top_concepts(entries: Iterable[str], limit: int = 10) -> List[dict]:
    """Concepts that span the most subjects (then publications), for "what is the library about"."""
    if not age.available():
        return []
    rows = age.cypher(
        "MATCH (e:Entry)-[:HAS_SECTION]->(:Section)-[:MENTIONS]->(c:Concept) WHERE e.id IN $entries "
        "OPTIONAL MATCH (e)-[:FILED_UNDER]->(j:Subject) "
        "RETURN c.term, count(DISTINCT j) AS subjects, count(DISTINCT e) AS publications "
        "ORDER BY subjects DESC, publications DESC LIMIT $limit",
        {"entries": list(entries), "limit": limit},
        ("term", "subjects", "publications"),
    )
    return [
        {"term": term, "subjects": subjects, "publications": publications} for term, subjects, publications in rows
    ]
