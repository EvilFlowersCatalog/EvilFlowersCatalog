"""
Regression tests for `.lcpl` link resolution (hint + status).

The LCP server writes the `hint` and `status` links into the license
*before* signing it, expanding `{license_id}` with the **LCP** license id
(`License.lcp_license_id`) — not our pk. The signature covers the links,
so the catalog cannot rewrite them at serve time.

That means every public endpoint a `.lcpl` link points at must accept
both identifiers. Before this, `StatusProxyView._get_license` and
`HintPageView` looked up `pk` only, so a config-driven `status` link would
404 — the reader could never register a device, and the licence stayed
`ready` forever instead of transitioning to `active`.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase

from apps.readium.views._license_lookup import resolve_license


class ResolveLicenseTests(SimpleTestCase):
    """`resolve_license` must accept the catalog pk and the LCP license id."""

    def test_queries_both_pk_and_lcp_license_id(self):
        sentinel = MagicMock(name="license")
        lcp_id = uuid4()

        with patch("apps.readium.views._license_lookup.License") as license_model:
            license_model.objects.filter.return_value.first.return_value = sentinel

            result = resolve_license(lcp_id)

        self.assertIs(result, sentinel)
        # The Q object must span both columns; a pk-only lookup is the bug.
        q = license_model.objects.filter.call_args.args[0]
        rendered = str(q)
        self.assertIn("pk", rendered)
        self.assertIn("lcp_license_id", rendered)

    def test_unknown_identifier_returns_none(self):
        with patch("apps.readium.views._license_lookup.License") as license_model:
            license_model.objects.filter.return_value.first.return_value = None

            self.assertIsNone(resolve_license(uuid4()))

    def test_malformed_identifier_returns_none_instead_of_raising(self):
        """The hint page takes `?license_id=` straight off the query string."""
        with patch("apps.readium.views._license_lookup.License") as license_model:
            license_model.objects.filter.side_effect = ValueError("badly formed UUID")

            self.assertIsNone(resolve_license("not-a-uuid"))
