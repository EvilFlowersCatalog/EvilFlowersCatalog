"""
Regression tests for GitHub #109 and #110 (2026-11-09 portal E2E pass).

#109 — returning a never-opened (`ready`) loan ends `cancelled` on the Status
Server, and no e-mail went out. `cancelled` now sends `license_returned`. The
admin "activate" action flipped only the local row; it now reconciles with
LSD and answers 409 while LSD still holds `ready`.

#110 — a 7-day embargo counted from `created_at` outlived a 1-week loan whose
`expires_at` is day-rounded, so it could never be renewed. The embargo is now
capped at half the loan, counted from `starts_at`, guards only the first
renewal, and is published per license as `renewable_from`.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from apps.core.errors import ProblemDetailException
from apps.readium.enums import LicenseAction
from apps.readium.models import License
from apps.readium.services.renew_policy import RenewDenial, evaluate_renew, renewable_from

# The #110 reproduction: borrowed 2026-11-09 10:37 UTC, starts_at day-rounded.
STARTS_AT = datetime(2026, 11, 9, tzinfo=dt_timezone.utc)
CREATED_AT = datetime(2026, 11, 9, 10, 37, tzinfo=dt_timezone.utc)


def _loan(days: int, renewal_count: int = 0, state=License.LicenseState.READY):
    return SimpleNamespace(
        pk=uuid4(),
        entry=object(),
        state=state,
        starts_at=STARTS_AT,
        created_at=CREATED_AT,
        expires_at=STARTS_AT + timedelta(days=days),
        renewal_count=renewal_count,
    )


@override_settings(
    EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS=7,
    EVILFLOWERS_READIUM_MAX_RENEW_DAYS=14,
    EVILFLOWERS_READIUM_MAX_RENEWALS=None,
)
class RenewableFromTests(SimpleTestCase):
    def test_one_week_loan_embargo_is_half_the_loan(self):
        loan = _loan(7)
        self.assertEqual(renewable_from(loan.starts_at, loan.expires_at, 0), STARTS_AT + timedelta(days=3, hours=12))

    def test_two_week_loan_keeps_the_configured_embargo(self):
        loan = _loan(14)
        self.assertEqual(renewable_from(loan.starts_at, loan.expires_at, 0), STARTS_AT + timedelta(days=7))

    def test_embargo_lifts_before_every_loan_ends(self):
        for days in (1, 7, 14, 30):
            loan = _loan(days)
            self.assertLess(renewable_from(loan.starts_at, loan.expires_at, 0), loan.expires_at, days)

    def test_embargo_does_not_re_arm_after_a_renewal(self):
        # 1-week loan renewed on day 4 to day 18: half of it would be 9 days.
        loan = _loan(18, renewal_count=1)
        self.assertIsNone(renewable_from(loan.starts_at, loan.expires_at, loan.renewal_count))

    @override_settings(EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS=0)
    def test_disabled_embargo(self):
        loan = _loan(7)
        self.assertIsNone(renewable_from(loan.starts_at, loan.expires_at, 0))


@override_settings(
    EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS=7,
    EVILFLOWERS_READIUM_MAX_RENEW_DAYS=14,
    EVILFLOWERS_READIUM_MAX_RENEWALS=None,
)
@patch("apps.readium.services.renew_policy.Reservation.objects.filter")
class EvaluateRenewEmbargoTests(SimpleTestCase):
    def _evaluate(self, loan, now):
        with patch("apps.readium.services.renew_policy.timezone.now", return_value=now):
            return evaluate_renew(loan)

    def test_one_week_loan_denied_on_day_two_with_stable_code(self, reservations):
        reservations.return_value.exists.return_value = False
        decision = self._evaluate(_loan(7), STARTS_AT + timedelta(days=2))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, RenewDenial.EMBARGO)
        self.assertEqual(
            decision.problem_data,
            {"code": "embargo", "renewable_from": (STARTS_AT + timedelta(days=3, hours=12)).isoformat()},
        )

    def test_one_week_loan_renewable_on_day_four(self, reservations):
        reservations.return_value.exists.return_value = False
        decision = self._evaluate(_loan(7), STARTS_AT + timedelta(days=4))

        self.assertTrue(decision.allowed, decision.reason)

    def test_second_renewal_is_not_blocked_by_the_embargo(self, reservations):
        reservations.return_value.exists.return_value = False
        decision = self._evaluate(_loan(18, renewal_count=1), STARTS_AT + timedelta(days=6))

        self.assertTrue(decision.allowed, decision.reason)

    def test_queue_denial_carries_a_code(self, reservations):
        reservations.return_value.exists.return_value = True
        decision = self._evaluate(_loan(7), STARTS_AT + timedelta(days=4))

        self.assertEqual(decision.problem_data, {"code": RenewDenial.QUEUE})


class LicenseSerializerRenewableFromTests(SimpleTestCase):
    @override_settings(EVILFLOWERS_READIUM_RENEW_EMBARGO_DAYS=7, EVILFLOWERS_READIUM_MAX_RENEWALS=None)
    def test_license_publishes_renewable_from(self):
        from apps.readium.serializers import LicenseSerializer

        loan = _loan(7)
        row = SimpleNamespace(
            id=loan.pk,
            entry_id=uuid4(),
            user_id=uuid4(),
            state=loan.state,
            starts_at=loan.starts_at,
            expires_at=loan.expires_at,
            created_at=loan.created_at,
            updated_at=loan.created_at,
            renewal_count=0,
            download_url="",
        )
        with patch("apps.readium.serializers._mint_lcpl_download_url", return_value=""):
            data = LicenseSerializer.Base.model_validate(row).model_dump()

        self.assertEqual(data["renewable_from"], STARTS_AT + timedelta(days=3, hours=12))


@override_settings(EVILFLOWERS_NOTIFICATIONS_ENABLED=True)
class CancelledLoanNotificationTests(SimpleTestCase):
    def _license(self, state, original_state):
        return MagicMock(pk=uuid4(), state=state, _original_state=original_state)

    @patch("apps.readium.signals._enqueue")
    def test_cancelled_return_of_ready_loan_sends_license_returned(self, enqueue):
        from apps.readium.signals import _notify_license_transitions

        lic = self._license(License.LicenseState.CANCELLED, License.LicenseState.READY)
        _notify_license_transitions(License, lic, created=False)

        self.assertEqual(enqueue.call_args.args[0], "license_returned")

    @patch("apps.readium.signals._enqueue")
    def test_activation_stays_silent(self, enqueue):
        from apps.readium.signals import _notify_license_transitions

        lic = self._license(License.LicenseState.ACTIVE, License.LicenseState.READY)
        _notify_license_transitions(License, lic, created=False)

        enqueue.assert_not_called()


class AdminActivateTests(SimpleTestCase):
    def _dispatch(self, license):
        from apps.readium.views.licenses import LicenseDetail

        LicenseDetail()._dispatch_action(license, LicenseAction.ACTIVATE, {})

    def _license(self, state=License.LicenseState.READY):
        return SimpleNamespace(pk=uuid4(), state=state, lcp_license_id="lcp-1", device_count=0)

    @patch("apps.readium.views.licenses.StatusServerSyncService")
    def test_still_ready_upstream_is_refused_and_not_flipped_locally(self, sync):
        license = self._license()

        with self.assertRaises(ProblemDetailException) as ctx:
            self._dispatch(license)

        sync.return_value.reconcile.assert_called_once_with(license)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(license.state, License.LicenseState.READY)
        self.assertEqual(license.device_count, 0)

    @patch("apps.readium.views.licenses.StatusServerSyncService")
    def test_active_upstream_is_taken_from_lsd(self, sync):
        license = self._license()
        sync.return_value.reconcile.side_effect = lambda lic: setattr(lic, "state", License.LicenseState.ACTIVE)

        self._dispatch(license)

        self.assertEqual(license.state, License.LicenseState.ACTIVE)
