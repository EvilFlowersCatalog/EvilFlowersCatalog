"""The AGE graph (IP-030 Phase 3b): projection idempotence, ACL, rebuild equals incremental.

These need PostgreSQL with Apache AGE (the evilflowers-postgresql image) and skip elsewhere:

    PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres python manage.py test apps.graph
"""

import unittest

from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase

from apps.assistant.models import SectionSummary
from apps.assistant.tests import fixtures
from apps.extraction import references
from apps.extraction.models import ParagraphLink
from apps.graph import age, projection, queries


def _has_age() -> bool:
    age.forget()
    return age.available()


class GraphTests(TransactionTestCase):
    def setUp(self):
        if not _has_age():
            age.create_graph()
            if not _has_age():
                raise unittest.SkipTest("Apache AGE is not installed in this database")
        projection.rebuild()
        self.reader = fixtures.user()
        self.open = fixtures.catalog(self.reader)
        self.hidden = fixtures.catalog(self.reader, public=False, name="hidden")
        self.entry, self.run, self.rows, self.venturi, _shear = fixtures.publication(self.reader, self.open)
        self.other, self.other_run, self.other_rows, _f, _s = fixtures.publication(
            self.reader, self.open, title="Hydraulics"
        )
        self.secret, self.secret_run, self.secret_rows, _f2, _s2 = fixtures.publication(
            self.reader, self.hidden, title="Secret"
        )
        references.build(self.run)
        for target in self.other_rows[2:5]:
            for source in self.rows[2:5]:
                ParagraphLink.objects.create(source=source, target=target, kind=ParagraphLink.Kind.SIMILAR, score=0.9)
        for extraction, entry in (
            (self.run, self.entry),
            (self.other_run, self.other),
            (self.secret_run, self.secret),
        ):
            summary = SectionSummary.objects.create(
                run=extraction,
                entry=entry,
                section="2 Bernoulli",
                first_page=3,
                last_page=5,
                summary="s",
                digest="d",
                concepts=[{"term": "Tlak", "role": "defines" if entry == self.entry else "discusses"}],
            )
            self.summary = summary if entry == self.entry else getattr(self, "summary", None)

    def _project_all(self):
        for extraction in (self.run, self.other_run, self.secret_run):
            projection.project_run(extraction)

    def test_projection_is_idempotent(self):
        self._project_all()
        first = projection.counts()
        self._project_all()
        self.assertEqual(projection.counts(), first)
        self.assertEqual(first["Section"], 6)
        self.assertEqual(first["Concept"], 1)
        self.assertEqual(first["Figure"], 2)

    def test_rebuild_equals_incremental(self):
        self._project_all()
        incremental = projection.counts()
        projection.rebuild()
        self.assertEqual(projection.counts(), incremental)

    def test_related_sections_never_surface_a_hidden_publication(self):
        self._project_all()
        key = projection.section_key(self.run.pk, "2 Bernoulli")
        request = fixtures.request_for(self.reader)
        request.user = AnonymousUser()
        found = queries.related_sections(key, queries.visible_entry_ids(request))
        self.assertEqual([item["entry_id"] for item in found], [str(self.other.pk)])
        self.assertEqual(found[0]["via"], "concepts+similar")
        everyone = queries.related_sections(key, [str(self.other.pk), str(self.secret.pk)])
        self.assertEqual({item["entry_id"] for item in everyone}, {str(self.other.pk), str(self.secret.pk)})

    def test_prerequisites_and_top_concepts(self):
        self._project_all()
        other_key = projection.section_key(self.other_run.pk, "2 Bernoulli")
        before = queries.prerequisites(other_key, [str(self.entry.pk)])
        self.assertEqual([item["entry_id"] for item in before], [str(self.entry.pk)])
        top = queries.top_concepts([str(self.entry.pk), str(self.other.pk)])
        self.assertEqual(top[0]["term"], "Tlak")
        self.assertEqual(top[0]["publications"], 2)

    def test_concepts_are_parameters_never_query_text(self):
        self._project_all()
        hostile = "x' }) DETACH DELETE n //$$"
        projection.replace_concepts(
            projection.section_key(self.run.pk, "2 Bernoulli"),
            [{"key": projection.concept_key(hostile), "term": hostile, "role": "defines"}],
        )
        self.assertEqual(projection.counts()["Section"], 6)

    def test_follow_related_reads_the_graph_under_the_readers_access(self):
        from apps.assistant import evidence, tools
        from apps.assistant.library import ToolContext

        self._project_all()
        request = fixtures.request_for(self.reader)
        request.user = AnonymousUser()
        context = ToolContext(request=request, ledger=evidence.Ledger(), reading=tools.Reading())
        label, _ = context.ledger.issue(
            {"kind": "passage", "entry_id": str(self.entry.pk), "page": 3, "paragraph_id": str(self.rows[2].pk)}
        )
        payload, _text = tools.run(request, tools.FOLLOW, {"label": label, "relation": "related"}, context=context)
        self.assertEqual([p["entry_id"] for p in payload["passages"]], [str(self.other.pk)])
        self.assertEqual(payload["passages"][0]["label"], "E2")

    def test_deleting_an_entry_removes_its_nodes(self):
        self._project_all()
        projection.delete_entry(self.other.pk)
        counts = projection.counts()
        self.assertEqual(counts["Section"], 4)
        self.assertEqual(counts["Entry"], 2)
