import struct
import unittest

from save_engine import DsonDocument, DsonError, MAGIC


def build_dson(tree):
    """Build a small valid DSON fixture from nested dicts and typed scalars."""

    records = []
    meta1 = []

    def visit(name, value, parent_index):
        meta2_index = len(records)
        if isinstance(value, dict):
            object_index = len(meta1)
            meta1.append([parent_index, meta2_index, len(value), 0])
            record = {"name": name, "object_index": object_index, "value": None}
            records.append(record)
            for child_name, child_value in value.items():
                visit(child_name, child_value, object_index)
            descendant_count = len(records) - meta2_index - 1
            meta1[object_index][3] = descendant_count
        else:
            record = {"name": name, "object_index": None, "value": value}
            records.append(record)

    for root_name, root_value in tree.items():
        visit(root_name, root_value, -1)

    data = bytearray()
    meta2 = []
    for index, record in enumerate(records):
        name = record["name"].encode("utf-8") + b"\x00"
        name_offset = len(data)
        data.extend(name)
        object_index = record["object_index"]
        is_object = object_index is not None
        field_info = (len(name) << 2) | (1 if is_object else 0)
        if is_object:
            field_info |= object_index << 11
        hashed_name = string_hash(record["name"])
        if hashed_name >= 0x80000000:
            hashed_name -= 0x100000000
        meta2.append((hashed_name, name_offset, field_info))
        if is_object:
            continue

        while len(data) % 4:
            data.append(0)
        value = record["value"]
        kind, raw_value = value
        if kind == "int32":
            data.extend(struct.pack("<i", raw_value))
        elif kind == "float32":
            data.extend(struct.pack("<f", raw_value))
        elif kind == "string":
            encoded = raw_value.encode("utf-8") + b"\x00"
            data.extend(struct.pack("<i", len(encoded)))
            data.extend(encoded)
        elif kind == "embedded":
            data.extend(struct.pack("<i", len(raw_value)))
            data.extend(raw_value)
        elif kind == "bytes":
            data.extend(raw_value)
        else:
            raise AssertionError(f"unsupported fixture value type {kind}")

    meta1_bytes = b"".join(struct.pack("<iiii", *entry) for entry in meta1)
    meta1_offset = 64
    meta2_offset = meta1_offset + len(meta1_bytes)
    data_offset = meta2_offset + len(meta2) * 12
    header = struct.pack(
        "<4s4sIIIIIQQIIIII",
        MAGIC,
        b"\x00\x00\x01\x00",
        64,
        0,
        len(meta1_bytes),
        len(meta1),
        meta1_offset,
        0,
        0,
        len(meta2),
        meta2_offset,
        0,
        len(data),
        data_offset,
    )
    meta2_bytes = b"".join(struct.pack("<iII", *entry) for entry in meta2)
    return header + meta1_bytes + meta2_bytes + bytes(data)


def string_hash(value):
    result = 0
    for byte in value.encode("utf-8"):
        result = (result * 53 + byte) & 0xFFFFFFFF
    return result


def estate_tree(embedded_hero):
    wallet = {}
    for index, (resource, amount) in enumerate(
        (("gold", 100), ("bust", 2), ("portrait", 3), ("deed", 4), ("crest", 5))
    ):
        wallet[str(index)] = {"amount": ("int32", amount), "type": ("string", resource)}
    return {
        "base_root": {
            "wallet": wallet,
            "heroes": {"7": {"hero_file_data": {"raw_data": ("embedded", embedded_hero)}}},
            "unknown_preserved": {"vendor_value": ("int32", 0x12345678)},
        }
    }


class DsonDocumentTests(unittest.TestCase):
    def setUp(self):
        hero = build_dson(
            {
                "base_root": {
                    "actor": {
                        "m_Stress": ("float32", 42.5),
                        "current_hp": ("float32", 18.0),
                        "unrecognized_future_field": ("bytes", b"opaque"),
                    },
                    "other_unknown": {"opaque": ("string", "keep this")},
                }
            }
        )
        self.hero = hero
        self.original = build_dson(estate_tree(hero))
        self.document = DsonDocument.from_bytes(self.original)

    def test_wallet_read_and_write_uses_saved_type_ids(self):
        self.assertEqual(
            self.document.read_wallet(),
            {"gold": 100, "bust": 2, "portrait": 3, "deed": 4, "crest": 5},
        )
        self.document.write_wallet("gold", 9000)
        self.document.write_wallet("bust", 99)
        self.assertEqual(self.document.read_wallet()["gold"], 9000)
        self.assertEqual(self.document.read_wallet()["bust"], 99)
        self.assertEqual(self.document.read_wallet()["portrait"], 3)

    def test_embedded_hero_stress_is_float32_and_unknown_data_is_untouched(self):
        before = self.document.to_bytes()
        changes = self.document.write_named_float("m_Stress", 0.0)
        self.assertEqual(len(changes), 1)
        stress_field = self.document.find_named_fields("m_Stress")[0]
        self.assertEqual(self.document.read_scalar(stress_field.path, "float32"), 0.0)

        after = self.document.to_bytes()
        changed_indexes = {i for i, (old, new) in enumerate(zip(before, after)) if old != new}
        allowed = set(range(changes[0].offset, changes[0].offset + 4))
        self.assertTrue(changed_indexes)
        self.assertLessEqual(changed_indexes, allowed)
        self.assertEqual(
            self.document.read_scalar(
                ("base_root", "heroes", "7", "hero_file_data", "raw_data", "base_root", "actor", "current_hp"),
                "float32",
            ),
            18.0,
        )

    def test_scalar_writer_rejects_objects_and_invalid_resource_amounts(self):
        with self.assertRaises(DsonError):
            self.document.write_scalar(("base_root", "wallet"), 1, "int32")
        with self.assertRaises(ValueError):
            self.document.write_wallet("gold", -1)
        with self.assertRaises(ValueError):
            self.document.write_wallet("shard", 1)

    def test_bad_magic_and_bad_header_are_rejected(self):
        with self.assertRaises(DsonError):
            DsonDocument.from_bytes(b"not a save")
        corrupt = bytearray(self.original)
        struct.pack_into("<I", corrupt, 60, 999999)
        with self.assertRaises(DsonError):
            DsonDocument.from_bytes(corrupt)


if __name__ == "__main__":
    unittest.main()
