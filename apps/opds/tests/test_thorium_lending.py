"""
OPDS lending contract as Thorium Reader consumes it (verified against
thorium-reader `develop`):

- a borrow link is followed with GET, and only when typed
  `application/opds-publication+json`; any other type is opened in the web
  browser, so the LCP license must sit in `indirectAcquisition`;
- a `401` must carry the Authentication Document — a `problem+json` body is
  shown as an error and the sign-in dialog never appears;
- `availability.state` drives the button (`unavailable` → "Place hold",
  `reserved` hides it).

Plus the OPDS 1.2 / Atom spec fixes that rode along (category attributes,
OpenSearch `{searchTerms}`, Dublin Core metadata).
"""

import datetime
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from lxml import etree

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Category, Entry, User
from apps.opds.services.borrow_link import LCP_LICENSE_MIME, OPDS_PUBLICATION_MIME, REL_ACQUISITION, REL_BORROW
from apps.readium.models import EncryptedContent, License, Reservation

ATOM = "{http://www.w3.org/2005/Atom}"
OPDS = "{http://opds-spec.org/2010/catalog}"
DC = "{http://purl.org/dc/terms/}"
AUTH_DOC_TYPE = "application/opds-authentication+json"
CREATE_LICENSE = "apps.opds2.views.borrow.LicenseService.create_license"


class LendingTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.owner = cls._user(source, "owner")
        cls.reader = cls._user(source, "reader")
        cls.other = cls._user(source, "other")

        cls.catalog = Catalog.objects.create(creator=cls.owner, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(
            creator=cls.owner,
            catalog=cls.catalog,
            title="Protected book",
            publisher="STU Press",
            identifiers={"isbn": "978-80-227-0000-0"},
            config={"readium_enabled": True, "readium_amount": 1},
        )
        cls.acquisition = Acquisition.objects.create(
            entry=cls.entry, mime=Acquisition.AcquisitionMIME.PDF, relation=Acquisition.AcquisitionType.ACQUISITION
        )
        EncryptedContent.objects.create(
            acquisition=cls.acquisition,
            lcp_content_id="content-1",
            encrypted_path="x.lcpdf",
            status=EncryptedContent.EncryptionStatus.REGISTERED,
        )
        category = Category.objects.create(catalog=cls.catalog, creator=cls.owner, term="004", scheme="mdt")
        cls.entry.categories.add(category)

    @staticmethod
    def _user(source, username):
        user = User(username=username, name=username.title(), surname="Test", auth_source=source)
        user.set_unusable_password()
        user.save()
        return user

    @staticmethod
    def auth(user) -> dict:
        api_key = ApiKey.objects.create(user=user, name=f"opds-{user.username}")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(api_key.pk))}"}

    def loan(self, user, days=14):
        return License.objects.create(
            entry=self.entry,
            user=user,
            encrypted_content=self.acquisition.encrypted_content,
            state=License.LicenseState.READY,
            starts_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=days),
        )

    @property
    def borrow_url(self):
        return f"/opds/v2/open/publications/{self.entry.pk}/borrow"

    def publication(self, user=None):
        response = self.client.get(
            f"/opds/v2/open/publications/{self.entry.pk}", headers=self.auth(user) if user else {}
        )
        self.assertEqual(response["Content-Type"], OPDS_PUBLICATION_MIME)
        return response.json()

    @staticmethod
    def link(publication, rel):
        return next(link for link in publication["links"] if link.get("rel") == rel)


class Opds2BorrowLinkTests(LendingTestCase):
    def test_borrow_link_is_an_opds_publication_wrapping_the_lcp_license(self):
        borrow = self.link(self.publication(), REL_BORROW)

        self.assertEqual(borrow["type"], OPDS_PUBLICATION_MIME)
        indirect = borrow["properties"]["indirectAcquisition"][0]
        self.assertEqual(indirect["type"], LCP_LICENSE_MIME)
        self.assertEqual(indirect["children"][0]["type"], "application/pdf+lcp")
        self.assertEqual(borrow["properties"]["availability"]["state"], "available")
        self.assertEqual(borrow["properties"]["copies"], {"total": 1, "available": 1})

    def test_a_title_out_on_loan_is_unavailable_until_the_loan_ends(self):
        loan = self.loan(self.other)
        borrow = self.link(self.publication(self.reader), REL_BORROW)

        self.assertEqual(borrow["properties"]["availability"]["state"], "unavailable")
        self.assertIsNotNone(borrow["properties"]["availability"]["until"])
        self.assertEqual(borrow["properties"]["copies"]["available"], 0)
        self.assertTrue(loan.expires_at)

    def test_a_borrower_sees_the_license_link_with_the_loan_period(self):
        self.loan(self.reader)
        publication = self.publication(self.reader)

        acquisition = self.link(publication, REL_ACQUISITION)
        self.assertEqual(acquisition["type"], LCP_LICENSE_MIME)
        self.assertIn("?token=", acquisition["href"])
        self.assertIn("until", acquisition["properties"]["availability"])
        self.assertFalse(any(link.get("rel") == REL_BORROW for link in publication["links"]))

    def test_metadata_carries_an_identifier_and_type(self):
        metadata = self.publication()["metadata"]
        self.assertEqual(metadata["identifier"], "urn:isbn:9788022700000")
        self.assertEqual(metadata["@type"], "http://schema.org/Book")


class Opds2BorrowGetTests(LendingTestCase):
    def _issue(self, entry, user, **kwargs):
        return self.loan(user)

    def test_get_borrows_and_answers_with_the_publication(self):
        with mock.patch(CREATE_LICENSE, side_effect=self._issue) as create:
            response = self.client.get(self.borrow_url, headers=self.auth(self.reader))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], OPDS_PUBLICATION_MIME)
        self.assertEqual(create.call_count, 1)
        self.link(response.json(), REL_ACQUISITION)

    def test_get_is_idempotent_for_a_reader_who_already_holds_the_loan(self):
        self.loan(self.reader)
        with mock.patch(CREATE_LICENSE) as create:
            response = self.client.get(self.borrow_url, headers=self.auth(self.reader))

        self.assertEqual(response.status_code, 200)
        create.assert_not_called()
        self.link(response.json(), REL_ACQUISITION)

    def test_get_on_a_title_out_on_loan_places_a_hold(self):
        # A hold needs no LCP passphrase: `create_license` checks the
        # passphrase before availability, so it must not be the hold path.
        self.assertFalse(self.reader.lcp_passphrase_hash)
        self.loan(self.other)
        response = self.client.get(self.borrow_url, headers=self.auth(self.reader))

        self.assertEqual(response.status_code, 200)
        hold = Reservation.objects.get(entry=self.entry, user=self.reader)
        self.assertEqual(hold.status, Reservation.Status.QUEUED)
        borrow = self.link(response.json(), REL_BORROW)
        self.assertEqual(borrow["properties"]["availability"]["state"], "reserved")
        self.assertEqual(borrow["properties"]["holds"]["position"], hold.position)

        # Following the link again reports the hold, it does not queue twice.
        again = self.client.get(self.borrow_url, headers=self.auth(self.reader))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(Reservation.objects.filter(entry=self.entry, user=self.reader).count(), 1)


class AuthenticationDocumentOn401Tests(LendingTestCase):
    def assert_auth_document(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["Content-Type"], AUTH_DOC_TYPE)
        self.assertIn("authentication", response.json())
        self.assertIn("http://opds-spec.org/auth/document", response["Link"])

    def test_anonymous_borrow_gets_the_authentication_document(self):
        self.assert_auth_document(self.client.get(self.borrow_url))

    def test_bad_credentials_get_the_authentication_document(self):
        self.assert_auth_document(self.client.get("/opds/v2/open/shelf", headers={"Authorization": "Bearer nope"}))

    def test_opds12_shelf_gets_the_authentication_document(self):
        self.assert_auth_document(self.client.get("/opds/v1.2/open/shelf"))


class Opds12SpecTests(LendingTestCase):
    def entry_xml(self, user=None):
        response = self.client.get(f"/opds/v1.2/open/entries/{self.entry.pk}", headers=self.auth(user) if user else {})
        self.assertEqual(response.status_code, 200)
        return etree.fromstring(response.content)

    def test_category_is_an_empty_element_with_attributes(self):
        category = self.entry_xml().find(f"{ATOM}category")
        self.assertEqual(category.get("term"), "004")
        self.assertEqual(category.get("scheme"), "mdt")
        self.assertEqual(len(category), 0)

    def test_dublin_core_metadata(self):
        xml = self.entry_xml()
        self.assertEqual(xml.findtext(f"{DC}identifier"), "urn:isbn:9788022700000")
        self.assertEqual(xml.findtext(f"{DC}publisher"), "STU Press")

    def test_borrow_link_carries_indirect_acquisition_and_lending_state(self):
        borrow = next(link for link in self.entry_xml().findall(f"{ATOM}link") if link.get("rel") == REL_BORROW)
        self.assertEqual(borrow.get("type"), OPDS_PUBLICATION_MIME)
        indirect = borrow.find(f"{OPDS}indirectAcquisition")
        self.assertEqual(indirect.get("type"), LCP_LICENSE_MIME)
        self.assertEqual(indirect.find(f"{OPDS}indirectAcquisition").get("type"), "application/pdf+lcp")
        self.assertEqual(borrow.find(f"{OPDS}availability").get("status"), "available")
        self.assertEqual(borrow.find(f"{OPDS}copies").get("total"), "1")

    def test_opensearch_template_has_search_terms(self):
        response = self.client.get("/opds/v1.2/open/search.xml")
        self.assertIn(b"query={searchTerms}", response.content)

    def test_another_readers_loan_changes_the_feed_etag(self):
        first = self.client.get("/opds/v1.2/open/complete", headers=self.auth(self.reader))
        self.loan(self.other)
        second = self.client.get("/opds/v1.2/open/complete", headers=self.auth(self.reader))
        self.assertNotEqual(first["ETag"], second["ETag"])
