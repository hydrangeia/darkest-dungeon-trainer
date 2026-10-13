"""Periodic, read-only snapshots of stable on-disk Darkest Dungeon saves."""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import Callable

from save_runtime import ROOT
from sl_runtime import CheckpointManager, SnapshotCancelledError, SnapshotNotQuietError


_SETTINGS_SCHEMA = 1
_DEFAULT_INTERVAL_MINUTES = 10
_MIN_INTERVAL_MINUTES = 1
_MAX_INTERVAL_MINUTES = 1440


def _default_settings() -> dict:
    return {
        "schema_version": _SETTINGS_SCHEMA,
        "enabled": False,
        "interval_minutes": _DEFAULT_INTERVAL_MINUTES,
        "profiles": [],
        "backup_while_running": True,
    }


def _profile_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def normalize_settings(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("自动回档点设置格式无效")
    allowed = {"schema_version", "enabled", "interval_minutes", "profiles", "backup_while_running"}
    if set(value) - allowed:
        raise ValueError("自动回档点设置包含未知字段")
    version = value.get("schema_version", _SETTINGS_SCHEMA)
    if isinstance(version, bool) or version != _SETTINGS_SCHEMA:
        raise ValueError("自动回档点设置版本不受支持")
    enabled = value.get("enabled", False)
    backup_while_running = value.get("backup_while_running", True)
    if not isinstance(enabled, bool) or not isinstance(backup_while_running, bool):
        raise ValueError("自动回档点开关必须是布尔值")
    interval = value.get("interval_minutes", _DEFAULT_INTERVAL_MINUTES)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or not _MIN_INTERVAL_MINUTES <= interval <= _MAX_INTERVAL_MINUTES):
        raise ValueError("自动回档间隔必须是1到1440分钟")
    profiles = value.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("自动回档点存档列表无效")
    normalized_profiles = []
    seen = set()
    for item in profiles:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValueError("自动回档点存档路径无效")
        key = _profile_key(item)
        if key not in seen:
            normalized_profiles.append(item)
            seen.add(key)
    return {
        "schema_version": _SETTINGS_SCHEMA,
        "enabled": enabled,
        "interval_minutes": interval,
        "profiles": normalized_profiles,
        "backup_while_running": backup_while_running,
    }


class AutoBackupSettingsStore:
    """Atomic JSON persistence for the automatic checkpoint preferences."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else ROOT / "backups" / "autosave_settings.json"

    def load(self) -> dict:
        if not os.path.lexists(self.path):
            return _default_settings()
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("自动回档点设置文件无效")
        if self.path.stat().st_size > 1024 * 1024:
            raise ValueError("自动回档点设置文件过大")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("自动回档点设置文件损坏") from exc
        return normalize_settings(value)

    def save(self, settings: dict) -> dict:
        value = normalize_settings(settings)
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("拒绝覆盖符号链接设置文件")
        fd, temp_name = tempfile.mkstemp(prefix=".autosave-settings-", suffix=".tmp", dir=parent)
        temp_path = Path(temp_name)
        owned = True
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            owned = False
            return value
        finally:
            if owned and os.path.lexists(temp_path):
                temp_path.unlink()


class AutoBackupController:
    """Poll game state, snapshot selected slots, and publish UI events.

    ``tick`` is public for deterministic tests and host integrations. The
    worker calls the same method at short polling intervals so it can notice
    when the game exits before the next long backup interval.
    """

    def __init__(self, manager: CheckpointManager | None = None, *,
                 settings_path: str | Path | None = None,
                 settings_store: AutoBackupSettingsStore | None = None,
                 clock: Callable[[], float | datetime.datetime] | None = None,
                 game_running: Callable[[], bool] | None = None,
                 sleeper: Callable[[float], None] | None = None,
                 poll_interval: float = 1.0,
                 backoff_seconds: float = 30.0,
                 quiet_seconds: float = 4.0):
        self.manager = manager or CheckpointManager()
        self.store = settings_store or AutoBackupSettingsStore(settings_path)
        self.clock = clock or time.time
        self._game_running_probe = game_running
        self._external_sleeper = sleeper
        self.poll_interval = max(0.05, float(poll_interval))
        self.backoff_seconds = max(0.1, float(backoff_seconds))
        self.quiet_seconds = max(0.0, float(quiet_seconds))
        self._lock = threading.RLock()
        self._events: queue.Queue[dict] = queue.Queue()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_due = self._clock_seconds()
        self._was_running = False
        self._fingerprints: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}
        self._controller_error_message: str | None = None
        self._status = {
            "running": False,
            "waiting_for_game": False,
            "last_check": None,
            "last_error": None,
            "last_message": "",
        }
        self._settings_error = None
        try:
            self._settings = self.store.load()
        except Exception as exc:
            self._settings = _default_settings()
            self._settings_error = str(exc)
            self._emit("error", message=f"读取自动回档设置失败：{exc}")

    def _clock_seconds(self) -> float:
        value = self.clock()
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("自动回档时钟必须返回带时区时间")
            return value.timestamp()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("自动回档时钟必须返回时间戳或带时区时间")
        return float(value)

    def _clock_datetime(self) -> datetime.datetime:
        value = self.clock()
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("自动回档时钟必须返回带时区时间")
            return value.astimezone(datetime.timezone.utc)
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)

    @property
    def settings(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._settings)

    @property
    def status(self) -> dict:
        with self._lock:
            return {**copy.deepcopy(self._status), **copy.deepcopy(self._settings),
                    "settings_error": self._settings_error}

    def _emit(self, event_type: str, **payload) -> None:
        self._events.put({"type": event_type, **payload})

    def get_events(self, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("事件读取数量无效")
        events = []
        for _ in range(limit):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def _set_status(self, **changes) -> None:
        with self._lock:
            self._status.update(changes)

    def configure(self, *, enabled: bool | None = None, interval_minutes: int | None = None,
                  profiles: list[str] | None = None,
                  backup_while_running: bool | None = None) -> dict:
        with self._lock:
            updated = copy.deepcopy(self._settings)
        if enabled is not None:
            updated["enabled"] = enabled
        if interval_minutes is not None:
            updated["interval_minutes"] = interval_minutes
        if profiles is not None:
            updated["profiles"] = profiles
        if backup_while_running is not None:
            updated["backup_while_running"] = backup_while_running
        saved = self.store.save(updated)
        with self._lock:
            previous_profiles = self._settings["profiles"]
            self._settings = saved
            if previous_profiles != saved["profiles"]:
                self._fingerprints.clear()
                self._retry_at.clear()
                self._error_counts.clear()
            self._next_due = self._clock_seconds()
            self._settings_error = None
        self._wake_event.set()
        self._set_status(last_error=None, last_message="自动回档设置已保存")
        self._emit("status", message="自动回档设置已保存", settings=self.settings)
        if saved["enabled"] and (self._thread is None or not self._thread.is_alive()):
            self.start()
        return self.settings

    def start(self) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status
            self._stop_event.clear()
            self._wake_event.clear()
            self._next_due = self._clock_seconds()
            self._status["running"] = True
            self._thread = threading.Thread(target=self._worker, name="DDTrainerAutoBackup", daemon=True)
            thread = self._thread
        self._emit("status", message="自动回档后台检查已启动", status=self.status)
        thread.start()
        return self.status

    def stop(self, timeout: float | None = None) -> dict:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._status["running"] = False
        self._emit("status", message="自动回档后台检查已停止", status=self.status)
        return self.status

    def _worker(self) -> None:
        try:
            while not self._stop_event.is_set():
                self.tick()
                if self._stop_event.is_set():
                    break
                now = self._clock_seconds()
                with self._lock:
                    waiting_for_game = self._status["waiting_for_game"]
                    enabled = self._settings["enabled"]
                    due = self._next_due
                timeout = self.poll_interval if (waiting_for_game or not enabled) else min(
                    self.poll_interval, max(0.05, due - now)
                )
                self._wake_event.wait(timeout)
                self._wake_event.clear()
        except Exception as exc:
            self._record_error(None, exc)
        finally:
            with self._lock:
                self._status["running"] = False
            self._emit("status", message="自动回档后台检查已退出", status=self.status)

    def _game_running(self) -> bool:
        if self._game_running_probe is not None:
            result = self._game_running_probe()
            if not isinstance(result, bool):
                raise ValueError("game_running探测必须返回布尔值")
            return result
        try:
            self.manager.saves.guard()
            return False
        except ValueError as exc:
            message = str(exc).lower()
            if "请先完全退出游戏" in message or "game is running" in message:
                return True
            raise

    def _wait_for_quiet(self, seconds: float) -> None:
        if seconds <= 0 or self._stop_event.is_set():
            return
        if self._external_sleeper is not None:
            self._external_sleeper(seconds)
        else:
            self._stop_event.wait(seconds)

    @staticmethod
    def _fingerprint(entries: list[dict]) -> str:
        encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _record_error(self, profile: str | None, error: Exception) -> None:
        key = _profile_key(profile) if profile else "<controller>"
        attempts = self._error_counts.get(key, 0) + 1
        self._error_counts[key] = attempts
        delay = min(self.backoff_seconds * (2 ** min(attempts - 1, 5)), 900.0)
        self._retry_at[key] = self._clock_seconds() + delay
        message = str(error) or error.__class__.__name__
        self._set_status(last_error=message, last_message=message, last_check=self._clock_datetime().isoformat())
        self._emit("error", profile=profile, message=message, retry_in_seconds=delay)

    def tick(self) -> None:
        if self._stop_event.is_set():
            return
        settings = self.settings
        if not settings["enabled"]:
            self._set_status(waiting_for_game=False, last_message="自动回档已关闭")
            return
        now = self._clock_seconds()
        controller_retry_at = self._retry_at.get("<controller>", 0.0)
        if now < controller_retry_at:
            return
        try:
            running = self._game_running()
        except Exception as exc:
            self._record_error(None, exc)
            self._controller_error_message = self.status["last_error"]
            self._next_due = self._retry_at.get("<controller>", self._clock_seconds() + self.backoff_seconds)
            return

        if self._controller_error_message is not None:
            with self._lock:
                self._retry_at.pop("<controller>", None)
                self._error_counts.pop("<controller>", None)
                if self._status.get("last_error") == self._controller_error_message:
                    self._status["last_error"] = None
            self._controller_error_message = None

        with self._lock:
            due = now >= self._next_due
            was_running = self._was_running
            if running:
                self._was_running = True
        if running:
            was_waiting = self.status["waiting_for_game"]
            message = "游戏运行中；仅在启用时读取稳定的落盘存档"
            self._set_status(waiting_for_game=True, last_message=message)
            if not was_waiting:
                self._emit("waiting_game", message=message)
            if due:
                self._next_due = self._clock_seconds() + settings["interval_minutes"] * 60
                if settings["backup_while_running"]:
                    self._process_profiles(settings["profiles"], settings["backup_while_running"])
            return

        was_waiting = self.status["waiting_for_game"]
        self._set_status(waiting_for_game=False)
        if was_waiting:
            self._emit("status", message="游戏已退出；正在检查最终落盘进度")
        if was_running or due:
            self._next_due = self._clock_seconds() + settings["interval_minutes"] * 60
            self._process_profiles(settings["profiles"], settings["backup_while_running"])
            self._was_running = False

    def _process_profiles(self, profiles: list[str], backup_while_running: bool) -> None:
        if not profiles:
            self._set_status(last_check=self._clock_datetime().isoformat(), last_message="尚未选择存档槽")
            return
        for profile_path in profiles:
            if self._stop_event.is_set():
                return
            try:
                if self._game_running():
                    with self._lock:
                        self._was_running = True
                    if not backup_while_running:
                        message = "游戏运行中，已按设置跳过自动回档点"
                        self._set_status(waiting_for_game=True, last_message=message)
                        self._emit("waiting_game", message=message)
                        return
            except Exception as exc:
                self._record_error(profile_path, exc)
                continue
            key = _profile_key(profile_path)
            now = self._clock_seconds()
            if now < self._retry_at.get(key, 0.0):
                continue
            try:
                try:
                    profile = self.manager._checked_profile(profile_path)
                except ValueError as exc:
                    candidate = Path(profile_path)
                    if (candidate.is_dir() and (candidate / "persist.estate.json").is_file()
                            and (candidate / "persist.roster.json").is_file()):
                        raise SnapshotNotQuietError("核心存档文件暂不可解析，稍后重试自动回档点") from exc
                    raise
                try:
                    entries = self.manager._scan_tree(profile)
                except ValueError as exc:
                    if "变化" in str(exc) or "读取期间发生变化" in str(exc):
                        raise SnapshotNotQuietError("存档在读取期间发生变化，稍后重试") from exc
                    raise
                fingerprint = self._fingerprint(entries)
                previous = self._fingerprints.get(key)
                if previous == fingerprint:
                    self._set_status(last_check=self._clock_datetime().isoformat(), last_error=None,
                                     last_message=f"存档内容未变化：{profile_path}")
                    self._emit("unchanged", profile=profile_path,
                               message=f"存档内容未变化，跳过重复回档点：{profile_path}")
                    continue
                if previous is None:
                    history = self.manager.list_checkpoints(profile_path)
                    if history:
                        _manifest, previous_entries, _checked = self.manager._load_checkpoint(history[0]["id"])
                        if self._fingerprint(previous_entries) == fingerprint:
                            self._fingerprints[key] = fingerprint
                            self._set_status(last_check=self._clock_datetime().isoformat(), last_error=None,
                                             last_message=f"最近回档点内容相同，跳过重复备份：{profile_path}")
                            self._emit("unchanged", profile=profile_path,
                                       message=f"最近回档点内容相同，跳过重复备份：{profile_path}")
                            continue
                if self._stop_event.is_set():
                    return

                def cancelled() -> bool:
                    if self._stop_event.is_set():
                        return True
                    current = self.settings
                    if (not current["enabled"]
                            or key not in {_profile_key(path) for path in current["profiles"]}):
                        return True
                    return not backup_while_running and self._game_running()

                checkpoint = self.manager.create_automatic_checkpoint(
                    profile_path,
                    "Automatic save",
                    created_at=self._clock_datetime(),
                    quiet_seconds=self.quiet_seconds,
                    clock=self.clock,
                    sleeper=self._wait_for_quiet,
                    cancelled=cancelled,
                )
                _manifest, saved_entries, _checked = self.manager._load_checkpoint(checkpoint["id"])
                self._fingerprints[key] = self._fingerprint(saved_entries)
                self._retry_at.pop(key, None)
                self._error_counts.pop(key, None)
                self._set_status(last_check=self._clock_datetime().isoformat(), last_error=None,
                                 last_message=f"已创建自动回档点：{profile_path}")
                self._emit("checkpoint_created", profile=str(profile), checkpoint=checkpoint,
                           message=f"已创建自动回档点：{profile_path}")
            except SnapshotCancelledError:
                if self._stop_event.is_set():
                    return
                try:
                    if self._game_running():
                        with self._lock:
                            self._was_running = True
                        self._set_status(waiting_for_game=True,
                                         last_message="游戏启动期间已跳过自动回档")
                        self._emit("waiting_game", message="游戏启动期间已跳过自动回档")
                        return
                except Exception:
                    pass
                return
            except SnapshotNotQuietError as exc:
                retry_delay = max(1.0, self.quiet_seconds)
                retry_at = self._clock_seconds() + retry_delay
                self._retry_at[key] = retry_at
                self._next_due = min(self._next_due, retry_at)
                self._set_status(last_error=None, last_message=str(exc))
                self._emit("unchanged", profile=profile_path, message=str(exc),
                           retry_in_seconds=retry_delay)
            except Exception as exc:
                try:
                    if self._game_running():
                        with self._lock:
                            self._was_running = True
                        self._set_status(waiting_for_game=True, last_message="游戏启动期间已跳过此次自动回档")
                        return
                except Exception:
                    pass
                self._record_error(profile_path, exc)


__all__ = [
    "AutoBackupController",
    "AutoBackupSettingsStore",
    "normalize_settings",
]
