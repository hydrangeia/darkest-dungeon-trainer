"""Verified whole-slot save checkpoints for Darkest Dungeon.

Every checkpoint is an immutable directory snapshot with a manifest of paths,
sizes, and SHA-256 hashes. Restore swaps a verified staging directory beside
the live slot and first records the current slot as a ``before_restore`` point.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import uuid

from save_engine import DsonDocument, DsonError
from save_runtime import ROOT, SaveManager


_CHECKPOINT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_LIMIT = 16 * 1024 * 1024
_REPARSE_POINT = 0x400


class SnapshotNotQuietError(ValueError):
    """The game's on-disk save tree changed or has not settled yet."""


class SnapshotCancelledError(ValueError):
    """An automatic snapshot was cancelled before it could be published."""


def _sha_file(path: Path) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    after = path.stat(follow_symlinks=False)
    if (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None)) != (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", None),
    ):
        raise ValueError(f"文件在校验期间发生变化：{path.name}")
    if size != after.st_size:
        raise ValueError(f"文件在读取期间发生变化：{path.name}")
    return digest.hexdigest(), size


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        info = path.lstat()
    except FileNotFoundError:
        return False
    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def _reject_reparse_chain(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if _is_reparse(component):
            raise ValueError(f"路径包含符号链接或 junction，拒绝访问：{component}")


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(left)))) == os.path.normcase(
        os.path.normpath(os.path.abspath(str(right)))
    )


def _safe_relative(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw or "\x00" in raw:
        raise ValueError("检查点清单包含无效相对路径")
    pure = PurePosixPath(raw)
    parts = pure.parts
    if pure.is_absolute() or not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("检查点清单包含越界路径")
    if pure.as_posix() != raw:
        raise ValueError("检查点清单路径格式无效")
    return parts


class CheckpointManager:
    def __init__(self, save_manager: SaveManager | None = None, backup_root: str | Path | None = None):
        self.saves = save_manager or SaveManager()
        self.backup_root = Path(backup_root) if backup_root is not None else ROOT / "backups" / "checkpoints"
        self._lock = threading.RLock()

    def _ensure_backup_root(self, create: bool) -> Path | None:
        root = Path(os.path.abspath(self.backup_root))
        _reject_reparse_chain(root)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            return None
        if _is_reparse(root) or not root.is_dir():
            raise ValueError("检查点根目录无效或包含 reparse point")
        resolved = root.resolve(strict=True)
        _reject_reparse_chain(resolved)
        return resolved

    def _checked_profile(self, profile_path: str | Path) -> Path:
        if not isinstance(profile_path, (str, Path)) or not str(profile_path):
            raise ValueError("请选择自动识别的有效存档")
        requested = Path(profile_path)
        if requested.exists() and _is_reparse(requested):
            raise ValueError("存档目录不能是符号链接或 junction")
        _reject_reparse_chain(requested)
        profile = self.saves.checked_profile(str(profile_path))
        if _is_reparse(profile) or not profile.is_dir():
            raise ValueError("存档目录无效或包含 reparse point")
        _reject_reparse_chain(profile)
        return profile

    def _reject_backup_profile_overlap(self, profile: Path) -> None:
        """A checkpoint root must not sit inside, contain, or equal a save slot."""
        profile_abs = os.path.normcase(os.path.normpath(str(profile.resolve(strict=True))))
        backup_abs = os.path.normcase(os.path.normpath(str(self.backup_root.resolve(strict=False))))
        try:
            common = os.path.normcase(os.path.normpath(os.path.commonpath((profile_abs, backup_abs))))
        except ValueError:
            return  # Different Windows drives cannot overlap.
        if common in (profile_abs, backup_abs):
            raise ValueError("检查点目录不能与存档目录重叠")

    @staticmethod
    def _scan_tree(root: Path) -> list[dict]:
        if _is_reparse(root) or not root.is_dir():
            raise ValueError(f"存档快照目录无效或包含 reparse point：{root}")
        entries: list[dict] = []
        pending = [(root, ())]
        while pending:
            directory, prefix = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ValueError(f"无法读取存档目录：{directory}") from exc
            with_children = []
            for child in children:
                path = Path(child.path)
                parts = prefix + (child.name,)
                raw = "/".join(parts)
                _safe_relative(raw)
                if _is_reparse(path):
                    raise ValueError(f"存档包含符号链接或 junction：{raw}")
                try:
                    info = path.stat(follow_symlinks=False)
                except OSError as exc:
                    raise ValueError(f"无法检查存档路径：{raw}") from exc
                if stat.S_ISDIR(info.st_mode):
                    entries.append({"path": raw, "kind": "directory", "size": 0, "sha256": ""})
                    with_children.append((path, parts))
                elif stat.S_ISREG(info.st_mode):
                    digest, size = _sha_file(path)
                    entries.append({"path": raw, "kind": "file", "size": size, "sha256": digest})
                else:
                    raise ValueError(f"存档包含不支持的文件类型：{raw}")
            pending.extend(reversed(with_children))
        return sorted(entries, key=lambda item: (item["path"].casefold(), item["path"]))

    @staticmethod
    def _tree_signature(root: Path) -> tuple[tuple[str, str, int, int], ...]:
        """Cheap size/mtime inventory used to establish a quiet save interval."""
        if _is_reparse(root) or not root.is_dir():
            raise ValueError("存档目录不可读或包含 reparse point")
        entries: list[tuple[str, str, int, int]] = []
        pending = [(root, "")]
        root_info = root.stat(follow_symlinks=False)
        entries.append(("", "directory", 0, root_info.st_mtime_ns))
        while pending:
            directory, prefix = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ValueError(f"无法检查存档目录：{directory}") from exc
            child_dirs = []
            for child in children:
                path = Path(child.path)
                relative = f"{prefix}/{child.name}" if prefix else child.name
                _safe_relative(relative)
                if _is_reparse(path):
                    raise ValueError(f"存档包含符号链接或 junction：{relative}")
                info = path.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    entries.append((relative, "directory", 0, info.st_mtime_ns))
                    child_dirs.append((path, relative))
                elif stat.S_ISREG(info.st_mode):
                    entries.append((relative, "file", info.st_size, info.st_mtime_ns))
                else:
                    raise ValueError(f"存档包含不支持的文件类型：{relative}")
            pending.extend(reversed(child_dirs))
        return tuple(sorted(entries, key=lambda item: (item[0].casefold(), item[0])))

    @staticmethod
    def _copy_tree(source: Path, destination: Path, mtime_ns: int | None = None,
                   *, destination_owned: bool = False) -> None:
        if _is_reparse(source) or not source.is_dir():
            raise ValueError("复制来源不是安全目录")
        if destination_owned:
            if not destination.is_dir() or _is_reparse(destination) or any(destination.iterdir()):
                raise ValueError("已创建的复制目标无效或非空")
        else:
            if os.path.lexists(destination):
                raise ValueError("复制目标已存在")
            destination.mkdir()
        pending = [(source, destination)]
        while pending:
            src_dir, dst_dir = pending.pop()
            try:
                children = sorted(os.scandir(src_dir), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ValueError(f"无法读取复制来源：{src_dir}") from exc
            dirs = []
            for child in children:
                src = Path(child.path)
                dst = dst_dir / child.name
                _safe_relative(dst.relative_to(destination).as_posix())
                if _is_reparse(src):
                    raise ValueError(f"复制来源包含符号链接或 junction：{src.name}")
                info = src.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    dst.mkdir()
                    dirs.append((src, dst))
                elif stat.S_ISREG(info.st_mode):
                    before = src.stat(follow_symlinks=False)
                    digest = hashlib.sha256()
                    copied = 0
                    with src.open("rb") as input_stream, dst.open("xb") as output_stream:
                        while True:
                            block = input_stream.read(1024 * 1024)
                            if not block:
                                break
                            output_stream.write(block)
                            digest.update(block)
                            copied += len(block)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                    after = src.stat(follow_symlinks=False)
                    if (before.st_size, before.st_mtime_ns, getattr(before, "st_ino", None)) != (
                        after.st_size,
                        after.st_mtime_ns,
                        getattr(after, "st_ino", None),
                    ) or copied != after.st_size:
                        raise ValueError(f"文件在复制期间发生变化：{src.name}")
                    if mtime_ns is not None:
                        os.utime(dst, ns=(mtime_ns, mtime_ns))
                else:
                    raise ValueError(f"复制来源包含不支持的文件类型：{src.name}")
            pending.extend(reversed(dirs))

    @staticmethod
    def _manifest_entries(entries: list[dict]) -> tuple[int, int]:
        files = [entry for entry in entries if entry["kind"] == "file"]
        return len(files), sum(entry["size"] for entry in files)

    @staticmethod
    def _validate_snapshot_profile(snapshot: Path, entries: list[dict], *,
                                   all_persist_dson: bool = False) -> None:
        """Require the same essential DSON structure used to discover a slot."""
        files = {entry["path"].casefold(): entry for entry in entries if entry["kind"] == "file"}
        required = ("persist.estate.json", "persist.roster.json")
        if any(name not in files for name in required):
            raise ValueError("检查点缺少有效存档所需的核心文件")
        documents = {}
        try:
            for name in required:
                entry = files[name]
                path = snapshot.joinpath(*_safe_relative(entry["path"]))
                if _is_reparse(path) or not path.is_file():
                    raise ValueError("核心存档文件无效")
                data = path.read_bytes()
                if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("核心存档文件在验证期间发生变化")
                documents[name] = DsonDocument.from_bytes(data)
            documents["persist.estate.json"].read_wallet()
            roster = documents["persist.roster.json"]
            for field in roster.find_named_fields("m_Stress"):
                roster.read_scalar(field.path, "float32")
            if all_persist_dson:
                for entry in entries:
                    relative = entry["path"].casefold()
                    if (entry["kind"] != "file" or "/" in relative
                            or not relative.startswith("persist.") or not relative.endswith(".json")):
                        continue
                    path = snapshot.joinpath(*_safe_relative(entry["path"]))
                    if _is_reparse(path) or not path.is_file():
                        raise ValueError("persist DSON文件无效")
                    data = path.read_bytes()
                    if (len(data) != entry["size"]
                            or hashlib.sha256(data).hexdigest() != entry["sha256"]):
                        raise ValueError("persist DSON文件在验证期间发生变化")
                    if entry["path"].casefold() not in documents:
                        DsonDocument.from_bytes(data)
        except (OSError, DsonError, ValueError) as exc:
            raise ValueError("检查点核心 DSON 文件不可读或不符合存档识别条件") from exc

    @staticmethod
    def _snapshot_game_week(snapshot: Path) -> int | None:
        """Read the saved campaign total_weeks field; never infer from logs."""
        path = snapshot / "persist.campaign_log.json"
        if _is_reparse(path) or not path.is_file():
            return None
        try:
            document = DsonDocument.from_bytes(path.read_bytes())
            field = document.find_field(("base_root", "total_weeks"), recursive=False)
            if field.is_object:
                return None
            week = document.read_scalar(field.path, "int32")
            if isinstance(week, bool) or not isinstance(week, int) or week < 0:
                return None
            return week
        except (OSError, DsonError, ValueError, OverflowError, KeyError):
            return None

    @staticmethod
    def _clock_ns(clock) -> int:
        value = clock()
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("自动回档点时钟必须返回带时区时间")
            return int(value.timestamp() * 1_000_000_000)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("自动回档点时钟必须返回时间戳或带时区时间")
        return int(value * 1_000_000_000)

    @staticmethod
    def _public_metadata(manifest: dict) -> dict:
        metadata = {key: manifest[key] for key in ("id", "label", "profile", "created_at", "file_count", "bytes", "kind")}
        metadata["game_week"] = manifest.get("game_week")
        return metadata

    def _new_id(self, created_at: datetime.datetime | None = None) -> tuple[str, datetime.datetime]:
        created = created_at or datetime.datetime.now(datetime.timezone.utc)
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("检查点时间必须带时区")
        created = created.astimezone(datetime.timezone.utc)
        return created.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex, created

    def _write_manifest(self, path: Path, manifest: dict) -> None:
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        if len(data) > _MANIFEST_LIMIT:
            raise ValueError("检查点清单过大")
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _assert_direct_child(self, path: Path, parent: Path, allow_missing: bool = True) -> None:
        _reject_reparse_chain(parent)
        parent_resolved = parent.resolve(strict=True)
        absolute = Path(os.path.abspath(path))
        if absolute.parent.resolve(strict=True) != parent_resolved:
            raise ValueError("临时目录不在预期父目录中")
        if os.path.lexists(absolute):
            if _is_reparse(absolute):
                raise ValueError("拒绝清理符号链接或 junction")
            resolved = absolute.resolve(strict=True)
            try:
                resolved.relative_to(parent_resolved)
            except ValueError as exc:
                raise ValueError("临时目录路径逃逸") from exc
        elif not allow_missing:
            raise ValueError("预期目录不存在")

    def _remove_tree_checked(self, path: Path, parent: Path) -> None:
        self._assert_direct_child(path, parent)
        if not os.path.lexists(path):
            return
        if path.is_dir():
            # Scan first so shutil.rmtree never follows a junction/link inside.
            self._scan_tree(path)
            shutil.rmtree(path)
        else:
            if _is_reparse(path):
                raise ValueError("拒绝删除符号链接或 junction")
            path.unlink()

    def _create_checkpoint(self, profile_path: str | Path, label: str, kind: str) -> tuple[dict, list[dict]]:
        if kind not in ("manual", "before_restore"):
            raise ValueError("检查点类型无效")
        if not isinstance(label, str) or len(label) > 240:
            raise ValueError("标签长度不能超过240个字符")
        self.saves.guard()
        profile = self._checked_profile(profile_path)
        self._reject_backup_profile_overlap(profile)
        root = self._ensure_backup_root(create=True)
        assert root is not None
        self.saves.guard()
        before = self._scan_tree(profile)
        ident, created = self._new_id()
        staging = root / (".staging-" + ident)
        final = root / ident
        self._assert_direct_child(staging, root)
        self._assert_direct_child(final, root)
        if os.path.lexists(staging) or os.path.lexists(final):
            raise ValueError("检查点编号冲突")
        staging_owned = False
        final_owned = False
        try:
            staging.mkdir()
            staging_owned = True
            snapshot = staging / "snapshot"
            self._copy_tree(profile, snapshot)
            copied = self._scan_tree(snapshot)
            self._validate_snapshot_profile(snapshot, copied)
            game_week = self._snapshot_game_week(snapshot)
            after = self._scan_tree(profile)
            if before != copied or before != after:
                raise ValueError("存档在复制期间发生变化，检查点未提交")
            self.saves.guard()
            if self._scan_tree(profile) != before:
                raise ValueError("存档在提交检查点前发生变化，检查点未提交")
            self.saves.guard()
            file_count, total_bytes = self._manifest_entries(before)
            manifest = {
                "schema_version": 1,
                "id": ident,
                "label": label.strip(),
                "profile": str(profile),
                "created_at": created.isoformat(timespec="seconds"),
                "file_count": file_count,
                "bytes": total_bytes,
                "kind": kind,
                "entries": before,
            }
            if game_week is not None:
                manifest["game_week"] = game_week
            self._write_manifest(staging / "manifest.json", manifest)
            self._assert_direct_child(staging, root, allow_missing=False)
            try:
                os.replace(staging, final)
                staging_owned = False
                final_owned = True
            except Exception:
                # os.replace may complete and then surface an error. Accept the
                # checkpoint only if the published directory verifies fully.
                if os.path.lexists(final):
                    loaded, verified, _profile = self._load_checkpoint(ident)
                    if loaded == manifest and verified == before:
                        return self._public_metadata(manifest), before
                raise
            loaded, verified, _profile = self._load_checkpoint(ident)
            if loaded != manifest or verified != before:
                raise ValueError("已提交检查点校验失败")
            return self._public_metadata(manifest), before
        except Exception:
            if staging_owned and os.path.lexists(staging):
                self._remove_tree_checked(staging, root)
            elif final_owned and os.path.lexists(final):
                # The publish rename may have completed before raising.
                self._remove_tree_checked(final, root)
            raise

    def create_checkpoint(self, profile_path: str | Path, label: str = "") -> dict:
        with self._lock:
            metadata, _ = self._create_checkpoint(profile_path, label, "manual")
            return metadata

    def create_automatic_checkpoint(self, profile_path: str | Path, label: str = "Automatic save",
                                    *, created_at: datetime.datetime | None = None,
                                    quiet_seconds: float = 4.0, clock=time.time,
                                    sleeper=time.sleep, cancelled=None) -> dict:
        """Read a quiet, consistent on-disk save tree without touching the game."""
        if not isinstance(label, str) or len(label) > 240:
            raise ValueError("标签长度不能超过240个字符")
        if isinstance(quiet_seconds, bool) or not isinstance(quiet_seconds, (int, float)) or quiet_seconds < 0:
            raise ValueError("quiet_seconds必须是非负数")
        with self._lock:
            try:
                profile = self._checked_profile(profile_path)
            except ValueError as exc:
                candidate = Path(profile_path)
                if (candidate.is_dir() and (candidate / "persist.estate.json").is_file()
                        and (candidate / "persist.roster.json").is_file()):
                    raise SnapshotNotQuietError("核心存档文件暂不可解析，稍后重试自动回档点") from exc
                raise
            self._reject_backup_profile_overlap(profile)
            quiet_ns = int(quiet_seconds * 1_000_000_000)
            signature = self._tree_signature(profile)
            if signature:
                newest_write_ns = max(entry[3] for entry in signature)
                remaining = quiet_seconds - ((self._clock_ns(clock) - newest_write_ns) / 1_000_000_000)
                if remaining > 0:
                    if cancelled is not None and cancelled():
                        raise SnapshotCancelledError("自动回档点已取消")
                    sleeper(remaining)
            if cancelled is not None and cancelled():
                raise SnapshotCancelledError("自动回档点已取消")
            after_quiet = self._tree_signature(profile)
            now_ns = self._clock_ns(clock)
            if after_quiet != signature or (
                quiet_ns and after_quiet and now_ns - max(row[3] for row in after_quiet) < quiet_ns
            ):
                raise SnapshotNotQuietError("存档仍在写入，稍后重试自动回档点")

            before = self._scan_tree(profile)
            if self._tree_signature(profile) != signature:
                raise SnapshotNotQuietError("存档在读取前发生变化，稍后重试")
            root = self._ensure_backup_root(create=True)
            assert root is not None
            created_at = created_at or datetime.datetime.fromtimestamp(self._clock_ns(clock) / 1_000_000_000,
                                                                        datetime.timezone.utc)
            ident, created = self._new_id(created_at)
            staging = root / (".staging-" + ident)
            final = root / ident
            self._assert_direct_child(staging, root)
            self._assert_direct_child(final, root)
            if os.path.lexists(staging) or os.path.lexists(final):
                raise ValueError("检查点编号冲突")
            staging_owned = False
            final_owned = False
            try:
                staging.mkdir()
                staging_owned = True
                snapshot = staging / "snapshot"
                self._copy_tree(profile, snapshot)
                copied = self._scan_tree(snapshot)
                try:
                    self._validate_snapshot_profile(snapshot, copied, all_persist_dson=True)
                except ValueError as exc:
                    raise SnapshotNotQuietError("核心DSON在复制期间不可用，未提交自动回档点") from exc
                after = self._scan_tree(profile)
                signature_after_copy = self._tree_signature(profile)
                if before != copied or before != after or signature_after_copy != signature:
                    raise SnapshotNotQuietError("存档在复制期间发生变化，未提交自动回档点")
                game_week = self._snapshot_game_week(snapshot)
                file_count, total_bytes = self._manifest_entries(before)
                manifest = {
                    "schema_version": 1,
                    "id": ident,
                    "label": label.strip(),
                    "profile": str(profile),
                    "created_at": created.isoformat(timespec="seconds"),
                    "file_count": file_count,
                    "bytes": total_bytes,
                    "kind": "automatic",
                    "entries": before,
                }
                if game_week is not None:
                    manifest["game_week"] = game_week
                self._write_manifest(staging / "manifest.json", manifest)

                if cancelled is not None and cancelled():
                    raise SnapshotCancelledError("自动回档点已取消")
                if self._scan_tree(profile) != before or self._scan_tree(snapshot) != before:
                    raise SnapshotNotQuietError("存档在提交前发生变化，未提交自动回档点")
                if self._snapshot_game_week(snapshot) != game_week:
                    raise SnapshotNotQuietError("周数文件在提交前发生变化，未提交自动回档点")
                if self._tree_signature(profile) != signature:
                    raise SnapshotNotQuietError("存档在提交前发生变化，未提交自动回档点")
                self._assert_direct_child(staging, root, allow_missing=False)
                try:
                    if cancelled is not None and cancelled():
                        raise SnapshotCancelledError("自动回档点已取消")
                    os.replace(staging, final)
                    staging_owned = False
                    final_owned = True
                except Exception:
                    if os.path.lexists(final):
                        loaded, verified, _profile = self._load_checkpoint(ident)
                        if loaded == manifest and verified == before:
                            return self._public_metadata(manifest)
                    raise
                loaded, verified, _profile = self._load_checkpoint(ident)
                if loaded != manifest or verified != before:
                    raise ValueError("已提交自动回档点校验失败")
                return self._public_metadata(manifest)
            except Exception:
                if staging_owned and os.path.lexists(staging):
                    self._remove_tree_checked(staging, root)
                elif final_owned and os.path.lexists(final):
                    self._remove_tree_checked(final, root)
                raise

    def _checkpoint_directory(self, checkpoint_id: str) -> tuple[Path, Path]:
        if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("检查点编号无效")
        root = self._ensure_backup_root(create=False)
        if root is None:
            raise ValueError("找不到该检查点")
        folder = root / checkpoint_id
        self._assert_direct_child(folder, root, allow_missing=False)
        if not folder.is_dir():
            raise ValueError("检查点目录无效")
        return root, folder

    def _read_manifest(self, folder: Path) -> dict:
        manifest_path = folder / "manifest.json"
        if _is_reparse(manifest_path) or not manifest_path.is_file():
            raise ValueError("检查点清单缺失或无效")
        if manifest_path.stat().st_size > _MANIFEST_LIMIT:
            raise ValueError("检查点清单过大")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("检查点清单损坏") from exc
        if not isinstance(manifest, dict):
            raise ValueError("检查点清单格式无效")
        return manifest

    def _validate_manifest(self, checkpoint_id: str, folder: Path, manifest: dict,
                           expected_profile: Path | None = None) -> tuple[dict, list[dict], Path]:
        if {child.name for child in folder.iterdir()} != {"manifest.json", "snapshot"}:
            raise ValueError("检查点目录包含未登记内容")
        required = {"schema_version", "id", "label", "profile", "created_at", "file_count", "bytes", "kind", "entries"}
        allowed = required | {"game_week"}
        if (not required.issubset(manifest) or not set(manifest).issubset(allowed)
                or manifest.get("schema_version") != 1 or manifest.get("id") != checkpoint_id):
            raise ValueError("检查点清单身份或版本无效")
        if manifest["kind"] not in ("manual", "before_restore", "automatic"):
            raise ValueError("检查点类型无效")
        saved_week = manifest.get("game_week")
        if saved_week is not None and (
            isinstance(saved_week, bool) or not isinstance(saved_week, int) or saved_week < 0
        ):
            raise ValueError("检查点游戏周数无效")
        if not isinstance(manifest["label"], str) or len(manifest["label"]) > 240:
            raise ValueError("检查点标签无效")
        if not isinstance(manifest["created_at"], str):
            raise ValueError("检查点时间无效")
        try:
            parsed_created = datetime.datetime.fromisoformat(manifest["created_at"])
            if parsed_created.tzinfo is None or parsed_created.utcoffset() is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise ValueError("检查点时间无效") from exc
        if not isinstance(manifest["profile"], str) or not manifest["profile"]:
            raise ValueError("检查点存档路径无效")
        profile = self._checked_profile(manifest["profile"])
        if expected_profile is not None and not _same_path(profile, expected_profile):
            raise ValueError("检查点属于另一个存档槽")
        entries = manifest["entries"]
        if not isinstance(entries, list):
            raise ValueError("检查点文件清单无效")
        normalized: list[dict] = []
        seen: set[str] = set()
        files: set[str] = set()
        total_bytes = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "kind", "size", "sha256"}:
                raise ValueError("检查点条目格式无效")
            parts = _safe_relative(entry["path"])
            key = "/".join(parts).casefold()
            if key in seen:
                raise ValueError("检查点清单包含重复路径")
            seen.add(key)
            kind, size, digest = entry["kind"], entry["size"], entry["sha256"]
            if kind not in ("file", "directory") or isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("检查点条目类型或大小无效")
            if kind == "file":
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise ValueError("检查点文件哈希格式无效")
                files.add(key)
                total_bytes += size
            elif size != 0 or digest != "":
                raise ValueError("检查点目录条目无效")
            normalized.append({"path": "/".join(parts), "kind": kind, "size": size, "sha256": digest})
        for key in seen:
            components = key.split("/")
            if any("/".join(components[:index]) in files for index in range(1, len(components))):
                raise ValueError("检查点清单将文件用作目录")
        file_count, calculated_bytes = self._manifest_entries(normalized)
        if manifest["file_count"] != file_count or manifest["bytes"] != calculated_bytes or total_bytes != calculated_bytes:
            raise ValueError("检查点统计信息与文件清单不符")
        if isinstance(manifest["file_count"], bool) or not isinstance(manifest["file_count"], int):
            raise ValueError("检查点文件数无效")
        if isinstance(manifest["bytes"], bool) or not isinstance(manifest["bytes"], int):
            raise ValueError("检查点字节数无效")
        snapshot = folder / "snapshot"
        self._assert_direct_child(snapshot, folder, allow_missing=False)
        actual = self._scan_tree(snapshot)
        if actual != sorted(normalized, key=lambda item: (item["path"].casefold(), item["path"])):
            raise ValueError("检查点快照与清单哈希不符")
        self._validate_snapshot_profile(snapshot, actual,
                                        all_persist_dson=manifest["kind"] == "automatic")
        snapshot_week = self._snapshot_game_week(snapshot)
        if saved_week is not None and saved_week != snapshot_week:
            raise ValueError("检查点游戏周数与快照不符")
        return manifest, actual, profile

    def _load_checkpoint(self, checkpoint_id: str, expected_profile: Path | None = None) -> tuple[dict, list[dict], Path]:
        _root, folder = self._checkpoint_directory(checkpoint_id)
        manifest = self._read_manifest(folder)
        return self._validate_manifest(checkpoint_id, folder, manifest, expected_profile)

    def list_checkpoints(self, profile_path: str | Path) -> list[dict]:
        with self._lock:
            profile = self._checked_profile(profile_path)
            root = self._ensure_backup_root(create=False)
            if root is None:
                return []
            results = []
            for folder in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
                if _is_reparse(folder):
                    raise ValueError("检查点目录包含符号链接或 junction")
                if folder.name.startswith(".staging-"):
                    continue
                if not _CHECKPOINT_ID.fullmatch(folder.name) or not folder.is_dir():
                    raise ValueError("检查点根目录包含无效项目")
                manifest = self._read_manifest(folder)
                stored_profile = manifest.get("profile")
                if not isinstance(stored_profile, str):
                    raise ValueError("检查点清单存档路径无效")
                if not _same_path(stored_profile, profile):
                    continue
                checked, _entries, _checked_profile = self._validate_manifest(folder.name, folder, manifest, profile)
                metadata = self._public_metadata(checked)
                if metadata["game_week"] is None:
                    metadata["game_week"] = self._snapshot_game_week(folder / "snapshot")
                results.append(metadata)
            results.sort(key=lambda item: item["created_at"], reverse=True)
            return results

    def select_checkpoint(self, profile_path: str | Path, *, days_ago: int | None = None,
                          game_week: int | None = None,
                          now: datetime.datetime | None = None) -> dict | None:
        if (days_ago is None) == (game_week is None):
            raise ValueError("请选择按现实天数或游戏周数筛选")
        checkpoints = self.list_checkpoints(profile_path)
        if game_week is not None:
            if isinstance(game_week, bool) or not isinstance(game_week, int) or game_week < 0:
                raise ValueError("游戏周数必须是非负整数")
            candidates = [item for item in checkpoints if item.get("game_week") == game_week]
        else:
            if isinstance(days_ago, bool) or not isinstance(days_ago, int) or days_ago < 0:
                raise ValueError("现实天数必须是非负整数")
            reference = now or datetime.datetime.now(datetime.timezone.utc)
            if not isinstance(reference, datetime.datetime) or reference.tzinfo is None or reference.utcoffset() is None:
                raise ValueError("筛选时间必须是带时区时间")
            cutoff = reference.astimezone(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
            candidates = []
            for item in checkpoints:
                created = datetime.datetime.fromisoformat(item["created_at"])
                if created.tzinfo is None or created.utcoffset() is None:
                    raise ValueError("检查点时间缺少时区")
                if created.astimezone(datetime.timezone.utc) <= cutoff:
                    candidates.append(item)
        if not candidates:
            return None
        return max(candidates, key=lambda item: datetime.datetime.fromisoformat(item["created_at"]).astimezone(datetime.timezone.utc))

    def _rollback_swap(self, profile: Path, old: Path, stage: Path,
                       original_entries: list[dict], target_entries: list[dict]) -> None:
        parent = profile.parent
        self._assert_direct_child(old, parent)
        self._assert_direct_child(stage, parent)
        self._assert_direct_child(profile, parent, allow_missing=False) if os.path.lexists(profile) else None
        if not os.path.lexists(old):
            if os.path.lexists(profile) and self._scan_tree(profile) == original_entries:
                return
            raise RuntimeError("原存档目录无法确认，拒绝覆盖")
        if self._scan_tree(old) != original_entries:
            raise RuntimeError("原存档目录校验失败，保留目录供手动恢复")

        quarantine = parent / f".{profile.name}.ddtrainer-quarantine-{uuid.uuid4().hex}"
        self._assert_direct_child(quarantine, parent)
        if os.path.lexists(quarantine):
            raise RuntimeError("回滚隔离目录编号冲突，未改动现有目录")
        moved_profile = False
        if os.path.lexists(profile):
            if self._scan_tree(profile) != target_entries:
                raise RuntimeError("恢复目录内容意外变化，拒绝覆盖")
            try:
                os.replace(profile, quarantine)
                moved_profile = True
            except Exception:
                # A rename can take effect before its wrapper/filesystem reports failure.
                if (not os.path.lexists(profile) and os.path.lexists(quarantine)
                        and self._scan_tree(quarantine) == target_entries):
                    moved_profile = True
                else:
                    raise
        try:
            os.replace(old, profile)
        except Exception:
            if (os.path.lexists(profile) and not os.path.lexists(old)
                    and self._scan_tree(profile) == original_entries):
                pass
            else:
                # Preserve both recoverable trees if the original rename did not land.
                if moved_profile and os.path.lexists(quarantine) and not os.path.lexists(profile):
                    try:
                        os.replace(quarantine, profile)
                    except Exception as restore_target_error:
                        if not (os.path.lexists(profile) and not os.path.lexists(quarantine)
                                and self._scan_tree(profile) == target_entries):
                            raise RuntimeError(
                                f"原存档仍在旧目录，恢复目标暂存在 {quarantine}：{restore_target_error}"
                            ) from restore_target_error
                raise
        if not os.path.lexists(profile) or self._scan_tree(profile) != original_entries:
            # Do not delete a tree whose identity changed during rollback.
            recovery = parent / f".{profile.name}.ddtrainer-original-recovery-{uuid.uuid4().hex}"
            self._assert_direct_child(recovery, parent)
            if os.path.lexists(profile) and not os.path.lexists(recovery):
                os.replace(profile, recovery)
            if moved_profile and os.path.lexists(quarantine) and not os.path.lexists(profile):
                os.replace(quarantine, profile)
            raise RuntimeError("原存档目录回滚后校验失败；异常目录已保留供恢复")
        if moved_profile and os.path.lexists(quarantine):
            if self._scan_tree(quarantine) != target_entries:
                raise RuntimeError("回滚隔离目录变化，保留该目录供手动恢复")
            self._remove_tree_checked(quarantine, parent)

    def restore_checkpoint(self, checkpoint_id: str) -> dict:
        with self._lock:
            self.saves.guard()
            manifest, target_entries, profile = self._load_checkpoint(checkpoint_id)
            self.saves.guard()
            safety_label = f"Before restore {manifest['label'] or checkpoint_id}"[:240]
            safety_meta, original_entries = self._create_checkpoint(
                profile,
                safety_label,
                "before_restore",
            )
            safety_id = safety_meta["id"]
            root = self._ensure_backup_root(create=False)
            assert root is not None
            stage = profile.parent / f".{profile.name}.ddtrainer-stage-{uuid.uuid4().hex}"
            old = profile.parent / f".{profile.name}.ddtrainer-old-{uuid.uuid4().hex}"
            self._assert_direct_child(stage, profile.parent)
            self._assert_direct_child(old, profile.parent)
            stage_owned = False
            try:
                self.saves.guard()
                if self._scan_tree(profile) != original_entries:
                    raise ValueError("存档在创建安全回档点后发生变化，已取消恢复")
                if os.path.lexists(stage) or os.path.lexists(old):
                    raise ValueError("恢复临时目录编号冲突")
                snapshot = root / checkpoint_id / "snapshot"
                restore_time = time.time_ns()
                stage.mkdir()
                stage_owned = True
                self._copy_tree(snapshot, stage, mtime_ns=restore_time, destination_owned=True)
                if self._scan_tree(stage) != target_entries:
                    raise ValueError("恢复暂存目录校验失败")
                self.saves.guard()
                if self._scan_tree(profile) != original_entries:
                    raise ValueError("存档在恢复准备期间发生变化，已取消恢复")
                self._assert_direct_child(profile, profile.parent, allow_missing=False)
                self._assert_direct_child(old, profile.parent)
                self._assert_direct_child(stage, profile.parent, allow_missing=False)
                try:
                    os.replace(profile, old)
                    self.saves.guard()
                    os.replace(stage, profile)
                    if self._scan_tree(profile) != target_entries:
                        raise ValueError("恢复后的存档校验失败")
                except Exception as swap_error:
                    try:
                        self._rollback_swap(profile, old, stage, original_entries, target_entries)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            f"恢复失败且原目录回滚未完成；安全回档点 {safety_id} 仍保留：{rollback_error}"
                        ) from swap_error
                    raise RuntimeError(
                        f"恢复失败，原存档已回滚；安全回档点 {safety_id} 仍保留：{swap_error}"
                    ) from swap_error
                cleanup_warning = ""
                try:
                    self._remove_tree_checked(old, profile.parent)
                except Exception as exc:
                    cleanup_warning = f"（旧目录清理未完成：{exc}）"
                return {
                    "success": True,
                    "checkpoint_id": checkpoint_id,
                    "safety_checkpoint_id": safety_id,
                    "message": f"已恢复整槽存档；可用安全回档点 {safety_id} 撤销本次恢复{cleanup_warning}",
                }
            except Exception as exc:
                if stage_owned and os.path.lexists(stage):
                    try:
                        self._remove_tree_checked(stage, profile.parent)
                    except Exception as cleanup_error:
                        raise RuntimeError(f"恢复失败，暂存目录清理未完成：{cleanup_error}；{exc}") from exc
                if safety_id not in str(exc):
                    raise RuntimeError(f"{exc}；恢复前安全回档点为 {safety_id}") from exc
                raise


_default_manager = CheckpointManager()


def create_checkpoint(profile_path: str | Path, label: str = "") -> dict:
    return _default_manager.create_checkpoint(profile_path, label)


def list_checkpoints(profile_path: str | Path) -> list[dict]:
    return _default_manager.list_checkpoints(profile_path)


def restore_checkpoint(checkpoint_id: str) -> dict:
    return _default_manager.restore_checkpoint(checkpoint_id)


def select_checkpoint(profile_path: str | Path, *, days_ago: int | None = None,
                      game_week: int | None = None,
                      now: datetime.datetime | None = None) -> dict | None:
    return _default_manager.select_checkpoint(profile_path, days_ago=days_ago,
                                              game_week=game_week, now=now)
