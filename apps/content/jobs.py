"""Job kinds of the content app (IP-031): the disclosure log's pruning (#175) and Crossref licences (#177)."""

from apps.tasks.contract import maintenance

maintenance(
    "maintenance.prune_content_disclosures",
    "apps.tasks.tasks.prune_content_disclosures",
    "Prune the disclosure log",
    "Drop content disclosures past their policy's log.retention_days, never inside a budget window (#175).",
)
maintenance(
    "maintenance.content_crossref_rights",
    "apps.content.tasks.refresh_crossref_rights",
    "Crossref licences",
    "Fill entry.rights from Crossref for entries with a DOI and no rights yet (#177).",
)
