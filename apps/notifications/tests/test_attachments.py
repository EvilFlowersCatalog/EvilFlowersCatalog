"""
Tests for the attachment resolver registry.

The registry is the seam that keeps `NotificationService` ignorant of any
domain: a notification carries a serialisable ref, and the worker materialises
it at send time. These tests pin the contract — resolution, fail-soft, and
that an unknown/erroring resolver never breaks the email.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.notifications import attachments
from apps.notifications.attachments import (
    EmailAttachment,
    register_attachment_resolver,
    resolve_attachments,
)


class AttachmentRegistryTests(SimpleTestCase):
    def setUp(self):
        # Isolate the module-level registry per test.
        self._saved = dict(attachments._RESOLVERS)
        attachments._RESOLVERS.clear()

    def tearDown(self):
        attachments._RESOLVERS.clear()
        attachments._RESOLVERS.update(self._saved)

    def test_registered_resolver_is_materialised(self):
        register_attachment_resolver("demo", lambda params: EmailAttachment(params["name"], b"x", "text/plain"))

        out = resolve_attachments([{"resolver": "demo", "params": {"name": "f.txt"}}])

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].filename, "f.txt")

    def test_unknown_resolver_is_skipped_not_raised(self):
        self.assertEqual(resolve_attachments([{"resolver": "nope", "params": {}}]), [])

    def test_resolver_returning_none_is_dropped(self):
        register_attachment_resolver("empty", lambda params: None)
        self.assertEqual(resolve_attachments([{"resolver": "empty", "params": {}}]), [])

    def test_erroring_resolver_does_not_break_the_batch(self):
        def boom(params):
            raise RuntimeError("lcp server down")

        register_attachment_resolver("boom", boom)
        register_attachment_resolver("ok", lambda p: EmailAttachment("a", b"1", "text/plain"))

        out = resolve_attachments([{"resolver": "boom", "params": {}}, {"resolver": "ok", "params": {}}])

        self.assertEqual([a.filename for a in out], ["a"])

    def test_none_refs_yields_empty(self):
        self.assertEqual(resolve_attachments(None), [])


class LcplResolverTests(SimpleTestCase):
    """Readium's resolver is registered and fetches the fresh .lcpl."""

    def test_lcpl_resolver_is_registered_under_stable_name(self):
        from apps.readium.notifications import LCPL_RESOLVER

        self.assertIn(LCPL_RESOLVER, attachments._RESOLVERS)

    def test_lcpl_ref_shape(self):
        from apps.readium.notifications import LCPL_RESOLVER, lcpl_attachment_ref

        ref = lcpl_attachment_ref("abc")
        self.assertEqual(ref["resolver"], LCPL_RESOLVER)
        self.assertEqual(ref["params"], {"license_id": "abc"})

    def test_missing_license_returns_none(self):
        from apps.readium import notifications as rn

        # `License` is imported lazily inside the resolver — patch it at source.
        with patch("apps.readium.models.License") as license_model:
            license_model.objects.select_related.return_value.filter.return_value.first.return_value = None
            self.assertIsNone(rn.resolve_lcpl_attachment({"license_id": "gone"}))
