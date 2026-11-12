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
"""

import datetime
import hashlib
import json
import logging
import time
from mimetypes import guess_extension
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.core.files import File
from django.core.files.storage import Storage
from django.utils import timezone

from apps.core.models import UserAcquisition

logger = logging.getLogger(__name__)

PREFIX = "personalised"
# Longer than any render; a lock left by a killed worker expires after this.
LOCK_SECONDS = 300
# How long a request waits for another request's render before rendering its own copy, served whole.
WAIT_SECONDS = 120
POLL_SECONDS = 0.25


def _idle_seconds() -> int:
    return int(settings.EVILFLOWERS_PERSONALISED_COPY_IDLE_HOURS * 3600)


def _ready_key(name: str) -> str:
    return f"personalised-ready:{name}"


def _lock_key(name: str) -> str:
    return f"personalised-lock:{name}"


def copy_name(user_acquisition: UserAcquisition, context: dict, page, annotation_map: dict) -> str:
    """Storage name of the copy these inputs render to."""
    acquisition = user_acquisition.acquisition
    key = {
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


def ensure_copy(storage: Storage, name: str, render: Callable[[], File]) -> bool:
    """Make `name` hold a complete render. False when another request's render did not finish in time.

    Exceptions from `render` propagate; the lock is released.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        if cache.touch(_ready_key(name), _idle_seconds()):
            return True
        if cache.add(_lock_key(name), 1, LOCK_SECONDS):
            try:
                if storage.exists(name):
                    # Left without a marker: possibly half-written. Never serve it.
                    storage.delete(name)
                storage.save(name, render())
                cache.set(_ready_key(name), 1, _idle_seconds())
            finally:
                cache.delete(_lock_key(name))
            return True
        if time.monotonic() >= deadline:
            logger.warning("personalised.wait_timeout name=%s", name)
            return False
        time.sleep(POLL_SECONDS)


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
