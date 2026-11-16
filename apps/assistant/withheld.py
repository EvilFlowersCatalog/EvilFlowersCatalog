"""The `access` frame (#186): a concrete next step instead of unlock advice the model would have to guess.

When a tool of the turn could not hand the model a publication's text (Readium LCP, outside the IP ranges, the
content policy) or the reader's reading budget cut it short, the place that refused notes the publication on the
request (`note`). The turn then sends one `access` frame per publication (`frames`):

```json
{"entry_id": "…", "title": "…", "tier": "outline", "basis": "none", "reason": "lcp", "how_to_unlock": ["borrow"]}
```

The portal renders a card with the matching action (borrow, open the detail, sign in) next to the answer, so the
model never explains licensing in prose. `how_to_unlock` are the content gate's ways (`borrow`, `campus_network`,
`local_processor`), with `sign_in` first for a signed-out reader who could borrow.
"""

from typing import Dict, List, Set

#: Why a publication was noted: its text was refused, or the reading budget cut it short.
WITHHELD = "withheld"
BUDGET = "budget"

_ATTRIBUTE = "_evilflowers_assistant_withheld"


def note(request, entry_id, cause: str = WITHHELD) -> None:
    """Remember that this turn could not give the model this publication's text."""
    if request is None or not entry_id:
        return
    noted: Dict[str, str] = getattr(request, _ATTRIBUTE, None)
    if noted is None:
        noted = {}
        try:
            setattr(request, _ATTRIBUTE, noted)
        except AttributeError:
            return
    noted.setdefault(str(entry_id), cause)


def frames(request, sent: Set[str]) -> List[dict]:
    """`access` frames for the publications noted since the last call and not sent yet in this turn (`sent`)."""
    from apps.content import access
    from apps.core.models import Entry

    noted: Dict[str, str] = getattr(request, _ATTRIBUTE, None) or {}
    pending = {entry_id: cause for entry_id, cause in noted.items() if entry_id not in sent}
    # Consumed: a request reused for several turns (the evaluation) starts each one clean.
    noted.clear()
    if not pending:
        return []
    found = []
    for entry in Entry.objects.filter(pk__in=list(pending)):
        entry_id = str(entry.pk)
        sent.add(entry_id)
        decision = access.decide(request, entry, surface=access.Surface.ASSISTANT)
        if not decision.visible:
            continue
        budget = pending[entry_id] == BUDGET
        if decision.allows(access.Tier.PASSAGE) and not budget:
            # Refused for a reason the gate does not hold against the reader (a page outside their copy).
            continue
        ways = list(decision.how_to_unlock)
        if "borrow" in ways and not request.user.is_authenticated:
            ways.insert(0, "sign_in")
        found.append(
            {
                "entry_id": entry_id,
                "title": entry.title,
                "tier": decision.tier,
                "basis": decision.basis,
                "reason": access.Reason.BUDGET if budget else (decision.reason or None),
                "how_to_unlock": ways,
            }
        )
    return found
