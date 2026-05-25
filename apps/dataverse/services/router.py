"""
Catalog routing for Dataverse pre-publish callbacks.

IP-008 Phase 4 D3 / Q3 resolution: implement the documented precedence
that the legacy view promised in a comment but never wired.

Precedence (first match wins):
  1. `settings.EVILFLOWERS_DATAVERSE_CATALOG_MAP` — explicit dataset_id
     → catalog url_name map.
  2. `settings.EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES` — list of
     `{prefix, catalog}` entries, longest prefix wins.
  3. `settings.EVILFLOWERS_DATAVERSE_CATALOG_URL_NAME` — deployment-wide
     default.
  4. First catalog by id — last-resort fallback, logged at ERROR.

The decision returns BOTH the catalog and the matched-rule name so the
caller can log at the right severity (INFO/WARNING/ERROR by step) and
operators can grep prepublish logs by `matched_rule`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from django.conf import settings

from apps.core.models import Catalog
from apps.dataverse.exceptions import DataverseConfigError

logger = logging.getLogger(__name__)


class MatchedRule(str, Enum):
    EXPLICIT_MAP = "explicit_map"
    GLOBAL_PREFIX = "global_prefix"
    ENV_DEFAULT = "env_default"
    FALLBACK_FIRST_CATALOG = "fallback_first_catalog"


@dataclass(frozen=True)
class RoutingDecision:
    catalog: Catalog
    matched_rule: MatchedRule
    match_input: Optional[str] = None  # dataset_id / matched prefix / env value


class CatalogRouter:
    """Resolves the catalog a Dataverse dataset belongs to."""

    SETTING_MAP = "EVILFLOWERS_DATAVERSE_CATALOG_MAP"
    SETTING_PREFIXES = "EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES"
    SETTING_DEFAULT = "EVILFLOWERS_DATAVERSE_CATALOG_URL_NAME"

    def resolve(self, *, dataset_id: Optional[str], global_id: Optional[str]) -> RoutingDecision:
        # 1) Explicit map by dataset_id
        if dataset_id is not None:
            decision = self._try_explicit_map(str(dataset_id))
            if decision is not None:
                logger.info(
                    "Dataverse routing matched explicit_map: dataset_id=%s -> catalog=%s",
                    dataset_id,
                    decision.catalog.url_name,
                )
                return decision

        # 2) Global id prefix
        if global_id:
            decision = self._try_global_prefix(global_id)
            if decision is not None:
                logger.info(
                    "Dataverse routing matched global_prefix: prefix=%s global_id=%s -> catalog=%s",
                    decision.match_input,
                    global_id,
                    decision.catalog.url_name,
                )
                return decision

        # 3) Settings default
        default_value = (getattr(settings, self.SETTING_DEFAULT, "") or "").strip()
        if default_value:
            catalog = Catalog.objects.filter(url_name=default_value).first()
            if catalog is not None:
                logger.warning(
                    "Dataverse routing fell through to env_default: %s=%s catalog=%s",
                    self.SETTING_DEFAULT,
                    default_value,
                    catalog.url_name,
                )
                return RoutingDecision(catalog, MatchedRule.ENV_DEFAULT, default_value)
            logger.warning(
                "Dataverse routing env_default %s=%s does not match any catalog",
                self.SETTING_DEFAULT,
                default_value,
            )

        # 4) Fallback: first catalog
        catalog = Catalog.objects.order_by("id").first()
        if catalog is None:
            raise DataverseConfigError("No catalog available to receive Dataverse dataset")
        logger.error(
            "Dataverse routing fell back to first catalog (no map/prefix/env match): "
            "dataset_id=%s global_id=%s catalog=%s — add a routing rule!",
            dataset_id,
            global_id,
            catalog.url_name,
        )
        return RoutingDecision(catalog, MatchedRule.FALLBACK_FIRST_CATALOG)

    # ----- precedence steps -----------------------------------------------

    def _load_map(self) -> Dict[str, str]:
        raw = getattr(settings, self.SETTING_MAP, None) or ""
        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DataverseConfigError(f"Invalid JSON in {self.SETTING_MAP}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise DataverseConfigError(
                f"{self.SETTING_MAP} must be a JSON object mapping dataset_id -> catalog url_name"
            )
        return {str(k): str(v) for k, v in parsed.items()}

    def _load_prefixes(self) -> List[Dict[str, str]]:
        raw = getattr(settings, self.SETTING_PREFIXES, None) or ""
        if not raw:
            return []
        if isinstance(raw, list):
            entries = raw
        else:
            try:
                entries = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DataverseConfigError(f"Invalid JSON in {self.SETTING_PREFIXES}: {exc}") from exc
        if not isinstance(entries, list):
            raise DataverseConfigError(
                f"{self.SETTING_PREFIXES} must be a JSON list of " '{"prefix": ..., "catalog": ...} entries'
            )
        normalized: List[Dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            prefix = str(entry.get("prefix") or "").strip()
            catalog_name = str(entry.get("catalog") or "").strip()
            if prefix and catalog_name:
                normalized.append({"prefix": prefix, "catalog": catalog_name})
        # Longest prefix wins on ties.
        normalized.sort(key=lambda item: len(item["prefix"]), reverse=True)
        return normalized

    def _try_explicit_map(self, dataset_id: str) -> Optional[RoutingDecision]:
        mapping = self._load_map()
        catalog_url_name = mapping.get(dataset_id)
        if not catalog_url_name:
            return None
        catalog = Catalog.objects.filter(url_name=catalog_url_name).first()
        if catalog is None:
            logger.warning(
                "Dataverse routing %s entry for dataset_id=%s -> %s does not match any catalog",
                self.SETTING_MAP,
                dataset_id,
                catalog_url_name,
            )
            return None
        return RoutingDecision(catalog, MatchedRule.EXPLICIT_MAP, dataset_id)

    def _try_global_prefix(self, global_id: str) -> Optional[RoutingDecision]:
        for entry in self._load_prefixes():
            prefix = entry["prefix"]
            if global_id.startswith(prefix):
                catalog = Catalog.objects.filter(url_name=entry["catalog"]).first()
                if catalog is None:
                    logger.warning(
                        "Dataverse routing prefix %s -> %s does not match any catalog",
                        prefix,
                        entry["catalog"],
                    )
                    continue
                return RoutingDecision(catalog, MatchedRule.GLOBAL_PREFIX, prefix)
        return None
