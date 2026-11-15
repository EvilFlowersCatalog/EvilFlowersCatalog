"""#177: rights metadata on entries, its sources, the TDM cap and where it is published."""

import json
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.content import policies, rights, tdmrep
from apps.content.tests.fixtures import ALLOWED_RANGES, INSIDE, Scenarios, request_for
from apps.core import settings_store
from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, Entry, Language, UserCatalog

CC_BY = "https://creativecommons.org/licenses/by/4.0/"


class CleanTests(SimpleTestCase):
    def test_clean_validates_and_stamps_the_source(self):
        cleaned = rights.clean({"license_url": CC_BY, "license_name": "CC BY 4.0", "tdm_reservation": True}, "manual")
        self.assertEqual(
            {key: cleaned[key] for key in ("license_url", "license_name", "tdm_reservation", "source")},
            {"license_url": CC_BY, "license_name": "CC BY 4.0", "tdm_reservation": 1, "source": "manual"},
        )
        self.assertIsNone(rights.clean({}, "manual"))
        self.assertIsNone(rights.clean(None, "manual"))
        for bad in ({"license_url": "ftp://x"}, {"tdm_reservation": 2}, {"colour": "red"}, "CC BY"):
            with self.subTest(bad=bad), self.assertRaises(rights.RightsError):
                rights.clean(bad, "manual")

    def test_an_automatic_source_never_overwrites_a_manual_edit(self):
        manual = rights.clean({"license_url": CC_BY}, "manual")
        crossref = rights.clean({"license_url": "https://example.org/terms"}, "crossref")
        self.assertIs(rights.merge(manual, crossref), manual)
        self.assertIs(rights.merge(crossref, manual), manual)
        self.assertIs(rights.merge(None, crossref), crossref)

    def test_crossref_prefers_the_published_version_and_reads_the_tdm_licence(self):
        message = {
            "license": [
                {"URL": "https://example.org/am", "content-version": "am"},
                {"URL": CC_BY, "content-version": "vor", "start": {"date-time": "2020-01-01T00:00:00Z"}},
                {"URL": "https://example.org/tdm", "content-version": "tdm"},
                {
                    "URL": "https://example.org/future",
                    "content-version": "vor",
                    "start": {"date-time": "2999-01-01T00:00:00Z"},
                },
            ]
        }
        found = rights.from_crossref_message(message)
        self.assertEqual((found["license_url"], found["tdm_policy_url"]), (CC_BY, "https://example.org/tdm"))
        self.assertEqual(found["source"], "crossref")
        self.assertIsNone(rights.from_crossref_message({"license": []}))

    def test_dataverse_licence(self):
        found = rights.from_dataverse(
            {"license": {"name": "CC0 1.0", "uri": "http://creativecommons.org/publicdomain/zero/1.0"}}
        )
        self.assertEqual((found["license_name"], found["source"]), ("CC0 1.0", "dataverse"))
        self.assertIsNone(rights.from_dataverse({}))


@override_settings(EVILFLOWERS_ALLOWED_IP_RANGES=ALLOWED_RANGES)
class RightsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Scenarios.build(cls)
        UserCatalog.objects.create(user=cls.owner, catalog=cls.public, mode=UserCatalog.Mode.MANAGE)
        cls.language = Language.objects.filter(alpha2="en").first() or Language.objects.create(
            name="English", alpha2="en", alpha3="eng"
        )

    def setUp(self):
        policies.invalidate()
        tdmrep.invalidate()
        self.addCleanup(policies.invalidate)
        self.addCleanup(tdmrep.invalidate)

    def _reserve(self, entry, **extra):
        entry.rights = {"tdm_reservation": 1, "source": "manual", **extra}
        entry.save()

    def test_a_reservation_applies_the_tdm_cap_and_explain_shows_it(self):
        from apps.content import services

        self._reserve(self.open)
        request = request_for(self.reader, INSIDE)
        explained = services.explain(request, self.open.pk, policy_key="open-access")
        self.assertIn({"cap": "tdm_reserved", "tier": "snippet"}, explained["caps"])
        self.assertEqual((explained["tier"], explained["reason"]), ("snippet", "tdm_reserved"))
        # `parity` leaves it at full: rights never pick or tighten a policy beyond its own cap (Q7).
        self.assertEqual(services.explain(request, self.open.pk)["tier"], "full")

    def test_rest_writes_rights_as_a_manual_edit(self):
        key = ApiKey.objects.create(user=self.owner, name="test")
        headers = {"Authorization": f"Bearer {JWTFactory(self.owner.pk).api_key(str(key.pk))}"}
        response = self.client.put(
            f"/api/v1/catalogs/{self.public.pk}/entries/{self.open.pk}",
            {"title": self.open.title, "language_code": "en", "rights": {"license_url": CC_BY, "tdm_reservation": 0}},
            content_type="application/json",
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.open.refresh_from_db()
        self.assertEqual((self.open.rights["license_url"], self.open.rights["source"]), (CC_BY, "manual"))
        self.assertEqual(response.json()["response"]["rights"]["license_url"], CC_BY)

        refused = self.client.put(
            f"/api/v1/catalogs/{self.public.pk}/entries/{self.open.pk}",
            {"title": self.open.title, "language_code": "en", "rights": {"tdm_reservation": 5}},
            content_type="application/json",
            headers=headers,
        )
        self.assertEqual(refused.status_code, 422)

    def test_crossref_fills_rights_by_doi(self):
        self.open.identifiers = {"doi": "doi:10.1234/abc"}
        self.open.save()
        message = {"license": [{"URL": CC_BY, "content-version": "vor"}]}
        with mock.patch.object(rights, "crossref_work", return_value=message) as fetch:
            from apps.content.tasks import refresh_crossref_rights

            counts = refresh_crossref_rights([self.open.pk])
        fetch.assert_called_once_with("10.1234/abc")
        self.assertEqual(counts, {"checked": 1, "updated": 1})
        self.open.refresh_from_db()
        self.assertEqual(self.open.rights["source"], "crossref")
        settings_store.update("content.crossref_enabled", False)
        self.addCleanup(settings_store.invalidate)
        with mock.patch.object(rights, "crossref_work") as fetch:
            self.assertFalse(rights.refresh_crossref(self.open))
        fetch.assert_not_called()

    def test_opds_shows_the_licence(self):
        from apps.opds.schema import AcquisitionEntry
        from apps.opds2.services.manifest_builder import ManifestBuilder

        self.open.rights = rights.clean({"license_url": CC_BY, "license_name": "CC BY 4.0"}, "dataverse")
        self.open.save()
        entry = Entry.objects.select_related("catalog", "language").get(pk=self.open.pk)
        xml = AcquisitionEntry.from_model(entry, complete=True).to_xml().decode()
        self.assertIn('rel="license"', xml)
        self.assertIn(CC_BY, xml)
        self.assertIn("CC BY 4.0</dc:rights>", xml)

        publication = ManifestBuilder.build_publication(entry, RequestFactory().get("/"))
        licence = [link for link in publication.links if link.rel == "license"]
        self.assertEqual((licence[0].href, licence[0].title), (CC_BY, "CC BY 4.0"))

    def test_tdmrep_lists_the_reserved_publications_of_public_catalogs(self):
        Acquisition.objects.create(entry=self.open, mime=Acquisition.AcquisitionMIME.PDF)
        self._reserve(self.open, tdm_policy_url="https://example.org/tdm")
        self._reserve(self.hidden)
        rules = json.loads(self.client.get("/.well-known/tdmrep.json").content)
        locations = {rule["location"] for rule in rules}
        self.assertTrue(any(str(self.open.pk) in location for location in locations))
        self.assertTrue(any(location.startswith("/data/v1/") for location in locations))
        self.assertFalse(any(str(self.hidden.pk) in location for location in locations))
        self.assertTrue(all(rule["tdm-reservation"] == 1 for rule in rules))
        self.assertTrue(all(rule["tdm-policy"] == "https://example.org/tdm" for rule in rules))

        self.open.rights = None
        self.open.save()
        self.assertEqual(json.loads(self.client.get("/.well-known/tdmrep.json").content), [])
