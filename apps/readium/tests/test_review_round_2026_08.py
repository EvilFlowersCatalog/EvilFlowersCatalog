"""
Regression tests for the August 2026 library review round + GitHub #73.

- `GET /readium/v1/reservations` is the caller's own queue by default; the
  wider manager/superuser view is opt-in (`scope=managed`). The old default
  leaked other users' reservations into the portal's "My reservations" page
  for anyone with a manage grant (rendered as a "double reservation").
- Per-row reservation access: read for owner / superuser / catalog manager,
  cancel for owner / superuser, claim for the owner only.
- The `license_created` e-mail button carries a long-lived capability token
  the gateway accepts. It used to carry a scoped JWT in `?access_token=`,
  which the capability-only gateway rejects — every e-mail link 401'd.
- `EVILFLOWERS_BASE_URL` falls back to the public readium base URL and a
  startup warning fires when notification links would point at loopback.
- Borrow readiness failures surface a reader-facing sentence with
  `reason_code=not_lendable` instead of the raw technical cause.
- `ready` loans are live loans: `is_active_loan` + `GET /licenses?active=true`.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.core.checks import Warning
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from apps.core.models import User
from apps.core.services.capability_tokens import CapabilityTokenService
from apps.readium.capability_scopes import LCPL_DOWNLOAD, LCPL_EMAIL_DOWNLOAD
from apps.readium.filters import LicenseFilter, ReservationFilter
from apps.readium.models import License, Reservation
from apps.readium.serializers import LicenseSerializer
from apps.readium.services.exceptions import BorrowError, NotLendableError

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _user(*, superuser: bool = False) -> User:
    # Unsaved instance: `.pk` is set, `.is_authenticated` is True, no DB round-trip.
    return User(pk=uuid4(), username=f"u-{uuid4().hex[:6]}", is_superuser=superuser)


def _request(user: User, query: dict | None = None):
    request = RequestFactory().get("/readium/v1/reservations", data=query or {})
    request.user = user
    return request


class ReservationListScopeTests(SimpleTestCase):
    """Issue #73 — the collection is scoped to the caller unless asked otherwise."""

    def _sql(self, user: User, query: dict | None = None) -> str:
        request = _request(user, query)
        filterset = ReservationFilter(request.GET, queryset=Reservation.objects.all(), request=request)
        sql = str(filterset.qs.query)
        # Only the predicate matters; the SELECT list always names user_id.
        return sql.split(" WHERE ", 1)[1] if " WHERE " in sql else ""

    def test_default_scope_is_own_rows_for_regular_user(self):
        sql = self._sql(_user())
        self.assertIn('"reservations"."user_id"', sql)
        self.assertNotIn("user_catalogs", sql, "managed-catalog rows must not leak into the default listing")

    def test_default_scope_is_own_rows_even_for_superuser(self):
        sql = self._sql(_user(superuser=True))
        self.assertIn('"reservations"."user_id"', sql)

    def test_managed_scope_adds_managed_catalog_rows(self):
        sql = self._sql(_user(), {"scope": "managed"})
        self.assertIn("user_catalogs", sql)
        self.assertIn('"reservations"."user_id"', sql)

    def test_managed_scope_for_superuser_is_unrestricted(self):
        sql = self._sql(_user(superuser=True), {"scope": "managed"})
        self.assertNotIn('"reservations"."user_id"', sql)
        self.assertNotIn("user_catalogs", sql)

    def test_anonymous_gets_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        request = _request(AnonymousUser())
        filterset = ReservationFilter(request.GET, queryset=Reservation.objects.all(), request=request)
        self.assertFalse(filterset.qs.exists())


class ReservationDetailPermissionTests(SimpleTestCase):
    """Per-row access on `GET/PATCH /readium/v1/reservations/{id}`."""

    def setUp(self):
        from apps.readium.views import reservations as views

        self.views = views
        self.owner = _user()
        self.reservation = MagicMock()
        self.reservation.user_id = self.owner.pk
        self.reservation.entry.catalog_id = uuid4()

        reservation_model = patch.object(views, "Reservation")
        self.Reservation = reservation_model.start()
        self.addCleanup(reservation_model.stop)
        self.Reservation.objects.select_related.return_value.get.return_value = self.reservation

        manager_check = patch.object(views.ReservationDetail, "_is_catalog_manager", return_value=False)
        self.is_manager = manager_check.start()
        self.addCleanup(manager_check.stop)

    def _get(self, user, **kwargs):
        return self.views.ReservationDetail._get_reservation(_request(user), uuid4(), **kwargs)

    def test_owner_can_read(self):
        self.assertIs(self._get(self.owner), self.reservation)

    def test_superuser_can_read(self):
        self.assertIs(self._get(_user(superuser=True)), self.reservation)

    def test_catalog_manager_can_read(self):
        self.is_manager.return_value = True
        self.assertIs(self._get(_user()), self.reservation)

    def test_stranger_cannot_read(self):
        from apps.core.errors import ProblemDetailException

        with self.assertRaises(ProblemDetailException) as ctx:
            self._get(_user())
        self.assertEqual(ctx.exception.status, 403)

    def test_claim_is_owner_only(self):
        from apps.core.errors import ProblemDetailException

        self.is_manager.return_value = True
        with self.assertRaises(ProblemDetailException):
            self._get(_user(superuser=True), owner_only=True)
        self.assertIs(self._get(self.owner, owner_only=True), self.reservation)

    def test_cancel_denied_to_plain_catalog_manager(self):
        # Guard exists in source: cancel path re-checks owner-or-superuser.
        import inspect

        body = inspect.getsource(self.views.ReservationDetail.patch)
        self.assertIn("if not (reservation.user_id == request.user.pk or request.user.is_superuser)", body)


@override_settings(
    CACHES=LOCMEM_CACHE,
    EVILFLOWERS_BASE_URL="https://library.example.org",
    EVILFLOWERS_NOTIFICATION_SCOPED_TOKEN_TTL_HOURS=72,
)
class EmailDownloadLinkTests(SimpleTestCase):
    def test_email_link_is_a_gateway_capability_url(self):
        from apps.readium.notifications import lcpl_email_download_url

        license_id, user_id = uuid4(), uuid4()
        url = lcpl_email_download_url(license_id, user_id)

        self.assertTrue(url.startswith(f"https://library.example.org/readium/v1/licenses/{license_id}.lcpl?token="))
        self.assertNotIn("access_token", url)

        token = url.rsplit("token=", 1)[1]
        payload = CapabilityTokenService.peek(token, expected_scope=LCPL_EMAIL_DOWNLOAD)
        self.assertIsNotNone(payload, "gateway must be able to resolve the e-mail token")
        self.assertEqual(payload["resource_id"], str(license_id))
        self.assertEqual(payload["sub"], str(user_id))
        self.assertFalse(payload["single_use"], "Thorium fetches the URL twice; the button must survive re-clicks")
        # A second peek still works (multi-use), and the short-lived UI scope does not resolve it.
        self.assertIsNotNone(CapabilityTokenService.peek(token, expected_scope=LCPL_EMAIL_DOWNLOAD))
        self.assertIsNone(CapabilityTokenService.consume(token, expected_scope=LCPL_DOWNLOAD))

    def test_gateway_view_accepts_email_scope(self):
        import inspect

        from apps.readium.views.download import LicenseDownloadView

        src = inspect.getsource(LicenseDownloadView.get)
        self.assertIn("LCPL_EMAIL_DOWNLOAD", src)

    def test_license_created_signal_no_longer_uses_scoped_jwt(self):
        from pathlib import Path

        src = Path("apps/notifications/signals.py").read_text()
        self.assertNotIn('scope="license:read"', src)
        self.assertIn("lcpl_email_download_url", src)


class BaseUrlCheckTests(SimpleTestCase):
    def _run(self):
        from apps.readium.checks import check_notification_base_url_is_public

        return check_notification_base_url_is_public(None)

    @override_settings(EVILFLOWERS_NOTIFICATIONS_ENABLED=True, EVILFLOWERS_BASE_URL="http://127.0.0.1:8000")
    def test_loopback_base_url_warns_when_notifications_enabled(self):
        result = self._run()
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], Warning)
        self.assertEqual(result[0].id, "readium.W001")

    @override_settings(EVILFLOWERS_NOTIFICATIONS_ENABLED=True, EVILFLOWERS_BASE_URL="https://elvira.stuba.sk")
    def test_public_base_url_is_silent(self):
        self.assertEqual(self._run(), [])

    @override_settings(EVILFLOWERS_NOTIFICATIONS_ENABLED=False, EVILFLOWERS_BASE_URL="http://localhost:8000")
    def test_disabled_notifications_are_silent(self):
        self.assertEqual(self._run(), [])

    def test_base_url_falls_back_to_readium_base_url(self):
        from pathlib import Path

        src = Path("evil_flowers_catalog/settings/base.py").read_text()
        self.assertIn('EVILFLOWERS_BASE_URL = os.getenv("EVILFLOWERS_BASE_URL", EVILFLOWERS_READIUM_BASE_URL)', src)


class NotLendableErrorTests(SimpleTestCase):
    def test_reader_facing_message_and_technical_detail(self):
        exc = NotLendableError("Entry has no EPUB or PDF acquisition suitable for LCP protection")
        self.assertIsInstance(exc, BorrowError)
        self.assertEqual(exc.reason_code, "not_lendable")
        self.assertNotIn("EPUB", str(exc))
        self.assertIn("contact the library", str(exc))
        self.assertIn("EPUB or PDF", exc.technical_detail)

    def test_view_handles_it_before_the_generic_borrow_error(self):
        import inspect

        from apps.readium.views.licenses import LicenseManagement

        src = inspect.getsource(LicenseManagement.post)
        self.assertLess(src.index("except NotLendableError"), src.index("except BorrowError"))
        self.assertIn('"contact_email"', src)

    def test_create_license_raises_it_for_readiness_problems(self):
        import inspect

        from apps.readium.services.license_service import LicenseService

        src = inspect.getsource(LicenseService.create_license)
        self.assertEqual(src.count("raise NotLendableError("), 3)
        self.assertIn("readium_print_limit", src)
        self.assertIn("EVILFLOWERS_READIUM_PRINT_LIMIT_PAGES", src)


class EnqueueRaceGuardTests(SimpleTestCase):
    def test_integrity_error_becomes_already_reserved(self):
        import inspect

        from apps.readium.services.reservation_service import ReservationService

        src = inspect.getsource(ReservationService.enqueue.__wrapped__)
        self.assertIn("except IntegrityError", src)
        self.assertIn("raise AlreadyReservedError", src)


@patch("apps.readium.serializers.CapabilityTokenService.mint", return_value="T")
class ActiveLoanTests(SimpleTestCase):
    def _license(self, state, expires_in_days):
        now = timezone.now()
        return SimpleNamespace(
            id=uuid4(),
            entry_id=uuid4(),
            user_id=uuid4(),
            state=state,
            starts_at=now,
            expires_at=now + timedelta(days=expires_in_days),
            created_at=now,
            updated_at=now,
            renewal_count=0,
        )

    def test_ready_is_a_live_loan(self, _mint):
        data = LicenseSerializer.Base.model_validate(self._license(License.LicenseState.READY, 7))
        self.assertTrue(data.is_active_loan)
        self.assertIn("is_active_loan", data.model_dump())

    def test_active_is_a_live_loan(self, _mint):
        data = LicenseSerializer.Base.model_validate(self._license(License.LicenseState.ACTIVE, 7))
        self.assertTrue(data.is_active_loan)

    def test_lapsed_or_terminal_is_not(self, _mint):
        lapsed = LicenseSerializer.Base.model_validate(self._license(License.LicenseState.ACTIVE, -1))
        returned = LicenseSerializer.Base.model_validate(self._license(License.LicenseState.RETURNED, 7))
        self.assertFalse(lapsed.is_active_loan)
        self.assertFalse(returned.is_active_loan)

    def test_active_filter_covers_ready_and_active(self, _mint):
        request = RequestFactory().get("/readium/v1/licenses", data={"active": "true"})
        request.user = _user()
        sql = str(LicenseFilter(request.GET, queryset=License.objects.all(), request=request).qs.query)
        self.assertIn('"licenses"."state" IN (ready, active)', sql)
        self.assertIn('"licenses"."expires_at" >', sql)
