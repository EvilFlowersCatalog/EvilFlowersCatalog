"""#202: the provider queue (merge field by field, go on past the first match) and its drivers, offline."""

from unittest import mock

import pymarc
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.models import AuthSource, Catalog, Entry, User
from apps.core.services import isbn_metadata, isbn_providers, z3950
from apps.core.services.isbn_providers import ProviderError, Record
from apps.harvest.records import SourceAuthor

ISBN = "9788022755047"


def _response(payload, status=200):
    response = mock.MagicMock(status_code=status, text=str(payload))
    response.json.return_value = payload
    return response


class QueueTests(SimpleTestCase):
    def providers(self, **answers):
        """Fake drivers: each answers its Record, None, or raises; calls are recorded."""
        self.calls = []

        def driver(name, answer):
            def ask(isbn):
                self.calls.append(name)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            return ask

        return mock.patch.dict(isbn_providers.PROVIDERS, {name: driver(name, a) for name, a in answers.items()})

    def test_goes_on_past_a_partial_match_and_stops_when_complete(self):
        with self.providers(
            a=Record("a", authors=[SourceAuthor("Haffner", "Oto")], title="Strojové videnie"),
            b=None,
            c=Record("c", publisher="SPEKTRUM STU", year="2024", authors=[SourceAuthor("Other")]),
            d=Record("d", year="1999"),
        ):
            found = isbn_metadata.lookup(ISBN, services=["a", "b", "c", "d"])
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertEqual([(p.surname, p.name) for p in found.authors], [("Haffner", "Oto")])  # first provider wins
        self.assertEqual((found.publisher, found.year), ("SPEKTRUM STU", "2024"))
        self.assertEqual(found.sources["authors"], "a")
        self.assertEqual(found.sources["year"], "c")
        self.assertEqual(found.service, "a+c")
        self.assertEqual(found.asked, {"a": "found", "b": "none", "c": "found"})

    def test_wanted_limits_the_queue(self):
        with self.providers(a=Record("a", year="2020"), b=Record("b", publisher="P")):
            found = isbn_metadata.lookup(ISBN, services=["a", "b"], wanted={"published_at"})
        self.assertEqual(self.calls, ["a"])
        self.assertEqual(found.year, "2020")

    def test_failures_and_unknown_providers_are_skipped(self):
        with self.providers(a=ProviderError("down"), b=RuntimeError("bug"), c=Record("c", publisher="P")):
            found = isbn_metadata.lookup(ISBN, services=["a", "nope", "b", "c"])
        self.assertEqual(found.publisher, "P")
        self.assertEqual(found.asked, {"a": "failed", "nope": "skipped", "b": "failed", "c": "found"})

    def test_nothing_found_is_none(self):
        with self.providers(a=None):
            self.assertIsNone(isbn_metadata.lookup(ISBN, services=["a"]))
        self.assertIsNone(isbn_metadata.lookup("urn:isbn:123", services=["a"]))

    def test_editors_satisfy_authors_and_become_the_people(self):
        with self.providers(
            a=Record("a", editors=[SourceAuthor("Doe", "Jane")], publisher="Springer", year="2016"),
            b=Record("b", authors=[SourceAuthor("Never")]),
        ):
            found = isbn_metadata.lookup(ISBN, services=["a", "b"])
        self.assertEqual(self.calls, ["a"])
        self.assertEqual([(p.surname, p.name) for p in found.people], [("Doe", "Jane")])
        self.assertEqual(found.authors, [])


@override_settings(EVILFLOWERS_ISBN_METADATA_SERVICES=["a", "b"])
class EnrichQueueTests(TestCase):
    def test_enrich_asks_only_for_what_the_entry_lacks(self):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        user = User.objects.create(username="o", name="O", surname="W", auth_source=source)
        catalog = Catalog.objects.create(creator=user, title="STU", url_name="stu")
        entry = Entry.objects.create(
            creator=user, catalog=catalog, title="Book", publisher="STU", identifiers={"isbn": ISBN}
        )
        calls = []

        def a(isbn):
            calls.append("a")
            return Record("a", authors=[SourceAuthor("Haffner", "Oto")])

        def b(isbn):
            calls.append("b")
            return Record("b", year="2025", publisher="Elsewhere")

        with mock.patch.dict(isbn_providers.PROVIDERS, {"a": a, "b": b}):
            found = isbn_metadata.enrich(entry)
        entry.refresh_from_db()
        self.assertEqual(calls, ["a", "b"])
        self.assertEqual(set(found), {"authors", "published_at"})
        self.assertEqual(entry.publisher, "STU")
        self.assertEqual(entry.entry_authors.get().author.surname, "Haffner")


@override_settings(
    EVILFLOWERS_CROSSREF_MAILTO="library@example.org",
    EVILFLOWERS_GOOGLE_BOOKS_API_KEY="",
    EVILFLOWERS_ISBN_METADATA_TIMEOUT=5,
)
class DriverTests(SimpleTestCase):
    def test_crossref_prefers_the_book_and_keeps_editors_apart(self):
        payload = {
            "message": {
                "items": [
                    {"type": "book-chapter", "ISBN": [ISBN], "author": [{"given": "C", "family": "Chapter"}]},
                    {
                        "type": "edited-book",
                        "ISBN": ["978-80-227-5504-7"],
                        "title": ["Dignity"],
                        "editor": [{"given": "Doris", "family": "Schroeder"}],
                        "publisher": "Springer",
                        "issued": {"date-parts": [[2017, 5]]},
                        "DOI": "10.1007/x",
                    },
                ]
            }
        }
        with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response(payload)) as get:
            record = isbn_providers.crossref(ISBN)
        self.assertEqual(get.call_args.kwargs["params"]["mailto"], "library@example.org")
        self.assertEqual(record.authors, [])
        self.assertEqual([(p.surname, p.name) for p in record.editors], [("Schroeder", "Doris")])
        self.assertEqual((record.publisher, record.year, record.doi), ("Springer", "2017", "10.1007/x"))

    def test_crossref_chapter_only_gives_the_book_facts_not_its_authors(self):
        payload = {
            "message": {
                "items": [
                    {
                        "type": "book-chapter",
                        "author": [{"given": "C", "family": "Chapter"}],
                        "editor": [{"given": "E", "family": "Editor"}],
                        "publisher": "Springer",
                        "issued": {"date-parts": [[2016]]},
                    }
                ]
            }
        }
        with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response(payload)):
            record = isbn_providers.crossref(ISBN)
        self.assertEqual(record.authors, [])
        self.assertEqual([p.surname for p in record.editors], ["Editor"])
        self.assertEqual(record.year, "2016")

    def test_openlibrary(self):
        payload = {
            f"ISBN:{ISBN}": {
                "title": "Dignity",
                "subtitle": "Middle East and West",
                "authors": [{"name": "Doris Schroeder"}],
                "publishers": [{"name": "Springer"}],
                "publish_date": "Jun 05, 2017",
            }
        }
        with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response(payload)):
            record = isbn_providers.openlibrary(ISBN)
        self.assertEqual([(p.surname, p.name) for p in record.authors], [("Schroeder", "Doris")])
        self.assertEqual(
            (record.publisher, record.year, record.title), ("Springer", "2017", "Dignity : Middle East and West")
        )
        with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response({})):
            self.assertIsNone(isbn_providers.openlibrary(ISBN))

    def test_http_errors_raise(self):
        with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response({}, status=429)):
            with self.assertRaises(ProviderError):
                isbn_providers.crossref(ISBN)

    def test_googlebooks_is_skipped_without_a_key_and_used_with_one(self):
        with mock.patch("apps.core.services.isbn_providers.requests.get") as get:
            self.assertIsNone(isbn_providers.googlebooks(ISBN))
        get.assert_not_called()
        payload = {"items": [{"volumeInfo": {"title": "T", "authors": ["Jane Doe"], "publishedDate": "2016-01"}}]}
        with override_settings(EVILFLOWERS_GOOGLE_BOOKS_API_KEY="k"):
            with mock.patch("apps.core.services.isbn_providers.requests.get", return_value=_response(payload)) as get:
                record = isbn_providers.googlebooks(ISBN)
        self.assertEqual(get.call_args.kwargs["params"]["key"], "k")
        self.assertEqual((record.year, record.authors[0].surname), ("2016", "Doe"))


def _marc() -> bytes:
    record = pymarc.Record(force_utf8=True)
    record.leader = pymarc.Leader("00000nam a2200000   4500")
    record.add_field(pymarc.Field(tag="008", data="250101s2025    xo            000 0 slo d"))
    subfield = pymarc.Subfield
    record.add_field(pymarc.Field("020", [" ", " "], [subfield("a", ISBN)]))
    record.add_field(pymarc.Field("100", ["1", " "], [subfield("a", "Haffner, Oto,"), subfield("4", "aut")]))
    record.add_field(pymarc.Field("245", ["1", "0"], [subfield("a", "Strojové videnie pre Industry 4.0 /")]))
    record.add_field(pymarc.Field("264", [" ", "1"], [subfield("a", "Bratislava :"), subfield("b", "SPEKTRUM STU,")]))
    record.add_field(pymarc.Field("700", ["1", " "], [subfield("a", "Tanuška, Pavol,"), subfield("4", "rev")]))
    record.add_field(pymarc.Field("700", ["1", " "], [subfield("a", "Kozák, Štefan,"), subfield("4", "edt")]))
    return record.as_marc()


@override_settings(EVILFLOWERS_SNK_Z3950="z3950.snk.sk:1111/clas01", EVILFLOWERS_ISBN_METADATA_TIMEOUT=5)
class SnkTests(SimpleTestCase):
    def test_marc_roles_publisher_and_008_year(self):
        record = isbn_providers.parse_marc(_marc())
        self.assertEqual([(p.surname, p.name) for p in record.authors], [("Haffner", "Oto")])
        self.assertEqual([(p.surname, p.name) for p in record.editors], [("Kozák", "Štefan")])  # reviewer left out
        self.assertEqual((record.publisher, record.year, record.language), ("SPEKTRUM STU", "2025", "slo"))
        self.assertEqual(record.title, "Strojové videnie pre Industry 4.0")

    def test_snk_asks_the_configured_catalogue(self):
        with mock.patch("apps.core.services.z3950.search", return_value=[_marc()]) as search:
            record = isbn_providers.snk(ISBN)
        self.assertEqual(search.call_args.args[:4], ("z3950.snk.sk", 1111, "clas01", ISBN))
        self.assertEqual(record.provider, "snk")
        with mock.patch("apps.core.services.z3950.search", return_value=[]):
            self.assertIsNone(isbn_providers.snk(ISBN))
        with mock.patch("apps.core.services.z3950.search", side_effect=z3950.Z3950Error("closed")):
            with self.assertRaises(ProviderError):
                isbn_providers.snk(ISBN)


class BerTests(SimpleTestCase):
    def test_round_trip_and_long_tags(self):
        apdu = z3950.search_request("clas01", ISBN)
        [node] = z3950.decode(apdu)
        self.assertEqual((node.cls, node.number), (z3950.CONTEXT, 22))
        [term] = node.find(z3950.CONTEXT, 45)
        self.assertEqual(term.value, ISBN.encode())
        [use] = node.find(z3950.CONTEXT, 121)
        self.assertEqual(use.integer(), z3950.USE_ISBN)

    def test_indefinite_lengths_and_incomplete_buffers(self):
        marc = _marc()
        external = b"\x28\x80" + z3950.oid(z3950.MARC21) + z3950.tlv(1, marc) + b"\x00\x00"
        present = b"\xb9\x80" + z3950.integer(24, 1) + b"\xbc\x80\x30\x80\xa1\x80" + external + b"\x00\x00" * 3
        present += b"\x00\x00"
        node, end = z3950._one(present, 0)
        self.assertEqual(end, len(present))
        [found] = node.find(z3950.UNIVERSAL, 8, constructed=True)
        self.assertEqual([child.value for child in found.children if child.number == 1], [marc])
        with self.assertRaises(z3950.Incomplete):
            z3950._one(present[:-3], 0)

    def test_search_through_a_fake_server(self):
        init = z3950.seq(21, z3950.boolean(12, True))
        searched = z3950.seq(23, z3950.integer(23, 1))
        presented = z3950.seq(
            25,
            z3950.integer(24, 1),
            z3950.seq(
                28,
                z3950.seq(
                    16,
                    z3950.seq(
                        1,
                        z3950.seq(
                            1, z3950.seq(8, z3950.oid(z3950.MARC21), z3950.tlv(1, b"MARC"), cls=z3950.UNIVERSAL)
                        ),
                    ),
                    cls=z3950.UNIVERSAL,
                ),
            ),
        )
        replies = [init, searched, presented]
        sock = mock.MagicMock()
        sock.recv.side_effect = lambda size: replies.pop(0)
        with mock.patch("apps.core.services.z3950.socket.create_connection", return_value=sock):
            self.assertEqual(z3950.search("h", 1, "db", ISBN), [b"MARC"])
        self.assertEqual(sock.sendall.call_count, 3)
