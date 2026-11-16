"""IP-033 Q10: the ISBN resolver and availability, and `/api/v1/oai/status`."""

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, User, UserCatalog
from apps.oai.tests.base import PORTAL, OaiTestCase
from apps.readium.models import EncryptedContent, License


class ResolverTests(OaiTestCase):
    def setUp(self):
        super().setUp()
        self.book = self.entry(identifiers={"isbn": "978-80-227-1234-7"})
        self.sync(self.book)

    def test_an_exposed_isbn_redirects_to_the_portal(self):
        for isbn in ("9788022712347", "80-227-1234-5"):
            response = self.client.get(f"/resolve/isbn/{isbn}")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], f"{PORTAL}/library?entry-detail-id={self.book.pk}")

    def test_unknown_invalid_or_hidden_isbns_are_404(self):
        hidden = self.entry(catalog=self.private, identifiers={"isbn": "9780306406157"})
        self.sync(hidden)
        for isbn in ("9780306406157", "123", "9781234567897"):
            self.assertEqual(self.client.get(f"/resolve/isbn/{isbn}").status_code, 404)

    def test_no_portal_url_is_404(self):
        from unittest.mock import patch

        with patch("apps.oai.projection.settings_store.get", return_value=""):
            self.sync(self.book)
        self.assertEqual(self.client.get("/resolve/isbn/9788022712347").status_code, 404)

    @override_settings(EVILFLOWERS_OAI_ENABLED=False)
    def test_disabled_is_404(self):
        self.assertEqual(self.client.get("/resolve/isbn/9788022712347").status_code, 404)


class AvailabilityTests(OaiTestCase):
    def test_open_and_lcp_titles(self):
        open_book = self.entry(identifiers={"isbn": "9788022712347"})
        lent = self.entry(
            title="Požičaná",
            identifiers={"isbn": "80-227-1234-5"},
            config={"readium_enabled": True, "readium_amount": 2},
        )
        EncryptedContent.objects.create(
            acquisition=lent.acquisitions.get(),
            lcp_content_id="lcp-avail",
            encrypted_path="x.lcpdf",
            status=EncryptedContent.EncryptionStatus.REGISTERED,
            encrypted_at=timezone.now(),
        )
        now = timezone.now()
        License.objects.create(
            entry=lent,
            user=self.owner,
            state=License.LicenseState.ACTIVE,
            starts_at=now,
            expires_at=now + timedelta(days=7),
        )
        self.sync(open_book, lent)

        response = self.client.get("/availability", {"isbn": "978-80-227-1234-7"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["isbn"], "9788022712347")
        by_id = {item["id"]: item for item in payload["entries"]}
        self.assertEqual(
            by_id[str(open_book.pk)],
            {
                "id": str(open_book.pk),
                "title": open_book.title,
                "lcp": False,
                "copies": None,
                "available": None,
                "next_free": None,
            },
        )
        self.assertEqual(by_id[str(lent.pk)]["copies"], 2)
        self.assertEqual(by_id[str(lent.pk)]["available"], 1)
        self.assertTrue(by_id[str(lent.pk)]["lcp"])

    def test_hidden_entries_are_not_listed_and_a_bad_isbn_is_400(self):
        self.sync(self.entry(catalog=self.private, identifiers={"isbn": "9788022712347"}))
        self.assertEqual(self.client.get("/availability", {"isbn": "9788022712347"}).json()["entries"], [])
        self.assertEqual(self.client.get("/availability", {"isbn": "nope"}).status_code, 400)
        self.assertEqual(self.client.get("/availability").status_code, 400)


class StatusTests(OaiTestCase):
    def headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def test_status_for_managers_only(self):
        book = self.entry()
        gone = self.entry(title="Zmazaná")
        self.sync(book, gone)
        gone_id = gone.pk
        gone.delete()
        self.sync(gone_id)

        source = AuthSource.objects.first()
        reader = User.objects.create(username="reader", name="R", surname="D", auth_source=source)
        manager = User.objects.create(username="manager", name="M", surname="D", auth_source=source)
        UserCatalog.objects.create(user=manager, catalog=self.private, mode=UserCatalog.Mode.MANAGE)

        self.assertEqual(self.client.get("/api/v1/oai/status").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/oai/status", headers=self.headers(reader)).status_code, 403)
        response = self.client.get("/api/v1/oai/status", headers=self.headers(manager))
        self.assertEqual(response.status_code, 200)
        status = response.json()["response"]
        self.assertEqual(status["base_url"], "https://evilflowers.elvira.stuba.sk/oai")
        self.assertEqual(status["repository_identifier"], "evilflowers.elvira.stuba.sk")
        self.assertEqual((status["live"], status["deleted"]), (1, 1))
        self.assertTrue(status["earliest_datestamp"].endswith("Z"))
        self.assertTrue(status["last_datestamp"].endswith("Z"))
