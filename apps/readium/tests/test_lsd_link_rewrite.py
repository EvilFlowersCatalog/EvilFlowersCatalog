"""
Regression: the LSD `license` rel must resolve to the `.lcpl` gateway.

In a License Status Document the `license` rel points at the LCP License
Document — the reader re-downloads the refreshed license from it after a
renew/return. It must NOT be collapsed onto the device-`register` endpoint
(a POST-only route): a reader following it would 405 or accidentally register
a device, breaking the "download updated license" step of the lending flow.

The gateway is capability-token only, so the rewritten href must also carry a
token the (bearer-less) reader can redeem.
"""

from unittest.mock import MagicMock
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from apps.readium.models import License
from apps.readium.views.status_proxy import StatusProxyView

_LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _request():
    req = MagicMock()
    req.scheme = "https"
    req.get_host.return_value = "catalog.example.test"
    return req


def _license():
    lic = MagicMock(spec=License)
    lic.pk = uuid4()
    lic.user_id = uuid4()
    lic.lcp_license_id = uuid4()
    return lic


@override_settings(CACHES=_LOCMEM_CACHE)
class LsdLinkRewriteTests(SimpleTestCase):
    def setUp(self):
        self.view = StatusProxyView()
        self.license = _license()

    def _rewrite(self, links):
        return self.view._rewrite_links({"links": links}, _request(), self.license)

    def test_license_rel_points_at_the_lcpl_gateway_with_a_token(self):
        data = self._rewrite([{"rel": "license", "href": "http://127.0.0.1:8990/licenses/abc"}])
        href = data["links"][0]["href"]

        self.assertIn(f"/licenses/{self.license.pk}.lcpl", href)
        self.assertIn("token=", href)
        self.assertNotIn("/register", href)

    def test_license_rel_is_not_collapsed_onto_register(self):
        data = self._rewrite(
            [
                {"rel": "register", "href": "http://127.0.0.1:8990/licenses/abc/register"},
                {"rel": "license", "href": "http://127.0.0.1:8990/licenses/abc"},
            ]
        )
        register_href = data["links"][0]["href"]
        license_href = data["links"][1]["href"]

        self.assertIn("/register", register_href)
        self.assertNotEqual(register_href, license_href)
