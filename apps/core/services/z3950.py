"""A minimal Z39.50 client (ANSI/NISO Z39.50-2003, APDUs in BER): init, search one term, present MARC21 records.

Enough for one question: "records whose ISBN is X" against a library catalogue without an HTTP API, such as the
Slovak National Library's union catalogue (`z3950.snk.sk:1111`, database `clas01`). No result sets beyond the
default, no scan, no sort, no authentication: anonymous access only.
"""

import socket
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: bib-1 attribute set and the MARC21 record syntax.
BIB1 = "1.2.840.10003.3.1"
MARC21 = "1.2.840.10003.5.10"
#: bib-1 use attributes.
USE_ISBN = 7
USE_ISSN = 8

UNIVERSAL, CONTEXT = 0x00, 0x80


class Z3950Error(Exception):
    """The server refused, closed the connection, or answered something this client does not read."""


# -- BER ----------------------------------------------------------------------------------------------------


def _length(size: int) -> bytes:
    if size < 0x80:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tag(number: int, cls: int = CONTEXT, constructed: bool = False) -> bytes:
    first = cls | (0x20 if constructed else 0)
    if number < 31:
        return bytes([first | number])
    digits = []
    while True:
        digits.insert(0, number & 0x7F)
        number >>= 7
        if not number:
            break
    return bytes([first | 0x1F] + [digit | 0x80 for digit in digits[:-1]] + [digits[-1]])


def tlv(number: int, value: bytes, cls: int = CONTEXT, constructed: bool = False) -> bytes:
    return _tag(number, cls, constructed) + _length(len(value)) + value


def seq(number: int, *parts: bytes, cls: int = CONTEXT) -> bytes:
    return tlv(number, b"".join(parts), cls, constructed=True)


def integer(number: int, value: int, cls: int = CONTEXT) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)
    return tlv(number, value.to_bytes(size, "big", signed=True), cls)


def string(number: int, value: str, cls: int = CONTEXT) -> bytes:
    return tlv(number, value.encode("utf-8"), cls)


def boolean(number: int, value: bool) -> bytes:
    return tlv(number, b"\xff" if value else b"\x00")


def oid(dotted: str, number: int = 6, cls: int = UNIVERSAL) -> bytes:
    parts = [int(part) for part in dotted.split(".")]
    body = [40 * parts[0] + parts[1]]
    encoded = bytearray(body)
    for part in parts[2:]:
        chunk = [part & 0x7F]
        part >>= 7
        while part:
            chunk.insert(0, 0x80 | (part & 0x7F))
            part >>= 7
        encoded.extend(chunk)
    return tlv(number, bytes(encoded), cls)


def bits(number: int, flags: List[int], size: int) -> bytes:
    """A BIT STRING of `size` bits with `flags` set (bit 0 is the most significant of the first byte)."""
    octets = bytearray((size + 7) // 8)
    for flag in flags:
        octets[flag // 8] |= 0x80 >> (flag % 8)
    return tlv(number, bytes([len(octets) * 8 - size]) + bytes(octets))


@dataclass
class Node:
    cls: int
    constructed: bool
    number: int
    value: bytes
    children: List["Node"]

    def find(self, cls: int, number: int, constructed: Optional[bool] = None) -> List["Node"]:
        """Every descendant (depth first, self included) with this class and tag number."""
        found = []
        if self.cls == cls and self.number == number and (constructed is None or self.constructed == constructed):
            found.append(self)
        for child in self.children:
            found.extend(child.find(cls, number, constructed))
        return found

    def integer(self) -> int:
        return int.from_bytes(self.value, "big", signed=True)


class Incomplete(Exception):
    """The buffer ends inside a TLV: read more."""


def _byte(data: bytes, offset: int) -> int:
    if offset >= len(data):
        raise Incomplete()
    return data[offset]


def _header(data: bytes, offset: int) -> Tuple[int, bool, int, Optional[int], int]:
    """`(cls, constructed, number, length or None when indefinite, value offset)` of the TLV at `offset`."""
    first = _byte(data, offset)
    offset += 1
    cls, constructed, number = first & 0xC0, bool(first & 0x20), first & 0x1F
    if number == 0x1F:
        number = 0
        while True:
            byte = _byte(data, offset)
            offset += 1
            number = (number << 7) | (byte & 0x7F)
            if not byte & 0x80:
                break
    size = _byte(data, offset)
    offset += 1
    if size & 0x80:
        count = size & 0x7F
        if count == 0:
            if not constructed:
                raise Z3950Error("indefinite length on a primitive value")
            return cls, constructed, number, None, offset
        if offset + count > len(data):
            raise Incomplete()
        size = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    return cls, constructed, number, size, offset


def _one(data: bytes, offset: int) -> Tuple[Node, int]:
    """The TLV at `offset` and the offset after it; definite and indefinite (end-of-contents) lengths."""
    cls, constructed, number, size, start = _header(data, offset)
    if size is None:
        children, cursor = [], start
        while True:
            if _byte(data, cursor) == 0 and _byte(data, cursor + 1) == 0:
                return Node(cls, constructed, number, data[start:cursor], children), cursor + 2
            child, cursor = _one(data, cursor)
            children.append(child)
    if start + size > len(data):
        raise Incomplete()
    value = data[start : start + size]
    children = decode(data, start, start + size) if constructed else []
    return Node(cls, constructed, number, value, children), start + size


def decode(data: bytes, offset: int = 0, end: Optional[int] = None) -> List[Node]:
    end = len(data) if end is None else end
    nodes = []
    while offset < end:
        node, offset = _one(data[:end], offset)
        nodes.append(node)
    return nodes


# -- APDUs --------------------------------------------------------------------------------------------------


def init_request(message_size: int = 1 << 20) -> bytes:
    return seq(
        20,
        bits(3, [0, 1, 2], 3),  # protocol versions 1-3
        bits(4, [0, 1], 16),  # search, present
        integer(5, message_size),
        integer(6, message_size),
        string(110, "evilflowers"),
        string(111, "EvilFlowersCatalog"),
        string(112, "1"),
    )


def search_request(database: str, term: str, use: int = USE_ISBN) -> bytes:
    attribute = seq(16, integer(120, 1), integer(121, use), cls=UNIVERSAL)
    operand = seq(102, seq(44, attribute), tlv(45, term.encode("utf-8")))
    query = seq(21, seq(1, oid(BIB1), seq(0, operand)))
    return seq(
        22,
        integer(13, 0),  # smallSetUpperBound: no records with the search
        integer(14, 1),  # largeSetLowerBound
        integer(15, 0),  # mediumSetPresentNumber
        boolean(16, True),  # replace the default result set
        string(17, "default"),
        seq(18, string(105, database)),
        query,
    )


def present_request(count: int, syntax: str = MARC21) -> bytes:
    return seq(
        24,
        string(31, "default"),
        integer(30, 1),
        integer(29, count),
        seq(19, string(0, "F")),  # full records
        oid(syntax, 104, CONTEXT),
    )


# -- the connection -----------------------------------------------------------------------------------------


class Connection:
    def __init__(self, host: str, port: int, timeout: float):
        try:
            self.socket = socket.create_connection((host, port), timeout=timeout)
        except OSError as error:
            raise Z3950Error(f"cannot connect to {host}:{port}: {error}") from error

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    #: Bytes of one answer at most: a few MARC records are kilobytes.
    MAX_APDU = 8 << 20

    def exchange(self, apdu: bytes) -> Node:
        """Send one APDU and read until one complete APDU has arrived (definite or indefinite length)."""
        buffer = b""
        try:
            self.socket.sendall(apdu)
            while True:
                chunk = self.socket.recv(65536)
                if not chunk:
                    raise Z3950Error("the server closed the connection")
                buffer += chunk
                if len(buffer) > self.MAX_APDU:
                    raise Z3950Error("answer too large")
                try:
                    node, _end = _one(buffer, 0)
                    break
                except Incomplete:
                    continue
        except OSError as error:
            raise Z3950Error(f"connection failed: {error}") from error
        if node.number == 48:
            raise Z3950Error("the server closed the session")
        return node


def search(
    host: str, port: int, database: str, term: str, *, use: int = USE_ISBN, count: int = 3, timeout: float = 10.0
) -> List[bytes]:
    """ISO 2709 (MARC21) records whose `use` attribute matches `term`, at most `count`."""
    with Connection(host, port, timeout) as connection:
        response = connection.exchange(init_request())
        results = response.find(CONTEXT, 12, constructed=False)  # [12] result BOOLEAN
        if response.number != 21 or (results and results[0].value == b"\x00"):
            raise Z3950Error("init refused")
        response = connection.exchange(search_request(database, term, use))
        if response.number != 23:
            raise Z3950Error(f"unexpected answer to search: [{response.number}]")
        found = [child for child in response.children if child.cls == CONTEXT and child.number == 23]
        total = found[0].integer() if found else 0
        if total <= 0:
            return []
        response = connection.exchange(present_request(min(total, count)))
        if response.number != 25:
            raise Z3950Error(f"unexpected answer to present: [{response.number}]")
        records = []
        # NamePlusRecord → record [1] → retrievalRecord [1] EXTERNAL (universal 8) → octet-aligned [1].
        for external in response.find(UNIVERSAL, 8, constructed=True):
            for child in external.children:
                if child.cls == CONTEXT and child.number == 1 and not child.constructed:
                    records.append(child.value)
        return records
