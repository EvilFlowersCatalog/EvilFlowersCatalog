"""
HTTP Range on stored files (#70, RFC 9110 §14).

Django has no built-in `206 Partial Content`: `FileResponse` always sends the
whole body. The viewer (pdf.js `getDocument({url})`) asks for byte ranges so it
can draw page 1 before the rest of the book has arrived; this module answers
them for a file that sits in the storage backend unchanged.

- No `Range`, a malformed one, several ranges, or an `If-Range` → the whole
  file, `200`, with `Accept-Ranges: bytes` and `Content-Length`. Ignoring the
  header is always allowed (§14.2); this code emits no validators, so an
  `If-Range` can never be proven to match.
- One satisfiable range → `206` with `Content-Range` and only those bytes.
- A range that starts past the end → `416` with `Content-Range: bytes */size`.

Only for bytes that are the same on every request. A personalised PDF
(`PDFModifier.generate`, new `generated_at` on each call) is rendered once and
stored first (`apps.files.personalised`); ranges are cut from that copy, never
from two different renders.
"""

import mimetypes
import re
from http import HTTPStatus
from typing import Iterator, Optional, Tuple

from django.core.files.storage import Storage
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.utils.http import content_disposition_header

CHUNK_SIZE = 64 * 1024

_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class Unsatisfiable(Exception):
    pass


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """`(first, last)` byte positions, inclusive, of a single-range header, or None to serve it all.

    Raises `Unsatisfiable` for a range that selects nothing of a `size`-byte file.
    """
    if not header:
        return None
    match = _RANGE.match(header)
    if match is None:
        # Multiple ranges ("bytes=0-1,5-6"), other units or bad syntax: serve the whole file.
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None

    if first == "":
        # Suffix range: the last N bytes.
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise Unsatisfiable()
        return max(0, size - suffix), size - 1

    start = int(first)
    if last != "" and int(last) < start:
        return None
    if start >= size:
        raise Unsatisfiable()
    end = int(last) if last != "" else size - 1
    return start, min(end, size - 1)


def is_continuation(request) -> bool:
    """True for a range request past byte 0: the rest of a download already counted.

    pdf.js fetches one book in dozens of ranges; popularity and activity count the first one only.
    """
    header = request.headers.get("Range")
    if not header:
        return False
    match = _RANGE.match(header)
    # An unparsable Range is ignored and the whole file is served, so it counts.
    return match is not None and match.group(1) != "0"


def _read(handle, start: int, length: int) -> Iterator[bytes]:
    try:
        if start:
            handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        handle.close()


def _open_part(storage: Storage, name: str, start: int, length: int) -> Iterator[bytes]:
    open_range = getattr(storage, "open_range", None)
    if open_range is not None:
        # S3: the object store returns just these bytes; its stream cannot seek.
        return _read(open_range(name, start, length), 0, length)
    return _read(storage.open(name, "rb"), start, length)


def ranged_file_response(
    request,
    storage: Storage,
    name: str,
    *,
    content_type: Optional[str] = None,
    filename: str = "",
    as_attachment: bool = False,
) -> HttpResponseBase:
    """Serve `name` from `storage`, honouring a single `Range: bytes=…` request header."""
    size = storage.size(name)

    try:
        byte_range = parse_range(request.headers.get("Range"), size)
    except Unsatisfiable:
        response = HttpResponse(status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        response["Content-Range"] = f"bytes */{size}"
        response["Accept-Ranges"] = "bytes"
        return response

    if byte_range is not None and request.headers.get("If-Range"):
        byte_range = None

    if byte_range is None:
        response = FileResponse(
            storage.open(name, "rb"), as_attachment=as_attachment, filename=filename, content_type=content_type
        )
        response["Content-Length"] = str(size)
        response["Accept-Ranges"] = "bytes"
        return response

    start, end = byte_range
    length = end - start + 1
    response = StreamingHttpResponse(
        _open_part(storage, name, start, length),
        status=HTTPStatus.PARTIAL_CONTENT,
        content_type=content_type or mimetypes.guess_type(filename or name)[0] or "application/octet-stream",
    )
    disposition = content_disposition_header(as_attachment, filename)
    if disposition:
        response["Content-Disposition"] = disposition
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    return response
