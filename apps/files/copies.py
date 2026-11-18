"""
Whose copy, rendered how (IP-036 Phase 2 and 5).

One place decides what a personalised copy is, so the download view and the pre-render task produce the same
stored file under the same name:

- **Scope** (`EVILFLOWERS_PERSONALISED_COPY_SCOPE`):
  - `reader` (default): a copy belongs to the reader who opens it, whatever grant they open it through. It lives
    in `personalised/<user_id>/` and its watermark carries the reader and a **copy id** (the copy's digest), not
    a grant id. Re-opens through other grants of the same reader reuse it.
  - `grant` (before IP-036): a copy per grant, in `personalised/<grant id>/`, watermarked with the grant id.
- **Reader**: the signed-in requester; for a personal grant that is its owner (the access check made sure), for
  a shared grant whoever opens it (IP-036 Q6). An anonymous recipient of a shared grant gets no stored copy: it is
  rendered per request with the owner's data and a random id, as before.
- **Annotations** (`?annotations=true`): under `reader` scope the reader's annotations on every grant of this
  acquisition with the same page range (pages index the generated document, so other ranges would land on the
  wrong pages); under `grant` scope this grant's.
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.db import transaction
from django.utils.module_loading import import_string

from apps.core.fields.multirange import depack
from apps.core.models import AnnotationItem, EntryAuthor, User, UserAcquisition
from apps.files import personalised

logger = logging.getLogger(__name__)

READER = "reader"
GRANT = "grant"

#: Seconds between creating a grant and rendering it ahead: a GET that follows at once takes the lock first.
PRERENDER_COUNTDOWN = 15
#: One queued pre-render per grant.
PRERENDER_QUEUE_SECONDS = 10 * 60


def scope() -> str:
    return GRANT if settings.EVILFLOWERS_PERSONALISED_COPY_SCOPE == GRANT else READER


def copy_reader(user_acquisition: UserAcquisition, requester) -> Optional[User]:
    """The user a stored copy belongs to; None for an anonymous requester (rendered per request)."""
    if requester is None or not requester.is_authenticated:
        return None
    if scope() == GRANT:
        return user_acquisition.user
    return requester


def is_modified(user_acquisition: UserAcquisition) -> bool:
    return user_acquisition.acquisition.mime in settings.EVILFLOWERS_MODIFIERS


@dataclass
class Copy:
    """Everything that shapes one personalised copy, and how to render it."""

    user_acquisition: UserAcquisition
    reader: Optional[User]
    page: Optional[str]
    annotation_map: Dict[int, List[str]]
    context: dict
    #: The stored copy's name; None when it is rendered per request (anonymous).
    name: Optional[str] = None
    _modifier: object = field(default=None, repr=False)

    @property
    def copy_id(self) -> str:
        return self.context.get("id", "")

    def render(self) -> File:
        """Render the copy: from the file's shared normalised base when there is one (#214)."""
        acquisition = self.user_acquisition.acquisition
        source, normalised = personalised.render_source(acquisition, self._modifier)
        try:
            return self._modifier.generate(
                source, self.page, annotation_map=self.annotation_map, normalised=normalised
            )
        finally:
            if normalised:
                source.close()

    def ensure(self) -> bool:
        """Make the stored copy exist (rendered once, under a lock). False when it could not be waited for."""
        if self.name is None:
            return False
        return personalised.ensure_copy(self.user_acquisition.acquisition.content.storage, self.name, self.render)


def _authors(user_acquisition: UserAcquisition) -> str:
    # In the order of the entry: the copy's name hashes this string, so it must not depend on query plans.
    names = (
        EntryAuthor.objects.filter(entry_id=user_acquisition.acquisition.entry_id)
        .select_related("author")
        .order_by("position")
    )
    return ", ".join(item.author.full_name for item in names)


def _annotations(user_acquisition: UserAcquisition, reader: Optional[User]) -> Dict[int, List[str]]:
    if reader is not None and scope() == READER:
        same_range = (
            {"annotation__user_acquisition__range__isnull": True}
            if user_acquisition.range is None
            else {"annotation__user_acquisition__range": user_acquisition.range}
        )
        items = AnnotationItem.objects.filter(
            annotation__user_acquisition__user=reader,
            annotation__user_acquisition__acquisition_id=user_acquisition.acquisition_id,
            **same_range,
        )
    else:
        items = AnnotationItem.objects.filter(annotation__user_acquisition=user_acquisition)
    annotation_map = defaultdict(list)
    for item in items.order_by("annotation__created_at", "created_at").values("page", "content"):
        annotation_map[item["page"]].append(item["content"])
    return dict(annotation_map)


def prepare(
    user_acquisition: UserAcquisition, requester, *, page: Optional[str] = None, annotations: bool = False
) -> Copy:
    """The copy `requester` gets of `user_acquisition` (the access check has passed already)."""
    reader = copy_reader(user_acquisition, requester)
    entry = user_acquisition.acquisition.entry
    marked = reader if reader is not None else user_acquisition.user
    context = {
        "user_id": str(marked.pk),
        "title": entry.title,
        "username": marked.username,
        "authors": _authors(user_acquisition),
        "language": entry.language.alpha2 if entry.language_id else None,
    }
    annotation_map = _annotations(user_acquisition, reader) if annotations else {}

    name = None
    if reader is None:
        # Anonymous: rendered per request; a random id, as before IP-036.
        context["id"] = str(uuid.uuid4())
    elif scope() == GRANT:
        context["id"] = str(user_acquisition.pk)
        name = personalised.copy_name(user_acquisition, context, page, annotation_map)
    else:
        # The copy id is the digest of everything else, so it is known before the render and printed in it.
        name = personalised.copy_name(user_acquisition, context, page, annotation_map, reader=reader)
        context["id"] = personalised.copy_digest(name)

    modifier = import_string(settings.EVILFLOWERS_MODIFIERS[user_acquisition.acquisition.mime])(
        context=context,
        pages=depack(user_acquisition.range) if user_acquisition.range else None,
    )
    return Copy(
        user_acquisition=user_acquisition,
        reader=reader,
        page=page,
        annotation_map=annotation_map,
        context=context,
        name=name,
        _modifier=modifier,
    )


# --- pre-render (IP-036 Phase 5) --------------------------------------------------------------------------------


def queue_prerender(user_acquisition: UserAcquisition) -> None:
    """Render the copy of a new personal grant ahead of its first GET, in a worker. Best effort.

    Only personal grants of a modified (PDF) acquisition, with `EVILFLOWERS_PERSONALISED_PRERENDER` on. Queued
    after commit with a countdown, so a GET that follows the grant at once takes the render lock itself; the task
    then finds the copy ready.
    """
    if not settings.EVILFLOWERS_PERSONALISED_PRERENDER:
        return
    if user_acquisition.type != UserAcquisition.UserAcquisitionType.PERSONAL:
        return
    if user_acquisition.acquisition.mime not in settings.EVILFLOWERS_MODIFIERS:
        return
    if not cache.add(f"personalised-prerender:{user_acquisition.pk}", 1, PRERENDER_QUEUE_SECONDS):
        return

    grant_id = str(user_acquisition.pk)

    def send():
        from apps.tasks.tasks import prerender_personalised_copy

        try:
            prerender_personalised_copy.apply_async(args=[grant_id], countdown=PRERENDER_COUNTDOWN, retry=False)
        except Exception:  # the first GET renders instead
            logger.warning("personalised.prerender_queue_failed grant=%s", grant_id, exc_info=True)

    transaction.on_commit(send)


def prerender(user_acquisition: UserAcquisition) -> bool:
    """The worker's half: render the default copy (whole grant, no `?page=`, no annotations) if missing."""
    if user_acquisition.is_expired or not is_modified(user_acquisition):
        return False
    if not user_acquisition.acquisition.content:
        return False
    copy = prepare(user_acquisition, user_acquisition.user)
    return copy.ensure()


__all__ = ["Copy", "GRANT", "READER", "copy_reader", "prepare", "prerender", "queue_prerender", "scope"]
