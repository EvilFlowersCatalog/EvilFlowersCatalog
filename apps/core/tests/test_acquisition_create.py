"""`POST /api/v1/acquisitions` (#7) and the upload checks it shares with the entry endpoint (#112)."""

import json
import shutil
import tempfile
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.core.auth import JWTFactory
from apps.core.models import Acquisition, ApiKey, AuthSource, Catalog, Currency, Entry, Price, User
from apps.core.models.user_catalog import UserCatalog

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class AcquisitionCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.manager = cls._user(source, "manager")
        cls.reader = cls._user(source, "reader")
        cls.catalog = Catalog.objects.create(creator=cls.manager, title="Open", url_name="open", is_public=True)
        UserCatalog.objects.create(user=cls.manager, catalog=cls.catalog, mode=UserCatalog.Mode.MANAGE)
        cls.entry = Entry.objects.create(creator=cls.manager, catalog=cls.catalog, title="A Book")
        Currency.objects.get_or_create(code="EUR", defaults={"name": "Euro"})

    @staticmethod
    def _user(source, username) -> User:
        user = User(username=username, name=username.title(), surname="T", auth_source=source)
        user.set_unusable_password()
        user.save()
        return user

    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(EVILFLOWERS_STORAGE_FILESYSTEM_DATADIR=root)
        override.enable()
        self.addCleanup(override.disable)
        patcher = mock.patch("apps.extraction.services.queue")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, user, content=PDF, **fields):
        key = ApiKey.objects.create(user=user, name="test")
        data = {"entry_id": str(self.entry.pk), **fields}
        if content is not None:
            data["content"] = SimpleUploadedFile("book.pdf", content, content_type="application/pdf")
        return self.client.post(
            "/api/v1/acquisitions",
            data=data,
            headers={"Authorization": f"Bearer {JWTFactory(user.pk).api_key(str(key.pk))}"},
        )

    def test_a_manager_creates_an_acquisition_with_prices(self):
        metadata = {"relation": "acquisition", "prices": [{"currency_code": "EUR", "value": "9.90"}]}
        response = self._post(self.manager, metadata=json.dumps(metadata))
        self.assertEqual(response.status_code, 201, response.content[:300])
        acquisition = Acquisition.objects.get(entry=self.entry)
        self.assertEqual(acquisition.mime, "application/pdf")
        self.assertEqual(Price.objects.filter(acquisition=acquisition).count(), 1)

    def test_a_reader_may_not(self):
        self.assertEqual(self._post(self.reader).status_code, 403)
        self.assertFalse(Acquisition.objects.exists())

    def test_an_empty_or_mislabelled_file_is_refused(self):
        self.assertEqual(self._post(self.manager, content=b"").status_code, 400)
        self.assertEqual(self._post(self.manager, content=b"<html></html>").status_code, 415)
        self.assertFalse(Acquisition.objects.exists())

    def test_invalid_metadata_is_a_validation_error_not_a_crash(self):
        # A list index in the error path used to break the error renderer (500).
        metadata = {"prices": [{"currency_code": "XXX", "value": "x"}]}
        self.assertEqual(self._post(self.manager, metadata=json.dumps(metadata)).status_code, 422)

    def test_entry_and_content_are_required(self):
        self.assertEqual(self._post(self.manager, entry_id="nope").status_code, 400)
        self.assertEqual(self._post(self.manager, content=None).status_code, 400)
