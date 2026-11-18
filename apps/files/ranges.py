"""
HTTP Range on stored files (#70, #214; RFC 9110 §13.1.5, §14).

Django has no built-in `206 Partial Content`: `FileResponse` always sends the
whole body. The viewer (pdf.js `getDocument({url})`) asks for byte ranges so it
can draw page 1 before the rest of the book has arrived; this module answers
them for a file that sits in the storage backend unchanged.

- No `Range`, a malformed one or several ranges → the whole file, `200`, with
  `Accept-Ranges: bytes` and `Content-Length`. Ignoring the header is always
  allowed (§14.2).
- One satisfiable range → `206` with `Content-Range` and only those bytes.
- A range that starts past the end → `416` with `Content-Range: bytes */size`.
- Every answer carries a strong `ETag` and `Last-Modified`. An `If-Range` that
  matches them (strong comparison; a weak tag never matches) keeps the `206`;
  any other `If-Range` gets the whole file, so a client never stitches ranges
  of two different files.
- `HEAD` (Django runs `get`) answers the same headers; the WSGI server drops the body.

pdf.js disables ranges when it sees `Content-Encoding`; the GZip middleware
skips every response that carries `Accept-Ranges` (`apps.api.middleware.gzip`).

The range is read from the storage backend and streamed by the web worker
(S3: the store returns just those bytes). No nginx offload: decided against in
#214, the checks run per request anyway and the transfer is the short part.

Only for bytes that are the same on every request. A personalised PDF is
rendered once and stored first (`apps.files.personalised`); ranges are cut
from that copy, never from two different renders.
"""

import hashlib
import mimetypes
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Iterator, Optional, Tuple

from django.core.files.storage import Storage
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.utils.crypto import constant_time_compare
from django.utils.http import content_disposition_header, http_date, parse_http_date_safe, quote_etag

CHUNK_SIZE = 64 * 1024

_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)


class Unsatisfiable(Exception):
    pass


@dataclass(frozen=True)
class Validators:
    """What a response says about the file it serves: its length and its strong validators."""

    size: int
    etag: str
    last_modified: Optional[int] = None

    def as_dict(self) -> dict:
        return {"size": self.size, "etag": self.etag, "last_modified": self.last_modified}


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
    """True for any byte-range request: a part of a download, never a download of its own (#214).

    pdf.js opens a book with one plain GET (headers only, then aborted) and fetches
    every byte after that in ranges, `bytes=0-65535` included. Counting the range from
    byte 0 as a new download counted each open twice.
    """
    header = request.headers.get("Range", "")
    return header.strip().lower().startswith("bytes")


def counts_as_open(request) -> bool:
    """Popularity, activity and reads count a GET without `Range` only: one per opened book."""
    return request.method == "GET" and not is_continuation(request)


def file_etag(name: str, size: int, modified: Optional[float]) -> str:
    """A strong ETag for a stored file: a rewrite under the same name changes its time (to the µs) or size."""
    digest = hashlib.sha256(f"{name}|{size}|{modified!r}".encode()).hexdigest()[:24]
    return quote_etag(f"{digest}-{size}")


def _modified(storage: Storage, name: str) -> Optional[float]:
    try:
        return storage.get_modified_time(name).timestamp()
    except (NotImplementedError, OSError, AttributeError, ValueError):
        return None


def file_validators(
    storage: Storage, name: str, *, size: Optional[int] = None, etag: Optional[str] = None
) -> Validators:
    """Size, ETag and Last-Modified of `name`. One `stat` on a filesystem; `etag` skips the hash."""
    if size is None:
        size = storage.size(name)
    modified = _modified(storage, name)
    return Validators(
        size=size,
        etag=etag or file_etag(name, size, modified),
        last_modified=int(modified) if modified is not None else None,
    )


def if_range_matches(header: Optional[str], validators: Validators) -> bool:
    """RFC 9110 §13.1.5: a strong ETag equal to ours, or exactly our Last-Modified date."""
    if not header:
        return True
    header = header.strip()
    if header.startswith("W/"):
        return False
    if header.startswith('"'):
        return constant_time_compare(header, validators.etag)
    date = parse_http_date_safe(header)
    if date is None or validators.last_modified is None:
        return False
    # A date is a strong validator only when the file is at least a second older than now (§8.8.2.2).
    return date == validators.last_modified and validators.last_modified <= time.time() - 1


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


def _set_validators(response: HttpResponseBase, validators: Validators) -> None:
    response["Accept-Ranges"] = "bytes"
    response["ETag"] = validators.etag
    if validators.last_modified is not None:
        response["Last-Modified"] = http_date(validators.last_modified)


def ranged_file_response(
    request,
    storage: Storage,
    name: str,
    *,
    content_type: Optional[str] = None,
    filename: str = "",
    as_attachment: bool = False,
    validators: Optional[Validators] = None,
) -> HttpResponseBase:
    """Serve `name` from `storage`, honouring a single `Range: bytes=…` and its `If-Range`.

    `validators` (size, ETag, Last-Modified) skips the `stat` when the caller already knows them.
    """
    content_type = content_type or mimetypes.guess_type(filename or name)[0] or "application/octet-stream"

    if validators is None:
        validators = file_validators(storage, name)
    size = validators.size

    try:
        byte_range = parse_range(request.headers.get("Range"), size)
    except Unsatisfiable:
        response = HttpResponse(status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        response["Content-Range"] = f"bytes */{size}"
        _set_validators(response, validators)
        return response

    if byte_range is not None and not if_range_matches(request.headers.get("If-Range"), validators):
        byte_range = None

    if byte_range is None:
        response = FileResponse(
            storage.open(name, "rb"), as_attachment=as_attachment, filename=filename, content_type=content_type
        )
        response["Content-Length"] = str(size)
        _set_validators(response, validators)
        return response

    start, end = byte_range
    length = end - start + 1
    response = StreamingHttpResponse(
        _open_part(storage, name, start, length), status=HTTPStatus.PARTIAL_CONTENT, content_type=content_type
    )
    disposition = content_disposition_header(as_attachment, filename)
    if disposition:
        response["Content-Disposition"] = disposition
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(length)
    _set_validators(response, validators)
    return response
