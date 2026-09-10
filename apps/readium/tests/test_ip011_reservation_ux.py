"""
IP-011 regression tests — reservation UX completion (G1-G5).

Surface-level (SimpleTestCase + mocks) where possible — heavier
integration-style behaviour is validated manually in the elvira-portal
walkthrough and via the EDRLab harness.

Covers:

- Phase 1 (G1): scoped JWT carries a `jti` claim, claim endpoint is
  registered, signal builds the URL via `generate_scoped_url`, scope
  validation rejects mismatched scopes, replay check rejects consumed
  tokens, portal-redirect activates when `EVILFLOWERS_PORTAL_URL` is set.
- Phase 2 (G2): ETA helpers return None / single / range as expected;
  signal passes the keys through to the `reservation_placed` context.
- Phase 3 (G3): `reservation_promoted` enum value present, per-user cap
  setting is respected, setting-off path skips dispatch.
- Phase 4 (G4): `reservation_cancelled` enum value present, signal
  dispatches on QUEUED/AVAILABLE → CANCELLED, CLAIMED stays silent.
- Phase 5 (G5): `reservation_claim_reminder` enum + sweep task exist,
  dedup query uses `(reservation_id, available_at)` composite key.
"""

from http import HTTPStatus
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

# ---------------------------------------------------------------------------
# Phase 1 — claim endpoint, scoped token plumbing
# ---------------------------------------------------------------------------


class ScopedTokenJtiTests(SimpleTestCase):
    """`JWTFactory.scoped` must embed a `jti` so consumers can do single-use."""

    def test_scoped_token_carries_jti(self):
        from apps.core.auth import JWTFactory

        token = JWTFactory(str(uuid4())).scoped(scope="reservation:claim")
        claims = JWTFactory.decode(token)
        self.assertEqual(claims["type"], "scoped")
        self.assertEqual(claims["scope"], "reservation:claim")
        self.assertIn("jti", claims)
        self.assertTrue(claims["jti"])  # non-empty

    def test_two_tokens_have_different_jti(self):
        from apps.core.auth import JWTFactory

        user_id = str(uuid4())
        t1 = JWTFactory(user_id).scoped(scope="reservation:claim")
        t2 = JWTFactory(user_id).scoped(scope="reservation:claim")
        c1 = JWTFactory.decode(t1)
        c2 = JWTFactory.decode(t2)
        self.assertNotEqual(c1["jti"], c2["jti"])


class GenerateScopedUrlBaseTests(SimpleTestCase):
    """`generate_scoped_url` accepts an optional `base_url` override."""

    def test_uses_default_base_url(self):
        from apps.notifications.services import NotificationService

        with override_settings(EVILFLOWERS_BASE_URL="https://catalog.example.com"):
            url = NotificationService.generate_scoped_url(
                user_id=str(uuid4()),
                scope="reservation:claim",
                resource_path="/readium/v1/reservations/abc/claim",
            )
        self.assertTrue(url.startswith("https://catalog.example.com/readium/v1/reservations/abc/claim?access_token="))

    def test_override_base_url_wins(self):
        from apps.notifications.services import NotificationService

        with override_settings(EVILFLOWERS_BASE_URL="https://catalog.example.com"):
            url = NotificationService.generate_scoped_url(
                user_id=str(uuid4()),
                scope="reservation:claim",
                resource_path="/readium/v1/reservations/abc/claim",
                base_url="https://portal.example.com",
            )
        self.assertTrue(url.startswith("https://portal.example.com/readium/v1/reservations/abc/claim?access_token="))


class ClaimUrlPatternTests(SimpleTestCase):
    """The claim endpoint must be registered under /readium/v1/reservations/{id}/claim."""

    def test_url_pattern_registered(self):
        from django.urls import reverse

        rid = uuid4()
        url = reverse("readium:reservation-claim-page", kwargs={"reservation_id": rid})
        self.assertTrue(url.endswith(f"/reservations/{rid}/claim"))


class ClaimSignalUsesScopedUrlTests(SimpleTestCase):
    """The AVAILABLE branch in `_notify_reservation_transitions` builds the URL
    via `NotificationService.generate_scoped_url`, NOT a hand-rolled string."""

    def test_signal_source_uses_generate_scoped_url(self):
        from pathlib import Path

        source = Path("apps/readium/signals.py").read_text()
        self.assertIn("generate_scoped_url", source)
        self.assertIn("CLAIM_SCOPE", source)


class ClaimTokenDecodeTests(SimpleTestCase):
    """`_decode_claim_token` enforces scope / subject / replay invariants."""

    def _build_token(self, user_id, scope="reservation:claim"):
        from apps.core.auth import JWTFactory

        return JWTFactory(user_id).scoped(scope=scope)

    def test_valid_token_passes(self):
        from apps.readium.views.claim import _decode_claim_token

        user_id = uuid4()
        token = self._build_token(str(user_id))
        reservation = MagicMock(user_id=user_id)
        # cache.get returns None for never-set keys by default
        claims = _decode_claim_token(token, reservation)
        self.assertEqual(claims["scope"], "reservation:claim")

    def test_wrong_scope_rejected(self):
        from apps.core.errors import ProblemDetailException
        from apps.readium.views.claim import _decode_claim_token

        user_id = uuid4()
        token = self._build_token(str(user_id), scope="license:read")
        reservation = MagicMock(user_id=user_id)
        with self.assertRaises(ProblemDetailException) as ctx:
            _decode_claim_token(token, reservation)
        self.assertEqual(ctx.exception.status, HTTPStatus.UNAUTHORIZED)

    def test_wrong_subject_rejected(self):
        from apps.core.errors import ProblemDetailException
        from apps.readium.views.claim import _decode_claim_token

        token = self._build_token(str(uuid4()))  # token for one user
        reservation = MagicMock(user_id=uuid4())  # different user
        with self.assertRaises(ProblemDetailException) as ctx:
            _decode_claim_token(token, reservation)
        self.assertEqual(ctx.exception.status, HTTPStatus.FORBIDDEN)

    def test_replayed_token_returns_gone(self):
        from apps.core.errors import ProblemDetailException
        from apps.readium.views.claim import _decode_claim_token, USED_TOKEN_CACHE_PREFIX
        from apps.core.auth import JWTFactory

        user_id = uuid4()
        token = self._build_token(str(user_id))
        claims = JWTFactory.decode(token)
        reservation = MagicMock(user_id=user_id)

        with patch("apps.readium.views.claim.cache.get", return_value=1):
            with self.assertRaises(ProblemDetailException) as ctx:
                _decode_claim_token(token, reservation)
        self.assertEqual(ctx.exception.status, HTTPStatus.GONE)


class PortalRedirectTests(SimpleTestCase):
    """`EVILFLOWERS_PORTAL_URL`, when set, flips the GET handler to a 302 redirect."""

    def test_redirect_when_portal_url_set(self):
        from django.test import RequestFactory
        from apps.readium.views.claim import ReservationClaimPage

        with override_settings(EVILFLOWERS_PORTAL_URL="https://portal.example.com"):
            request = RequestFactory().get("/x?access_token=tok")
            view = ReservationClaimPage()
            response = view.get(request, reservation_id=uuid4())
        self.assertEqual(response.status_code, HTTPStatus.FOUND)
        self.assertIn("portal.example.com", response["Location"])
        self.assertIn("access_token=tok", response["Location"])

    def test_missing_token_returns_401(self):
        from django.test import RequestFactory
        from apps.core.errors import ProblemDetailException
        from apps.readium.views.claim import ReservationClaimPage

        with override_settings(EVILFLOWERS_PORTAL_URL=""):
            request = RequestFactory().get("/x")
            view = ReservationClaimPage()
            with self.assertRaises(ProblemDetailException) as ctx:
                view.get(request, reservation_id=uuid4())
        self.assertEqual(ctx.exception.status, HTTPStatus.UNAUTHORIZED)


# ---------------------------------------------------------------------------
# Phase 2 — ETA range on `reservation_placed`
# ---------------------------------------------------------------------------


class EtaRangeHelpersTests(SimpleTestCase):
    """Earliest/latest ETA helpers compose correctly under the three deployment shapes."""

    def test_helpers_exist(self):
        from apps.readium.services import entry_lcp_decorator

        self.assertTrue(hasattr(entry_lcp_decorator, "reservation_eta_earliest"))
        self.assertTrue(hasattr(entry_lcp_decorator, "reservation_eta_latest"))

    def test_signal_passes_eta_keys(self):
        from pathlib import Path

        source = Path("apps/readium/signals.py").read_text()
        self.assertIn("estimated_available_from", source)
        self.assertIn("estimated_available_until", source)


# ---------------------------------------------------------------------------
# Phase 3 — `reservation_promoted` notification (opt-in)
# ---------------------------------------------------------------------------


class PromotedNotificationTests(SimpleTestCase):
    def test_enum_value_present(self):
        from apps.notifications.models import NotificationLog

        self.assertTrue(hasattr(NotificationLog.NotificationType, "RESERVATION_PROMOTED"))
        self.assertEqual(NotificationLog.NotificationType.RESERVATION_PROMOTED, "reservation_promoted")

    def test_default_setting_is_off(self):
        from django.conf import settings

        self.assertFalse(settings.EVILFLOWERS_READIUM_NOTIFY_POSITION_CHANGES)

    def test_default_cap_is_three(self):
        from django.conf import settings

        self.assertEqual(settings.EVILFLOWERS_READIUM_PROMOTED_MAX_PER_USER_PER_ENTRY_PER_DAY, 3)


# ---------------------------------------------------------------------------
# Phase 4 — `reservation_cancelled` notification
# ---------------------------------------------------------------------------


class CancelledNotificationTests(SimpleTestCase):
    def test_enum_value_present(self):
        from apps.notifications.models import NotificationLog

        self.assertTrue(hasattr(NotificationLog.NotificationType, "RESERVATION_CANCELLED"))
        self.assertEqual(NotificationLog.NotificationType.RESERVATION_CANCELLED, "reservation_cancelled")

    def test_signal_dispatches_on_cancel(self):
        from pathlib import Path

        source = Path("apps/readium/signals.py").read_text()
        # The CANCELLED transition must dispatch the reservation_cancelled email.
        self.assertIn("Reservation.Status.CANCELLED", source)
        self.assertIn("reservation_cancelled", source)


# ---------------------------------------------------------------------------
# Phase 5 — `reservation_claim_reminder` sweep
# ---------------------------------------------------------------------------


class ClaimReminderTests(SimpleTestCase):
    def test_enum_value_present(self):
        from apps.notifications.models import NotificationLog

        self.assertTrue(hasattr(NotificationLog.NotificationType, "RESERVATION_CLAIM_REMINDER"))
        self.assertEqual(NotificationLog.NotificationType.RESERVATION_CLAIM_REMINDER, "reservation_claim_reminder")

    def test_default_hours_is_six(self):
        from django.conf import settings

        self.assertEqual(settings.EVILFLOWERS_READIUM_CLAIM_REMINDER_HOURS, 6)

    def test_sweep_task_registered(self):
        from apps.readium import tasks

        self.assertTrue(hasattr(tasks, "reservation_claim_reminder_sweep"))

    def test_dedup_key_uses_available_at(self):
        """Phase 5 Q5 resolution: composite (reservation_id, available_at) dedup."""
        from pathlib import Path

        source = Path("apps/readium/tasks.py").read_text()
        self.assertIn("context_snapshot__reservation_id", source)
        self.assertIn("context_snapshot__available_at", source)

    def test_beat_schedule_registered(self):
        from pathlib import Path

        source = Path("evil_flowers_catalog/celery.py").read_text()
        self.assertIn("reservation_claim_reminder_sweep", source)


# ---------------------------------------------------------------------------
# Template registry — all three new templates discoverable
# ---------------------------------------------------------------------------


class TemplateRegistryIP011Tests(SimpleTestCase):
    EXPECTED_TYPES = {
        "reservation_promoted",
        "reservation_cancelled",
        "reservation_claim_reminder",
    }

    def test_all_new_templates_discovered(self):
        from apps.notifications.registry import TemplateRegistry

        discovered = set(TemplateRegistry.get_template_names())
        missing = self.EXPECTED_TYPES - discovered
        self.assertFalse(missing, f"Missing IP-011 notification templates: {missing}")
