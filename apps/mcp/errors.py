"""Failures a tool reports back to the model rather than to the transport."""

from django.utils.translation import gettext as _


class ToolError(Exception):
    """An expected, explainable failure — bad argument, missing record, no access.

    The server turns this into a `tools/call` result with `isError: true` so the
    model reads the message and can correct itself. Anything that is *not* a
    `ToolError` is a bug: it is logged with a stack trace and reported to the
    model as a generic internal error.
    """


class ToolNotFound(ToolError):
    """A record the caller named does not exist, or is outside their boundary.

    Deliberately one message for both cases. Distinguishing them would let a
    caller probe for the existence of records in catalogs they cannot read.
    """

    def __init__(self, resource: str, identifier: str):
        super().__init__(
            _("%(resource)s '%(id)s' does not exist or is not readable with these credentials.")
            % {"resource": resource, "id": identifier}
        )


class ToolPermissionDenied(ToolError):
    """The caller is authenticated and the record exists, but they may not do this."""

    def __init__(self, action: str, resource: str):
        super().__init__(
            _("Insufficient permissions to %(action)s %(resource)s — this requires `manage` access on the catalog.")
            % {"action": action, "resource": resource}
        )


class ToolConflict(ToolError):
    """The write would violate a uniqueness rule the catalog enforces."""


class ToolValidationError(ToolError):
    """A payload the underlying Django form rejected.

    Field errors are flattened into one message so the model gets the whole
    picture in a single turn instead of fixing one field per round-trip.
    """

    def __init__(self, form):
        problems = []
        for item in form.errors:
            path = ".".join(str(part) for part in (getattr(item, "path", None) or []))
            message = item.message % (item.params or ()) if getattr(item, "params", None) else item.message
            problems.append(f"{path}: {message}" if path else str(message))

        super().__init__(_("Invalid arguments — %(problems)s") % {"problems": "; ".join(problems)})


__all__ = ["ToolConflict", "ToolError", "ToolNotFound", "ToolPermissionDenied", "ToolValidationError"]
