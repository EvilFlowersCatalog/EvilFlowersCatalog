"""#123: once `PUT /readium/v1/licenses/{id}` returns a loan, the list says so.

The Status Server is mocked; everything from the PUT to the next list GET runs
through the real view, service, middleware and database. A list GET issued
after the PUT has answered must never report the loan as READY/ACTIVE, and
must never be answered 304 against a copy fetched before the return.
"""

import datetime
import uuid
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.auth import JWTFactory
from apps.core.models import ApiKey, AuthSource, Catalog, Entry, User
from apps.readium.models import License

LIST = "/readium/v1/licenses"
TRANSPORT = "apps.readium.services.status_server_sync.LsdTransport"
RIGHTS = "apps.readium.services.lcp_server_client.LCPServerClient.update_license_rights"


@override_settings(EVILFLOWERS_EVENT_BROKER_EXECUTOR=None)
class LicenseReturnFreshnessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        source = AuthSource.objects.create(name="database", driver=AuthSource.Driver.DATABASE)
        cls.reader = User(username="reader", name="Re", surname="Ader", auth_source=source)
        cls.reader.set_unusable_password()
        cls.reader.save()
        catalog = Catalog.objects.create(creator=cls.reader, title="Open", url_name="open", is_public=True)
        cls.entry = Entry.objects.create(
            creator=cls.reader, catalog=catalog, title="Lent book", config={"readium_enabled": True}
        )

    def setUp(self):
        key = ApiKey.objects.create(user=self.reader, name="test")
        self.auth = {"Authorization": f"Bearer {JWTFactory(self.reader.pk).api_key(str(key.pk))}"}

    def _loan(self, state) -> License:
        return License.objects.create(
            entry=self.entry,
            user=self.reader,
            state=state,
            lcp_license_id=uuid.uuid4(),
            starts_at=timezone.now(),
            expires_at=timezone.now() + datetime.timedelta(days=14),
        )

    def _list_states(self, response) -> dict:
        return {item["id"]: item["state"] for item in response.json()["items"]}

    def _return_and_list(self, initial_state, lsd_status, expected_state):
        loan = self._loan(initial_state)
        url = f"{LIST}?user_id={self.reader.pk}&page=1&limit=50&order_by=-starts_at"

        before = self.client.get(url, headers=self.auth)
        self.assertEqual(self._list_states(before)[str(loan.pk)], initial_state)

        with mock.patch(TRANSPORT) as transport, mock.patch(RIGHTS):
            transport.return_value.get_status.return_value = {"status": lsd_status}
            put = self.client.put(
                f"{LIST}/{loan.pk}", data={"action": "returned"}, content_type="application/json", headers=self.auth
            )
        self.assertEqual(put.status_code, 200, put.content)
        self.assertEqual(put.json()["response"]["state"], expected_state)
        self.assertEqual(put["Cache-Control"], "no-store")

        # The portal's refetch, revalidating the copy it held before the return.
        after = self.client.get(url, headers={**self.auth, "If-None-Match": before["ETag"]})
        self.assertEqual(after.status_code, 200)
        self.assertEqual(self._list_states(after)[str(loan.pk)], expected_state)
        self.assertIn("no-cache", after["Cache-Control"])

    def test_returning_an_active_loan(self):
        self._return_and_list(License.LicenseState.ACTIVE, "returned", License.LicenseState.RETURNED)

    def test_returning_a_ready_loan(self):
        # LSD answers a return of a never-opened loan with `cancelled`.
        self._return_and_list(License.LicenseState.READY, "cancelled", License.LicenseState.CANCELLED)
