"""
Readium lifecycle vocabulary.

`LicenseAction` is the *verb* enum — the set of transitions a caller
can request via `PUT /readium/v1/licenses/{id}`. It is intentionally
separate from `LicenseState` (the *resting* state of the License row)
because the two are not interchangeable: a successful renewal leaves
the license in state `active`, not `renewed`.

Lives at module level (not nested on `License`) so forms / services /
serializers can import it without dragging in the `apps.readium.models`
import graph — `License` itself depends on `apps.core.models`, and the
form layer needs the enum before the model module is safe to import in
some contexts (Django form classes are constructed at import time).
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class LicenseAction(models.TextChoices):
    ACTIVATE = "active", _("Activate")
    RETURN = "returned", _("Return")
    RENEW = "renewed", _("Renew")
    REVOKE = "revoked", _("Revoke")
    CANCEL = "cancelled", _("Cancel")
