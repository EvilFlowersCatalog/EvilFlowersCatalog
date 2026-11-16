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


class DataverseNotFoundError(DataverseError):
    """The dataset or version does not exist (HTTP 404). Only this, never a network or 5xx failure, may count as
    "gone" for the reconcile (IP-034 Q6)."""


class DataverseStaleError(DataverseError):
    """`editMetadata` refused `sourceLastUpdateTime`: the dataset changed since it was read (IP-034 push)."""
