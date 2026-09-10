"""
Regression tests for license creation (IP-003 Phase 0).

Covers:
- Issue #56: the historical NameError caused by `BorrowView` passing
  `passphrase_hash=` to `LicenseService.create_license` which did not accept
  that kwarg. The fix added `passphrase_hash` to the signature and resolution
  precedence (user_passphrase > passphrase_hash arg > user default).
- Passphrase pre-flight: missing passphrase raises the typed
  `PassphraseRequiredError` instead of a generic `ValueError`.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.readium.services.license_service import LicenseService, PassphraseRequiredError


class CreateLicenseSignatureTests(SimpleTestCase):
    """The signature of create_license must accept both user_passphrase and passphrase_hash."""

    def test_accepts_passphrase_hash_kwarg(self):
        import inspect

        sig = inspect.signature(LicenseService.create_license)
        params = sig.parameters
        self.assertIn("passphrase_hash", params, "passphrase_hash must be a kwarg of create_license")
        self.assertIn("user_passphrase", params, "user_passphrase must remain a kwarg of create_license")

    def test_passphrase_required_error_is_value_error(self):
        self.assertTrue(issubclass(PassphraseRequiredError, ValueError))


class PassphraseResolutionTests(SimpleTestCase):
    """The passphrase resolution branch is the historical bug surface — exercise it directly."""

    def _make_user(self, hash_value=None, hint=None):
        user = MagicMock()
        user.lcp_passphrase_hash = hash_value
        user.lcp_passphrase_hint = hint
        return user

    def test_missing_passphrase_raises_passphrase_required(self):
        user = self._make_user(hash_value=None)

        with patch.object(LicenseService, "can_user_borrow") as mock_borrow:
            mock_borrow.return_value = {"can_borrow": True, "available_slots": 1}
            with self.assertRaises(PassphraseRequiredError):
                LicenseService.create_license(entry=MagicMock(), user=user)
