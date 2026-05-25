"""Unit tests for `apps.dataverse.services.router.CatalogRouter`."""

import json

import pytest
from django.test import override_settings

from apps.dataverse.exceptions import DataverseConfigError
from apps.dataverse.services.router import CatalogRouter, MatchedRule


def _make_catalog(url_name: str):
    """Create a minimal Catalog row for routing tests.

    Uses model_bakery if available, falls back to direct ORM create.
    """
    from apps.core.models import Catalog, User

    user, _ = User.objects.get_or_create(
        username=f"system-{url_name}",
        defaults={"name": "Sys", "surname": "User", "auth_source_id": _ensure_auth_source().pk},
    )
    return Catalog.objects.create(
        creator=user,
        title=f"Catalog {url_name}",
        url_name=url_name,
    )


def _ensure_auth_source():
    from apps.core.models import AuthSource

    src, _ = AuthSource.objects.get_or_create(name="local", defaults={"driver": "local"})
    return src


@pytest.mark.django_db
class TestCatalogRouter:
    def test_fallback_first_catalog_when_no_rules(self):
        catalog = _make_catalog("stu")
        router = CatalogRouter()
        decision = router.resolve(dataset_id="42", global_id="doi:10.5072/STU/X")
        assert decision.catalog.pk == catalog.pk
        assert decision.matched_rule == MatchedRule.FALLBACK_FIRST_CATALOG

    @override_settings(EVILFLOWERS_DATAVERSE_CATALOG_MAP=json.dumps({"42": "comenius"}))
    def test_explicit_map(self):
        _make_catalog("stu")
        target = _make_catalog("comenius")
        router = CatalogRouter()
        decision = router.resolve(dataset_id="42", global_id="doi:10.5072/STU/X")
        assert decision.catalog.pk == target.pk
        assert decision.matched_rule == MatchedRule.EXPLICIT_MAP
        assert decision.match_input == "42"

    @override_settings(
        EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES=json.dumps(
            [
                {"prefix": "doi:10.5072/STU/", "catalog": "stu"},
                {"prefix": "doi:10.5072/COM/", "catalog": "comenius"},
            ]
        )
    )
    def test_global_prefix(self):
        stu = _make_catalog("stu")
        _make_catalog("comenius")
        router = CatalogRouter()
        decision = router.resolve(dataset_id="42", global_id="doi:10.5072/STU/ABC")
        assert decision.catalog.pk == stu.pk
        assert decision.matched_rule == MatchedRule.GLOBAL_PREFIX
        assert decision.match_input == "doi:10.5072/STU/"

    @override_settings(
        EVILFLOWERS_DATAVERSE_GLOBAL_ID_PREFIXES=json.dumps(
            [
                {"prefix": "doi:", "catalog": "default"},
                {"prefix": "doi:10.5072/STU/", "catalog": "stu"},
            ]
        )
    )
    def test_longest_prefix_wins(self):
        _make_catalog("default")
        stu = _make_catalog("stu")
        router = CatalogRouter()
        decision = router.resolve(dataset_id=None, global_id="doi:10.5072/STU/ABC")
        assert decision.catalog.pk == stu.pk

    def test_env_default(self, monkeypatch):
        _make_catalog("first")
        target = _make_catalog("env-default")
        monkeypatch.setenv("DATAVERSE_CATALOG_URL_NAME", "env-default")

        router = CatalogRouter()
        decision = router.resolve(dataset_id="999", global_id="urn:unknown:X")
        assert decision.catalog.pk == target.pk
        assert decision.matched_rule == MatchedRule.ENV_DEFAULT

    def test_no_catalogs_raises(self):
        router = CatalogRouter()
        with pytest.raises(DataverseConfigError):
            router.resolve(dataset_id="1", global_id="X")

    @override_settings(EVILFLOWERS_DATAVERSE_CATALOG_MAP='{"not": "valid')
    def test_invalid_json_raises(self):
        _make_catalog("a")
        router = CatalogRouter()
        with pytest.raises(DataverseConfigError):
            router.resolve(dataset_id="not", global_id="X")
