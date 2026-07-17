"""
Regression: the OPDS 1.2 Atom `<link>` schema must accept the LCP borrow rel.

`BorrowLinkResolver` emits `rel="http://opds-spec.org/acquisition/borrow"`
for every readium-enabled entry (shared with OPDS 2.0). The 1.2 `Link.rel`
field is a closed `LinkType` enum; if `borrow` is not a member, constructing
the link raises a pydantic `ValidationError` and every 1.2 feed/entry that
contains an LCP title 500s.
"""

from django.test import SimpleTestCase

from apps.opds.schema import Link, LinkType
from apps.opds.services.borrow_link import REL_BORROW


class BorrowLinkSchemaTests(SimpleTestCase):
    def test_borrow_rel_is_a_valid_linktype_member(self):
        self.assertEqual(LinkType.BORROW.value, REL_BORROW)

    def test_link_accepts_the_borrow_rel_without_raising(self):
        link = Link(
            rel=REL_BORROW,
            href="https://example.test/opds/v2/cat/publications/x/borrow",
            type="application/vnd.readium.lcp.license.v1.0+json",
            title="Borrow",
        )
        self.assertEqual(link.rel, LinkType.BORROW)
