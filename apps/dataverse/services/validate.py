"""IP-034 Q9: the pre-publish check of `book` datasets, called by a `PrePublishDataset` `http/sr` step.

The step's `expectedResponse` is `OK.*`, so anything else stops the publish. Pre-publish sees the draft, so the
check reads the dataset's `latestVersion` (the draft being published), or `:draft` when that is missing. The rules
are the connector's (`DataverseConnector.validate`, i.e. `mapper.book_problems`).

Fails closed: a dataset Elvira cannot read is refused (`FAILED: could not read the dataset …`), because an
unchecked book could otherwise be published with a bad ISBN. A dataset of another type, or from a Dataverse
without dataset types (< 6.4), is not Elvira's to judge and answers OK.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from apps.dataverse.exceptions import DataverseError
from apps.dataverse.services.client import DataverseClient
from apps.dataverse.services.mapper import DATASET_TYPE_BOOK, dataset_to_source_record, dataset_type

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: Optional[str] = None

    @property
    def text(self) -> str:
        return "OK" if self.ok else f"FAILED: {self.reason}"


def validate_dataset(client: DataverseClient, dataset_id: str, source=None) -> Verdict:
    from apps.dataverse.connector import DataverseConnector

    try:
        dataset = client.get_dataset(dataset_id) or {}
    except DataverseError as exc:
        logger.warning("dataverse.validate dataset=%s unreadable: %s", dataset_id, exc)
        return Verdict(False, f"could not read the dataset in Elvira ({type(exc).__name__})")
    kind = dataset_type(dataset)
    if kind != DATASET_TYPE_BOOK:
        return Verdict(True)
    version = dataset.get("latestVersion")
    if not version or not version.get("metadataBlocks"):
        try:
            version = client.get_version(dataset_id, ":draft")
        except DataverseError as exc:
            logger.warning("dataverse.validate dataset=%s draft unreadable: %s", dataset_id, exc)
            return Verdict(False, f"could not read the draft in Elvira ({type(exc).__name__})")
    record = dataset_to_source_record(version or {}, dataset_id=dataset_id)
    record.extra["dataset_type"] = kind
    problems = DataverseConnector().validate(source, record)
    return Verdict(not problems, "; ".join(problems) or None)


__all__ = ["Verdict", "validate_dataset"]
