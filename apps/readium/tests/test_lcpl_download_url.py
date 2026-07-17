"""
Regression tests for the `.lcpl` download URL (IP-009 Phase 4).

The License gateway is capability-token only, so `download_url` is only
usable when the serializer minted a token — and it only mints one when a
`request` sits in the serializer context.

`LicenseDetail.get`, the create response and the reservation-claim
response all called `model_validate(license)` with no context, so they
emitted a bare relative path (`/readium/v1/licenses/{id}.lcpl`) with no
token. The portal resolves that against its own origin, so "download"
404'd against the portal instead of hitting the gateway. The list
endpoint was unaffected because `PaginationResponse` passes
`serializer_context`.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from apps.readium.models import License
from apps.readium.serializers import LicenseSerializer


def _license_stub():
    now = timezone.now()
    return SimpleNamespace(
        id=uuid4(),
        entry_id=uuid4(),
        user_id=uuid4(),
        state=License.LicenseState.READY,
        starts_at=now,
        expires_at=now + timedelta(days=7),
        created_at=now,
        updated_at=now,
        renewal_count=0,
    )


@patch("apps.readium.serializers.CapabilityTokenService.mint", return_value="TESTTOKEN")
class LcplDownloadUrlTests(SimpleTestCase):
    def test_request_context_yields_absolute_tokenised_url(self, _mint):
        license_obj = _license_stub()
        request = RequestFactory().get("/readium/v1/licenses")

        data = LicenseSerializer.Base.model_validate(license_obj, context={"request": request})

        self.assertTrue(
            data.download_url.startswith("http://testserver/"),
            f"download_url must be absolute, got {data.download_url!r}",
        )
        self.assertIn("token=TESTTOKEN", data.download_url)
        self.assertIn(str(license_obj.id), data.download_url)

    def test_missing_context_yields_unusable_url(self, _mint):
        """Documents the operator/test fallback: no request → no token.

        Any client-facing view must therefore pass `context={"request": request}`.
        """
        data = LicenseSerializer.Base.model_validate(_license_stub())

        self.assertNotIn("token=", data.download_url)
        self.assertFalse(data.download_url.startswith("http"))

    def test_feed_context_mints_multi_use_scope(self, mint):
        request = RequestFactory().get("/opds/feed")

        LicenseSerializer.Base.model_validate(
            _license_stub(), context={"request": request, "opds_feed": True}
        )

        self.assertEqual(mint.call_args.kwargs["scope"], "lcpl_feed_download")
        self.assertFalse(mint.call_args.kwargs["single_use"])
