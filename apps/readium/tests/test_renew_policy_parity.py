"""
Regression tests: both renewal doors must enforce the same policy.

A loan can be renewed through two endpoints:

    PUT /readium/v1/licenses/{id}          action=renewed   -> portal
    PUT /readium/v1/licenses/{id}/renew                     -> reading apps
                                                              (the `renew` link
                                                               in the LSD status
                                                               document)

`RenewProxyView` used to forward straight to the Status Server with no policy
at all, so a reading app could renew while other users were queued for the
title, inside the acquisition embargo, or past the per-loan cap — and it never
incremented `renewal_count`, so the cap could not be reached on the portal path
either. The bypass only stayed unreachable while `license_status.renew` was
disabled and the status document therefore carried no `renew` link.

Both doors must route through `evaluate_renew` + `LicenseService.renew_license`.
"""

import inspect

from django.test import SimpleTestCase

from apps.readium.views import status_proxy


class RenewProxyPolicyParityTests(SimpleTestCase):
    def test_renew_proxy_consults_the_renewal_policy(self):
        source = inspect.getsource(status_proxy.RenewProxyView)

        self.assertIn(
            "evaluate_renew",
            source,
            "RenewProxyView must consult evaluate_renew — otherwise reading apps "
            "bypass the embargo, the reservation-queue check and the renewal cap.",
        )

    def test_renew_proxy_delegates_the_transition_to_the_service(self):
        source = inspect.getsource(status_proxy.RenewProxyView)

        self.assertIn(
            "LicenseService.renew_license",
            source,
            "RenewProxyView must delegate to LicenseService.renew_license so "
            "renewal_count is incremented identically to the portal path.",
        )

    def test_renew_proxy_does_not_forward_a_raw_renew_to_the_lsd(self):
        """The view may only GET the status doc; the transition belongs to the service.

        A bare `http_requests.put(...)` here means the policy gate above it can
        be sidestepped by whatever the reader passes through.
        """
        source = inspect.getsource(status_proxy.RenewProxyView)

        self.assertNotIn(
            "http_requests.put",
            source,
            "RenewProxyView must not PUT the Status Server directly; delegate to "
            "LicenseService.renew_license so the policy and renewal_count apply.",
        )

    def test_return_proxy_refetches_by_resolved_pk_not_url_kwarg(self):
        """Links in a signed `.lcpl` carry the LCP id, not our pk.

        `_get_license` resolves either, but a follow-up `get(pk=license_id)`
        would 500 whenever a reading app arrives via the LCP id.
        """
        source = inspect.getsource(status_proxy.ReturnProxyView)

        self.assertNotIn(
            "select_for_update().get(pk=license_id)",
            source,
            "Re-fetch by license_obj.pk — license_id may be the LCP license id.",
        )
        self.assertIn("select_for_update().get(pk=license_obj.pk)", source)
