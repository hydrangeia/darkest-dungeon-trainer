"""Conservative reader and in-place scalar patcher for Darkest Dungeon DSON saves.

The format does not store primitive value types in its metadata. Callers must
specify the scalar type when reading or writing. This module never rebuilds a
DSON document: an update replaces exactly four bytes at a verified field
payload offset and leaves the header, metadata, and all other fields intact.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Iterator, Literal, Sequence


ScalarKind = Literal["int32", "float32"]
MAGIC = b"\x01\xb1\x00\x00"
HEADER_SIZE = 64
_HEADER = struct.Struct("<4s4sIIIIIQQIIIII")
_META1_ENTRY = struct.Struct("<iiii")
_META2_ENTRY = struct.Struct("<iII")
_WALLET_RESOURCES = frozenset({"gold", "bust", "portrait", "deed", "crest"})


class DsonError(ValueError):
    """Raised when a DSON file or requested field is not safe to edit."""


@dataclass(frozen=True)
class DsonField:
    """A field's decoded name and original byte ranges.

    ``value_offset`` is the absolute offset in the root save buffer. Primitive
    values have a payload offset; object fields have ``None``. ``path`` is the
    full object path, including the containing ``raw_data`` path for embedded
    hero files.
    """

    name: str
    path: tuple[str, ...]
    is_object: bool
    meta2_index: int
    object_index: int | None
    name_offset: int
    value_offset: int | None
    alignment_skip: int
    field_end: int


@dataclass(frozen=True)
class ScalarChange:
    path: tuple[str, ...]
    kind: ScalarKind
    old_value: int | float
    new_value: int | float
    offset: int


class DsonDocument:
    """Parse DSON metadata and expose byte-preserving scalar edits.

    Construct this with bytes from a single ``.json`` save file. Embedded
    ``raw_data`` DSON files share the same mutable backing buffer, so a nested
    field edit updates its original bytes without reserializing either file.
    """

    def __init__(
        self,
        data: bytes | bytearray,
        *,
        _root: bytearray | None = None,
        _base_offset: int = 0,
        _length: int | None = None,
        _prefix: tuple[str, ...] = (),
    ) -> None:
        if _root is None:
            self._root = data if isinstance(data, bytearray) else bytearray(data)
            self._base_offset = 0
            self._length = len(self._root)
            self._prefix = ()
        else:
            self._root = _root
            self._base_offset = _base_offset
            self._length = _length if _length is not None else len(_root) - _base_offset
            self._prefix = _prefix
        self._fields: list[DsonField] = []
        self._embedded_cache: list[DsonDocument] | None = None
        self._parse()

    @classmethod
    def from_bytes(cls, data: bytes | bytearray) -> "DsonDocument":
        return cls(data)

    @property
    def fields(self) -> tuple[DsonField, ...]:
        """Fields directly contained by this DSON document, in metadata order."""

        return tuple(self._fields)

    @property
    def revision(self) -> bytes:
        return bytes(self._root[self._base_offset + 4 : self._base_offset + 8])

    def to_bytes(self) -> bytes:
        """Return the complete root buffer, including all embedded save data."""

        return bytes(self._root)

    def _parse(self) -> None:
        base = self._base_offset
        end = base + self._length
        if self._length < HEADER_SIZE or end > len(self._root):
            raise DsonError("DSON document is shorter than its header or exceeds its parent field")
        header = _HEADER.unpack_from(self._root, base)
        magic, _revision, header_length, _zeroes, meta1_size, meta1_count, meta1_offset, _z2, _z3, meta2_count, meta2_offset, _z4, data_length, data_offset = header
        if magic != MAGIC:
            raise DsonError("File does not have the Darkest Dungeon DSON magic number")
        if header_length != HEADER_SIZE or meta1_offset != header_length:
            raise DsonError("Unsupported DSON header layout")
        if min(meta1_count, meta2_count, data_length) < 0:
            raise DsonError("DSON header contains a negative count or length")
        if meta1_size != meta1_count * _META1_ENTRY.size:
            raise DsonError("DSON Meta1 size does not match its declared entry count")
        meta1_end = meta1_offset + meta1_size
        meta2_size = meta2_count * _META2_ENTRY.size
        data_end = data_offset + data_length
        if meta1_end != meta2_offset or meta2_offset + meta2_size != data_offset:
            raise DsonError("DSON metadata offsets are inconsistent")
        if data_end != self._length:
            raise DsonError("DSON data length does not match the containing file")

        meta1: list[tuple[int, int, int, int]] = []
        for index in range(meta1_count):
            offset = base + meta1_offset + index * _META1_ENTRY.size
            meta1.append(_META1_ENTRY.unpack_from(self._root, offset))

        data_start = base + data_offset
        meta2: list[tuple[int, int, int]] = []
        for index in range(meta2_count):
            offset = base + meta2_offset + index * _META2_ENTRY.size
            meta2.append(_META2_ENTRY.unpack_from(self._root, offset))

        starts = [entry[1] for entry in meta2]
        if len(starts) != len(set(starts)):
            raise DsonError("DSON fields have duplicate data offsets")
        if any(offset < 0 or offset >= data_length for offset in starts):
            raise DsonError("DSON field offset falls outside the data block")

        parsed: list[tuple[int, int, int, str, bool, int | None, int | None, int]] = []
        for index, (name_hash, name_rel, field_info) in enumerate(meta2):
            name_length = (field_info >> 2) & 0x1FF
            if name_length < 1 or name_rel + name_length > data_length:
                raise DsonError(f"DSON field {index} has an invalid name length")
            name_bytes = bytes(self._root[data_start + name_rel : data_start + name_rel + name_length])
            if name_bytes[-1:] != b"\x00" or b"\x00" in name_bytes[:-1]:
                raise DsonError(f"DSON field {index} has a malformed null-terminated name")
            try:
                name = name_bytes[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise DsonError(f"DSON field {index} has a non-UTF-8 name") from exc
            if _string_hash(name) != (name_hash & 0xFFFFFFFF):
                raise DsonError(f"DSON field {index} name hash does not match {name!r}")

            is_object = bool(field_info & 1)
            object_index = ((field_info >> 11) & 0xFFFFF) if is_object else None
            if is_object and (object_index is None or object_index >= meta1_count):
                raise DsonError(f"DSON object field {name!r} references an invalid Meta1 entry")
            next_rel = min((candidate for candidate in starts if candidate > name_rel), default=data_length)
            value_rel: int | None = None
            alignment_skip = 0
            if not is_object:
                unaligned_value_rel = name_rel + name_length
                alignment_skip = (4 - (unaligned_value_rel % 4)) % 4
                # Primitive booleans and chars are byte-aligned. DSON does not
                # encode a type tag, so defer the aligned offset until a caller
                # requests a known int32/float32/string field.
                value_rel = unaligned_value_rel
            parsed.append(
                (
                    index,
                    name_rel,
                    field_info,
                    name,
                    is_object,
                    object_index,
                    None if value_rel is None else data_start + value_rel,
                    alignment_skip,
                    data_start + next_rel,
                )
            )

        # The object child counts and parent indexes define the field paths.
        # Metadata order is the format's traversal order; data offsets are used
        # separately to locate each field's payload without rewriting it.
        stack: list[dict[str, int | tuple[str, ...]]] = []
        encountered_objects = 0
        for index, _name_rel, _info, name, is_object, object_index, value_offset, alignment_skip, field_end in parsed:
            if stack:
                parent_index = int(stack[-1]["object_index"])
                path = tuple(stack[-1]["path"]) + (name,)
                stack[-1]["remaining"] = int(stack[-1]["remaining"]) - 1
            else:
                parent_index = -1
                path = self._prefix + (name,)

            if is_object:
                assert object_index is not None
                stored_parent, stored_meta2_index, direct_children, _all_children = meta1[object_index]
                if stored_parent != parent_index:
                    raise DsonError(f"DSON object {name!r} has an unexpected parent index")
                if stored_meta2_index != index:
                    raise DsonError(f"DSON object {name!r} Meta1 and Meta2 indexes disagree")
                encountered_objects += 1
            self._fields.append(
                DsonField(
                    name=name,
                    path=path,
                    is_object=is_object,
                    meta2_index=index,
                    object_index=object_index,
                    name_offset=data_start + _name_rel,
                    value_offset=value_offset,
                    alignment_skip=alignment_skip,
                    field_end=field_end,
                )
            )
            if is_object:
                assert object_index is not None
                stack.append({"object_index": object_index, "path": path, "remaining": meta1[object_index][2]})
            while stack and int(stack[-1]["remaining"]) == 0:
                stack.pop()
            if stack and int(stack[-1]["remaining"]) < 0:
                raise DsonError("DSON object child counts are inconsistent")
        if stack:
            raise DsonError("DSON object child counts leave unconsumed fields")
        if encountered_objects != meta1_count:
            raise DsonError("DSON Meta1 object count does not match the Meta2 object fields")

    def find_field(self, path: Sequence[str] | str, *, recursive: bool = True) -> DsonField:
        normalized = _normalize_path(path)
        matches = [field for field in self.fields if field.path == normalized]
        if not matches and recursive:
            for embedded in self.embedded_documents():
                try:
                    return embedded.find_field(normalized, recursive=True)
                except KeyError:
                    pass
        if not matches:
            raise KeyError("DSON field not found: " + "/".join(normalized))
        if len(matches) > 1:
            raise DsonError("DSON field path is ambiguous: " + "/".join(normalized))
        return matches[0]

    def read_scalar(self, path: Sequence[str] | str, kind: ScalarKind) -> int | float:
        field = self.find_field(path)
        return self._read_scalar_field(field, kind)

    def write_scalar(self, path: Sequence[str] | str, value: int | float, kind: ScalarKind) -> ScalarChange:
        field = self.find_field(path)
        return self._write_scalar_field(field, value, kind)

    def _read_scalar_field(self, field: DsonField, kind: ScalarKind) -> int | float:
        offset = _require_scalar_offset(field) + field.alignment_skip
        fmt = _scalar_struct(kind)
        if offset + fmt.size > field.field_end:
            raise DsonError(f"Field {'/'.join(field.path)} is too short for {kind}")
        return fmt.unpack_from(self._root, offset)[0]

    def _write_scalar_field(self, field: DsonField, value: int | float, kind: ScalarKind) -> ScalarChange:
        offset = _require_scalar_offset(field) + field.alignment_skip
        fmt = _scalar_struct(kind)
        if offset + fmt.size > field.field_end:
            raise DsonError(f"Field {'/'.join(field.path)} is too short for {kind}")
        old_value = fmt.unpack_from(self._root, offset)[0]
        if isinstance(value, bool):
            raise ValueError(f"Boolean values cannot be written as {kind}")
        if kind == "int32" and not isinstance(value, int):
            raise ValueError("DSON int32 values must be integers")
        if kind == "float32":
            try:
                finite_value = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("DSON float32 values must be numeric") from exc
            if not math.isfinite(finite_value):
                raise ValueError("DSON float values must be finite")
        try:
            encoded = fmt.pack(value)
        except (OverflowError, struct.error, TypeError) as exc:
            raise ValueError(f"Value {value!r} cannot be represented as {kind}") from exc
        self._root[offset : offset + fmt.size] = encoded
        new_value = fmt.unpack_from(self._root, offset)[0]
        return ScalarChange(field.path, kind, old_value, new_value, offset)

    def read_string(self, path: Sequence[str] | str) -> str:
        field = self.find_field(path)
        offset = _require_scalar_offset(field) + field.alignment_skip
        if offset + 4 > field.field_end:
            raise DsonError(f"String field {'/'.join(field.path)} has no length prefix")
        length = struct.unpack_from("<i", self._root, offset)[0]
        if length < 1 or offset + 4 + length > field.field_end:
            raise DsonError(f"String field {'/'.join(field.path)} has an invalid byte length")
        raw = bytes(self._root[offset + 4 : offset + 4 + length])
        if raw[-1:] != b"\x00":
            raise DsonError(f"String field {'/'.join(field.path)} is missing its null terminator")
        try:
            return raw[:-1].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DsonError(f"String field {'/'.join(field.path)} is not UTF-8") from exc

    def embedded_documents(self) -> tuple["DsonDocument", ...]:
        """Return embedded DSON documents carried by ``raw_data`` string fields."""

        if self._embedded_cache is not None:
            return tuple(self._embedded_cache)
        documents: list[DsonDocument] = []
        for field in self._fields:
            if field.is_object or field.name != "raw_data" or field.value_offset is None:
                continue
            value_offset = field.value_offset + field.alignment_skip
            if value_offset + 8 > field.field_end:
                continue
            byte_length = struct.unpack_from("<i", self._root, value_offset)[0]
            if byte_length < HEADER_SIZE or value_offset + 4 + byte_length > field.field_end:
                continue
            nested_offset = value_offset + 4
            if bytes(self._root[nested_offset : nested_offset + 4]) != MAGIC:
                continue
            documents.append(
                DsonDocument(
                    self._root,
                    _root=self._root,
                    _base_offset=nested_offset,
                    _length=byte_length,
                    _prefix=field.path,
                )
            )
        self._embedded_cache = documents
        return tuple(documents)

    def all_fields(self) -> Iterator[DsonField]:
        yield from self._fields
        for embedded in self.embedded_documents():
            yield from embedded.all_fields()

    def find_named_fields(self, name: str, *, recursive: bool = True) -> tuple[DsonField, ...]:
        if not recursive:
            return tuple(field for field in self._fields if field.name == name)
        return tuple(field for field in self.all_fields() if field.name == name)

    def write_named_float(self, name: str, value: float, *, recursive: bool = True) -> tuple[ScalarChange, ...]:
        fields = self.find_named_fields(name, recursive=recursive)
        changes = []
        for field in fields:
            changes.append(self._write_scalar_field(field, value, "float32"))
        return tuple(changes)

    def read_wallet(self) -> dict[str, int]:
        """Read the five estate wallet quantities by their saved type strings."""

        wallet_path = self._prefix + ("base_root", "wallet")
        wallet = self.find_field(wallet_path, recursive=False)
        if not wallet.is_object:
            raise DsonError("Estate wallet is not a DSON object")
        values: dict[str, int] = {}
        wallet_depth = len(wallet_path)
        for entry in self._fields:
            if not entry.is_object or len(entry.path) != wallet_depth + 1 or entry.path[:wallet_depth] != wallet_path:
                continue
            item_path = entry.path
            item_type = self.read_string(item_path + ("type",))
            if item_type not in _WALLET_RESOURCES:
                continue
            if item_type in values:
                raise DsonError(f"Estate wallet contains duplicate {item_type!r} entries")
            values[item_type] = int(self.read_scalar(item_path + ("amount",), "int32"))
        missing = _WALLET_RESOURCES.difference(values)
        if missing:
            raise DsonError("Estate wallet is missing resource entries: " + ", ".join(sorted(missing)))
        return values

    def write_wallet(self, resource: str, amount: int) -> ScalarChange:
        """Update one existing wallet amount; this never creates a new DSON field."""

        if resource not in _WALLET_RESOURCES:
            raise ValueError(f"Unknown estate resource: {resource!r}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Estate resource amounts must be non-negative integers")
        wallet_path = self._prefix + ("base_root", "wallet")
        wallet = self.find_field(wallet_path, recursive=False)
        if not wallet.is_object:
            raise DsonError("Estate wallet is not a DSON object")
        wallet_depth = len(wallet_path)
        matches: list[tuple[str, ...]] = []
        for entry in self._fields:
            if not entry.is_object or len(entry.path) != wallet_depth + 1 or entry.path[:wallet_depth] != wallet_path:
                continue
            item_path = entry.path
            try:
                item_type = self.read_string(item_path + ("type",))
            except KeyError:
                continue
            if item_type == resource:
                matches.append(item_path)
        if len(matches) != 1:
            raise DsonError(f"Expected one estate wallet entry for {resource!r}, found {len(matches)}")
        return self.write_scalar(matches[0] + ("amount",), amount, "int32")


def _normalize_path(path: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(path, str):
        return tuple(part for part in path.split("/") if part)
    return tuple(str(part) for part in path)


def _require_scalar_offset(field: DsonField) -> int:
    if field.is_object or field.value_offset is None:
        raise DsonError(f"Field {'/'.join(field.path)} is an object, not a scalar")
    return field.value_offset


def _scalar_struct(kind: ScalarKind) -> struct.Struct:
    if kind == "int32":
        return struct.Struct("<i")
    if kind == "float32":
        return struct.Struct("<f")
    raise ValueError(f"Unsupported scalar kind: {kind!r}")


def _string_hash(value: str) -> int:
    result = 0
    for byte in value.encode("utf-8"):
        result = (result * 53 + byte) & 0xFFFFFFFF
    return result
