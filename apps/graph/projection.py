"""The `library` graph, projected from relational tables (IP-030 §4, Phase 3b).

| Node | From | Edges |
|---|---|---|
| `Entry {id, title, catalog_id}` | the entry | `WRITTEN_BY → Author`, `FILED_UNDER → Subject`, `HAS_SECTION → Section` |
| `Author {id, name}`, `Subject {id, term}` | authors, categories | — |
| `Section {key, entry_id, run_id, title, first_page, last_page, seq}` | `outline(run)` | `NEXT → Section`, `SHOWS → Figure`, `MENTIONS {role} → Concept`, `SIMILAR_TO {weight} → Section` |
| `Figure {id, entry_id, kind, page, caption}` | referenced figures and tables | — |
| `Concept {key, term}` | section summaries, `key_concepts` | — |

`MENTIONS` carries `role: defines | discusses` (the proposal's DEFINES / DISCUSSES), so one edge label serves
both and a query can ask for either. `SIMILAR_TO` aggregates `similar` paragraph links between two sections of
different publications (at least `MIN_SIMILAR_LINKS`). The 225 K paragraphs stay out of the graph.

Every projection is idempotent: `project_run` deletes the entry's sections and figures before creating them,
`project_concepts` replaces a section's concept edges. `rebuild` drops the graph and projects everything again,
and equals the incremental result.
"""

import hashlib
import logging
import re
from typing import Dict, List

from django.db import connection

from apps.graph import age

logger = logging.getLogger(__name__)

#: `similar` paragraph links between two sections before they are `SIMILAR_TO`.
MIN_SIMILAR_LINKS = 3

#: Characters of a figure caption kept on its node.
CAPTION_CHARS = 200

#: Rows per UNWIND statement.
BATCH = 500


def available() -> bool:
    return age.available()


def section_key(run_id, section: str) -> str:
    return hashlib.sha1(f"{run_id}\x00{section}".encode("utf-8")).hexdigest()[:20]


def concept_key(term: str) -> str:
    from apps.assistant.citations import fold

    return " ".join(re.sub(r"[^\w\s-]", " ", fold(term)).split())[:120]


def _batches(rows: List[dict]):
    for start in range(0, len(rows), BATCH):
        yield rows[start : start + BATCH]


def project_entry(entry) -> None:
    """The entry node with its authors and subjects."""
    authors = [
        {"id": str(link.author_id), "name": link.author.full_name}
        for link in entry.entry_authors.select_related("author")
    ]
    subjects = [{"id": str(category.pk), "term": category.term} for category in entry.categories.all()]
    entry_id = str(entry.pk)
    age.execute_many(
        [
            (
                "MERGE (e:Entry {id: $id}) SET e.title = $title, e.catalog_id = $catalog_id",
                {"id": entry_id, "title": entry.title, "catalog_id": str(entry.catalog_id)},
            ),
            ("MATCH (e:Entry {id: $id})-[r:WRITTEN_BY]->() DELETE r", {"id": entry_id}),
            ("MATCH (e:Entry {id: $id})-[r:FILED_UNDER]->() DELETE r", {"id": entry_id}),
            (
                "MATCH (e:Entry {id: $id}) UNWIND $rows AS row "
                "MERGE (a:Author {id: row.id}) SET a.name = row.name MERGE (e)-[:WRITTEN_BY]->(a)",
                {"id": entry_id, "rows": authors},
            ),
            (
                "MATCH (e:Entry {id: $id}) UNWIND $rows AS row "
                "MERGE (s:Subject {id: row.id}) SET s.term = row.term MERGE (e)-[:FILED_UNDER]->(s)",
                {"id": entry_id, "rows": subjects},
            ),
        ]
    )


def project_run(run) -> Dict[str, int]:
    """Sections, figures and their edges of one active run (replacing the entry's previous ones)."""
    from apps.assistant.models import SectionSummary
    from apps.extraction.models import Reference
    from apps.extraction.outline import outline

    if not available():
        return {}
    entry = run.acquisition.entry
    entry_id, run_id = str(entry.pk), str(run.pk)
    project_entry(entry)

    sections = outline(run)
    rows = [
        {
            "key": section_key(run.pk, section.title),
            "title": section.title[:300],
            "first_page": section.first_page,
            "last_page": section.last_page,
            "seq": index,
        }
        for index, section in enumerate(sections)
    ]
    statements = [
        ("MATCH (s:Section {entry_id: $entry_id}) DETACH DELETE s", {"entry_id": entry_id}),
        ("MATCH (f:Figure {entry_id: $entry_id}) DETACH DELETE f", {"entry_id": entry_id}),
    ]
    for batch in _batches(rows):
        statements.append(
            (
                "MATCH (e:Entry {id: $entry_id}) UNWIND $rows AS row "
                "CREATE (e)-[:HAS_SECTION]->(:Section {key: row.key, entry_id: $entry_id, run_id: $run_id, "
                "title: row.title, first_page: row.first_page, last_page: row.last_page, seq: row.seq})",
                {"entry_id": entry_id, "run_id": run_id, "rows": batch},
            )
        )
    pairs = [{"a": rows[index]["key"], "b": rows[index + 1]["key"]} for index in range(len(rows) - 1)]
    for batch in _batches(pairs):
        statements.append(
            (
                "UNWIND $rows AS row MATCH (a:Section {key: row.a}), (b:Section {key: row.b}) CREATE (a)-[:NEXT]->(b)",
                {"rows": batch},
            )
        )

    # Figures and tables a section's text refers to.
    shown: Dict[tuple, dict] = {}
    figures: Dict[str, dict] = {}
    references = (
        Reference.objects.filter(run=run, region__isnull=False, paragraph__section__isnull=False)
        .select_related("region")
        .values_list("paragraph__section", "region_id", "region__kind", "region__page", "region__printed_caption")
    )
    for section, region_id, kind, page, caption in references:
        figures[str(region_id)] = {
            "id": str(region_id),
            "kind": kind,
            "page": page,
            "caption": (caption or "")[:CAPTION_CHARS],
        }
        shown[(section_key(run.pk, section), str(region_id))] = {
            "s": section_key(run.pk, section),
            "f": str(region_id),
        }
    for batch in _batches(list(figures.values())):
        statements.append(
            (
                "UNWIND $rows AS row CREATE (:Figure {id: row.id, entry_id: $entry_id, kind: row.kind, "
                "page: row.page, caption: row.caption})",
                {"entry_id": entry_id, "rows": batch},
            )
        )
    for batch in _batches(list(shown.values())):
        statements.append(
            (
                "UNWIND $rows AS row MATCH (s:Section {key: row.s}), (f:Figure {id: row.f}) CREATE (s)-[:SHOWS]->(f)",
                {"rows": batch},
            )
        )

    similar = _similar_sections(run)
    for batch in _batches(similar):
        statements.append(
            (
                "UNWIND $rows AS row MATCH (a:Section {key: row.a}), (b:Section {key: row.b}) "
                "MERGE (a)-[r:SIMILAR_TO]->(b) SET r.weight = row.weight",
                {"rows": batch},
            )
        )
    age.execute_many(statements)

    summaries = SectionSummary.objects.filter(run=run)
    for summary in summaries:
        project_concepts(summary)
    return {"sections": len(rows), "figures": len(figures), "similar": len(similar), "summaries": len(summaries)}


def _similar_sections(run) -> List[dict]:
    """`[{a, b, weight}]`: sections of this run and of other active runs joined by enough `similar` links.

    Links in both directions: the run's sections were just re-created, so edges from sections projected
    earlier have to be drawn again too. An edge to a section not projected yet is drawn when that run is.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT s.run_id, s.section, t.run_id, t.section, COUNT(*) FROM extraction_paragraph_link l "
            "JOIN extraction_paragraph s ON s.id = l.source_id "
            "JOIN extraction_paragraph t ON t.id = l.target_id "
            "JOIN extraction_run rs ON rs.id = s.run_id "
            "JOIN extraction_run rt ON rt.id = t.run_id "
            "WHERE l.kind = 'similar' AND (s.run_id = %s OR t.run_id = %s) AND s.run_id <> t.run_id "
            "AND rs.is_active AND rt.is_active AND s.section IS NOT NULL AND t.section IS NOT NULL "
            "GROUP BY s.run_id, s.section, t.run_id, t.section HAVING COUNT(*) >= %s",
            [run.pk, run.pk, MIN_SIMILAR_LINKS],
        )
        rows = cursor.fetchall()
    return [
        {"a": section_key(source_run, source), "b": section_key(target_run, target), "weight": count}
        for source_run, source, target_run, target, count in rows
    ]


def project_concepts(summary) -> int:
    """Replace a section's `MENTIONS` edges with the concepts of its summary. Returns how many."""
    concepts = []
    seen = set()
    for item in summary.concepts or []:
        if not isinstance(item, dict) or not item.get("term"):
            continue
        key = concept_key(str(item["term"]))
        if not key or key in seen:
            continue
        seen.add(key)
        role = "defines" if item.get("role") == "defines" else "discusses"
        concepts.append({"key": key, "term": str(item["term"])[:120], "role": role})
    return replace_concepts(section_key(summary.run_id, summary.section), concepts)


def replace_concepts(section: str, concepts: List[dict], roles=("defines", "discusses")) -> int:
    if not available():
        return 0
    statements = [
        (
            "MATCH (s:Section {key: $key})-[m:MENTIONS]->() WHERE m.role IN $roles DELETE m",
            {"key": section, "roles": list(roles)},
        )
    ]
    if concepts:
        statements.append(
            (
                "MATCH (s:Section {key: $key}) UNWIND $rows AS row "
                "MERGE (c:Concept {key: row.key}) SET c.term = row.term "
                "CREATE (s)-[:MENTIONS {role: row.role}]->(c)",
                {"key": section, "rows": concepts},
            )
        )
    age.execute_many(statements)
    return len(concepts)


def add_definitions(run_id, section: str, terms: List[str]) -> int:
    """`key_concepts` (study mode) records the terms a section defines, next to its summary's concepts."""
    concepts = [
        {"key": concept_key(term), "term": term[:120], "role": "defines"} for term in terms if concept_key(term)
    ]
    if not available() or not concepts:
        return 0
    age.cypher(
        "MATCH (s:Section {key: $key}) UNWIND $rows AS row "
        "MERGE (c:Concept {key: row.key}) SET c.term = row.term "
        "MERGE (s)-[:MENTIONS {role: row.role}]->(c)",
        {"key": section_key(run_id, section), "rows": concepts},
    )
    return len(concepts)


def delete_entry(entry_id) -> None:
    if not available():
        return
    entry_id = str(entry_id)
    age.execute_many(
        [
            ("MATCH (n:Section {entry_id: $id}) DETACH DELETE n", {"id": entry_id}),
            ("MATCH (n:Figure {entry_id: $id}) DETACH DELETE n", {"id": entry_id}),
            ("MATCH (n:Entry {id: $id}) DETACH DELETE n", {"id": entry_id}),
        ]
    )


def rebuild() -> Dict[str, int]:
    """Drop and project everything again: every active run, then every summary's concepts."""
    from apps.extraction.models import ExtractionRun

    age.drop_graph()
    if not age.create_graph():
        return {}
    totals = {"runs": 0, "sections": 0}
    for run in ExtractionRun.objects.filter(is_active=True).select_related("acquisition__entry").iterator():
        counts = project_run(run)
        totals["runs"] += 1
        totals["sections"] += counts.get("sections", 0)
    return totals


def counts() -> Dict[str, int]:
    """Nodes per label, to compare with the relational tables (drift check)."""
    if not available():
        return {}
    result = {}
    for label in ("Entry", "Author", "Subject", "Section", "Figure", "Concept"):
        rows = age.cypher(f"MATCH (n:{label}) RETURN count(n)")
        result[label] = rows[0][0] if rows else 0
    return result
