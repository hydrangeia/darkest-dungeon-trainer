"""Native regression harness for the DD1 Inventory::System removal hook.

This copies the target function from the installed PE into an executable page
owned by this Python process. Only that private copy is patched and executed;
the game process and executable file are never written or opened as a process.

The merge helper is replaced by a `ret` stub so these tests cover the removal
routine's own count/status behavior, not stack consolidation. Security-cookie
access/checks and external helper calls are redirected to local test stubs.
"""

from __future__ import annotations

import ctypes as C
import hashlib
import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".deps"))

import pefile  # noqa: E402
from hooks import CONFIG, inventory_stub, near_jump  # noqa: E402
from memory_win import Process  # noqa: E402


GAME = Path(os.environ.get('DD_GAME_EXE', r"F:\steam\steamapps\common\DarkestDungeon\_windows\win64\Darkest.exe"))
EXPECTED_SHA256 = "4d78fbfaa65b7d002d2208d02da809200f4a5723119521180e30100d6f1a75f2"
FUNCTION_RVA = 0x5D5B90
FUNCTION_END_RVA = 0x5D5CCB
FUNCTION_SIZE = FUNCTION_END_RVA - FUNCTION_RVA
PATCH_SITE_RVA = 0x5D5BFE
PATCH_SITE_OFFSET = PATCH_SITE_RVA - FUNCTION_RVA
PATCH_SITE_BYTES = bytes.fromhex("2b d7 89 13 85 d2")
ITEM_STRIDE = 0x5A8

CODE_SIZE = 0x2000
BASE_COPY_OFFSET = 0x000
HOOKED_COPY_OFFSET = 0x200
CAVE_OFFSET = 0x500
CLEAR_STUB_OFFSET = 0x800
MERGE_STUB_OFFSET = 0x820
COOKIE_STUB_OFFSET = 0x830
ERROR_STUB_OFFSET = 0x840
PREDICATE_TRUE_OFFSET = 0x850
PREDICATE_FALSE_OFFSET = 0x860
CLEANUP_STUB_OFFSET = 0x870
CONFIG_OFFSET = 0x1800

TYPE_HASH = {
    "raid": 0x01073744,
    "provision": 0x72F14069,
    "trinket_store": 0x1854C54D,
    "estate": 0x0CB28342,
}


class Inventory(C.Structure):
    _fields_ = [
        ("vptr", C.c_uint64),
        ("items_begin", C.c_uint64),
        ("items_end", C.c_uint64),
        ("opaque_18", C.c_uint64),
        ("config_type", C.c_uint32),
        ("active_count", C.c_int32),
    ]


class CallbackObject(C.Structure):
    _fields_ = [("vtable", C.POINTER(C.c_void_p))]


class PredicateHolder(C.Structure):
    _fields_ = [("padding", C.c_ubyte * 0x38), ("callable", C.c_void_p)]


def _relative_call(code: bytearray, copy_base: int, call_rva: int, target: int) -> None:
    offset = call_rva - FUNCTION_RVA
    if code[offset] != 0xE8:
        raise AssertionError(f"expected direct CALL at RVA 0x{call_rva:x}")
    displacement = target - (copy_base + offset + 5)
    if not -(1 << 31) <= displacement < (1 << 31):
        raise AssertionError("local helper stub is outside rel32 range")
    struct.pack_into("<i", code, offset + 1, displacement)


@unittest.skipUnless(os.name == "nt" and C.sizeof(C.c_void_p) == 8, "Windows x64 only")
class NativeInventoryHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not GAME.is_file():
            raise unittest.SkipTest(f"Darkest Dungeon executable not found: {GAME}")

        executable = GAME.read_bytes()
        digest = hashlib.sha256(executable).hexdigest()
        if digest != EXPECTED_SHA256:
            raise unittest.SkipTest(
                "This native harness is pinned to the researched PE build; "
                f"found SHA-256 {digest}"
            )

        pe = pefile.PE(data=executable, fast_load=True)
        mapped = pe.get_memory_mapped_image()
        start = FUNCTION_RVA
        end = FUNCTION_END_RVA
        original = bytearray(mapped[start:end])
        if len(original) != FUNCTION_SIZE:
            raise AssertionError("PE RVA conversion returned an unexpected function length")
        site = PATCH_SITE_OFFSET
        if original[site : site + len(PATCH_SITE_BYTES)] != PATCH_SITE_BYTES:
            raise AssertionError("removal-site bytes differ from the researched build")

        cls.process = Process(os.getpid())
        if cls.process.pid != os.getpid():
            cls.process.close()
            raise AssertionError("native harness must allocate only in its own Python process")
        cls.page = cls.process.allocate(CODE_SIZE)
        cls.config_address = cls.page + CONFIG_OFFSET

        # Short local host stubs: clear count, bypass stack merge/security
        # check, provide true/false callback results, and safely neutralize the
        # unreachable null-predicate error import in the copied function.
        cls._write(CLEAR_STUB_OFFSET, bytes.fromhex("c7 01 00 00 00 00 c3"))
        cls._write(MERGE_STUB_OFFSET, b"\xc3")
        cls._write(COOKIE_STUB_OFFSET, b"\xc3")
        cls._write(ERROR_STUB_OFFSET, b"\xc3")
        cls._write(PREDICATE_TRUE_OFFSET, bytes.fromhex("b0 01 c3"))
        cls._write(PREDICATE_FALSE_OFFSET, bytes.fromhex("b0 00 c3"))
        cls._write(CLEANUP_STUB_OFFSET, b"\xc3")

        base_address = cls.page + BASE_COPY_OFFSET
        hooked_address = cls.page + HOOKED_COPY_OFFSET
        for copy_offset, copy_address in (
            (BASE_COPY_OFFSET, base_address),
            (HOOKED_COPY_OFFSET, hooked_address),
        ):
            code = bytearray(original)
            # Remove only the copied image's RIP-relative cookie dependency;
            # the following xor/save pair and epilogue still execute.
            cookie_load = 0x5D5BA6 - FUNCTION_RVA
            code[cookie_load : cookie_load + 7] = bytes.fromhex("31 c0 90 90 90 90 90")
            for call_rva, stub_offset in (
                (0x5D5C09, CLEAR_STUB_OFFSET),
                (0x5D5C7A, MERGE_STUB_OFFSET),
                (0x5D5CA8, COOKIE_STUB_OFFSET),
            ):
                _relative_call(code, copy_address, call_rva, cls.page + stub_offset)

            # The valid test predicate never takes the null-object branch, but
            # its original FF 15 operand points into Darkest.exe's IAT. Redirect
            # that exceptional path into a local harmless ret instead of letting
            # a harness bug call through a stale game-image address.
            error_call = 0x5D5CC4 - FUNCTION_RVA
            if code[error_call : error_call + 2] != b"\xff\x15":
                raise AssertionError("expected null-predicate IAT call at 0x5D5CC4")
            displacement = (cls.page + ERROR_STUB_OFFSET) - (copy_address + error_call + 5)
            code[error_call : error_call + 6] = b"\xe8" + struct.pack("<i", displacement) + b"\x90"
            cls._write(copy_offset, code)

        # Install the real project hook stub over the copied six-byte site.
        hook_address = cls.page + CAVE_OFFSET
        hook_code = inventory_stub(cls.config_address, hooked_address + PATCH_SITE_OFFSET + 6)
        cls._write(CAVE_OFFSET, hook_code)
        replacement = near_jump(
            hooked_address + PATCH_SITE_OFFSET,
            hook_address,
            size=len(PATCH_SITE_BYTES),
        )
        cls._write(HOOKED_COPY_OFFSET + PATCH_SITE_OFFSET, replacement)

        cls.base_function = C.CFUNCTYPE(C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p)(base_address)
        cls.hooked_function = C.CFUNCTYPE(C.c_uint32, C.c_void_p, C.c_uint32, C.c_void_p)(hooked_address)
        cls._write(CONFIG_OFFSET, bytes(64))

    @classmethod
    def _write(cls, offset: int, data: bytes) -> None:
        cls.process.write(cls.page + offset, data)

    @classmethod
    def tearDownClass(cls):
        process = getattr(cls, "process", None)
        if process is not None:
            process.close()

    def invoke(self, hooked, item_counts, request, config_type, predicate=True, items_enabled=False, shop_enabled=False):
        item_memory = C.create_string_buffer(max(1, len(item_counts) * ITEM_STRIDE))
        for index, count in enumerate(item_counts):
            struct.pack_into("<i", item_memory, index * ITEM_STRIDE, count)

        system = Inventory()
        system.items_begin = C.addressof(item_memory)
        system.items_end = system.items_begin + len(item_counts) * ITEM_STRIDE
        system.config_type = config_type
        system.active_count = 0x1234  # verify the real routine recomputes it

        predicate_address = self.page + (
            PREDICATE_TRUE_OFFSET if predicate else PREDICATE_FALSE_OFFSET
        )
        vtable = (C.c_void_p * 5)()
        vtable[2] = predicate_address  # callback vtable slot +0x10
        vtable[4] = self.page + CLEANUP_STUB_OFFSET  # slot +0x20
        callable_object = CallbackObject(C.cast(vtable, C.POINTER(C.c_void_p)))
        holder = PredicateHolder()
        holder.callable = C.addressof(callable_object)

        config = bytearray(64)
        struct.pack_into("<I", config, CONFIG["items"], int(items_enabled))
        struct.pack_into("<I", config, CONFIG["shop"], int(shop_enabled))
        self.process.write(self.config_address, config)

        function = self.hooked_function if hooked else self.base_function
        status = function(C.addressof(system), request, C.addressof(holder))
        counts_after = [
            struct.unpack_from("<i", item_memory, index * ITEM_STRIDE)[0]
            for index in range(len(item_counts))
        ]
        return status, counts_after, system.active_count

    def test_unhooked_game_function_decrements_and_reports_shortage(self):
        # These calls execute the copied, unhooked game function first, proving
        # the native harness is exercising its actual stack-count path.
        self.assertEqual(self.invoke(False, [5], 2, TYPE_HASH["raid"]), (1, [3], 1))
        self.assertEqual(self.invoke(False, [5], 5, TYPE_HASH["raid"]), (3, [0], 0))
        self.assertEqual(self.invoke(False, [5], 7, TYPE_HASH["raid"]), (7, [0], 0))

    def test_items_hook_preserves_stack_for_less_equal_and_greater_requests(self):
        cases = ((2, 1), (5, 1), (7, 5))
        for request, expected_status in cases:
            with self.subTest(request=request):
                result = self.invoke(
                    True, [5], request, TYPE_HASH["raid"], items_enabled=True
                )
                self.assertEqual(result, (expected_status, [5], 1))

    def test_multi_stack_request_spans_matching_stacks_without_decreasing_them(self):
        result = self.invoke(
            True,
            [2, 3],
            4,
            TYPE_HASH["raid"],
            items_enabled=True,
        )
        self.assertEqual(result, (1, [2, 3], 2))

    def test_shop_hook_preserves_provision_and_trinket_store_counts(self):
        for kind in ("provision", "trinket_store"):
            with self.subTest(kind=kind):
                result = self.invoke(
                    True,
                    [4],
                    1,
                    TYPE_HASH[kind],
                    shop_enabled=True,
                )
                self.assertEqual(result, (1, [4], 1))

    def test_disabled_toggles_and_other_inventory_types_match_unhooked_behavior(self):
        cases = (
            (TYPE_HASH["raid"], False, True),
            (TYPE_HASH["provision"], True, False),
            (TYPE_HASH["trinket_store"], True, False),
            (TYPE_HASH["estate"], True, True),
        )
        for config_type, items_enabled, shop_enabled in cases:
            with self.subTest(config_type=hex(config_type)):
                baseline = self.invoke(False, [5], 2, config_type)
                hooked = self.invoke(
                    True,
                    [5],
                    2,
                    config_type,
                    items_enabled=items_enabled,
                    shop_enabled=shop_enabled,
                )
                self.assertEqual(hooked, baseline)

    def test_nonmatching_predicate_and_other_inventory_type_are_unchanged(self):
        nonmatching = self.invoke(
            True,
            [5],
            1,
            TYPE_HASH["raid"],
            predicate=False,
            items_enabled=True,
        )
        self.assertEqual(nonmatching, (4, [5], 1))

        estate = self.invoke(
            True,
            [5],
            2,
            TYPE_HASH["estate"],
            items_enabled=True,
            shop_enabled=True,
        )
        self.assertEqual(estate, (1, [3], 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
