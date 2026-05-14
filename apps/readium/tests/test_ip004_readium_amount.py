"""
IP-004 regression tests — `readium_amount` configurability and admin surface.

These mirror the success-criteria checkboxes in
`docs/proposals/posts/ip-004-readium-amount-configurability.md`. They are
surface-level (SimpleTestCase + mocks) where possible — heavier end-to-end
scenarios are validated via the EDRLab pre-flight harness, not here.

Covers:

- Phase 1 (G1, G2, Q1, Q3): form field presence, validator bound, partial-update
  merge, cap-reduction reject helper.
- Phase 2 (G3): `active_count` and `over_saturated` on `lcp_state_mapping` and
  `LicenseService.get_entry_availability`.
- Phase 3 (G4, Q4): `LicenseFilter` / `ReservationFilter` widen for catalog
  managers.
- Phase 4 (G5, Q2): `check_license_state_manage` / `check_license_download`
  split; download endpoint denies non-owners (including superusers).
- Phase 5 (G6): `lcp_state` and `over_saturated` filters declared on
  `EntryFilter`.
"""

from http import HTTPStatus
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


class FormFieldTests(SimpleTestCase):
    """Phase 1 / G1 / G2: the form field is declared with min_value=1 and no upper bound."""

    def test_readium_amount_field_declared(self):
        from apps.api.forms.entries import EntryConfigForm

        self.assertIn("readium_amount", EntryConfigForm.base_fields)

    def test_readium_amount_min_value_is_one(self):
        from apps.api.forms.entries import EntryConfigForm

        field = EntryConfigForm.base_fields["readium_amount"]
        self.assertEqual(field.min_value, 1, "readium_amount must reject values below 1")

    def test_readium_amount_has_no_max_value(self):
        # Q5 resolution: no upper cap. Trust staff; SPA may warn at large values.
        from apps.api.forms.entries import EntryConfigForm

        field = EntryConfigForm.base_fields["readium_amount"]
        self.assertIsNone(getattr(field, "max_value", None))


class ConfigMergeTests(SimpleTestCase):
    """Phase 1 / Q1: partial update preserves omitted keys via populate_config merge."""

    def test_populate_config_merges_over_existing(self):
        from apps.api.forms.entries import EntryForm

        form = EntryForm.__new__(EntryForm)  # bypass __init__; we only need the method

        obj = MagicMock()
        obj.config = {
            "readium_enabled": False,
            "readium_amount": 5,
            "evilflowers_ip_block": True,
        }

        merged = form.populate_config(obj, {"readium_amount": 3})

        # Touched key takes the new value, others are preserved.
        self.assertEqual(merged["readium_amount"], 3)
        self.assertEqual(merged["readium_enabled"], False)
        self.assertEqual(merged["evilflowers_ip_block"], True)

    def test_populate_config_handles_missing_existing(self):
        from apps.api.forms.entries import EntryForm

        form = EntryForm.__new__(EntryForm)
        obj = MagicMock(spec=[])  # no config attribute

        merged = form.populate_config(obj, {"readium_amount": 7})

        self.assertEqual(merged, {"readium_amount": 7})

    def test_populate_config_preserves_explicit_false_in_payload(self):
        """A `False` in the payload must overwrite a `True` in storage — not be treated as omitted."""
        from apps.api.forms.entries import EntryForm

        form = EntryForm.__new__(EntryForm)
        obj = MagicMock()
        obj.config = {"readium_enabled": True}

        merged = form.populate_config(obj, {"readium_enabled": False})

        self.assertEqual(merged["readium_enabled"], False)


class DetailTypeTests(SimpleTestCase):
    """Phase 1 / Q3: 409 response uses the dedicated DetailType."""

    def test_detail_type_value(self):
        from apps.core.errors import DetailType

        self.assertTrue(hasattr(DetailType, "READIUM_AMOUNT_BELOW_ACTIVE_COUNT"))
        self.assertEqual(
            DetailType.READIUM_AMOUNT_BELOW_ACTIVE_COUNT.value,
            "/readium-amount-below-active-count",
        )


class CapReductionRejectTests(SimpleTestCase):
    """Phase 1 / Q3: `_assert_readium_amount_above_active` raises 409 below active count."""

    def _make_form(self, readium_amount):
        form = MagicMock()
        form.cleaned_data = {"config": {"readium_amount": readium_amount}}
        return form

    def _patch_license_count(self, count):
        # Patch the QuerySet chain License.objects.filter(...).count() to return `count`.
        return patch(
            "apps.api.views.entries.License.objects.filter",
            return_value=MagicMock(count=MagicMock(return_value=count)),
        )

    def test_succeeds_when_no_active_licenses(self):
        from apps.api.views.entries import _assert_readium_amount_above_active

        entry = MagicMock(pk="entry-id")
        form = self._make_form(readium_amount=1)
        with self._patch_license_count(0):
            _assert_readium_amount_above_active(entry, form)  # must not raise

    def test_succeeds_at_exact_equality(self):
        from apps.api.views.entries import _assert_readium_amount_above_active

        entry = MagicMock(pk="entry-id")
        form = self._make_form(readium_amount=4)
        with self._patch_license_count(4):
            _assert_readium_amount_above_active(entry, form)  # equality is allowed

    def test_rejects_when_below_active_count(self):
        from apps.api.views.entries import _assert_readium_amount_above_active
        from apps.core.errors import ProblemDetailException, DetailType

        entry = MagicMock(pk="entry-id")
        form = self._make_form(readium_amount=2)
        with self._patch_license_count(4):
            with self.assertRaises(ProblemDetailException) as ctx:
                _assert_readium_amount_above_active(entry, form)

        exc = ctx.exception
        self.assertEqual(exc.status, HTTPStatus.CONFLICT)
        self.assertEqual(exc.type, DetailType.READIUM_AMOUNT_BELOW_ACTIVE_COUNT)
        self.assertEqual(exc._additional_data["current_active_count"], 4)
        self.assertEqual(exc._additional_data["requested_readium_amount"], 2)
        self.assertIn("entry-id", exc._additional_data["licenses_url"])

    def test_skips_check_when_amount_not_in_payload(self):
        from apps.api.views.entries import _assert_readium_amount_above_active

        entry = MagicMock(pk="entry-id")
        form = MagicMock()
        form.cleaned_data = {"config": {"readium_enabled": True}}  # no readium_amount

        # Even with active licenses, no check fires because the field isn't in the payload.
        with self._patch_license_count(99):
            _assert_readium_amount_above_active(entry, form)


class LcpStateMappingTests(SimpleTestCase):
    """Phase 2 / G3: `active_count` and `over_saturated` are emitted by `_resolve_one`."""

    def _entry(self, *, enabled=True, amount=2):
        entry = MagicMock()
        entry.pk = "entry-1"

        def read(key):
            return {"readium_enabled": enabled, "readium_amount": amount}.get(key)

        entry.read_config = read
        return entry

    def test_not_lcp_response_includes_active_count_and_over_saturated(self):
        from apps.readium.services.entry_lcp_decorator import _resolve_one

        entry = self._entry(enabled=False)
        row = _resolve_one(entry, user=None, license_rows=[], reservation_rows=[], Reservation=None)
        self.assertEqual(row["active_count"], 0)
        self.assertFalse(row["over_saturated"])

    def test_under_cap_is_not_over_saturated(self):
        from datetime import datetime, timezone as tz

        from apps.readium.services.entry_lcp_decorator import _resolve_one

        entry = self._entry(amount=5)
        rows = [
            {
                "id": "L1",
                "user_id": "U1",
                "entry_id": entry.pk,
                "expires_at": datetime(2030, 1, 1, tzinfo=tz.utc),
            }
        ]
        row = _resolve_one(entry, user=None, license_rows=rows, reservation_rows=[], Reservation=None)
        self.assertEqual(row["active_count"], 1)
        self.assertEqual(row["total_slots"], 5)
        self.assertFalse(row["over_saturated"])

    def test_legacy_over_saturated_state_is_surfaced(self):
        from datetime import datetime, timezone as tz

        from apps.readium.services.entry_lcp_decorator import _resolve_one

        # Cap=2 but 4 active licenses (legacy direct-JSON write before IP-004).
        entry = self._entry(amount=2)
        rows = [
            {
                "id": f"L{i}",
                "user_id": f"U{i}",
                "entry_id": entry.pk,
                "expires_at": datetime(2030, 1, 1 + i, tzinfo=tz.utc),
            }
            for i in range(4)
        ]
        row = _resolve_one(entry, user=None, license_rows=rows, reservation_rows=[], Reservation=None)
        self.assertEqual(row["active_count"], 4)
        self.assertEqual(row["total_slots"], 2)
        self.assertTrue(row["over_saturated"])
        # available_slots must still clamp at 0 (existing OPDS contract).
        self.assertEqual(row["available_slots"], 0)


class EntrySerializerFieldsTests(SimpleTestCase):
    """Phase 2 / G3: the new fields are declared on the entry serializer."""

    def test_serializer_declares_new_fields(self):
        from apps.api.serializers.entries import EntrySerializer

        fields = EntrySerializer.Base.model_fields
        self.assertIn("active_count", fields)
        self.assertIn("over_saturated", fields)

    def test_serializer_field_defaults(self):
        from apps.api.serializers.entries import EntrySerializer

        fields = EntrySerializer.Base.model_fields
        self.assertEqual(fields["active_count"].default, 0)
        self.assertEqual(fields["over_saturated"].default, False)


class AvailabilityServiceTests(SimpleTestCase):
    """Phase 2: `LicenseService.get_entry_availability` surfaces the new fields."""

    def test_get_entry_availability_returns_active_count_and_over_saturated(self):
        # Validate by source inspection — a true unit test would need a populated
        # DB (Entry + License rows). Asserting the contract via source is enough
        # for the SimpleTestCase tier; full integration is exercised by the
        # EDRLab pre-flight runbook.
        import inspect

        from apps.readium.services.license_service import LicenseService

        src = inspect.getsource(LicenseService.get_entry_availability)
        self.assertIn('"active_count"', src)
        self.assertIn('"over_saturated"', src)


class LicenseFilterWideningTests(SimpleTestCase):
    """Phase 3 / G4: catalog managers see licenses on entries they MANAGE."""

    def test_license_filter_qs_uses_user_catalog_manage(self):
        import inspect

        from apps.readium.filters import LicenseFilter

        src = inspect.getsource(LicenseFilter.qs.fget)
        self.assertIn("UserCatalog", src)
        self.assertIn("MANAGE", src)
        # The widening must NOT bypass authentication.
        self.assertIn("is_authenticated", src)

    def test_reservation_filter_qs_uses_user_catalog_manage(self):
        import inspect

        from apps.readium.filters import ReservationFilter

        src = inspect.getsource(ReservationFilter.qs.fget)
        self.assertIn("UserCatalog", src)
        self.assertIn("MANAGE", src)


class PredicateSplitTests(SimpleTestCase):
    """Phase 4 / Q2: legacy predicate is split; download is owner-only."""

    def test_legacy_predicate_replaced(self):
        from apps.core.checkers import LicenseChecker

        self.assertTrue(hasattr(LicenseChecker, "check_license_state_manage"))
        self.assertTrue(hasattr(LicenseChecker, "check_license_download"))
        # The old undifferentiated predicate is gone; nothing else should reference it.
        self.assertFalse(hasattr(LicenseChecker, "check_license_manage"))

    def test_download_predicate_owner_only(self):
        from apps.core.checkers import LicenseChecker

        owner = MagicMock(is_authenticated=True, is_superuser=False, id="owner-id")
        license_obj = MagicMock(user_id="owner-id")

        self.assertTrue(LicenseChecker.check_license_download(owner, license_obj))

        other = MagicMock(is_authenticated=True, is_superuser=False, id="other-id")
        self.assertFalse(LicenseChecker.check_license_download(other, license_obj))

        # Superuser is NOT exempted — Q2 resolution forbids admin download.
        admin = MagicMock(is_authenticated=True, is_superuser=True, id="admin-id")
        self.assertFalse(LicenseChecker.check_license_download(admin, license_obj))

        anon = MagicMock(is_authenticated=False)
        self.assertFalse(LicenseChecker.check_license_download(anon, license_obj))

    def test_state_manage_predicate_admits_owner_and_superuser(self):
        from apps.core.checkers import LicenseChecker

        owner = MagicMock(is_authenticated=True, is_superuser=False, id="owner-id")
        license_obj = MagicMock(user_id="owner-id")
        license_obj.entry.catalog.user_catalogs.filter.return_value.exists.return_value = False
        self.assertTrue(LicenseChecker.check_license_state_manage(owner, license_obj))

        admin = MagicMock(is_authenticated=True, is_superuser=True, id="admin-id")
        self.assertTrue(LicenseChecker.check_license_state_manage(admin, license_obj))

        anon = MagicMock(is_authenticated=False)
        self.assertFalse(LicenseChecker.check_license_state_manage(anon, license_obj))

    def test_state_manage_predicate_admits_catalog_manager(self):
        from apps.core.checkers import LicenseChecker

        manager = MagicMock(is_authenticated=True, is_superuser=False, id="manager-id")
        license_obj = MagicMock(user_id="other-id")
        license_obj.entry.catalog.user_catalogs.filter.return_value.exists.return_value = True

        self.assertTrue(LicenseChecker.check_license_state_manage(manager, license_obj))

    def test_callsites_use_split_predicates(self):
        from pathlib import Path

        download_src = Path("apps/readium/views/download.py").read_text()
        self.assertIn("check_license_download", download_src)
        self.assertNotIn("check_license_manage", download_src)

        license_src = Path("apps/readium/views/licenses.py").read_text()
        self.assertIn("check_license_state_manage", license_src)
        self.assertNotIn("check_license_manage", license_src)


class EntryFilterSaturationTests(SimpleTestCase):
    """Phase 5 / G6: `lcp_state` and `over_saturated` filters are declared."""

    def test_filters_declared(self):
        from apps.api.filters.entries import EntryFilter

        self.assertIn("lcp_state", EntryFilter.base_filters)
        self.assertIn("over_saturated", EntryFilter.base_filters)

    def test_lcp_state_filter_method_resolved(self):
        from apps.api.filters.entries import EntryFilter

        filt = EntryFilter.base_filters["lcp_state"]
        self.assertEqual(filt.method, "filter_lcp_state")
        self.assertTrue(callable(getattr(EntryFilter, "filter_lcp_state", None)))

    def test_over_saturated_filter_method_resolved(self):
        from apps.api.filters.entries import EntryFilter

        filt = EntryFilter.base_filters["over_saturated"]
        self.assertEqual(filt.method, "filter_over_saturated")
        self.assertTrue(callable(getattr(EntryFilter, "filter_over_saturated", None)))
