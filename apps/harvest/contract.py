"""What another app imports to contribute a harvest connector (IP-034), and nothing else from `apps.harvest`:

from apps.harvest.contract import Connector, PushResult, SourceRecord, StaleVersion, registry
"""

from apps.harvest.adapters.registry import Connector, PushResult, Reader, StaleVersion, registry
from apps.harvest.records import UDC_SCHEME, SourceAuthor, SourceRecord, SourceSubject

__all__ = [
    "Connector",
    "PushResult",
    "Reader",
    "SourceAuthor",
    "SourceRecord",
    "SourceSubject",
    "StaleVersion",
    "UDC_SCHEME",
    "registry",
]
