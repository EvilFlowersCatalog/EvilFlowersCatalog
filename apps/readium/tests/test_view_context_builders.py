"""
Guard against NameErrors in `context_builder` lambdas.

The reservation/license list views build their `lcp_states` context in a lambda
passed to `PaginationResponse`. A lambda body isn't executed at import time, so
a missing import (e.g. `lcp_state_mapping`) only blows up at request time with a
500 — invisible to the mock/SimpleTestCase suite, which never drives the list
view with real rows. This asserts the symbol is resolvable in each module.
"""

from django.test import SimpleTestCase


class ContextBuilderSymbolTests(SimpleTestCase):
    def test_reservations_view_can_resolve_lcp_state_mapping(self):
        from apps.readium.views import reservations

        self.assertTrue(
            hasattr(reservations, "lcp_state_mapping"),
            "reservations.py uses lcp_state_mapping in a context_builder lambda but "
            "does not import it — the list endpoint 500s at request time.",
        )

    def test_licenses_view_can_resolve_lcp_state_mapping(self):
        from apps.readium.views import licenses

        self.assertTrue(hasattr(licenses, "lcp_state_mapping"))
