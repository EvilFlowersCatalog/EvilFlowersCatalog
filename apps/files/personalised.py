"""
Personalised copies served in byte ranges (#70).

A signed-in reader's PDF is personalised on the way out (`PDFModifier.generate`:
license page, QR watermark, metadata, annotations). Each render carries a new
`generated_at`, so two renders differ byte for byte and ranges taken from two
of them would stitch into a corrupt file. pdf.js asks for one book in dozens
of ranges, so the copy is rendered once, stored under `personalised/`, and
every range of that download is cut from the stored file.

- The name hashes everything that changes the bytes: the user acquisition,
  the source file, its page range, `?page=`, the annotations drawn in and the
  modifier context. An edited annotation names a new copy; the old one idles out.
- A cache marker says the copy is complete. It is set only after the save
  returned, so a file without one (a worker killed mid-write, a flushed cache)
  is rendered again rather than served. Every use refreshes the marker.
- One render per copy: a cache lock. A request that finds the lock taken waits
  for the copy instead of rendering one of its own.
- `cleanup_idle` (hourly beat job) deletes copies whose marker has expired,
  i.e. that no request used for `EVILFLOWERS_PERSONALISED_COPY_IDLE_HOURS`.
  Eviction counts from the last use, not the first, so a book open in the
  viewer is not replaced under the reader.

Time to the first byte (#214). The first request of a download waits for the
render, and most of a render is the same for every reader: the full rewrite
(`garbage=4`, `clean`, deflate, object streams, encryption stripped). With
`EVILFLOWERS_PERSONALISED_SHARED_BASE` that rewrite runs once per source file,
in a Celery worker after the file's first reader, and is stored as a *base*
(`personalised/_base/`, same marker, lock and eviction as a copy, idle time
`EVILFLOWERS_PERSONALISED_BASE_IDLE_HOURS`, no reader data in it). The first
reader is rendered in full as before: building the base on the request would
cost them a full render plus a write. A reader's copy is the base plus
the licence page, watermark and annotations, saved with light options
(`PDFModifier.LIGHT_SAVE_OPTIONS`): 2–3× faster on the books measured, at the
size of the full render. It is one revision, not an incremental update, so no
clean original sits inside the copy, and pages left out by a page range are
dropped (`garbage=1`).

A reading session asks for one copy dozens of times. Its first request (a GET
without `Range`) pins what it served (`pin`): name, size and validators. The
ranges that follow read the pin instead of recomputing the copy's name (the
authors query, a `stat` of the source, the annotation query) and the `stat`
of the copy. A pin lives `PIN_SECONDS` after its last use and is dropped with
the copy's marker, so a new open always re-resolves (an edited annotation shows).
"""

import datetime
import hashlib
import json
import logging
import time
from mimetypes import guess_extension
from typing import Callable, Optional, Tuple

import fitz
from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import Storage
from django.utils import timezone

from apps.core.models import Acquisition, UserAcquisition
from evil_flowers_catalog import tracing

logger = logging.getLogger(__name__)

PREFIX = "personalised"
BASE_PREFIX = f"{PREFIX}/_base"
# A reading session's ranges reuse what its first request resolved for this long after the last one.
PIN_SECONDS = 15 * 60
# Longer than any render; a lock left by a killed worker expires after this.
LOCK_SECONDS = 300
# How long a request waits for another request's render before rendering its own copy, served whole.
WAIT_SECONDS = 120
POLL_SECONDS = 0.25


def _idle_seconds() -> int:
    return int(settings.EVILFLOWERS_PERSONALISED_COPY_IDLE_HOURS * 3600)


def _base_idle_seconds() -> int:
    # A base serves every reader of a file, so it is worth keeping longer than one reader's copy.
    return int(settings.EVILFLOWERS_PERSONALISED_BASE_IDLE_HOURS * 3600)


# One queued base build per file at a time; a build that never ran (worker down) is retried after this.
BASE_QUEUE_SECONDS = 10 * 60


def _ready_key(name: str) -> str:
    return f"personalised-ready:{name}"


def _lock_key(name: str) -> str:
    return f"personalised-lock:{name}"


def _render_version() -> str:
    # Part of every copy's name: a change of the render pipeline names new copies, and with them new ETags,
    # so a reader mid-book across a deploy never gets ranges of two different renders.
    return "base-1" if settings.EVILFLOWERS_PERSONALISED_SHARED_BASE else "full-1"


def copy_name(user_acquisition: UserAcquisition, context: dict, page, annotation_map: dict) -> str:
    """Storage name of the copy these inputs render to."""
    acquisition = user_acquisition.acquisition
    key = {
        "render": _render_version(),
        "modifier": settings.EVILFLOWERS_MODIFIERS[acquisition.mime],
        "source": acquisition.content.name,
        "size": acquisition.content.storage.size(acquisition.content.name),
        "range": user_acquisition.range,
        "page": page,
        "annotations": {str(number): items for number, items in annotation_map.items()},
        "context": context,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:40]
    return f"{PREFIX}/{user_acquisition.pk}/{digest}{guess_extension(acquisition.mime) or ''}"


def ensure_copy(
    storage: Storage, name: str, render: Callable[[], File], kind: str = "copy", idle: Optional[int] = None
) -> bool:
    """Make `name` hold a complete render. False when another request's render did not finish in time.

    Exceptions from `render` propagate; the lock is released. `kind` (`copy`, `base`) labels the spans;
    `idle` (seconds) overrides how long the marker outlives its last use.
    """
    idle = idle or _idle_seconds()
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        if cache.touch(_ready_key(name), idle):
            return True
        if cache.add(_lock_key(name), 1, LOCK_SECONDS):
            try:
                if storage.exists(name):
                    # Left without a marker: possibly half-written. Never serve it.
                    storage.delete(name)
                # The two halves of a first request's wait (#214): MuPDF's CPU, then the write to storage.
                with tracing.span("personalise.render", {"evilflowers.personalise.kind": kind}):
                    content = render()
                with tracing.span("personalise.save", {"evilflowers.personalise.kind": kind}) as span:
                    try:
                        span.set_attribute("evilflowers.personalise.bytes", content.size)
                    except (AttributeError, OSError, TypeError):
                        pass
                    storage.save(name, content)
                cache.set(_ready_key(name), 1, idle)
            finally:
                cache.delete(_lock_key(name))
            return True
        if time.monotonic() >= deadline:
            logger.warning("personalised.wait_timeout name=%s", name)
            return False
        time.sleep(POLL_SECONDS)


def base_name(acquisition: Acquisition, modifier) -> str:
    """Storage name of the shared base of `acquisition`'s file: changes with the file and the save options."""
    key = {
        "source": acquisition.content.name,
        "size": acquisition.content.storage.size(acquisition.content.name),
        "options": {name: str(value) for name, value in modifier.SAVE_OPTIONS.items()},
        "mupdf": fitz.VersionFitz,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:40]
    return f"{BASE_PREFIX}/{digest}{guess_extension(acquisition.mime) or ''}"


def _modifier(acquisition: Acquisition):
    from django.utils.module_loading import import_string

    return import_string(settings.EVILFLOWERS_MODIFIERS[acquisition.mime])(context={}, pages=None)


def build_base(acquisition: Acquisition) -> bool:
    """Store the shared base of `acquisition`'s file (Celery: `build_personalised_base`). False if not built."""
    if acquisition.mime not in settings.EVILFLOWERS_MODIFIERS or not acquisition.content:
        return False
    modifier = _modifier(acquisition)
    if not hasattr(modifier, "normalise"):
        return False
    storage = acquisition.content.storage

    def normalise() -> File:
        # A handle of its own: the field's file object may be read by a request in the same process.
        with storage.open(acquisition.content.name, "rb") as source:
            return modifier.normalise(source)

    return ensure_copy(storage, base_name(acquisition, modifier), normalise, kind="base", idle=_base_idle_seconds())


def queue_base(acquisition: Acquisition) -> None:
    """Have a worker build the base, once per file however many readers ask meanwhile. Best effort."""
    if not cache.add(f"personalised-base-queued:{acquisition.pk}", 1, BASE_QUEUE_SECONDS):
        return
    from apps.tasks.tasks import build_personalised_base

    try:
        build_personalised_base.apply_async(args=[str(acquisition.pk)], retry=False)
    except Exception:  # broker down: this reader is served the full render anyway
        logger.warning("personalised.base_queue_failed acquisition=%s", acquisition.pk, exc_info=True)


def render_source(acquisition: Acquisition, modifier) -> Tuple[File, bool]:
    """The file a reader's copy is rendered from, and whether it is the normalised shared base.

    The base never delays a reader. Building it costs about one full render plus a write, so the first
    reader of a file is rendered in full from the source, as before, while a worker builds the base;
    readers after that get the light render. Without the setting, or a modifier that cannot normalise:
    always the source.
    """
    if not settings.EVILFLOWERS_PERSONALISED_SHARED_BASE or not hasattr(modifier, "normalise"):
        return acquisition.content, False
    name = base_name(acquisition, modifier)
    if cache.touch(_ready_key(name), _base_idle_seconds()):
        return acquisition.content.storage.open(name, "rb"), True
    queue_base(acquisition)
    return acquisition.content, False


def _pin_key(user_acquisition_id, variant: str) -> str:
    return f"personalised-pin:{user_acquisition_id}:{hashlib.sha256(variant.encode()).hexdigest()[:16]}"


def pin(user_acquisition_id, variant: str, served: dict) -> None:
    """Remember what the first request of a reading session served (`served`: name, copy, size, etag, …)."""
    cache.set(_pin_key(user_acquisition_id, variant), served, PIN_SECONDS)


def pinned(user_acquisition_id, variant: str) -> Optional[dict]:
    """What this session's first request served, while it is still pinned and its copy still complete.

    Refreshes both: a range read keeps the copy from idling out, as `ensure_copy` would.
    """
    key = _pin_key(user_acquisition_id, variant)
    served = cache.get(key)
    if served is None:
        return None
    if served.get("copy") and not cache.touch(_ready_key(served["name"]), _idle_seconds()):
        # The copy was evicted (or never finished): resolve it again.
        cache.delete(key)
        return None
    cache.touch(key, PIN_SECONDS)
    return served


def cleanup_idle(storage: Storage = None) -> int:
    """Delete stored copies whose marker has expired. Returns how many were deleted."""
    if storage is None:
        from apps.files.storage import get_storage

        storage = get_storage()

    try:
        directories, _files = storage.listdir(PREFIX)
    except FileNotFoundError:
        return 0

    # A file younger than this may still be being written (its marker is not set yet).
    settled = timezone.now() - datetime.timedelta(seconds=LOCK_SECONDS)
    deleted = 0
    for directory in directories:
        folder = f"{PREFIX}/{directory}"
        _subdirectories, files = storage.listdir(folder)
        remaining = len(files)
        for file in files:
            name = f"{folder}/{file}"
            if cache.get(_ready_key(name)) is not None:
                continue
            if cache.get(_lock_key(name)) is not None or storage.get_modified_time(name) > settled:
                continue
            storage.delete(name)
            deleted += 1
            remaining -= 1
        if not remaining:
            try:
                # Filesystem storage removes the empty directory; object storage has none.
                storage.delete(folder)
            except OSError:
                pass
    return deleted
