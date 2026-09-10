"""Exception types used across the Dataverse integration."""


class DataverseError(Exception):
    """Generic Dataverse integration failure."""


class DataverseTransientError(DataverseError):
    """A transient failure worth retrying (network glitch, 404 race window,
    upstream temporarily unavailable). Celery autoretry hooks treat this
    class specially.
    """


class DataverseAuthError(DataverseError):
    """Authentication failure — wrong/missing token, secret mismatch."""


class DataverseConfigError(DataverseError):
    """Misconfiguration (missing env var, malformed setting)."""
