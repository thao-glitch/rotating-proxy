#!/usr/bin/env python3
"""
Minimal, dependency-free reader for the MaxMind DB (`.mmdb`) format.

State-level exit selection ("United States → California") needs a city
database, and making the feature depend on a compiled binding
(`python3-maxminddb`) would leave it unavailable on most machines, so this
module reads the file directly with `mmap` and the standard library.

The format is deliberately simple:

    [ search tree ][ 16 bytes of zero ][ data section ][ metadata marker + metadata ]

The tree is walked bit by bit with the address; a record pointing past the
last node says where in the data section the answer lives.  Data is a
stream of typed values (maps, strings, arrays, pointers into the same
section…), described by one metadata block at the very end of the file.

    import mmdb
    db = mmdb.open_database("/path/to/dbip-city-lite.mmdb")
    db.get("8.8.8.8")["subdivisions"][0]["names"]["en"]   # -> "California"
    db.metadata["database_type"]                          # -> "DBIP-City-Lite"

The reader is validated against the reference implementation in
`test_engine.py` whenever `maxminddb` happens to be installed.
"""

from __future__ import annotations

import mmap
import struct
from ipaddress import ip_address
from typing import Any

# the metadata block is announced by this marker in the last 128 KiB
MARKER = b"\xab\xcd\xefMaxMind.com"
METADATA_WINDOW = 131072
SEPARATOR = 16                       # bytes between tree and data section

# data types (top 3 bits of the control byte; 0 = extended, see below)
POINTER, UTF8, DOUBLE, BYTES, UINT16, UINT32, MAP = 1, 2, 3, 4, 5, 6, 7
INT32, UINT64, UINT128, ARRAY, CONTAINER, END, BOOLEAN, FLOAT = 8, 9, 10, 11, 12, 13, 14, 15


class InvalidDatabase(Exception):
    """The file is not a MaxMind DB database, or it is corrupt."""


def _size_from_ctrl(ctrl: int, buf: memoryview, offset: int, is_pointer: bool) -> tuple[int, int]:
    """Length carried by a control byte, plus where the payload starts."""
    size = ctrl & 0x1F
    if is_pointer or size < 29:                 # pointers keep their raw size
        return size, offset
    if size == 29:
        return size + buf[offset], offset + 1
    if size == 30:
        return 285 + int.from_bytes(buf[offset:offset + 2], "big"), offset + 2
    return 65821 + int.from_bytes(buf[offset:offset + 3], "big"), offset + 3


def _pointer_value(size: int, buf: memoryview, offset: int, base: int) -> tuple[int, int]:
    """Resolve the offset a type-1 value points at (see the format spec)."""
    pointer_size = (size >> 3) + 1
    value = int.from_bytes(buf[offset:offset + pointer_size], "big")
    if pointer_size <= 3:
        # the top bits of the offset live in the control byte itself
        value |= (size & 0x7) << (8 * pointer_size)
        if pointer_size == 2:
            value += 2048
        elif pointer_size == 3:
            value += 526336
    return value + base, offset + pointer_size


class Database:
    """An open `.mmdb` file: address in, decoded record out."""

    def __init__(self, path: str) -> None:
        try:
            self._file = open(path, "rb")
            self._buf = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as exc:
            raise InvalidDatabase(f"{path}: {exc}") from exc
        try:
            self._read_metadata()
        except Exception:
            self.close()
            raise

    # -- metadata ---------------------------------------------------------
    def _read_metadata(self) -> None:
        data = memoryview(self._buf)
        start = max(0, len(self._buf) - METADATA_WINDOW)
        marker = self._buf.rfind(MARKER, start)
        if marker < 0:
            raise InvalidDatabase("no MaxMind DB metadata marker found")
        meta_at = marker + len(MARKER)
        meta, _ = self._decode(meta_at, meta_at, data)
        if not isinstance(meta, dict):
            raise InvalidDatabase("metadata is not a map")
        self.metadata: dict[str, Any] = meta
        try:
            self._node_count = int(meta["node_count"])
            self._record_size = int(meta["record_size"])
            self._ip_version = int(meta["ip_version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidDatabase(f"incomplete metadata: {exc}") from exc
        if self._record_size not in (24, 28, 32):
            raise InvalidDatabase(f"unsupported record size {self._record_size}")
        self._node_bytes = self._record_size // 4
        self._tree_size = self._node_count * self._node_bytes
        self._data_at = self._tree_size + SEPARATOR
        # an IPv6 database keeps IPv4 addresses under a fixed 96-bit prefix
        self._v4_start = self._start_node()

    def _start_node(self) -> int:
        node, bits = 0, 96 if self._ip_version == 6 else 0
        while bits and node < self._node_count:
            node = self._record(node, 0)
            bits -= 1
        return node

    # -- tree -------------------------------------------------------------
    def _record(self, node: int, side: int) -> int:
        """Left (0) or right (1) child of `node`."""
        data = memoryview(self._buf)
        base = node * self._node_bytes
        if self._record_size == 24:
            offset = base + side * 3
            return int.from_bytes(data[offset:offset + 3], "big")
        if self._record_size == 32:
            offset = base + side * 4
            return int.from_bytes(data[offset:offset + 4], "big")
        # 28-bit records: 7 bytes, the middle byte's two nibbles belong to
        # the records on either side of it
        if side:
            offset = base + 3
            return ((data[offset] & 0x0F) << 24) | int.from_bytes(
                data[offset + 1:offset + 4], "big")
        offset = base
        return ((data[offset + 3] & 0xF0) << 20) | int.from_bytes(
            data[offset:offset + 3], "big")

    def get(self, address: str) -> Any:
        """Decoded record for an IP address, or None when it is not covered."""
        packed = ip_address(address).packed
        node = self._v4_start if len(packed) == 4 and self._ip_version == 6 else 0
        for byte in packed:
            for shift in range(7, -1, -1):
                if node >= self._node_count:
                    break                      # found the answer already
                node = self._record(node, (byte >> shift) & 1)
            else:
                continue                       # still branching: next byte
            break
        if node <= self._node_count:
            # the empty record (or an address the tree does not finish):
            # this database has nothing to say about it
            return None
        data = memoryview(self._buf)
        # a record past the last node is an offset into the data section,
        # counted from the start of the 16-byte separator before it
        value, _ = self._decode(node - self._node_count + self._tree_size,
                                self._data_at, data)
        return value

    # -- data section -----------------------------------------------------
    def _decode(self, offset: int, pointer_base: int, data: memoryview) -> tuple[Any, int]:
        ctrl = data[offset]
        offset += 1
        kind = ctrl >> 5
        if kind == 0:                           # extended type: 7 + next byte
            kind = data[offset] + 7
            offset += 1
        if kind == POINTER:
            target, offset = _pointer_value(ctrl & 0x1F, data, offset, pointer_base)
            return self._decode(target, pointer_base, data)[0], offset
        size, offset = _size_from_ctrl(ctrl, data, offset, False)
        end = offset + size

        if kind == UTF8:
            return bytes(data[offset:end]).decode("utf-8", "replace"), end
        if kind == BYTES:
            return bytes(data[offset:end]), end
        if kind == DOUBLE:
            if size != 8:
                raise InvalidDatabase(f"double of size {size} at {offset - 1}")
            return struct.unpack_from("!d", data, offset)[0], end
        if kind == FLOAT:
            if size != 4:
                raise InvalidDatabase(f"float of size {size} at {offset - 1}")
            return struct.unpack_from("!f", data, offset)[0], end
        if kind == INT32:
            if not size:
                return 0, offset                     # zero-length int32
            raw = bytes(data[offset:end])
            return struct.unpack("!i", raw.rjust(4, b"\x00"))[0], end
        if kind in (UINT16, UINT32, UINT64, UINT128):
            return int.from_bytes(data[offset:end], "big"), end
        if kind == BOOLEAN:
            return bool(size), offset                # no payload: size says it
        if kind in (MAP, ARRAY):
            out = {} if kind == MAP else []
            for _ in range(size):
                key, offset = self._decode(offset, pointer_base, data)
                if kind == MAP:
                    value, offset = self._decode(offset, pointer_base, data)
                    out[key] = value
                else:
                    out.append(key)
            return out, offset
        if kind == END:
            return None, offset
        raise InvalidDatabase(f"unsupported data type {kind} at offset {offset}")

    def close(self) -> None:
        for attr in ("_buf", "_file"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:               # noqa: BLE001
                    pass
                setattr(self, attr, None)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_database(path: str) -> Database:
    """Open `path` or raise `InvalidDatabase`."""
    return Database(path)
