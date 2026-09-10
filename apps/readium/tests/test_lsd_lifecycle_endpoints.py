"""
Regression tests: each LSD transition must use its own endpoint.

The Status Server exposes four distinct routes, and they are not
interchangeable (readium-lcp-server `lsdserver/server/server.go`):

    PUT   /licenses/{id}/return    -> LendingReturn
    PUT   /licenses/{id}/renew     -> LendingRenewal
    PATCH /licenses/{id}/status    -> LendingCancellation  (cancel|revoke ONLY)

`return_license` and `renew_license` used to go through `PATCH /status`,
which upstream rejects with:

    400 {"detail": "The new status must be either cancelled or revoked"}

surfacing to the portal as "Failed to return license".
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock
from uuid import uuid4

from django.test import SimpleTestCase

from apps.readium.models import License
from apps.readium.services.status_server_sync import StatusServerSyncService


def _license(state=License.LicenseState.ACTIVE):
    lic = MagicMock(spec=License)
    lic.pk = uuid4()
    lic.lcp_license_id = uuid4()
    lic.state = state
    lic.device_count = 0
    lic.expires_at = None
    return lic


class LsdLifecycleEndpointTests(SimpleTestCase):
    def setUp(self):
        self.transport = MagicMock()
        # Reconcile GET returns the canonical doc; keep it inert.
        self.transport.get_status.return_value = {}
        self.service = StatusServerSyncService(transport=self.transport)

    def test_return_uses_return_endpoint_not_status_patch(self):
        self.service.return_license(_license(License.LicenseState.ACTIVE))

        self.transport.put_return.assert_called_once()
        self.transport.patch_status.assert_not_called()

    def test_renew_uses_renew_endpoint_not_status_patch(self):
        new_end = datetime.now(dt_timezone.utc) + timedelta(days=7)

        self.service.renew_license(_license(), new_end_date=new_end)

        self.transport.put_renew.assert_called_once()
        self.assertEqual(self.transport.put_renew.call_args.kwargs["end"], new_end)
        self.transport.patch_status.assert_not_called()

    def test_cancel_still_uses_status_patch(self):
        self.service.cancel_license(_license(License.LicenseState.READY))

        self.transport.patch_status.assert_called_once()
        self.assertEqual(self.transport.patch_status.call_args.args[1]["status"], "cancelled")
        self.transport.put_return.assert_not_called()

    def test_revoke_still_uses_status_patch(self):
        self.service.revoke_license(_license())

        self.transport.patch_status.assert_called_once()
        self.assertEqual(self.transport.patch_status.call_args.args[1]["status"], "revoked")

    def test_returning_a_ready_loan_expects_cancelled(self):
        """Upstream maps READY -> cancelled on return, ACTIVE -> returned.

        The optimistic fallback must not claim `returned` for a loan the
        Status Server will mark `cancelled`.
        """
        from apps.readium.services.lsd_transport import LsdTransportError

        # Force the reconcile GET to fail so the fallback path is exercised.
        self.transport.get_status.side_effect = LsdTransportError("boom")
        lic = _license(License.LicenseState.READY)

        self.service.return_license(lic)

        self.assertEqual(lic.state, License.LicenseState.CANCELLED)

    def test_returning_an_active_loan_expects_returned(self):
        from apps.readium.services.lsd_transport import LsdTransportError

        self.transport.get_status.side_effect = LsdTransportError("boom")
        lic = _license(License.LicenseState.ACTIVE)

        self.service.return_license(lic)

        self.assertEqual(lic.state, License.LicenseState.RETURNED)
