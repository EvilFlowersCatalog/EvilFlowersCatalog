"""
AcquisitionStorageService — single dispatch point for acquisition I/O.

IP-008 Phase 5: replaces the ad-hoc `if acquisition.file_url else ...`
branches scattered through views and serializers. Callers ask the
service what they want to do (`open`, `url`, `exists`, `delete`) and
the service dispatches based on `Acquisition.storage_backend`.

The security gain is auth + IP-block + UserAcquisition tracking happen
BEFORE the dispatch in `apps/files/views.py::AcquisitionDownload`. The
old code's `file_url` redirect short-circuited those checks because it
ran before the auth branch.
"""

from __future__ import annotations

from typing import Optional, Union

from django.core.files import File
from django.http import FileResponse, HttpResponseRedirect, HttpResponse
from django.utils.text import slugify
from mimetypes import guess_extension

from apps.core.models import Acquisition
from apps.files.storage import get_storage


class AcquisitionStorageError(Exception):
    """Raised when the requested storage operation cannot be completed
    (e.g., external URL stored but `file_url` is None)."""


class AcquisitionStorageService:
    """Dispatcher for Acquisition payload access.

    Methods are class-level — there's no per-instance state. We treat
    this as a static service so tests can monkeypatch individual
    methods without dependency injection.
    """

    @classmethod
    def open(cls, acquisition: Acquisition) -> Optional[File]:
        """Return a readable file handle for LOCAL acquisitions.

        Returns None for EXTERNAL_URL acquisitions — callers that need
        the raw bytes for an external resource should fetch via
        `requests` against `acquisition.file_url` themselves (with
        their own retry/timeout policy).
        """
        if acquisition.storage_backend != Acquisition.StorageBackend.LOCAL:
            return None
        if not acquisition.content:
            return None
        return acquisition.content

    @classmethod
    def url(cls, acquisition: Acquisition, request=None) -> Optional[str]:
        """Resolve the URL to hand to the requester.

        - EXTERNAL_URL: returns `acquisition.file_url` as-is.
        - LOCAL: returns the absolute URL of the download endpoint.

        For HTTP serving, callers should usually use
        `download_response(...)` which builds the right response type
        (redirect vs FileResponse).
        """
        if acquisition.storage_backend == Acquisition.StorageBackend.EXTERNAL_URL:
            return acquisition.file_url
        if acquisition.file_url:
            # Defensive: a row with the LOCAL backend but a file_url
            # should not exist — but legacy data may. Honor file_url
            # so the response stays sensible.
            return acquisition.file_url
        if not acquisition.content:
            return None
        from django.urls import reverse

        path = reverse("files:acquisition-download", kwargs={"acquisition_id": acquisition.pk})
        if request is not None:
            return request.build_absolute_uri(path)
        return path

    @classmethod
    def exists(cls, acquisition: Acquisition) -> bool:
        """True if the payload is reachable.

        - EXTERNAL_URL: a non-empty `file_url` is treated as "exists";
          we do not probe the external host on every check.
        - LOCAL: probes the storage backend.
        """
        if acquisition.storage_backend == Acquisition.StorageBackend.EXTERNAL_URL:
            return bool(acquisition.file_url)
        if not acquisition.content:
            return False
        return acquisition.content.storage.exists(acquisition.content.name)

    @classmethod
    def delete(cls, acquisition: Acquisition) -> None:
        """Remove the payload (LOCAL only).

        EXTERNAL_URL acquisitions are pointers, not owned files —
        deleting the row removes the pointer; the upstream blob is the
        upstream's problem.
        """
        if acquisition.storage_backend != Acquisition.StorageBackend.LOCAL:
            return
        if not acquisition.content:
            return
        storage = get_storage()
        path = acquisition.content.name
        if path and storage.exists(path):
            storage.delete(path)

    @classmethod
    def download_response(
        cls,
        acquisition: Acquisition,
        *,
        as_attachment: bool = True,
    ) -> Union[FileResponse, HttpResponseRedirect, HttpResponse]:
        """Build the right HTTP response for serving the payload.

        - EXTERNAL_URL: 302 redirect to `file_url`.
        - LOCAL: `FileResponse` streaming the storage backend file.

        Callers MUST run their auth / IP-block / UserAcquisition
        bookkeeping BEFORE invoking this method.
        """
        if acquisition.storage_backend == Acquisition.StorageBackend.EXTERNAL_URL:
            if not acquisition.file_url:
                raise AcquisitionStorageError(
                    f"Acquisition {acquisition.pk} has storage_backend=external_url but no file_url"
                )
            return HttpResponseRedirect(acquisition.file_url)

        if not acquisition.content:
            raise AcquisitionStorageError(f"Acquisition {acquisition.pk} has no content stored")

        filename = cls._safe_filename(acquisition)
        return FileResponse(acquisition.content, as_attachment=as_attachment, filename=filename)

    @staticmethod
    def _safe_filename(acquisition: Acquisition) -> str:
        title = acquisition.entry.title or "download"
        ext = guess_extension(acquisition.mime or "") or ""
        return f"{slugify(title.lower())}{ext}"
