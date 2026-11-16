"""IP-034 Q6: a withdrawn entry is hidden from every reader-facing list and cannot be borrowed or reserved, but
lookups by id (detail, running loans) still find it."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, Language, User, UserCatalog
from apps.readium.services.exceptions import NotLendableError
from apps.readium.services.license_service import LicenseService
from apps.readium.services.reservation_service import ReservationService


class WithdrawnEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        Language.objects.get_or_create(alpha2="en", defaults={"name": "English", "alpha3": "eng"})
        cls.owner = cls._user(source, "owner", is_superuser=True)
        cls.manager = cls._user(source, "manager")
        cls.reader = cls._user(source, "reader")
        cls.catalog = Catalog.objects.create(creator=cls.owner, title="Open", url_name="open", is_public=True)
        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        cls.listed = Entry.objects.create(creator=cls.owner, catalog=cls.catalog, title="Listed book")
        cls.withdrawn = Entry.objects.create(
            creator=cls.owner,
            catalog=cls.catalog,
            title="Withdrawn book",
            withdrawn_at=timezone.now(),
            config={"readium_enabled": True, "readium_amount": 1},
        )

    @classmethod
    def _user(cls, source, username, **kwargs) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=source, **kwargs)
        user.set_password("password-1234")
        user.save()
        return user

    def _headers(self, user) -> dict:
        key = ApiKey.objects.create(user=user, name="test")
        return {"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"}

    def _ids(self, response) -> set:
        self.assertEqual(response.status_code, 200, response.content[:300])
        return {item["id"] for item in response.json()["items"]}

    def test_manager_listed_queryset(self):
        self.assertEqual(list(Entry.objects.listed().filter(catalog=self.catalog)), [self.listed])

    def test_rest_list_hides_withdrawn_entries(self):
        ids = self._ids(self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}"))
        self.assertEqual(ids, {str(self.listed.pk)})

    def test_rest_search_hides_withdrawn_entries(self):
        ids = self._ids(self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}&query=book"))
        self.assertNotIn(str(self.withdrawn.pk), ids)

    def test_manager_lists_withdrawn_entries_on_request(self):
        response = self.client.get(
            f"/api/v1/entries?catalog_id={self.catalog.pk}&withdrawn=true", headers=self._headers(self.manager)
        )
        self.assertEqual(self._ids(response), {str(self.withdrawn.pk)})

    def test_reader_and_anonymous_get_nothing_for_withdrawn_true(self):
        reader = self.client.get(
            f"/api/v1/entries?catalog_id={self.catalog.pk}&withdrawn=true", headers=self._headers(self.reader)
        )
        self.assertEqual(self._ids(reader), set())
        anonymous = self.client.get(f"/api/v1/entries?catalog_id={self.catalog.pk}&withdrawn=true")
        self.assertEqual(self._ids(anonymous), set())

    def test_detail_by_id_still_finds_a_withdrawn_entry(self):
        response = self.client.get(
            f"/api/v1/catalogs/{self.catalog.pk}/entries/{self.withdrawn.pk}", headers=self._headers(self.owner)
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertIsNotNone(response.json()["response"]["withdrawn_at"])

    def test_opds_complete_feed_hides_withdrawn_entries(self):
        response = self.client.get(reverse("opds:complete", kwargs={"catalog_name": "open"}))
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertIn(b"Listed book", response.content)
        self.assertNotIn(b"Withdrawn book", response.content)

    def test_opds_new_feed_hides_withdrawn_entries(self):
        response = self.client.get(reverse("opds:new", kwargs={"catalog_name": "open"}))
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertNotIn(b"Withdrawn book", response.content)

    def test_opds2_new_feed_hides_withdrawn_entries(self):
        response = self.client.get(reverse("opds2:new", kwargs={"catalog_name": "open"}))
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertIn(b"Listed book", response.content)
        self.assertNotIn(b"Withdrawn book", response.content)

    def test_borrowing_a_withdrawn_entry_is_refused(self):
        with self.assertRaises(NotLendableError):
            LicenseService.create_license(self.withdrawn, self.reader, user_passphrase="passphrase-1234")

    def test_reserving_a_withdrawn_entry_is_refused(self):
        with self.assertRaises(NotLendableError):
            ReservationService.enqueue(self.withdrawn, self.reader)

    def test_entry_update_withdraws_and_restores(self):
        url = f"/api/v1/catalogs/{self.catalog.pk}/entries/{self.listed.pk}"
        payload = {"title": "Listed book", "language_code": "en", "withdrawn": True}
        response = self.client.put(
            url, data=payload, content_type="application/json", headers=self._headers(self.owner)
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.listed.refresh_from_db()
        self.assertIsNotNone(self.listed.withdrawn_at)
        payload["withdrawn"] = False
        response = self.client.put(
            url, data=payload, content_type="application/json", headers=self._headers(self.owner)
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.listed.refresh_from_db()
        self.assertIsNone(self.listed.withdrawn_at)
