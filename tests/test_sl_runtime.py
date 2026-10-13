import contextlib
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sl_runtime
from save_engine import MAGIC
from save_runtime import SaveManager
from sl_runtime import CheckpointManager


def _hash_name(value):
    result = 0
    for byte in value.encode("utf-8"):
        result = (result * 53 + byte) & 0xFFFFFFFF
    return result


def _build_dson(tree):
    records = []
    meta1 = []

    def visit(name, value, parent_index):
        meta2_index = len(records)
        if isinstance(value, dict):
            object_index = len(meta1)
            meta1.append([parent_index, meta2_index, len(value), 0])
            records.append({"name": name, "object_index": object_index, "value": None})
            for child_name, child_value in value.items():
                visit(child_name, child_value, object_index)
            meta1[object_index][3] = len(records) - meta2_index - 1
        else:
            records.append({"name": name, "object_index": None, "value": value})

    for root_name, root_value in tree.items():
        visit(root_name, root_value, -1)

    data = bytearray()
    meta2 = []
    for record in records:
        name = record["name"].encode("utf-8") + b"\0"
        name_offset = len(data)
        data.extend(name)
        object_index = record["object_index"]
        is_object = object_index is not None
        info = (len(name) << 2) | (1 if is_object else 0)
        if is_object:
            info |= object_index << 11
        hashed = _hash_name(record["name"])
        if hashed >= 0x80000000:
            hashed -= 0x100000000
        meta2.append((hashed, name_offset, info))
        if is_object:
            continue
        while len(data) % 4:
            data.append(0)
        kind, value = record["value"]
        if kind == "int32":
            data.extend(struct.pack("<i", value))
        elif kind == "float32":
            data.extend(struct.pack("<f", value))
        elif kind == "string":
            encoded = value.encode("utf-8") + b"\0"
            data.extend(struct.pack("<i", len(encoded)))
            data.extend(encoded)
        else:
            raise AssertionError(f"unsupported synthetic DSON value: {kind}")

    meta1_data = b"".join(struct.pack("<iiii", *row) for row in meta1)
    meta1_offset = 64
    meta2_offset = meta1_offset + len(meta1_data)
    data_offset = meta2_offset + 12 * len(meta2)
    header = struct.pack(
        "<4s4sIIIIIQQIIIII",
        MAGIC, b"\0\0\1\0", 64, 0, len(meta1_data), len(meta1), meta1_offset,
        0, 0, len(meta2), meta2_offset, 0, len(data), data_offset,
    )
    meta2_data = b"".join(struct.pack("<iII", *row) for row in meta2)
    return header + meta1_data + meta2_data + bytes(data)


def _estate_bytes():
    wallet = {}
    for index, (name, amount) in enumerate((("gold", 100), ("bust", 2), ("portrait", 3), ("deed", 4), ("crest", 5))):
        wallet[str(index)] = {"amount": ("int32", amount), "type": ("string", name)}
    return _build_dson({"base_root": {"wallet": wallet}})


def _roster_bytes(stress=24.0):
    return _build_dson({"base_root": {"heroes": {"1": {"m_Stress": ("float32", stress)}}}})


def _tree(profile):
    files = {}
    dirs = set()
    for path in profile.rglob("*"):
        relative = path.relative_to(profile).as_posix()
        if path.is_dir():
            dirs.add(relative)
        else:
            files[relative] = path.read_bytes()
    return files, dirs


class CheckpointRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "steam-user" / "262060" / "remote"
        self.remote.mkdir(parents=True)
        self.checkpoint_root = self.root / "backups" / "checkpoints"
        self.profile0 = self._make_profile("profile_0", b"slot0")
        self.profile1 = self._make_profile("profile_1", b"slot1")
        self.manager = self._manager(self.checkpoint_root)

    def _make_profile(self, name, payload):
        profile = self.remote / name
        (profile / "nested" / "empty").mkdir(parents=True)
        (profile / "persist.estate.json").write_bytes(_estate_bytes())
        (profile / "persist.roster.json").write_bytes(_roster_bytes())
        (profile / "campaign.bin").write_bytes(payload)
        (profile / "nested" / "party.bin").write_bytes(payload + b"-party")
        return profile

    def _manager(self, checkpoint_root):
        saves = SaveManager(self.root / "unused-save-backups")
        saves.roots = lambda: [self.remote]
        saves.guard = lambda: None
        return CheckpointManager(saves, checkpoint_root)

    def test_whole_slot_restore_removes_new_files_restores_deleted_files_and_can_be_undone(self):
        original_files, original_dirs = _tree(self.profile0)
        os.utime(self.profile0 / "campaign.bin", (1_000_000_000, 1_000_000_000))
        original_files["campaign.bin"] = (self.profile0 / "campaign.bin").read_bytes()
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "known good")
        self.assertEqual(checkpoint["kind"], "manual")
        self.assertEqual(checkpoint["file_count"], len(original_files))
        self.assertEqual(checkpoint["bytes"], sum(map(len, original_files.values())))

        (self.profile0 / "campaign.bin").write_bytes(b"later campaign progress")
        (self.profile0 / "nested" / "party.bin").unlink()
        shutil.rmtree(self.profile0 / "nested" / "empty")
        (self.profile0 / "added" / "child").mkdir(parents=True)
        (self.profile0 / "added" / "child" / "new.bin").write_bytes(b"new progress")
        (self.profile0 / "added-root.bin").write_bytes(b"also new")
        later_files, later_dirs = _tree(self.profile0)

        restored = self.manager.restore_checkpoint(checkpoint["id"])
        self.assertTrue(restored["success"])
        self.assertEqual(_tree(self.profile0), (original_files, original_dirs))
        self.assertGreater((self.profile0 / "campaign.bin").stat().st_mtime, 1_000_000_000)
        safety = next(item for item in self.manager.list_checkpoints(str(self.profile0))
                      if item["id"] == restored["safety_checkpoint_id"])
        self.assertEqual(safety["kind"], "before_restore")

        undone = self.manager.restore_checkpoint(safety["id"])
        self.assertTrue(undone["success"])
        self.assertEqual(_tree(self.profile0), (later_files, later_dirs))

    def test_checkpoint_history_persists_and_is_isolated_by_slot(self):
        first = self.manager.create_checkpoint(str(self.profile0), "first")
        (self.profile0 / "campaign.bin").write_bytes(b"second state")
        second = self.manager.create_checkpoint(str(self.profile0), "second")
        other = self.manager.create_checkpoint(str(self.profile1), "other slot")

        restarted_manager = self._manager(self.checkpoint_root)
        slot0 = restarted_manager.list_checkpoints(str(self.profile0))
        slot1 = restarted_manager.list_checkpoints(str(self.profile1))
        self.assertEqual({item["id"] for item in slot0}, {first["id"], second["id"]})
        self.assertEqual({item["id"] for item in slot1}, {other["id"]})
        self.assertEqual({item["label"] for item in slot0}, {"first", "second"})

    def test_backup_root_cannot_overlap_profile_and_rejects_before_creating_inside_it(self):
        nested_root = self.profile0 / "trainer-checkpoints"
        manager = self._manager(nested_root)
        before = _tree(self.profile0)
        with self.assertRaisesRegex(ValueError, "不能与存档目录重叠"):
            manager.create_checkpoint(str(self.profile0), "unsafe location")
        self.assertEqual(_tree(self.profile0), before)
        self.assertFalse(nested_root.exists())

        ancestor_manager = self._manager(self.remote)
        with self.assertRaisesRegex(ValueError, "不能与存档目录重叠"):
            ancestor_manager.create_checkpoint(str(self.profile0), "ancestor location")

    def test_backup_overlap_uses_canonical_paths_for_short_name_aliases(self):
        alias_root = self.root / "PROFILE~1" / "checkpoints"
        manager = self._manager(alias_root)
        canonical_nested_root = self.profile0 / "canonical-checkpoints"
        path_type = type(alias_root)
        real_resolve = path_type.resolve

        def resolve_alias(path, strict=False):
            if path == alias_root:
                return canonical_nested_root
            return real_resolve(path, strict=strict)

        with patch.object(path_type, "resolve", new=resolve_alias):
            with self.assertRaisesRegex(ValueError, "不能与存档目录重叠"):
                manager.create_checkpoint(str(self.profile0), "short path alias")
        self.assertFalse(alias_root.exists())
        self.assertFalse(canonical_nested_root.exists())

    def test_hash_consistent_but_unusable_checkpoint_profiles_are_rejected(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "valid initially")
        folder = self.checkpoint_root / checkpoint["id"]
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot = folder / "snapshot"

        # Even a self-consistent empty tree is not a restorable game profile.
        shutil.rmtree(snapshot)
        snapshot.mkdir()
        manifest["entries"] = []
        manifest["file_count"] = 0
        manifest["bytes"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "缺少有效存档所需的核心文件"):
            self.manager.list_checkpoints(str(self.profile0))
        with self.assertRaisesRegex(ValueError, "缺少有效存档所需的核心文件"):
            self.manager.restore_checkpoint(checkpoint["id"])

        # A matching manifest hash does not make malformed core DSON usable.
        (snapshot / "persist.estate.json").write_bytes(b"not DSON")
        (snapshot / "persist.roster.json").write_bytes(_roster_bytes())
        manifest["entries"] = []
        for name in ("persist.estate.json", "persist.roster.json"):
            data = (snapshot / name).read_bytes()
            manifest["entries"].append({
                "path": name,
                "kind": "file",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        manifest["file_count"] = 2
        manifest["bytes"] = sum(entry["size"] for entry in manifest["entries"])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "核心 DSON 文件不可读"):
            self.manager.list_checkpoints(str(self.profile0))
        self.assertTrue((self.profile0 / "persist.estate.json").is_file())

    def test_restore_stage_collision_is_never_cleaned_as_owned(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "stage collision")
        (self.profile0 / "campaign.bin").write_bytes(b"current progress")
        # Construct the sibling name without allowing restore to own or remove it.
        sentinel = self.remote / (".profile_0.ddtrainer-stage-" + "b" * 32)
        sentinel.mkdir()
        (sentinel / "owner.txt").write_text("preexisting", encoding="utf-8")
        identities = iter((SimpleNamespace(hex="a" * 32),
                           SimpleNamespace(hex="b" * 32),
                           SimpleNamespace(hex="c" * 32)))
        with patch.object(sl_runtime.uuid, "uuid4", side_effect=lambda: next(identities)):
            with self.assertRaisesRegex(RuntimeError, "编号冲突"):
                self.manager.restore_checkpoint(checkpoint["id"])
        self.assertEqual((sentinel / "owner.txt").read_text(encoding="utf-8"), "preexisting")
        self.assertTrue(sentinel.is_dir())

    def test_rollback_checks_old_before_moving_or_removing_valid_target(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "verified target")
        _manifest, target_entries, _profile = self.manager._load_checkpoint(checkpoint["id"])
        current_entries = self.manager._scan_tree(self.profile0)
        old = self.remote / ".profile_0.ddtrainer-old-test"
        stage = self.remote / ".profile_0.ddtrainer-stage-test"
        shutil.copytree(self.profile0, old)
        (old / "campaign.bin").write_bytes(b"damaged old save")
        shutil.rmtree(self.profile0)
        shutil.copytree(self.checkpoint_root / checkpoint["id"] / "snapshot", self.profile0)
        target_tree = _tree(self.profile0)

        with self.assertRaisesRegex(RuntimeError, "原存档目录校验失败"):
            self.manager._rollback_swap(self.profile0, old, stage, current_entries, target_entries)

        self.assertEqual(_tree(self.profile0), target_tree)
        self.assertEqual((old / "campaign.bin").read_bytes(), b"damaged old save")
        self.assertFalse(list(self.remote.glob(".profile_0.ddtrainer-quarantine-*")))

    def test_rollback_preserves_target_if_restoring_verified_old_directory_fails(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "verified target")
        _manifest, target_entries, _profile = self.manager._load_checkpoint(checkpoint["id"])
        original_entries = self.manager._scan_tree(self.profile0)
        old = self.remote / ".profile_0.ddtrainer-old-restore-failure"
        stage = self.remote / ".profile_0.ddtrainer-stage-restore-failure"
        shutil.copytree(self.profile0, old)
        shutil.rmtree(self.profile0)
        shutil.copytree(self.checkpoint_root / checkpoint["id"] / "snapshot", self.profile0)
        target_tree = _tree(self.profile0)
        real_replace = os.replace

        def fail_old_restore(source, destination):
            if Path(source) == old and Path(destination) == self.profile0:
                raise OSError("injected pre-rename failure")
            return real_replace(source, destination)

        with patch.object(sl_runtime.os, "replace", side_effect=fail_old_restore):
            with self.assertRaisesRegex(OSError, "pre-rename failure"):
                self.manager._rollback_swap(self.profile0, old, stage, original_entries, target_entries)

        self.assertEqual(_tree(self.profile0), target_tree)
        self.assertEqual(self.manager._scan_tree(old), original_entries)
        self.assertFalse(list(self.remote.glob(".profile_0.ddtrainer-quarantine-*")))

    def test_corrupt_snapshot_malicious_manifest_and_path_escape_are_rejected(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "integrity")
        before = _tree(self.profile0)
        with self.assertRaisesRegex(ValueError, "编号无效"):
            self.manager.restore_checkpoint("../profile_0")
        self.assertEqual(_tree(self.profile0), before)

        folder = self.checkpoint_root / checkpoint["id"]
        manifest_path = folder / "manifest.json"
        original_manifest = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(original_manifest)
        manifest["profile"] = str(self.root / "outside" / "profile_0")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.restore_checkpoint(checkpoint["id"])
        self.assertEqual(_tree(self.profile0), before)

        manifest_path.write_text(original_manifest, encoding="utf-8")
        (folder / "untracked.bin").write_bytes(b"not in manifest")
        with self.assertRaisesRegex(ValueError, "未登记内容"):
            self.manager.list_checkpoints(str(self.profile0))
        (folder / "untracked.bin").unlink()

        manifest = json.loads(original_manifest)
        manifest["entries"][0]["path"] = "../../outside.bin"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "越界路径"):
            self.manager.restore_checkpoint(checkpoint["id"])
        self.assertEqual(_tree(self.profile0), before)

        manifest_path.write_text(original_manifest, encoding="utf-8")
        with (folder / "snapshot" / "campaign.bin").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "快照与清单哈希不符"):
            self.manager.list_checkpoints(str(self.profile0))
        with self.assertRaisesRegex(ValueError, "快照与清单哈希不符"):
            self.manager.restore_checkpoint(checkpoint["id"])
        self.assertEqual(_tree(self.profile0), before)

    def test_game_running_rejects_create_and_restore(self):
        current = _tree(self.profile0)
        with patch.object(self.manager.saves, "guard", side_effect=ValueError("game is running")):
            with self.assertRaisesRegex(ValueError, "game is running"):
                self.manager.create_checkpoint(str(self.profile0), "blocked")
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "allowed")
        (self.profile0 / "campaign.bin").write_bytes(b"later")
        later = _tree(self.profile0)
        with patch.object(self.manager.saves, "guard", side_effect=ValueError("game is running")):
            with self.assertRaisesRegex(ValueError, "game is running"):
                self.manager.restore_checkpoint(checkpoint["id"])
        self.assertEqual(_tree(self.profile0), later)
        self.assertNotEqual(current, later)

    def test_source_change_during_copy_aborts_and_removes_staging(self):
        original_copy = self.manager._copy_tree
        changed = False

        def copy_then_mutate(source, destination, mtime_ns=None):
            nonlocal changed
            original_copy(source, destination, mtime_ns)
            if source == self.profile0 and not changed:
                changed = True
                (self.profile0 / "campaign.bin").write_bytes(b"changed during copy")

        with patch.object(self.manager, "_copy_tree", side_effect=copy_then_mutate):
            with self.assertRaisesRegex(ValueError, "复制期间发生变化|复制期间发生变化|检查点未提交"):
                self.manager.create_checkpoint(str(self.profile0), "unstable")
        self.assertTrue(changed)
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(any(path.name.startswith(".staging-") for path in self.checkpoint_root.iterdir()))

    def test_swap_failure_after_rename_completion_restores_original_tree(self):
        checkpoint = self.manager.create_checkpoint(str(self.profile0), "target")
        (self.profile0 / "campaign.bin").write_bytes(b"live progress before failed restore")
        (self.profile0 / "new-file.bin").write_bytes(b"keep on failure")
        before = _tree(self.profile0)
        real_replace = os.replace
        raised = False

        def replace_then_raise(source, destination):
            nonlocal raised
            result = real_replace(source, destination)
            if (not raised and Path(destination) == self.profile0
                    and ".ddtrainer-stage-" in Path(source).name):
                raised = True
                raise OSError("injected failure after stage rename")
            return result

        with patch.object(sl_runtime.os, "replace", side_effect=replace_then_raise):
            with self.assertRaisesRegex(RuntimeError, "原存档已回滚"):
                self.manager.restore_checkpoint(checkpoint["id"])
        self.assertTrue(raised)
        self.assertEqual(_tree(self.profile0), before)
        siblings = list(self.remote.glob(".profile_0.ddtrainer-*"))
        self.assertEqual(siblings, [])
        self.assertTrue(any(item["kind"] == "before_restore" for item in self.manager.list_checkpoints(str(self.profile0))))

    def test_source_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.profile0 / "escape-link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(ValueError, "符号链接或 junction"):
            self.manager.create_checkpoint(str(self.profile0), "contains link")

    def test_reparse_directory_is_rejected_even_when_junction_creation_is_unavailable(self):
        junction = self.profile0 / "nested" / "simulated-junction"
        junction.mkdir()
        real_check = sl_runtime._is_reparse

        def mark_as_junction(path):
            return Path(path) == junction or real_check(Path(path))

        with patch.object(sl_runtime, "_is_reparse", side_effect=mark_as_junction):
            with self.assertRaisesRegex(ValueError, "符号链接或 junction"):
                self.manager.create_checkpoint(str(self.profile0), "junction")


if __name__ == "__main__":
    unittest.main(verbosity=2)
