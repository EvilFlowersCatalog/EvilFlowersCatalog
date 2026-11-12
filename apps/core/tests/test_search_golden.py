"""
IP-020 / #101: golden query set for the in-database full-text search.

A small trilingual corpus (EN/SK/DE, one Slovak title deliberately tagged `en`) and 40 queries:
inflected Slovak, input without diacritics, English/German stemming, author names, typos and
web-search syntax. The test reports recall@10 and MRR of the new search next to the previous
implementation (per-word `icontains`, kept below as `legacy_search`) and fails if the new search
gets worse than the thresholds or than the old one.

Run with `-v 2` to see the report.
"""

from django.db.models import Q
from django.test import TestCase

from apps.core.models import AuthSource, Catalog, Category, Entry, EntryAuthor, Author, Language, User

# key: (title, language, author (name, surname), category term, summary, popularity)
CORPUS = {
    "climate": (
        "Climate Change and Global Warming",
        "en",
        ("James", "Hansen"),
        "551",
        "How rising temperatures affect climates around the world.",
        5,
    ),
    "edupsych": ("Educational Psychology", "en", ("Anita", "Woolfolk"), "37", "Learning theories for teachers.", 3),
    "vzdelavanie": (
        "Vzdelávanie dospelých",
        "sk",
        ("Ján", "Novák"),
        "37",
        "<p>Teória a prax <b>vzdelávania</b> dospelých v Európe.</p>",
        1,
    ),
    "dejiny": ("Dejiny Slovenska", "sk", ("Dušan", "Kováč"), "94", "Prehľad dejín od praveku po súčasnosť.", 4),
    "philosophie": (
        "Geschichte der Philosophie",
        "de",
        ("Johannes", "Hirschberger"),
        "1",
        "Die großen Denker des Abendlandes.",
        2,
    ),
    "buecher": ("Die Bücher der Natur", "de", ("Konrad", "Lorenz"), "57", "Über das Verhalten der Tiere.", 2),
    "clrs": (
        "Introduction to Algorithms",
        "en",
        ("Thomas", "Cormen"),
        "004",
        "Sorting, graphs and dynamic programming.",
        9,
    ),
    "algoritmy": (
        "Algoritmy a dátové štruktúry",
        "sk",
        ("Pavol", "Návrat"),
        "004",
        "Základné algoritmy triedenia a vyhľadávania.",
        3,
    ),
    "ml": ("Machine Learning", "en", ("Tom", "Mitchell"), "004", "Neural networks and decision trees.", 7),
    "strojove": (
        "Strojové učenie v praxi",
        "sk",
        ("Mária", "Bieliková"),
        "004",
        "Neurónové siete a rozhodovacie stromy.",
        2,
    ),
    "rudin": ("Mathematical Analysis", "en", ("Walter", "Rudin"), "51", "Real and complex analysis.", 4),
    "salat": ("Matematická analýza", "sk", ("Tibor", "Šalát"), "51", "Diferenciálny a integrálny počet.", 3),
    "architektura": (
        "Architektúra Bratislavy",
        "sk",
        ("Štefan", "Šlachta"),
        "72",
        "Mestské stavby a ich história.",
        1,
    ),
    "sustainable": (
        "Sustainable Architecture",
        "en",
        ("Brian", "Edwards"),
        "72",
        "Green building design for changing climates.",
        1,
    ),
    "kniznice": ("Knihy a knižnice", "sk", ("Eva", "Kováčová"), "02", "O knižniciach na Slovensku.", 1),
    "woerterbuch": (
        "Wörterbuch der Informatik",
        "de",
        ("Hans", "Schneider"),
        "004",
        "Begriffe der Informatik.",
        1,
    ),
    # Slovak title tagged as English (the dev catalog has such entries).
    "ekonomika": (
        "Ekonomika podniku",
        "en",
        ("Jozef", "Majtán"),
        "33",
        "Riadenie a financovanie podnikov.",
        2,
    ),
    "mankiw": ("Principles of Economics", "en", ("Gregory", "Mankiw"), "33", "Supply, demand and markets.", 6),
    "chemia": ("Chemické reakcie", "sk", ("Viktor", "Milata"), "54", "Organická chémia pre študentov.", 1),
    "physik": ("Physik für Ingenieure", "de", ("Ekbert", "Hering"), "53", "Mechanik und Thermodynamik.", 1),
}

CATEGORIES = {
    "551": {"sk": "Meteorológia", "en": "Meteorology"},
    "37": {"sk": "Vzdelávanie", "en": "Education"},
    "94": {"sk": "Dejiny", "en": "History"},
    "1": {"sk": "Filozofia", "en": "Philosophy"},
    "57": {"sk": "Biológia", "en": "Biology"},
    "004": {"sk": "Informatika", "en": "Computer science"},
    "51": {"sk": "Matematika", "en": "Mathematics"},
    "72": {"sk": "Architektúra", "en": "Architecture"},
    "02": {"sk": "Knihovníctvo", "en": "Library science"},
    "33": {"sk": "Ekonómia", "en": "Economics"},
    "54": {"sk": "Chémia", "en": "Chemistry"},
    "53": {"sk": "Fyzika", "en": "Physics"},
}

# query -> relevant entries, the best one first (MRR counts the rank of the first relevant hit)
GOLDEN = [
    ("climates", ["climate", "sustainable"]),
    ("climate", ["climate", "sustainable"]),
    ("educational", ["edupsych", "vzdelavanie"]),
    ("education", ["edupsych", "vzdelavanie"]),
    ("vzdelávanie", ["vzdelavanie", "edupsych"]),
    ("vzdelavanie", ["vzdelavanie", "edupsych"]),
    ("vzdelávania", ["vzdelavanie"]),
    ("dejiny", ["dejiny"]),
    ("dejín", ["dejiny"]),
    ("slovensku", ["kniznice", "dejiny"]),
    ("philosophie", ["philosophie"]),
    ("Bücher", ["buecher"]),
    ("bucher", ["buecher"]),
    ("algorithms", ["clrs"]),
    ("algoritmy", ["algoritmy"]),
    ("algoritmus", ["algoritmy"]),
    ("machine learning", ["ml"]),
    ("strojové učenie", ["strojove"]),
    ("strojove ucenie", ["strojove"]),
    ("neurónové siete", ["strojove"]),
    ("neural networks", ["ml"]),
    ("Cormen", ["clrs"]),
    ("bielikova", ["strojove"]),
    ("Bieliková", ["strojove"]),
    ("Šalát", ["salat"]),
    ("matematika", ["salat", "rudin"]),
    ("analýza", ["salat"]),
    ("architektúry", ["architektura"]),
    ("knižniciach", ["kniznice"]),
    ("economics", ["mankiw", "ekonomika"]),
    ("ekonomika", ["ekonomika"]),
    ("podnikov", ["ekonomika"]),
    ("Wörterbuch", ["woerterbuch"]),
    ("Tier", ["buecher"]),
    ("Mankiw economics", ["mankiw"]),
    ('"global warming"', ["climate"]),
    ("climate -architecture", ["climate"]),
    # typos
    ("Hirshberger", ["philosophie"]),
    ("machne learnig", ["ml"]),
    ("algoritmi", ["algoritmy"]),
]

RECALL_AT_10_MIN = 0.95
MRR_MIN = 0.9


def legacy_search(qs, value):
    """The pre-#101 `EntryFilter.filter_query`: per-word icontains (no stemming, no unaccent)."""
    condition = Q()
    for term in value.strip().split():
        condition &= (
            Q(title__icontains=term)
            | Q(summary__icontains=term)
            | Q(publisher__icontains=term)
            | Q(authors__name__icontains=term)
            | Q(authors__surname__icontains=term)
            | Q(categories__label__icontains=term)
            | Q(categories__term__icontains=term)
        )
    return list(qs.filter(condition).distinct().order_by("-popularity", "-created_at").values_list("pk", flat=True))


def metrics(results: dict, keys: dict) -> tuple[float, float]:
    recall, reciprocal = [], []
    for query, relevant in GOLDEN:
        ranked = results[query][:10]
        ids = [keys[key] for key in relevant]
        recall.append(sum(1 for pk in ids if pk in ranked) / len(ids))
        rank = next((position for position, pk in enumerate(results[query], start=1) if pk == ids[0]), None)
        reciprocal.append(1 / rank if rank else 0.0)
    return sum(recall) / len(recall), sum(reciprocal) / len(reciprocal)


class SearchFixtureMixin:
    @classmethod
    def build_corpus(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.owner = User(username="owner", name="O", surname="W", auth_source=source)
        cls.owner.set_unusable_password()
        cls.owner.save()
        cls.catalog = Catalog.objects.create(creator=cls.owner, title="Open", url_name="open", is_public=True)

        languages = {}
        for code, alpha3, name in (("en", "eng", "English"), ("sk", "slk", "Slovak"), ("de", "deu", "German")):
            languages[code] = Language.objects.filter(alpha2=code).first() or Language.objects.create(
                alpha2=code, alpha3=alpha3, name=name
            )

        categories = {
            term: Category.objects.create(creator=cls.owner, catalog=cls.catalog, term=term, label=label)
            for term, label in CATEGORIES.items()
        }

        cls.keys = {}
        for key, (title, language, (name, surname), term, summary, popularity) in CORPUS.items():
            entry = Entry.objects.create(
                creator=cls.owner,
                catalog=cls.catalog,
                title=title,
                language=languages[language],
                summary=summary,
                popularity=popularity,
            )
            author = Author.objects.create(catalog=cls.catalog, name=name, surname=surname)
            EntryAuthor.objects.create(entry=entry, author=author, position=0)
            entry.categories.add(categories[term])
            cls.keys[key] = entry.pk
        cls.categories = categories


class GoldenQuerySetTests(SearchFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.build_corpus()

    def search(self, query: str) -> list:
        response = self.client.get(
            "/api/v1/entries", {"query": query, "catalog_id": str(self.catalog.pk), "limit": 50}
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        return [item["id"] for item in response.json()["items"]]

    def test_golden_queries(self):
        keys = {key: str(pk) for key, pk in self.keys.items()}
        new = {query: self.search(query) for query, _ in GOLDEN}
        old_qs = Entry.objects.filter(catalog=self.catalog)
        old = {query: [str(pk) for pk in legacy_search(old_qs, query)] for query, _ in GOLDEN}

        new_recall, new_mrr = metrics(new, keys)
        old_recall, old_mrr = metrics(old, keys)

        names = {pk: key for key, pk in keys.items()}
        misses = [
            f"  {query!r}: expected {relevant}, got {[names.get(pk, pk) for pk in new[query][:5]]}"
            for query, relevant in GOLDEN
            if not new[query] or new[query][0] != keys[relevant[0]]
        ]
        report = (
            f"\nGolden queries: {len(GOLDEN)}\n"
            f"  before (icontains): recall@10 {old_recall:.3f}  MRR {old_mrr:.3f}\n"
            f"  after  (fulltext):  recall@10 {new_recall:.3f}  MRR {new_mrr:.3f}\n"
            + ("Not ranked first:\n" + "\n".join(misses) if misses else "")
        )
        print(report)

        self.assertGreaterEqual(new_recall, RECALL_AT_10_MIN, report)
        self.assertGreaterEqual(new_mrr, MRR_MIN, report)
        self.assertGreaterEqual(new_recall, old_recall, report)
        self.assertGreaterEqual(new_mrr, old_mrr, report)

    def test_issue_acceptance_queries(self):
        """#101: `climates`, `educational`, `vzdelavanie`, `vzdelávania` return the expected entries."""
        self.assertIn(str(self.keys["climate"]), self.search("climates"))
        self.assertIn(str(self.keys["sustainable"]), self.search("climates"))
        self.assertEqual(self.search("educational")[0], str(self.keys["edupsych"]))
        self.assertEqual(self.search("vzdelavanie")[0], str(self.keys["vzdelavanie"]))
        self.assertEqual(self.search("vzdelávania")[0], str(self.keys["vzdelavanie"]))
