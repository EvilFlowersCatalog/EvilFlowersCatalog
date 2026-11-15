"""What the REST views and the MCP tools of #174 share: permissions, assignment and `explain`.

Q2 (#170): a superuser creates and edits policies; a catalog `manage` user only
assigns existing ones to the catalog and its entries.
"""

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import List, Optional

from django.db import transaction
from object_checker.base_object_checker import has_object_permission

from apps.content import access, policies
from apps.core.models import Catalog, Entry, User

logger = logging.getLogger(__name__)


class Refused(Exception):
    """A request the caller may not make (`status` 403) or that names nothing (`status` 404)."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message = message
        self.status = status


def require_superuser(user) -> None:
    if not getattr(user, "is_superuser", False):
        raise Refused(
            "Creating and editing content policies is restricted to administrators (superusers). A catalog "
            "manager may assign an existing policy to the catalog and its entries."
        )


def policy_payload(policy: policies.Policy) -> dict:
    return {
        "key": policy.key,
        "version": policy.version,
        "ref": policy.ref,
        "title": policy.title,
        "builtin": policy.version == 0,
        "document": policy.document,
    }


@dataclass
class Assignment:
    policy: Optional[str]
    catalog: Optional[dict]
    entries: List[dict]

    def payload(self) -> dict:
        return {"policy": self.policy, "catalog": self.catalog, "entries": self.entries}


def _visible_entries(request, entry_ids) -> List[Entry]:
    from apps.api.filters.entries import EntryFilter

    queryset = Entry.objects.filter(pk__in=list(entry_ids)).select_related("catalog")
    return list(EntryFilter({}, queryset=queryset, request=request).qs)


def assign(request, key: Optional[str], catalog_id=None, entry_ids=(), dry_run: bool = False) -> Assignment:
    """Point a catalog and/or entries at policy `key`; `None` clears the assignment so they inherit.

    The caller needs `manage` on the catalog and on the catalog of every entry. All or nothing.
    """
    if catalog_id is None and not entry_ids:
        raise Refused("Name a catalog or entries to assign the policy to.", status=400)

    catalog = None
    if catalog_id is not None:
        from apps.api.filters.catalogs import CatalogFilter
        from django.http import QueryDict

        catalog = CatalogFilter(
            QueryDict(mutable=True), queryset=Catalog.objects.filter(pk=catalog_id), request=request
        ).qs.first()
        if catalog is None:
            raise Refused(f"Catalog {catalog_id} not found.", status=404)
        if not has_object_permission("check_catalog_manage", request.user, catalog):
            raise Refused(
                f"Insufficient permissions: assigning a content policy needs `manage` access on catalog "
                f"'{catalog.title}'."
            )

    entries = []
    if entry_ids:
        wanted = {str(entry_id) for entry_id in entry_ids}
        entries = _visible_entries(request, wanted)
        missing = sorted(wanted - {str(entry.pk) for entry in entries})
        if missing:
            raise Refused(f"Entries not found: {', '.join(missing[:10])}.", status=404)
        for entry in entries:
            if not has_object_permission("check_catalog_manage", request.user, entry.catalog):
                raise Refused(
                    f"Insufficient permissions: assigning a content policy needs `manage` access on catalog "
                    f"'{entry.catalog.title}' (entry {entry.pk})."
                )

    # After the permission checks: a caller who may not assign learns nothing about which policies exist.
    if key is not None:
        policies.require(key)

    catalog_report = None
    entry_reports = []
    with transaction.atomic():
        if catalog is not None:
            changed = catalog.content_policy != key
            catalog_report = {"id": str(catalog.pk), "previous": catalog.content_policy, "changed": changed}
            if changed and not dry_run:
                Catalog.objects.filter(pk=catalog.pk).update(content_policy=key)
        for entry in entries:
            config = dict(entry.config or {})
            previous = config.get("content_policy")
            entry_reports.append({"id": str(entry.pk), "previous": previous, "changed": previous != key})
            if previous == key or dry_run:
                continue
            if key is None:
                config.pop("content_policy", None)
            else:
                config["content_policy"] = key
            # A targeted update: the entry's save signals (search refresh, touch) have nothing to do here.
            Entry.objects.filter(pk=entry.pk).update(config=config)
    if not dry_run:
        logger.info(
            "content_policy.assign user=%s policy=%s catalog=%s entries=%s",
            request.user.pk,
            key,
            catalog.pk if catalog else None,
            len(entries),
        )
    return Assignment(key, catalog_report, entry_reports)


def explain(
    request,
    entry_id,
    user_id=None,
    surface: Optional[str] = None,
    processor: Optional[str] = None,
    policy_key: Optional[str] = None,
) -> dict:
    """The decision of the gate for one entry, step by step: basis, policy `key@version`, caps, final tier.

    `user_id` explains for another reader (superusers: it reveals their loans); the caller's own IP address
    stands in for theirs. `policy_key` shows what a policy would decide without assigning it.
    """
    # The caller must see the entry themself; otherwise it does not exist for them.
    found = _visible_entries(request, [entry_id])
    if not found:
        raise Refused(f"Entry {entry_id} not found.", status=404)
    entry = found[0]

    subject = request.user
    if user_id is not None and str(user_id) != str(request.user.pk):
        if not request.user.is_superuser:
            raise Refused("Explaining access for another user is restricted to administrators.")
        subject = User.objects.filter(pk=user_id).first()
        if subject is None:
            raise Refused(f"User {user_id} not found.", status=404)
        # Their user, the caller's address; a machine credential of theirs is not involved.
        request = SimpleNamespace(user=subject, META=request.META, GET={}, api_key=None)

    override = policies.require(policy_key) if policy_key else None
    surface = surface or access.Surface.REST
    decision = access.decide(request, entry, surface=surface, processor=processor, policy=override)
    return {**decision.explain(), "user_id": str(subject.pk), "surface": surface}


__all__ = ["Assignment", "Refused", "assign", "explain", "policy_payload", "require_superuser"]
