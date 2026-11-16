"""#188: the retrieval benchmark summarises per `question language→book language`."""

from django.test import TestCase

from apps.assistant import retrieval_bench
from apps.assistant.tests import fixtures
from apps.core.models import Entry, Language


class ByLanguageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        admin = fixtures.user("admin")
        catalog = fixtures.catalog(admin)
        english = Language.objects.filter(alpha2="en").first() or Language.objects.create(
            name="English", alpha2="en", alpha3="eng"
        )
        cls.book = Entry.objects.create(creator=admin, catalog=catalog, title="Fluids", language=english)

    def test_groups_by_question_and_book_language(self):
        gold = [
            {"id": "a", "language": "sk", "question": "Čo je tlak?", "gold": [{"entry_id": str(self.book.pk)}]},
            {"id": "b", "language": "en", "question": "What is pressure?", "gold": [{"entry_id": str(self.book.pk)}]},
        ]
        results = [
            retrieval_bench.score_case(gold[0], [], []),
            retrieval_bench.score_case(gold[1], [str(self.book.pk)], []),
        ]
        groups = retrieval_bench.by_language(gold, results)
        self.assertEqual(set(groups), {"sk→en", "en→en"})
        self.assertEqual(groups["sk→en"]["entry_recall_5"], 0.0)
        self.assertEqual(groups["en→en"]["entry_recall_5"], 1.0)
