"""Binary incremental frame store for match snapshots (``.rcf``).

A bundle used to stream one full JSON snapshot per tick (~43 KB x 1801 frames,
67-77 MB per match). Almost none of that is new information: the map and the
published parameters never change, and only about ten leaf fields move per tick.
This module keeps the invariants once and streams the difference.

Layout of one file, little-endian throughout::

    [ header 112 B ][ zlib(static JSON) ][ record 0..N-1 ][ index block ]

A record is self-delimiting::

    u8 op (1=keyframe, 2=delta) | varint payload_len | u32 crc32 | payload

The index block is written once on close and lets any frame be reached in O(1)
by decoding from the nearest preceding keyframe. Nothing here is a security
boundary: crc32 only catches accidental corruption, and determinism is proven by
replaying the match and comparing states, not by trusting a digest.
"""
from __future__ import annotations

from bisect import bisect_right
import copy
import json
import struct
from pathlib import Path
from typing import Any, Iterator
import zlib

MAGIC = b"RCONSNAP"
INDEX_MAGIC = b"RCONIDX1"
FORMAT_VERSION = 1

HEADER_SIZE = 112
_HEADER = struct.Struct("<8sHHIIIIIQIIQQIIII32s")
_INDEX_HEAD = struct.Struct("<8sIIII")
_INDEX_ENTRY = struct.Struct("<QIIIIBBHI")
_INDEX_ENTRY_SIZE = 32
_U32 = struct.Struct("<I")
_F64 = struct.Struct("<d")
_S8 = struct.Struct("<b")

assert _HEADER.size == HEADER_SIZE, _HEADER.size

KEYFRAME = 1
DELTA = 2

# Value tags.
TAG_NULL = 0x00
TAG_TRUE = 0x01
TAG_FALSE = 0x02
TAG_INT = 0x03
TAG_INT8 = 0x04
TAG_F64 = 0x05
TAG_STR_REF = 0x06
TAG_STR_NEW = 0x07
TAG_DICT = 0x08
TAG_LIST = 0x09

# Path steps.
STEP_KEY = 0x01
STEP_INDEX = 0x02

# Keys published once into the static block instead of every frame. The engine
# publishes them as read-only shared references, so one copy is enough.
STATIC_KEYS = ("map", "parameters")


class FrameFormatError(ValueError):
    """The bytes do not form a valid frame stream."""


# ---------------------------------------------------------------------------
# primitives


def _write_varint(buf: bytearray, value: int) -> None:
    if value < 0:
        raise FrameFormatError("varint must be non-negative")
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            buf.append(chunk | 0x80)
        else:
            buf.append(chunk)
            return


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise FrameFormatError("truncated varint")
        chunk = data[pos]
        pos += 1
        result |= (chunk & 0x7F) << shift
        if not chunk & 0x80:
            return result, pos
        shift += 7
        if shift > 700:
            raise FrameFormatError("varint field is unreasonably long")


def _zigzag(value: int) -> int:
    return value << 1 if value >= 0 else ((-value) << 1) - 1


def _unzigzag(value: int) -> int:
    return value >> 1 if not value & 1 else -((value + 1) >> 1)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------
# value encoding


class _Encoder:
    """Writes values while keeping the interned string table in step with the reader."""

    def __init__(self) -> None:
        self.table: dict[str, int] = {}
        self.strings: list[str] = []

    def string(self, buf: bytearray, value: str) -> None:
        index = self.table.get(value)
        if index is not None:
            buf.append(TAG_STR_REF)
            _write_varint(buf, index)
            return
        raw = value.encode("utf-8")
        index = len(self.strings)
        self.table[value] = index
        self.strings.append(value)
        buf.append(TAG_STR_NEW)
        _write_varint(buf, index)
        _write_varint(buf, len(raw))
        buf += raw

    def value(self, buf: bytearray, value: Any) -> None:
        if value is None:
            buf.append(TAG_NULL)
        elif value is True:
            buf.append(TAG_TRUE)
        elif value is False:
            buf.append(TAG_FALSE)
        elif isinstance(value, int):
            if -128 <= value <= 127:
                buf.append(TAG_INT8)
                buf += _S8.pack(value)
            else:
                buf.append(TAG_INT)
                _write_varint(buf, _zigzag(value))
        elif isinstance(value, float):
            buf.append(TAG_F64)
            buf += _F64.pack(value)
        elif isinstance(value, str):
            self.string(buf, value)
        elif isinstance(value, dict):
            buf.append(TAG_DICT)
            _write_varint(buf, len(value))
            for key in sorted(value):
                self.string(buf, key)
                self.value(buf, value[key])
        elif isinstance(value, (list, tuple)):
            buf.append(TAG_LIST)
            _write_varint(buf, len(value))
            for item in value:
                self.value(buf, item)
        else:
            raise FrameFormatError(f"cannot encode {type(value).__name__}")


class _Decoder:
    def __init__(self, data: bytes, pos: int = 0, strings: list[str] | None = None) -> None:
        self.data = data
        self.pos = pos
        self.strings = [] if strings is None else strings

    def string(self) -> str:
        data = self.data
        if self.pos >= len(data):
            raise FrameFormatError("truncated string tag")
        tag = data[self.pos]
        self.pos += 1
        if tag == TAG_STR_REF:
            index, self.pos = _read_varint(data, self.pos)
            if index >= len(self.strings):
                raise FrameFormatError("string reference out of range")
            return self.strings[index]
        if tag != TAG_STR_NEW:
            raise FrameFormatError(f"expected a string tag, got 0x{tag:02x}")
        index, self.pos = _read_varint(data, self.pos)
        if index != len(self.strings):
            raise FrameFormatError("string table is out of sync")
        length, self.pos = _read_varint(data, self.pos)
        end = self.pos + length
        if end > len(data):
            raise FrameFormatError("truncated string body")
        try:
            value = data[self.pos:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameFormatError("invalid UTF-8 in the string table") from exc
        self.pos = end
        self.strings.append(value)
        return value

    def value(self) -> Any:
        data = self.data
        if self.pos >= len(data):
            raise FrameFormatError("truncated value")
        tag = data[self.pos]
        self.pos += 1
        if tag == TAG_NULL:
            return None
        if tag == TAG_TRUE:
            return True
        if tag == TAG_FALSE:
            return False
        if tag == TAG_INT8:
            if self.pos + 1 > len(data):
                raise FrameFormatError("truncated int8")
            value = _S8.unpack_from(data, self.pos)[0]
            self.pos += 1
            return value
        if tag == TAG_INT:
            raw, self.pos = _read_varint(data, self.pos)
            return _unzigzag(raw)
        if tag == TAG_F64:
            if self.pos + 8 > len(data):
                raise FrameFormatError("truncated float")
            value = _F64.unpack_from(data, self.pos)[0]
            self.pos += 8
            return value
        if tag in (TAG_STR_REF, TAG_STR_NEW):
            self.pos -= 1
            return self.string()
        if tag == TAG_DICT:
            count, self.pos = _read_varint(data, self.pos)
            return {self.string(): self.value() for _ in range(count)}
        if tag == TAG_LIST:
            count, self.pos = _read_varint(data, self.pos)
            return [self.value() for _ in range(count)]
        raise FrameFormatError(f"unknown value tag 0x{tag:02x}")


def _write_path(buf: bytearray, encoder: _Encoder, path: tuple[Any, ...]) -> None:
    _write_varint(buf, len(path))
    for step in path:
        if isinstance(step, int):
            buf.append(STEP_INDEX)
            _write_varint(buf, step)
        else:
            buf.append(STEP_KEY)
            encoder.string(buf, step)


def _read_path(decoder: _Decoder) -> tuple[Any, ...]:
    count, decoder.pos = _read_varint(decoder.data, decoder.pos)
    steps: list[Any] = []
    for _ in range(count):
        if decoder.pos >= len(decoder.data):
            raise FrameFormatError("truncated path step")
        kind = decoder.data[decoder.pos]
        decoder.pos += 1
        if kind == STEP_KEY:
            steps.append(decoder.string())
        elif kind == STEP_INDEX:
            index, decoder.pos = _read_varint(decoder.data, decoder.pos)
            steps.append(index)
        else:
            raise FrameFormatError(f"unknown path step 0x{kind:02x}")
    return tuple(steps)


# ---------------------------------------------------------------------------
# diffing


def _same(left: Any, right: Any) -> bool:
    # `type(...) is type(...)` keeps True/1 and 0/0.0 apart, so a type change is
    # always emitted instead of being swallowed by Python equality.
    return type(left) is type(right) and left == right


def _diff(previous: Any, current: Any, path: tuple[Any, ...],
          sets: list[tuple[tuple[Any, ...], Any]], dels: list[tuple[Any, ...]]) -> None:
    if isinstance(previous, dict) and isinstance(current, dict):
        previous_keys, current_keys = set(previous), set(current)
        for key in sorted(previous_keys - current_keys):
            dels.append(path + (key,))
        for key in sorted(current_keys - previous_keys):
            sets.append((path + (key,), current[key]))
        for key in sorted(current_keys & previous_keys):
            _diff(previous[key], current[key], path + (key,), sets, dels)
        return
    if _same(previous, current):
        return
    # Lists are atomic: one SET replaces the whole list. The engine's lists (the
    # events window, action segments, cargo) are either reprinted wholesale or
    # change together with neighbouring leaves, so element-level diffing would
    # buy a few bytes at the cost of index-shift bugs.
    sets.append((path, current))


def diff_state(previous: dict[str, Any],
               current: dict[str, Any]) -> tuple[list[tuple[tuple[Any, ...], Any]], list[tuple[Any, ...]]]:
    sets: list[tuple[tuple[Any, ...], Any]] = []
    dels: list[tuple[Any, ...]] = []
    _diff(previous, current, (), sets, dels)
    return sets, dels


def _assign(state: Any, path: tuple[Any, ...], value: Any) -> None:
    if not path:
        raise FrameFormatError("delta path must not be empty")
    node = state
    for step in path[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError) as exc:
            raise FrameFormatError(f"delta path {path!r} does not exist") from exc
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(node, list) or not 0 <= last < len(node):
            raise FrameFormatError(f"delta index {path!r} is out of range")
    elif not isinstance(node, dict):
        raise FrameFormatError(f"delta path {path!r} targets a non-map")
    node[last] = value


def _delete(state: Any, path: tuple[Any, ...]) -> None:
    if not path:
        raise FrameFormatError("delta path must not be empty")
    node = state
    for step in path[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError) as exc:
            raise FrameFormatError(f"delta path {path!r} does not exist") from exc
    last = path[-1]
    if not isinstance(node, dict) or last not in node:
        raise FrameFormatError(f"delta path {path!r} cannot be deleted")
    del node[last]


# ---------------------------------------------------------------------------
# payload codecs


def encode_keyframe(encoder: _Encoder, state: dict[str, Any]) -> bytes:
    buf = bytearray()
    encoder.value(buf, state)
    return bytes(buf)


def decode_keyframe(data: bytes) -> tuple[dict[str, Any], _Decoder]:
    decoder = _Decoder(data)
    state = decoder.value()
    if not isinstance(state, dict):
        raise FrameFormatError("keyframe body must be a map")
    return state, decoder


def encode_delta(encoder: _Encoder, sets: list[tuple[tuple[Any, ...], Any]],
                 dels: list[tuple[Any, ...]]) -> bytes:
    buf = bytearray()
    _write_varint(buf, len(sets))
    for path, value in sets:
        _write_path(buf, encoder, path)
        encoder.value(buf, value)
    _write_varint(buf, len(dels))
    for path in dels:
        _write_path(buf, encoder, path)
    return bytes(buf)


def apply_delta(payload: bytes, state: dict[str, Any], strings: list[str]) -> list[str]:
    """Apply one delta payload to `state` in place; returns the grown string table."""
    decoder = _Decoder(payload, 0, strings)
    count, pos = _read_varint(payload, 0)
    pending: list[tuple[tuple[Any, ...], Any]] = []
    for _ in range(count):
        decoder.pos = pos
        path = _read_path(decoder)
        pending.append((path, decoder.value()))
        pos = decoder.pos
    removal_count, pos = _read_varint(payload, pos)
    removals: list[tuple[Any, ...]] = []
    for _ in range(removal_count):
        decoder.pos = pos
        removals.append(_read_path(decoder))
        pos = decoder.pos
    if pos != len(payload):
        raise FrameFormatError("delta payload has trailing bytes")
    for path in removals:
        _delete(state, path)
    for path, value in pending:
        _assign(state, path, value)
    return decoder.strings


# ---------------------------------------------------------------------------
# writer


def _prepare_static(static: dict[str, Any]) -> dict[str, Any]:
    detached = json.loads(canonical(static))
    published = detached.get("map")
    if isinstance(published, dict) and "workpoints" in published:
        if published.get("workpoints") == published.get("points"):
            # The engine exposes the same mapping under both names; storing it
            # twice cost 5 KB of JSON for no information.
            published.pop("workpoints", None)
    return detached


class FrameWriter:
    """Stream snapshots to a ``.rcf`` file without keeping the match in memory."""

    def __init__(self, path: str | Path, static: dict[str, Any], *, tick_ms: int,
                 duration_ms: int, keyframe_interval: int = 100,
                 max_keyframe_bytes: int = 65536, max_delta_ops: int = 512,
                 flush_interval: int = 100) -> None:
        if keyframe_interval < 1:
            raise ValueError("keyframe_interval must be positive")
        self.path = Path(path)
        self.tick_ms = int(tick_ms)
        self.duration_ms = int(duration_ms)
        self.keyframe_interval = int(keyframe_interval)
        self.max_keyframe_bytes = int(max_keyframe_bytes)
        self.max_delta_ops = int(max_delta_ops)
        self.flush_interval = max(1, int(flush_interval))
        self._encoder = _Encoder()
        self._previous: dict[str, Any] | None = None
        self._entries: list[tuple[int, int, int, int, int, int, int, int]] = []
        self._keyframes: list[int] = []
        self._closed = False
        self._static = _prepare_static(static)
        self._static_bytes = zlib.compress(canonical(self._static).encode("utf-8"), 6)
        self._stream_offset = HEADER_SIZE + len(self._static_bytes)
        self._fp = self.path.open("w+b")
        # A valid but unfinalised header, so a crashed writer is distinguishable
        # from a file that is not a frame stream at all.
        self._fp.write(self._header(0, 0, 0, 0))
        self._fp.write(self._static_bytes)

    def _header(self, frames: int, index_offset: int, index_len: int,
                index_crc: int, final_hash: str = "") -> bytes:
        digest = bytes.fromhex(final_hash)[:32] if final_hash else b""
        return _HEADER.pack(
            MAGIC, FORMAT_VERSION, 0,
            self.tick_ms, self.duration_ms, self.keyframe_interval,
            frames, frames,
            HEADER_SIZE, len(self._static_bytes), zlib.crc32(self._static_bytes),
            self._stream_offset, index_offset, index_len, index_crc,
            len(self._keyframes), 0, digest.ljust(32, b"\x00"),
        )

    # -- public ------------------------------------------------------------

    @property
    def frames(self) -> int:
        return len(self._entries)

    @property
    def keyframe_count(self) -> int:
        return len(self._keyframes)

    def append(self, state: dict[str, Any]) -> int:
        """Append one boundary snapshot and return its frame number."""
        if self._closed:
            raise RuntimeError("frame writer is closed")
        index = len(self._entries)
        payload = {key: value for key, value in state.items() if key not in STATIC_KEYS}
        sets: list[tuple[tuple[Any, ...], Any]] = []
        dels: list[tuple[Any, ...]] = []
        is_keyframe = (
            self._previous is None
            or index % self.keyframe_interval == 0
        )
        if not is_keyframe:
            sets, dels = diff_state(self._previous, payload)
            body = encode_delta(self._encoder, sets, dels)
            if len(sets) + len(dels) > self.max_delta_ops or len(body) > self.max_keyframe_bytes:
                is_keyframe = True
        if is_keyframe:
            # Every keyframe starts a self-contained block: the interned string
            # table restarts here, so a reader can decode this frame without
            # having walked the preceding ones.
            self._encoder.table.clear()
            self._encoder.strings.clear()
            body = encode_keyframe(self._encoder, payload)
        crc = zlib.crc32(body)
        offset = self._fp.tell()
        head = bytearray([KEYFRAME if is_keyframe else DELTA])
        _write_varint(head, len(body))
        head += _U32.pack(crc)
        self._fp.write(head)
        self._fp.write(body)
        if is_keyframe:
            self._keyframes.append(index)
        self._entries.append((
            offset,
            len(head) + len(body),
            int(payload.get("seq", index)),
            int(payload.get("sim_time_ms", index * self.tick_ms)),
            crc,
            1 if is_keyframe else 0,
            min(255, len(sets) + len(dels)),
            0,
            len(self._keyframes) - 1,
        ))
        # Advancing `previous` is the single most fragile line in this module:
        # skipping it (for instance on the keyframe branch) desynchronises every
        # following frame while all checksums still verify.
        self._previous = payload
        if index % self.flush_interval == 0:
            self._fp.flush()
        return index

    def close(self, final_hash: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        index_offset = self._fp.tell()
        block = bytearray(INDEX_MAGIC)
        block += struct.pack("<IIII", len(self._entries), _INDEX_ENTRY_SIZE, len(self._keyframes), 0)
        for entry in self._entries:
            block += _INDEX_ENTRY.pack(*entry)
        for frame in self._keyframes:
            block += _U32.pack(frame)
        index_crc = zlib.crc32(block)
        block += _U32.pack(index_crc)
        self._fp.write(block)
        self._fp.seek(0)
        self._fp.write(self._header(len(self._entries), index_offset, len(block), index_crc, final_hash))
        self._fp.flush()
        self._fp.close()

    def abort(self) -> None:
        """Keep whatever was flushed; the header stays unfinalised on purpose."""
        if not self._closed:
            self._closed = True
            self._fp.flush()
            self._fp.close()


# ---------------------------------------------------------------------------
# reader


class FrameReader:
    """Random access over one ``.rcf`` file.

    ``state()`` returns a deep copy by default so callers may mutate it. Pass
    ``copy=False`` for the internal tree when the result is only read (or
    serialised) before the next call, which is what replay verification does.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = self.path.read_bytes()
        if len(self._data) < HEADER_SIZE:
            raise FrameFormatError("frame stream is shorter than its header")
        (magic, version, flags, tick_ms, duration_ms, interval, written, total,
         static_offset, static_len, static_crc, stream_offset, index_offset,
         index_len, index_crc, keyframe_count, _pad, final_hash) = _HEADER.unpack_from(self._data, 0)
        if magic != MAGIC:
            raise FrameFormatError("not an .rcf frame stream")
        if version != FORMAT_VERSION:
            raise FrameFormatError(f"unsupported frame format {version}")
        if flags:
            raise FrameFormatError("unknown frame stream flags")
        if index_offset == 0:
            raise FrameFormatError("frame stream was not finalised")
        self.tick_ms = tick_ms
        self.duration_ms = duration_ms
        self.keyframe_interval = interval
        self.frames_written = written
        self.frame_count = total
        self.final_hash = final_hash.rstrip(b"\x00").hex()
        self.static_offset = static_offset
        self.static_len = static_len
        self.static_crc32 = static_crc
        self.stream_offset = stream_offset
        self.index_offset = index_offset
        self.keyframe_count = keyframe_count
        if static_offset + static_len > len(self._data):
            raise FrameFormatError("static block lies outside the file")
        if index_offset + index_len > len(self._data):
            raise FrameFormatError("index block lies outside the file")
        if index_offset + index_len != len(self._data):
            raise FrameFormatError("unexpected trailing bytes after the index block")
        raw_static = self._data[static_offset:static_offset + static_len]
        if zlib.crc32(raw_static) != static_crc:
            raise FrameFormatError("static block checksum mismatch")
        try:
            raw = zlib.decompress(raw_static)
        except zlib.error as exc:
            raise FrameFormatError("static block is not valid zlib data") from exc
        try:
            self._static = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameFormatError("static block is not valid JSON") from exc
        published = self._static.get("map")
        if isinstance(published, dict) and "points" in published and "workpoints" not in published:
            published["workpoints"] = published["points"]
        self._load_index(index_len, index_crc, keyframe_count)
        self._cache_index = -1
        self._cache: dict[str, Any] | None = None
        self._strings: list[str] = []

    def __len__(self) -> int:
        return self.frame_count

    # -- index -------------------------------------------------------------

    def _load_index(self, index_len: int, index_crc: int, keyframe_count: int) -> None:
        block = self._data[self.index_offset:self.index_offset + index_len]
        if len(block) != index_len or len(block) < _INDEX_HEAD.size + 4:
            raise FrameFormatError("index block is truncated")
        stored_crc = _U32.unpack_from(block, len(block) - 4)[0]
        body = block[:-4]
        if zlib.crc32(body) != stored_crc:
            raise FrameFormatError("index checksum mismatch")
        if stored_crc != index_crc:
            raise FrameFormatError("index checksum does not match the header")
        magic, count, entry_size, keyframes, _reserved = _INDEX_HEAD.unpack_from(body, 0)
        if magic != INDEX_MAGIC:
            raise FrameFormatError("bad index magic")
        if entry_size != _INDEX_ENTRY_SIZE:
            raise FrameFormatError(f"unexpected index entry size {entry_size}")
        if count != self.frame_count:
            raise FrameFormatError("index does not cover every frame")
        if keyframes != keyframe_count:
            raise FrameFormatError("index keyframe count disagrees with the header")
        offset = _INDEX_HEAD.size
        entries = []
        for _ in range(count):
            if offset + _INDEX_ENTRY_SIZE > len(body):
                raise FrameFormatError("index entry table is truncated")
            entries.append(_INDEX_ENTRY.unpack_from(body, offset))
            offset += _INDEX_ENTRY_SIZE
        table: list[int] = []
        for _ in range(keyframes):
            if offset + 4 > len(body):
                raise FrameFormatError("keyframe table is truncated")
            table.append(_U32.unpack_from(body, offset)[0])
            offset += 4
        if offset != len(body):
            raise FrameFormatError("index block has trailing bytes")
        if count and (not table or table[0] != 0):
            raise FrameFormatError("frame 0 must be a keyframe")
        if any(b <= a for a, b in zip(table, table[1:])):
            raise FrameFormatError("keyframe table is not increasing")
        for frame, entry in enumerate(entries):
            if entry[8] != bisect_right(table, frame) - 1:
                raise FrameFormatError(f"frame {frame} points at the wrong keyframe")
        self._entries = entries
        self._keyframes = table

    # -- access ------------------------------------------------------------

    @property
    def static(self) -> dict[str, Any]:
        return self._static

    def meta(self) -> list[dict[str, Any]]:
        return [
            {"frame": frame, "seq": entry[2], "sim_time_ms": entry[3],
             "keyframe": bool(entry[5]), "changes": entry[6], "bytes": entry[1]}
            for frame, entry in enumerate(self._entries)
        ]

    def seek_time(self, sim_time_ms: int) -> int:
        """Return the index of the first frame at or after ``sim_time_ms``."""
        if not self._entries:
            raise IndexError("frame stream is empty")
        low, high = 0, len(self._entries)
        while low < high:
            mid = (low + high) // 2
            if self._entries[mid][3] < sim_time_ms:
                low = mid + 1
            else:
                high = mid
        return min(low, len(self._entries) - 1)

    def state(self, frame: int, *, include_static: bool = False, copy_state: bool = True) -> dict[str, Any]:
        if not 0 <= frame < self.frame_count:
            raise IndexError(f"frame {frame} out of range 0..{self.frame_count - 1}")
        self._advance_to(frame)
        state = self._cache if not copy_state else copy.deepcopy(self._cache)
        if include_static:
            merged = dict(state)
            merged.update(self._static)
            return merged
        return state

    def states(self, start: int, count: int, *, include_static: bool = False) -> list[dict[str, Any]]:
        if count < 0:
            raise ValueError("count must be non-negative")
        start = max(0, min(start, self.frame_count))
        end = min(self.frame_count, start + count)
        return [self.state(frame, include_static=include_static) for frame in range(start, end)]

    def iter_states(self, *, include_static: bool = False) -> Iterator[dict[str, Any]]:
        for frame in range(self.frame_count):
            yield self.state(frame, include_static=include_static)

    def _advance_to(self, frame: int) -> None:
        if self._cache is not None and self._cache_index == frame:
            return
        block_start = bisect_right(self._keyframes, frame) - 1
        if (self._cache is None or self._cache_index > frame
                or self._cache_index < self._keyframes[block_start]):
            state = self._decode_keyframe(self._keyframes[block_start])
            cursor = self._keyframes[block_start]
        else:
            state = self._cache
            cursor = self._cache_index
        while cursor < frame:
            cursor += 1
            kind, payload = self._record(cursor)
            if kind != DELTA:
                raise FrameFormatError(f"frame {cursor} is an unexpected keyframe")
            self._strings = apply_delta(payload, state, self._strings)
        self._cache = state
        self._cache_index = frame

    def _record(self, frame: int) -> tuple[int, bytes]:
        offset, length, _seq, _time, crc, _flag, _changes, _reserved, _block = self._entries[frame]
        chunk = self._data[offset:offset + length]
        if len(chunk) != length:
            raise FrameFormatError(f"frame {frame} record is truncated")
        kind = chunk[0]
        size, pos = _read_varint(chunk, 1)
        if pos + 4 + size != len(chunk):
            raise FrameFormatError(f"frame {frame} payload length disagrees with its record")
        stored = _U32.unpack_from(chunk, pos)[0]
        payload = chunk[pos + 4:]
        if zlib.crc32(payload) != stored or stored != crc:
            raise FrameFormatError(f"frame {frame} checksum mismatch")
        return kind, payload

    def _decode_keyframe(self, frame: int) -> dict[str, Any]:
        kind, payload = self._record(frame)
        if kind != KEYFRAME:
            raise FrameFormatError(f"frame {frame} should be a keyframe")
        state, decoder = decode_keyframe(payload)
        self._strings = decoder.strings
        return state

    # -- integrity ---------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Decode every frame, checking record framing and crc32 along the way."""
        for frame in range(self.frame_count):
            self.state(frame, copy_state=False)
        return {"frames": self.frame_count, "keyframes": self.keyframe_count,
                "bytes": len(self._data), "final_hash": self.final_hash}


def read_static(path: str | Path) -> dict[str, Any]:
    return FrameReader(path).static
