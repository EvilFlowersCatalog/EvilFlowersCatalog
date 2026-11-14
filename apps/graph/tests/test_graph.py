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
from apps.extraction.models import SectionSimilarity
from apps.graph import age, projection, queries


def _has_age() -> bool:
    age.forget()
    return age.available()


class GraphFixture(TransactionTestCase):
    """Three publications (one in a hidden catalog), each with a summary of "2 Bernoulli"; no tests of its own."""

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
        # Cross-book similarity comes from `apps.extraction.similarity` (#151; tested there), not paragraph links.
        SectionSimilarity.objects.create(
            source=self.run,
            source_section="2 Bernoulli",
            target=self.other_run,
            target_section="2 Bernoulli",
            score=0.8,
        )
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

    def _file(self, entry, term):
        from apps.core.models import Category

        category, _ = Category.objects.get_or_create(
            catalog=entry.catalog, term=term, defaults={"creator": self.reader}
        )
        entry.categories.add(category)


class GraphTests(GraphFixture):
    def test_projection_is_idempotent(self):
        self._project_all()
        first = projection.counts()
        self._project_all()
        self.assertEqual(projection.counts(), first)
        self.assertEqual(first["Section"], 6)
        self.assertEqual(first["Concept"], 1)
        self.assertEqual(first["Figure"], 2)
        self.assertEqual(first["SIMILAR_TO"], 1)

    def test_rebuild_projects_similarity_whatever_the_order(self):
        # #151: the run projected first draws no edge to a section not projected yet; the second draws it.
        projection.project_run(self.other_run)
        projection.project_run(self.run)
        self.assertEqual(projection.counts()["SIMILAR_TO"], 1)
        projection.project_run(self.other_run)
        self.assertEqual(projection.counts()["SIMILAR_TO"], 1)

    def test_similar_to_needs_both_runs_active(self):
        self.other_run.is_active = False
        self.other_run.save(update_fields=["is_active"])
        self._project_all()
        self.assertEqual(projection.counts()["SIMILAR_TO"], 0)

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

    def test_a_term_in_two_fields_is_two_concepts(self):
        # #153: "self-regulation" of the tobacco industry (UDC 61) is not the learner's (UDC 37).
        self._file(self.entry, "61")
        self._file(self.other, "37")
        SectionSummary.objects.filter(run__in=[self.run, self.other_run]).update(
            concepts=[{"term": "Self-regulation", "role": "defines"}]
        )
        self._project_all()
        self.assertEqual(projection.counts()["Concept"], 3)  # two fields, plus the hidden book's unscoped "Tlak"
        key = projection.section_key(self.run.pk, "2 Bernoulli")
        related = queries.related_sections(key, [str(self.other.pk)])
        self.assertNotIn("concepts", {item["via"] for item in related})
        self.assertNotIn("concepts+similar", {item["via"] for item in related})
        top = queries.top_concepts([str(self.entry.pk), str(self.other.pk)])
        self.assertEqual(
            sorted((item["term"], item["field"]) for item in top), [("Self-regulation", "3"), ("Self-regulation", "6")]
        )

        # The same field merges: both books in class 6 share one node again.
        self._file(self.other, "614")
        self.other.categories.filter(term="37").delete()
        projection.rebuild()
        self.assertEqual(projection.counts()["Concept"], 2)

    def test_prerequisites_never_point_forward_unlabelled(self):
        # #153: within one book an earlier section comes first; a later one is labelled.
        SectionSummary.objects.create(
            run=self.run,
            entry=self.entry,
            section="1 Introduction",
            first_page=1,
            last_page=2,
            summary="s",
            digest="d",
            concepts=[{"term": "Tlak", "role": "discusses"}],
        )
        SectionSummary.objects.create(
            run=self.other_run,
            entry=self.other,
            section="1 Introduction",
            first_page=1,
            last_page=2,
            summary="s",
            digest="d",
            concepts=[{"term": "Tlak", "role": "defines"}],
        )
        self._project_all()
        intro = projection.section_key(self.run.pk, "1 Introduction")
        found = queries.prerequisites(intro, [str(self.entry.pk), str(self.other.pk)])
        self.assertEqual(
            [(item["entry_id"], item["title"], item["later"]) for item in found],
            [(str(self.other.pk), "1 Introduction", False), (str(self.entry.pk), "2 Bernoulli", True)],
        )
        bernoulli = projection.section_key(self.other_run.pk, "2 Bernoulli")
        earlier = queries.prerequisites(bernoulli, [str(self.other.pk)])
        self.assertEqual([(item["title"], item["later"]) for item in earlier], [("1 Introduction", False)])

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
