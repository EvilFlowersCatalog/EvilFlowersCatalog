"""The one gate in front of every surface that shows a publication's content (#172).

`decide(request, entry)` answers how much of an entry's content this caller may see
here, as a *tier*:

    metadata < outline < snippet < passage < full

and a receipt saying why. Every surface asks it instead of writing out the rule
again: the assistant's reading tools and bound chats, citations, the REST
extraction, figure and locator endpoints, the graph's headings, and later the MCP
content tools (#176). A surface states what it is about to show
(`disclosure.allows(Tier.PASSAGE)`) and keeps its own wording for a refusal.

The decision, in order:

1. **Visibility** — the catalog ACL (`EntryFilter`). A hidden entry is not found;
   nothing below is evaluated. A grant may survive hiding when the policy says so.
2. **Policy** — entry → catalog → instance → `parity` (`apps.content.policies`).
3. **Basis** — `open` (not lent through Readium LCP), `loan` (a live LCP licence in
   `grants.license_states`, or an own copy of a type in `grants.acquisition_types`
   of an entry without LCP), `past_loan` (an ended licence the policy lists) or
   `none`. The basis whose tier is highest wins.
4. **Caps** — outside the entry's IP ranges, the caller's processor (an API key's
   `processor`; the in-app assistant is `local`), a TDM reservation, model-written
   text. Each can only lower the tier.
5. **Budget** — what the policy allows a basis to read in a window. Carried on the
   disclosure; enforced by #175.

`open` deliberately means only "no LCP": whether a stored PDF exists, and whether
the acquisition is lent or restricted, stays with `ReaderDocument.for_open_access`
— a passage from an extraction run is readable without one.
"""

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence

from apps.content import policies
from apps.content.policies import TIERS
from evil_flowers_catalog import tracing

logger = logging.getLogger(__name__)


class Tier:
    METADATA = "metadata"
    OUTLINE = "outline"
    SNIPPET = "snippet"
    PASSAGE = "passage"
    FULL = "full"


def rank(tier: Optional[str]) -> int:
    return TIERS.index(tier) if tier in TIERS else -1


class Surface:
    """Where the content is shown. The in-app assistant is a local processor; REST and MCP callers
    authenticated by an API key are whatever the key declares; anything else is a person in the portal."""

    ASSISTANT = "assistant"
    REST = "rest"
    MCP = "mcp"
    GRAPH = "graph"


class Basis:
    OPEN = "open"
    LOAN = "loan"
    PAST_LOAN = "past_loan"
    NONE = "none"


class Reason:
    """What held the tier below `full` (or hid the entry); empty when nothing did."""

    HIDDEN = "hidden"
    LCP = "lcp"
    OUTSIDE_IP = "outside_ip"
    PROCESSOR = "processor"
    TDM_RESERVED = "tdm_reserved"
    GENERATED = "generated"
    POLICY = "policy"


#: Licence states that are a loan right now; the others the policy lists are a past loan.
LIVE_LICENSE_STATES = ("ready", "active")


@dataclass
class Disclosure:
    entry_id: str
    visible: bool
    tier: Optional[str]
    basis: Optional[str]
    reason: str
    how_to_unlock: List[str]
    policy: str
    policy_source: str
    budget: Optional[dict]
    #: Facts the surfaces still word their refusals by (see `open_access_document`, `EntryFigures`).
    lcp: bool = False
    inside_ip: bool = True
    processor: Optional[str] = None
    caps: List[dict] = field(default_factory=list)
    _ip_error: Optional[Exception] = field(default=None, repr=False)

    def allows(self, tier: str) -> bool:
        return self.visible and rank(self.tier) >= rank(tier)

    def receipt(self) -> dict:
        """What a surface that renders the decision shows: `{tier, basis, reason, how_to_unlock, policy}`."""
        return {
            "tier": self.tier,
            "basis": self.basis,
            "reason": self.reason or None,
            "how_to_unlock": self.how_to_unlock,
            "policy": f"policy:{self.policy}",
        }

    def explain(self) -> dict:
        return {
            **self.receipt(),
            "entry_id": self.entry_id,
            "visible": self.visible,
            "policy_source": self.policy_source,
            "lcp": self.lcp,
            "inside_ip": self.inside_ip,
            "processor": self.processor,
            "caps": self.caps,
            "budget": self.budget,
        }

    def capped_below(self, name: str, tier: str) -> bool:
        """Whether cap `name` applied and on its own keeps this caller below `tier`."""
        return any(cap["cap"] == name and rank(cap["tier"]) < rank(tier) for cap in self.caps)

    def raise_outside_ip(self, tier: str) -> None:
        """Re-raise the IP check's own 403 when the IP cap keeps the caller below `tier`, for surfaces that
        answer an IP block with an error rather than with less content."""
        if self._ip_error is not None and self.capped_below(Reason.OUTSIDE_IP, tier):
            raise self._ip_error


def _hidden(entry_id) -> Disclosure:
    return Disclosure(
        entry_id=str(entry_id),
        visible=False,
        tier=None,
        basis=None,
        reason=Reason.HIDDEN,
        how_to_unlock=[],
        policy="",
        policy_source="",
        budget=None,
    )


def processor_of(request, surface: str) -> Optional[str]:
    """`local` | `external` for a machine caller; None for a person in the portal (no processor cap)."""
    if surface == Surface.ASSISTANT:
        return "local"
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        return None
    if api_key.processor:
        return api_key.processor
    from apps.core import settings_store

    return settings_store.get("content.external_processor_default")


def _ip_check(request, entry):
    """None when the caller is inside the entry's ranges (or it has none), else the 403 the check raised."""
    from apps.core.errors import ProblemDetailException
    from apps.files.views import _check_ip_block

    try:
        _check_ip_block(request, entry)
    except ProblemDetailException as error:
        return error
    return None


def _user(request):
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


class _Facts:
    """The per-request lookups a batch shares: one query each, only when some entry needs it."""

    def __init__(self, request, entries: Sequence):
        self.request = request
        self.user = _user(request)
        self.entries = entries
        self._licenses = None
        self._copies = None
        self._members = None
        self._catalogs = None
        self._resolver = None

    def resolver(self) -> policies.Resolver:
        """The policy map and the instance default, read once for the whole batch."""
        if self._resolver is None:
            self._resolver = policies.Resolver()
        return self._resolver

    def catalog(self, entry):
        """The entry's catalog as far as policy resolution needs it (its `content_policy`), one query a batch."""
        if self._catalogs is None:
            from apps.core.models import Catalog

            ids = {item.catalog_id for item in self.entries}
            self._catalogs = {
                pk: SimpleNamespace(content_policy=key)
                for pk, key in Catalog.objects.filter(pk__in=ids).values_list("pk", "content_policy")
            }
        return self._catalogs.get(entry.catalog_id)

    def licenses(self) -> Dict[str, List[tuple]]:
        if self._licenses is None:
            self._licenses = {}
            lcp_ids = [entry.pk for entry in self.entries if entry.read_config("readium_enabled")]
            if self.user is not None and lcp_ids:
                from apps.readium.models import License

                for entry_id, state, devices in License.objects.filter(
                    user=self.user, entry_id__in=lcp_ids
                ).values_list("entry_id", "state", "device_count"):
                    self._licenses.setdefault(str(entry_id), []).append((state, devices))
        return self._licenses

    def copies(self) -> Dict[str, set]:
        if self._copies is None:
            self._copies = {}
            if self.user is not None:
                from apps.core.models import UserAcquisition

                rows = UserAcquisition.objects.filter(
                    user=self.user, acquisition__entry_id__in=[entry.pk for entry in self.entries]
                ).values_list("acquisition__entry_id", "type")
                for entry_id, kind in rows:
                    self._copies.setdefault(str(entry_id), set()).add(kind)
        return self._copies

    def members(self) -> set:
        """Catalog ids the caller belongs to."""
        if self._members is None:
            self._members = set()
            if self.user is not None:
                from apps.core.models import UserCatalog

                self._members = {
                    str(catalog_id)
                    for catalog_id in UserCatalog.objects.filter(
                        user=self.user, catalog_id__in={entry.catalog_id for entry in self.entries}
                    ).values_list("catalog_id", flat=True)
                }
        return self._members


def _grants(entry, policy: policies.Policy, facts: _Facts) -> List[str]:
    """The bases this caller holds for `entry` besides `open`/`none`."""
    grants = policy.grants
    found = []
    if entry.read_config("readium_enabled"):
        states = set(grants["license_states"])
        for state, devices in facts.licenses().get(str(entry.pk), []):
            if state in states:
                found.append(Basis.LOAN if state in LIVE_LICENSE_STATES else Basis.PAST_LOAN)
            elif state == "expired" and grants["expired_if_opened"] and devices:
                found.append(Basis.PAST_LOAN)
    else:
        # An own copy grants a loan only of an entry without LCP: an LCP title is lent by licence.
        if facts.copies().get(str(entry.pk), set()) & set(grants["acquisition_types"]):
            found.append(Basis.LOAN)
    return found


def _decide_one(
    entry,
    visible: bool,
    facts: _Facts,
    surface: str,
    processor: Optional[str],
    generated: bool,
    override: Optional[policies.Policy] = None,
) -> Disclosure:
    if override is not None:
        resolution = policies.Resolution(override, "override")
    else:
        resolution = facts.resolver().resolve(entry, facts.catalog(entry))
    policy = resolution.policy
    lcp = bool(entry.read_config("readium_enabled"))

    granted = _grants(entry, policy, facts)
    if not visible and not (granted and policy.grants["survive_hidden"]):
        return _hidden(entry.pk)

    bases = granted + [Basis.NONE if lcp else Basis.OPEN]
    basis = max(bases, key=lambda name: rank(policy.tier(name)))
    tier = policy.tier(basis)
    reason = Reason.LCP if lcp and basis == Basis.NONE else ""
    how_to_unlock = []
    if lcp and basis == Basis.NONE and rank(policy.tier(Basis.LOAN)) > rank(tier):
        how_to_unlock.append("borrow")
    if basis not in (Basis.OPEN, Basis.LOAN) and rank(tier) < rank(Tier.FULL) and not reason:
        reason = Reason.POLICY

    caps = []

    def cap(name: str, limit: str, unlock: Optional[str] = None):
        nonlocal tier, reason
        caps.append({"cap": name, "tier": limit})
        if rank(limit) < rank(tier):
            tier = limit
            reason = name
            if unlock:
                how_to_unlock.append(unlock)

    ip_error = _ip_check(facts.request, entry)
    inside_ip = ip_error is None
    if not inside_ip and policy.grants["member_counts_as_inside_ip"] and str(entry.catalog_id) in facts.members():
        inside_ip = True
    if not inside_ip:
        cap(Reason.OUTSIDE_IP, policy.caps["outside_ip"], "campus_network")
    if processor is not None:
        cap(Reason.PROCESSOR, policy.caps["processor"][processor], "local_processor")
    if getattr(getattr(entry, "rights", None), "tdm_reservation", None) == 1:
        cap(Reason.TDM_RESERVED, policy.caps["tdm_reserved"])
    if generated:
        cap(Reason.GENERATED, policy.caps["generated"])

    budget = policy.budget if policy.budget and basis in policy.budget["bases"] else None
    return Disclosure(
        entry_id=str(entry.pk),
        visible=True,
        tier=tier,
        basis=basis,
        reason=reason,
        how_to_unlock=how_to_unlock,
        policy=policy.ref,
        policy_source=resolution.source,
        budget=budget,
        lcp=lcp,
        inside_ip=inside_ip,
        processor=processor,
        caps=caps,
        _ip_error=ip_error,
    )


def _visible_ids(request, entries) -> set:
    from apps.api.filters.entries import EntryFilter
    from apps.core.models import Entry

    queryset = Entry.objects.filter(pk__in=[entry.pk for entry in entries])
    return {str(pk) for pk in EntryFilter({}, queryset=queryset, request=request).qs.values_list("pk", flat=True)}


def decide_many(
    request,
    entries: Iterable,
    surface: str = Surface.REST,
    *,
    visible: Optional[bool] = None,
    processor: Optional[str] = None,
    generated: bool = False,
    policy: Optional[policies.Policy] = None,
) -> Dict[str, Disclosure]:
    """`decide` for several entries with shared lookups; keyed by entry id (str).

    `visible=True` skips the ACL query when the caller has already established that the reader may see
    these entries by another path — a reader's copy whose download checks it passed. `processor` and
    `policy` override the caller's own processor and the resolved policy (`explain` uses both).
    """
    entries = [entry for entry in entries if entry is not None]
    if not entries:
        return {}
    visible_ids = {str(entry.pk) for entry in entries} if visible else _visible_ids(request, entries)
    facts = _Facts(request, entries)
    processor = processor or processor_of(request, surface)
    with tracing.span(
        "content.decide", {"evilflowers.content.surface": surface, "evilflowers.content.entries": len(entries)}
    ) as span:
        decisions = {
            str(entry.pk): _decide_one(
                entry, str(entry.pk) in visible_ids, facts, surface, processor, generated, override=policy
            )
            for entry in entries
        }
        if len(decisions) == 1:
            decision = next(iter(decisions.values()))
            span.set_attribute(tracing.ENTRY_ID, decision.entry_id)
            span.set_attribute("evilflowers.content.tier", decision.tier or "hidden")
            span.set_attribute("evilflowers.content.basis", decision.basis or "")
            span.set_attribute("evilflowers.content.policy", decision.policy)
    return decisions


def decide(
    request,
    entry,
    locator=None,
    surface: str = Surface.REST,
    *,
    visible: Optional[bool] = None,
    processor: Optional[str] = None,
    generated: bool = False,
    policy: Optional[policies.Policy] = None,
) -> Disclosure:
    """How much of `entry` this caller may see on `surface`. `locator` is accepted for the locator
    surfaces (#171); today a locator is disclosed exactly like its entry."""
    decisions = decide_many(
        request, [entry], surface, visible=visible, processor=processor, generated=generated, policy=policy
    )
    return decisions[str(entry.pk)]


def readable_ids(
    request, entries: Iterable, tier: str, surface: str = Surface.REST, *, visible: Optional[bool] = None
) -> set:
    """Ids (str) of the entries whose content this caller may see at `tier` here."""
    decisions = decide_many(request, entries, surface, visible=visible)
    return {entry_id for entry_id, decision in decisions.items() if decision.allows(tier)}


__all__ = [
    "Basis",
    "Disclosure",
    "Reason",
    "Surface",
    "Tier",
    "decide",
    "decide_many",
    "processor_of",
    "rank",
    "readable_ids",
]
