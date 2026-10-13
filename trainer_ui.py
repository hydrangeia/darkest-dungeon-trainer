"""Darkest Dungeon single-player trainer UI.

This module deliberately treats the memory engine as optional. A control stays
disabled unless the loaded engine reports that capability and the game is
attached. Importing or opening this UI never starts the game.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _datetime
import json
import os
import platform
import queue
import struct
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any


APP_TITLE = "Darkest Dungeon I · 单机修改器"
APP_VERSION = "0.3.0"
TARGET_VERSION = "28063 (64-bit)"
TARGET_BUILD = f"目标版本：{TARGET_VERSION} / Steam 25463639"
POLL_MS = 1200

COLORS = {
    "bg": "#171a1f",
    "surface": "#20252c",
    "surface2": "#282f37",
    "surface3": "#313943",
    "text": "#e6e8eb",
    "muted": "#a3acb6",
    "dim": "#717b86",
    "accent": "#ef8b32",
    "accent_hover": "#ff9c3d",
    "green": "#73c991",
    "red": "#ef7777",
    "border": "#3a424c",
}

# The fourteen requested controls. Defaults follow the requested values for
# currency/resources, damage, defense, and speed. Other values are conservative
# starting points which are only sent after a user enables the control.
OPTIONS: tuple[dict[str, Any], ...] = (
    {"key": "health", "label": "无限生命", "kind": "toggle"},
    {"key": "stress", "label": "压力清零", "kind": "toggle"},
    {"key": "light", "label": "最大亮度", "kind": "toggle"},
    {"key": "items", "label": "物品不减", "kind": "toggle"},
    {"key": "gold", "label": "金币", "default": 9999999, "from": 0, "to": 99999999, "step": 1000, "kind": "int"},
    {"key": "bust", "label": "半身像", "default": 999, "from": 0, "to": 999999, "step": 1, "kind": "int"},
    {"key": "portrait", "label": "肖像", "default": 999, "from": 0, "to": 999999, "step": 1, "kind": "int"},
    {"key": "deed", "label": "地契", "default": 999, "from": 0, "to": 999999, "step": 1, "kind": "int"},
    {"key": "crest", "label": "徽章", "default": 999, "from": 0, "to": 999999, "step": 1, "kind": "int"},
    {"key": "one_hit", "label": "超级伤害 / 一击必杀", "kind": "toggle"},
    {"key": "damage", "label": "伤害倍率", "default": 2.0, "from": 0.1, "to": 100, "step": 0.1, "kind": "float"},
    {"key": "defense", "label": "防御倍率", "default": 2.0, "from": 0.1, "to": 100, "step": 0.1, "kind": "float"},
    {"key": "shop", "label": "商店无限库存", "kind": "toggle"},
    {"key": "speed", "label": "游戏速度", "default": 2.5, "from": 0.1, "to": 10, "step": 0.1, "kind": "float"},
)

OPTION_BY_KEY = {item["key"]: item for item in OPTIONS}
HOTKEY_MOD_CONTROL = 0x0002
HOTKEY_MOD_SHIFT = 0x0004
HOTKEY_MOD_NOREPEAT = 0x4000
HOTKEY_VK_NUMPAD = {
    "Num 0": 0x60,
    "Num 1": 0x61,
    "Num 2": 0x62,
    "Num 3": 0x63,
    "Num 4": 0x64,
    "Num 5": 0x65,
    "Num 6": 0x66,
    "Num 7": 0x67,
    "Num 8": 0x68,
    "Num 9": 0x69,
    "Num .": 0x6E,
    "Num +": 0x6B,
}
HOTKEY_OPTION_BINDINGS: tuple[tuple[str, str, int, int], ...] = (
    ("Num 1", "health", HOTKEY_VK_NUMPAD["Num 1"], 0),
    ("Num 2", "stress", HOTKEY_VK_NUMPAD["Num 2"], 0),
    ("Num 3", "light", HOTKEY_VK_NUMPAD["Num 3"], 0),
    ("Num 4", "items", HOTKEY_VK_NUMPAD["Num 4"], 0),
    ("Num 5", "gold", HOTKEY_VK_NUMPAD["Num 5"], 0),
    ("Num 6", "bust", HOTKEY_VK_NUMPAD["Num 6"], 0),
    ("Num 7", "portrait", HOTKEY_VK_NUMPAD["Num 7"], 0),
    ("Num 8", "deed", HOTKEY_VK_NUMPAD["Num 8"], 0),
    ("Num 9", "crest", HOTKEY_VK_NUMPAD["Num 9"], 0),
    ("Num 0", "one_hit", HOTKEY_VK_NUMPAD["Num 0"], 0),
    ("Num .", "damage", HOTKEY_VK_NUMPAD["Num ."], 0),
    ("Num +", "defense", HOTKEY_VK_NUMPAD["Num +"], 0),
    ("Ctrl + Num 1", "shop", HOTKEY_VK_NUMPAD["Num 1"], HOTKEY_MOD_CONTROL),
    ("Ctrl + Num 2", "speed", HOTKEY_VK_NUMPAD["Num 2"], HOTKEY_MOD_CONTROL),
)
HOTKEY_PAUSE_BINDING = ("Ctrl + Shift + Home", 0x24, HOTKEY_MOD_CONTROL | HOTKEY_MOD_SHIFT)
HOTKEYS = tuple((label, OPTION_BY_KEY[key]["label"]) for label, key, _vk, _mods in HOTKEY_OPTION_BINDINGS) + ((HOTKEY_PAUSE_BINDING[0], "暂停/恢复热键"),)


class _HotkeyPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _HotkeyMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _HotkeyPoint),
        ("lPrivate", ctypes.c_uint32),
    ]


class HotkeyManager:
    """Own global hotkeys only while the connected game is the foreground app."""

    WM_HOTKEY = 0x0312
    WM_TIMER = 0x0113
    WM_APP_WAKE = 0x8001
    PM_NOREMOVE = 0x0000
    PM_REMOVE = 0x0001
    TIMER_INTERVAL_MS = 50
    HOTKEY_ID_BASE = 0x4D00

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._commands: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._context_lock = threading.Lock()
        self._requested_context: tuple[bool, int | None, tuple[str, ...]] = (False, None, ())
        self._state_lock = threading.Lock()
        self._state: dict[str, Any] = {
            "state": "waiting", "message": "未连接游戏", "connected": False,
            "pid": None, "paused": False, "registered": [], "conflicts": [], "supported": [],
        }
        self._thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._user32: Any | None = None
        self._kernel32: Any | None = None
        self._owned: dict[int, tuple[str, str | None]] = {}
        self._errors: dict[int, int] = {}
        self._unregister_errors: dict[int, int] = {}
        self._connected = False
        self._pid: int | None = None
        self._supported_keys: set[str] = set()
        self._paused = False
        self._was_foreground = False
        self._timer_id: int | None = None
        self._stopped = False
        if os.name == "nt":
            self._thread = threading.Thread(target=self._thread_main, name="DDTrainerHotkeys", daemon=True)
            self._thread.start()
        else:
            self._publish("unsupported", "当前系统不支持 Windows 全局热键")

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                **self._state,
                "registered": list(self._state["registered"]),
                "conflicts": list(self._state["conflicts"]),
                "release_failures": list(self._state["release_failures"]),
                "supported": list(self._state["supported"]),
            }

    def set_context(self, connected: bool, pid: int | None, capabilities: dict[str, bool] | None = None) -> None:
        clean_pid = int(pid) if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None
        supported = tuple(sorted(key for key, value in (capabilities or {}).items() if value is True)) if connected else ()
        context = (bool(connected), clean_pid if connected else None, supported)
        with self._context_lock:
            if context == self._requested_context or self._stopped:
                return
            self._requested_context = context
        self._commands.put(("context", *context))
        self._wake_thread()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._thread is None:
            return
        self._commands.put(("stop",))
        self._wake_thread()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def _wake_thread(self) -> None:
        thread_id = self._thread_id
        if thread_id is None or self._user32 is None:
            return
        try:
            self._user32.PostThreadMessageW(thread_id, self.WM_APP_WAKE, 0, 0)
        except Exception:
            pass

    def _configure_win32(self) -> Any:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.RegisterHotKey.restype = ctypes.c_int
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.UnregisterHotKey.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [ctypes.POINTER(_HotkeyMessage), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(_HotkeyMessage), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [ctypes.c_uint32, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.PostThreadMessageW.restype = ctypes.c_int
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        user32.KillTimer.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        return user32

    def _thread_main(self) -> None:
        try:
            self._user32 = self._configure_win32()
            assert self._kernel32 is not None
            self._thread_id = int(self._kernel32.GetCurrentThreadId())
            # A thread message queue is created by PeekMessage before PostThreadMessage is used.
            message = _HotkeyMessage()
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, self.PM_NOREMOVE)
            self._timer_id = int(self._user32.SetTimer(None, 0, self.TIMER_INTERVAL_MS, None))
            if not self._timer_id:
                raise OSError("无法创建前台检测计时器")
            self._drain_commands()
            self._reconcile()
            while True:
                result = int(self._user32.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result <= 0:
                    break
                if message.message == self.WM_TIMER:
                    if int(message.wParam) == self._timer_id:
                        self._drain_commands()
                        self._reconcile()
                elif message.message == self.WM_APP_WAKE:
                    self._drain_commands()
                    self._reconcile()
                elif message.message == self.WM_HOTKEY:
                    self._handle_hotkey(int(message.wParam))
        except Exception as exc:
            self.events.put({"type": "log", "level": "error", "message": f"全局热键线程异常：{type(exc).__name__}: {exc}"})
            self._publish("error", "全局热键不可用，请查看运行记录")
        finally:
            self._unregister_all()
            if self._timer_id and self._user32 is not None:
                try:
                    self._user32.KillTimer(None, self._timer_id)
                except Exception:
                    pass
            self._thread_id = None

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command[0] == "context":
                connected, pid, supported = bool(command[1]), command[2], tuple(command[3])
                if (connected, pid, supported) != (self._connected, self._pid, tuple(sorted(self._supported_keys))):
                    self._connected, self._pid = connected, pid
                    self._supported_keys = set(supported)
                    self._errors.clear()
                    self._was_foreground = False
                if not connected:
                    self._paused = False
            elif command[0] == "stop":
                self._connected = False
                self._pid = None
                self._stopped = True
                for _attempt in range(5):
                    self._unregister_all()
                    if not self._owned:
                        break
                    time.sleep(0.02)
                self._publish("waiting", "已关闭全局热键")
                if self._user32 is not None and self._thread_id is not None:
                    self._user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
                return

    def _foreground_pid(self) -> int | None:
        if self._user32 is None:
            return None
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_uint32(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) if pid.value else None

    def _binding_records(self) -> list[tuple[int, str, str | None, int, int]]:
        records = []
        for index, (label, option_key, vk, modifiers) in enumerate(HOTKEY_OPTION_BINDINGS, start=1):
            records.append((self.HOTKEY_ID_BASE + index, label, option_key, vk, modifiers))
        pause_id = self.HOTKEY_ID_BASE + len(HOTKEY_OPTION_BINDINGS) + 1
        records.append((pause_id, HOTKEY_PAUSE_BINDING[0], None, HOTKEY_PAUSE_BINDING[1], HOTKEY_PAUSE_BINDING[2]))
        return records

    def _reconcile(self) -> None:
        if self._stopped:
            self._unregister_all()
            return
        if not self._connected:
            self._was_foreground = False
            self._unregister_all()
            self._publish("waiting", "未连接游戏")
            return
        if self._pid is None:
            self._was_foreground = False
            self._unregister_all()
            self._publish("waiting", "已连接，但后端未报告游戏 PID")
            return
        focused = self._foreground_pid() == self._pid
        if focused and not self._was_foreground:
            # A fresh foreground session gets one honest retry for prior OS conflicts.
            self._errors.clear()
        self._was_foreground = focused
        if not focused:
            self._unregister_all()
            self._publish("waiting", "游戏未在前台 · 未占用数字键")
            return

        desired = []
        for record in self._binding_records():
            _hotkey_id, _label, option_key, _vk, _modifiers = record
            if option_key is None or (not self._paused and option_key in self._supported_keys):
                desired.append(record)
        desired_ids = {record[0] for record in desired}
        for hotkey_id in list(self._owned):
            if hotkey_id not in desired_ids:
                self._unregister_one(hotkey_id)
            else:
                self._unregister_errors.pop(hotkey_id, None)
        for hotkey_id, label, option_key, vk, modifiers in desired:
            if hotkey_id in self._owned or hotkey_id in self._errors:
                continue
            assert self._user32 is not None
            ctypes.set_last_error(0)
            ok = self._user32.RegisterHotKey(None, hotkey_id, modifiers | HOTKEY_MOD_NOREPEAT, vk)
            if ok:
                self._owned[hotkey_id] = (label, option_key)
            else:
                error_code = ctypes.get_last_error()
                self._errors[hotkey_id] = error_code
                self.events.put({
                    "type": "log", "level": "warning",
                    "message": f"全局热键 {label} 注册冲突或失败（Win32 错误 {error_code}）；未更改已有系统映射。",
                })
        self._publish_current_state()

    def _handle_hotkey(self, hotkey_id: int) -> None:
        record = next((item for item in self._binding_records() if item[0] == hotkey_id), None)
        if record is None or hotkey_id not in self._owned:
            return
        if not self._connected or self._pid is None or self._foreground_pid() != self._pid:
            self._reconcile()
            return
        _hotkey_id, label, option_key, _vk, _modifiers = record
        if option_key is None:
            self._paused = not self._paused
            self.events.put({"type": "pause", "paused": self._paused})
            self._reconcile()
            return
        if self._paused:
            self._reconcile()
            return
        self.events.put({"type": "action", "key": option_key, "label": label})

    def _unregister_one(self, hotkey_id: int) -> None:
        owned = self._owned.get(hotkey_id)
        if owned is None:
            return
        try:
            assert self._user32 is not None
            ctypes.set_last_error(0)
            ok = self._user32.UnregisterHotKey(None, hotkey_id)
            if ok:
                self._owned.pop(hotkey_id, None)
                self._unregister_errors.pop(hotkey_id, None)
            else:
                error_code = ctypes.get_last_error()
                if self._unregister_errors.get(hotkey_id) != error_code:
                    self.events.put({"type": "log", "level": "warning", "message": f"取消热键 {owned[0]} 注册失败（Win32 错误 {error_code}），将继续重试。"})
                self._unregister_errors[hotkey_id] = error_code
        except Exception as exc:
            error_code = -1
            if self._unregister_errors.get(hotkey_id) != error_code:
                self.events.put({"type": "log", "level": "error", "message": f"取消热键 {owned[0]} 注册异常：{exc}；将继续重试。"})
            self._unregister_errors[hotkey_id] = error_code

    def _unregister_all(self) -> None:
        # Only IDs successfully registered by this manager are ever unregistered.
        for hotkey_id in list(self._owned):
            self._unregister_one(hotkey_id)

    def _publish_current_state(self) -> None:
        registered = [label for label, option_key in self._owned.values() if option_key is not None]
        relevant_ids = {
            record[0] for record in self._binding_records()
            if record[2] is None or record[2] in self._supported_keys
        }
        conflicts = [
            f"{record[1]} ({self._errors[record[0]]})"
            for record in self._binding_records() if record[0] in self._errors and record[0] in relevant_ids
        ]
        release_failures = [
            f"{self._owned[hotkey_id][0]} ({error_code})"
            for hotkey_id, error_code in self._unregister_errors.items() if hotkey_id in self._owned
        ]
        release_message = " · 热键释放失败：" + ", ".join(release_failures) if release_failures else ""
        if not self._connected:
            state, message = ("error", "未连接游戏" + release_message) if release_failures else ("waiting", "未连接游戏")
        elif self._pid is None:
            state, message = ("error", "已连接，但后端未报告游戏 PID" + release_message) if release_failures else ("waiting", "已连接，但后端未报告游戏 PID")
        elif not self._was_foreground:
            state, message = ("error", "游戏未在前台" + release_message) if release_failures else ("waiting", "游戏未在前台 · 未占用数字键")
        elif self._paused:
            pause_error = next((self._errors[record[0]] for record in self._binding_records() if record[2] is None and record[0] in self._errors), None)
            if pause_error is None:
                state, message = ("error", "已暂停" + release_message) if release_failures else ("paused", "已暂停 · Ctrl + Shift + Home 恢复")
            else:
                state, message = "conflict", f"已暂停 · Ctrl + Shift + Home 注册冲突（Win32 错误 {pause_error}）"
        elif conflicts:
            state, message = "conflict", f"可用 {len(registered)}/{len(self._supported_keys)} 项 · 注册冲突：" + ", ".join(conflicts)
        elif not self._supported_keys:
            state, message = "waiting", "已连接 · 后端未报告受支持的热键功能"
        else:
            state, message = "available", f"可用 {len(registered)}/{len(self._supported_keys)} 项（仅游戏前台）"
        if release_failures and state == "conflict":
            message += release_message
        elif release_failures and state != "error":
            state, message = "error", message + release_message
        self._publish(state, message, registered=registered, conflicts=conflicts, release_failures=release_failures)

    def _publish(self, state: str, message: str, **details: Any) -> None:
        payload = {
            "state": state, "message": message, "connected": self._connected,
            "pid": self._pid, "paused": self._paused,
            "registered": details.get("registered", [label for label, key in self._owned.values() if key is not None]),
            "pause_registered": any(key is None for _label, key in self._owned.values()),
            "conflicts": details.get("conflicts", []),
            "release_failures": details.get("release_failures", []),
            "supported": sorted(self._supported_keys),
        }
        with self._state_lock:
            if payload == self._state:
                return
            self._state = payload
        self.events.put({"type": "state", **payload})


def _enable_dpi_awareness() -> None:
    """Ask Windows for per-monitor DPI before creating the Tk root."""
    if os.name != "nt":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _safe_json(value: Any) -> Any:
    """Convert backend status into JSON-safe diagnostic data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v) for v in value]
    return repr(value)


def _load_engine() -> tuple[Any | None, str | None]:
    """Import and construct the optional backend without attaching to a game."""
    try:
        import trainer_engine  # type: ignore[import-not-found]

        factory = getattr(trainer_engine, "Engine", None)
        if factory is None:
            return None, "trainer_engine.py 尚未提供 Engine 类"
        return factory(), None
    except Exception as exc:
        return None, f"修改引擎不可用：{exc}"


def _read_flag(data: dict[str, Any], *names: str) -> bool:
    for name in names:
        value = data.get(name)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
    return False


def _normalize_capabilities(data: Any) -> dict[str, bool]:
    """Read only explicit backend capability declarations."""
    if isinstance(data, (list, tuple, set)):
        return {str(name): True for name in data}
    if not isinstance(data, dict):
        return {}
    normalized: dict[str, bool] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            supported = value.get("supported", value.get("available", False))
        else:
            supported = value
        normalized[str(key)] = supported is True or (isinstance(supported, (int, float)) and supported != 0)
    return normalized


def _option_value_for_toggle(key: str, enabled: bool, parsed_value: int | float | None = None) -> int | float | bool | None:
    """Return the exact payload sent by a checkbox or its matching hotkey."""
    if not enabled:
        return None
    meta = OPTION_BY_KEY[key]
    if meta["kind"] == "toggle":
        return True
    if parsed_value is None:
        raise ValueError(f"{meta['label']}需要数值")
    return parsed_value


class TrainerApp:
    def __init__(self, root: tk.Tk, *, smoke: bool = False, load_engine: bool = True) -> None:
        self.root = root
        self.smoke = smoke
        if load_engine:
            self.engine, engine_error = _load_engine()
        else:
            self.engine, engine_error = None, "自检模式：未加载游戏后端"
        self.engine_status: dict[str, Any] = {}
        self.capabilities: dict[str, bool] = {}
        self.connected = False
        self.game_running: bool | None = None
        self.closing = False
        self._status_after: str | None = None
        self._last_errors: dict[str, float] = {}
        self._last_attach_error: str | None = None
        self._log_lines: list[str] = []
        self._option_controls: dict[str, dict[str, Any]] = {}
        self._updating_controls = False
        self._engine_error = engine_error
        self._save_backend_message = "存档读写流程尚未接入界面；此页当前不会读取或写入存档文件。"
        self._save_module: Any | None = None
        self._save_profiles: list[dict[str, Any]] = []
        self._save_profiles_loaded = False
        self._save_profile_by_label: dict[str, dict[str, Any]] = {}
        self._save_resource_vars: dict[str, tk.StringVar] = {}
        self._save_resource_widgets: dict[str, Any] = {}
        self._backup_records: list[dict[str, Any]] = []
        self._sl_module: Any | None = None
        self._sl_list_ready = False
        self._sl_create_ready = False
        self._sl_restore_ready = False
        self._sl_select_ready = False
        self._sl_backend_message = "SL 回档接口尚未加载。"
        self._sl_checkpoints: list[dict[str, Any]] = []
        self._sl_checkpoint_labels: list[str] = []
        self._autosave_module: Any | None = None
        self._autosave_controller: Any | None = None
        self._autosave_ready = False
        self._autosave_error: str | None = None
        self._autosave_after: str | None = None
        self._autosave_profiles: list[str] = []
        self._autosave_last_message = ""
        self._autosave_last_level = "normal"
        self._autosave_updating_vars = False
        self.hotkey_manager = HotkeyManager()
        self._hotkey_after: str | None = None

        self.root.title(APP_TITLE)
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1040x870")
        self.root.minsize(900, 670)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._configure_style()
        self._build_ui()

        if engine_error and not smoke:
            self._log(engine_error, level="warning")
        self._refresh_status()
        self._status_after = self.root.after(POLL_MS, self._poll_status)
        self._hotkey_after = self.root.after(80, self._poll_hotkey_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("Card.TFrame", background=COLORS["surface2"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
        style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["text"])
        style.configure("Card.TLabel", background=COLORS["surface2"], foreground=COLORS["text"])
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"), foreground=COLORS["text"])
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground=COLORS["muted"])
        style.configure("Section.TLabel", background=COLORS["surface2"], font=("Segoe UI", 11, "bold"), foreground=COLORS["accent"])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["surface2"], foreground=COLORS["muted"], padding=(16, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", COLORS["surface3"])], foreground=[("selected", COLORS["text"])])
        style.configure("TCheckbutton", background=COLORS["surface2"], foreground=COLORS["text"], padding=3)
        style.map("TCheckbutton", background=[("active", COLORS["surface2"])], foreground=[("disabled", COLORS["dim"])])
        style.configure("TButton", background=COLORS["surface3"], foreground=COLORS["text"], padding=(12, 7), borderwidth=0)
        style.map("TButton", background=[("active", "#414b56"), ("disabled", COLORS["surface"])], foreground=[("disabled", COLORS["dim"])])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#15181d", font=("Segoe UI", 10, "bold"), padding=(15, 8))
        style.map("Accent.TButton", background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["surface3"])], foreground=[("disabled", COLORS["dim"])])
        style.configure("TSpinbox", fieldbackground=COLORS["surface"], background=COLORS["surface3"], foreground=COLORS["text"], arrowcolor=COLORS["text"], bordercolor=COLORS["border"], padding=5)
        style.configure("TCombobox", fieldbackground=COLORS["surface"], background=COLORS["surface3"], foreground=COLORS["text"], arrowcolor=COLORS["text"], bordercolor=COLORS["border"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["surface"]), ("disabled", COLORS["surface2"])], foreground=[("disabled", COLORS["dim"])], selectbackground=[("readonly", COLORS["surface3"])])
        style.configure("Status.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], padding=(9, 6))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(18, 14, 18, 12))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="Surface.TFrame", padding=(16, 12))
        header.pack(fill="x")
        title_col = ttk.Frame(header, style="Surface.TFrame")
        title_col.pack(side="left", fill="x", expand=True)
        ttk.Label(title_col, text="DARKEST DUNGEON", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_col, text=TARGET_BUILD, style="Subtitle.TLabel").pack(anchor="w", pady=(2, 0))
        self.connection_label = ttk.Label(header, text="● 正在检测游戏", style="Surface.TLabel", font=("Segoe UI", 10, "bold"))
        self.connection_label.pack(side="right", padx=(12, 8))

        bar = ttk.Frame(outer, style="Surface.TFrame", padding=(12, 8))
        bar.pack(fill="x", pady=(10, 9))
        self.status_label = ttk.Label(bar, text="引擎状态读取中…", style="Surface.TLabel")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.version_label = ttk.Label(bar, text="实测状态：待联机验证", style="Surface.TLabel", foreground=COLORS["accent"])
        self.version_label.pack(side="right", padx=(8, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 9))
        self.attach_button = ttk.Button(actions, text="连接游戏", style="Accent.TButton", command=self._attach)
        self.attach_button.pack(side="left")
        self.close_all_button = ttk.Button(actions, text="关闭所有", command=self._close_all)
        self.close_all_button.pack(side="left", padx=(8, 0))
        self.export_button = ttk.Button(actions, text="导出诊断", command=self._export_diagnostics)
        self.export_button.pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.live_tab = ttk.Frame(self.notebook, padding=12)
        self.save_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.live_tab, text="即时修改")
        self.notebook.add(self.save_tab, text="存档 / SL 回档")
        self._build_live_tab()
        self._build_save_tab()

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(9, 0))
        self.footer_label = ttk.Label(footer, text="仅供单机使用 · 支持状态由后端实时报告 · 功能待联机验证", style="Muted.TLabel")
        self.footer_label.pack(side="left")
        ttk.Label(footer, text=f"v{APP_VERSION}", style="Muted.TLabel").pack(side="right")

    def _build_live_tab(self) -> None:
        self.live_tab.columnconfigure(0, weight=1, uniform="column")
        self.live_tab.columnconfigure(1, weight=1, uniform="column")
        self.live_tab.rowconfigure(0, weight=1)
        self.live_tab.rowconfigure(1, weight=0)
        scroll_host = ttk.Frame(self.live_tab)
        scroll_host.grid(row=0, column=0, columnspan=2, sticky="nsew")
        scroll_host.columnconfigure(0, weight=1)
        scroll_host.rowconfigure(0, weight=1)
        canvas = tk.Canvas(scroll_host, bg=COLORS["bg"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.live_canvas = canvas
        self.live_content = ttk.Frame(canvas)
        self.live_content.columnconfigure(0, weight=1, uniform="column")
        self.live_content.columnconfigure(1, weight=1, uniform="column")
        self.live_window = canvas.create_window((0, 0), window=self.live_content, anchor="nw")
        self.live_content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(self.live_window, width=event.width))
        self.root.bind_all("<MouseWheel>", self._scroll_live, add="+")
        left = ttk.Frame(self.live_content)
        right = ttk.Frame(self.live_content)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self._add_option_group(left, "英雄与火把", ("health", "stress", "light"))
        self._add_option_group(left, "物品与财宝", ("items", "gold", "bust", "portrait", "deed", "crest"))
        self._add_option_group(right, "战斗与速度", ("one_hit", "damage", "defense", "shop", "speed"))
        self._add_hotkey_group(right)

        log_frame = ttk.Frame(self.live_tab, style="Surface.TFrame", padding=(10, 7))
        log_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        log_frame.columnconfigure(0, weight=1)
        ttk.Label(log_frame, text="运行记录", style="Surface.TLabel", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        self.log_text = tk.Text(log_frame, height=3, wrap="word", bg="#121519", fg=COLORS["muted"], insertbackground=COLORS["text"], relief="flat", bd=0, font=("Consolas", 9), state="disabled")
        self.log_text.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.log_text.tag_configure("warning", foreground="#f1b45f")
        self.log_text.tag_configure("error", foreground=COLORS["red"])
        self.log_text.tag_configure("success", foreground=COLORS["green"])
        self.log_text.tag_configure("normal", foreground=COLORS["muted"])

    def _scroll_live(self, event: tk.Event) -> str:
        widget: Any = event.widget
        while widget is not None:
            if widget is self.live_canvas:
                self.live_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                return "break"
            if hasattr(self, "save_canvas") and widget is self.save_canvas:
                self.save_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                return "break"
            widget = getattr(widget, "master", None)
        return "continue"

    def _add_option_group(self, parent: ttk.Frame, title: str, keys: tuple[str, ...]) -> None:
        group = ttk.Frame(parent, style="Card.TFrame", padding=(11, 9))
        group.pack(fill="x", pady=(0, 9))
        ttk.Label(group, text=title, style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 7))
        for row, key in enumerate(keys, start=1):
            meta = OPTION_BY_KEY[key]
            check_var = tk.BooleanVar(value=False)
            value_var = tk.StringVar(value=str(meta.get("default", "")))
            label = ttk.Label(group, text=meta["label"], style="Card.TLabel", width=13)
            label.grid(row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            check = ttk.Checkbutton(group, text="启用", variable=check_var, command=lambda option_key=key: self._on_option_toggle(option_key))
            check.grid(row=row, column=1, columnspan=2 if meta["kind"] == "toggle" else 1, sticky="w", padx=(1, 6), pady=2)
            spin = None
            if meta["kind"] != "toggle":
                spin = ttk.Spinbox(group, from_=meta["from"], to=meta["to"], increment=meta["step"], textvariable=value_var, width=12, justify="right", command=lambda option_key=key: self._on_value_change(option_key))
                spin.grid(row=row, column=2, sticky="e", pady=2)
                spin.bind("<Return>", lambda _event, option_key=key: self._on_value_change(option_key))
                spin.bind("<FocusOut>", lambda _event, option_key=key: self._on_value_change(option_key))
                value_var.trace_add("write", lambda *_args, option_key=key: self._on_value_change(option_key))
            self._option_controls[key] = {"check_var": check_var, "value_var": value_var, "check": check, "spin": spin, "label": label}
        group.columnconfigure(0, weight=1)

    def _add_hotkey_group(self, parent: ttk.Frame) -> None:
        group = ttk.Frame(parent, style="Card.TFrame", padding=(11, 9))
        group.pack(fill="both", expand=True, pady=(0, 9))
        ttk.Label(group, text="快捷键", style="Section.TLabel").pack(anchor="w")
        ttk.Label(group, text="仅连接游戏且游戏位于前台时注册数字键；切出游戏会自动释放热键。", style="Card.TLabel", wraplength=390).pack(anchor="w", pady=(3, 7))
        hotkey_grid = ttk.Frame(group, style="Card.TFrame")
        hotkey_grid.pack(fill="both", expand=True)
        for index, (key, description) in enumerate(HOTKEYS):
            row = index // 2
            col = (index % 2) * 2
            ttk.Label(hotkey_grid, text=key, style="Card.TLabel", foreground=COLORS["muted"], width=18).grid(row=row, column=col, sticky="w", padx=(0, 5), pady=2)
            ttk.Label(hotkey_grid, text=description, style="Card.TLabel", foreground=COLORS["dim"], width=13).grid(row=row, column=col + 1, sticky="w", pady=2)
        self.hotkey_status_label = ttk.Label(group, text="热键状态：正在初始化…", style="Card.TLabel", foreground=COLORS["dim"], wraplength=390)
        self.hotkey_status_label.pack(anchor="w", pady=(5, 0))

    def _build_save_tab(self) -> None:
        self.save_tab.columnconfigure(0, weight=1)
        self.save_tab.rowconfigure(1, weight=1)
        profile_card = ttk.Frame(self.save_tab, style="Card.TFrame", padding=12)
        profile_card.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        profile_card.columnconfigure(1, weight=1)
        ttk.Label(profile_card, text="存档文件", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.save_scan_button = ttk.Button(profile_card, text="扫描存档", command=self._scan_save_profiles)
        self.save_scan_button.grid(row=1, column=0, sticky="w", padx=(0, 9))
        self.save_profile_combo = ttk.Combobox(profile_card, state="disabled", values=())
        self.save_profile_combo.grid(row=1, column=1, sticky="ew")
        self.save_profile_combo.bind("<<ComboboxSelected>>", self._on_save_profile_selected)
        self.save_profile_detail = ttk.Label(profile_card, text="尚未扫描存档", style="Card.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.save_profile_detail.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        body = ttk.Frame(self.save_tab)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.save_canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0, bd=0)
        save_scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.save_canvas.yview)
        self.save_canvas.configure(yscrollcommand=save_scrollbar.set)
        self.save_canvas.grid(row=0, column=0, sticky="nsew")
        save_scrollbar.grid(row=0, column=1, sticky="ns")
        self.save_content = ttk.Frame(self.save_canvas)
        self.save_content.columnconfigure(0, weight=3, uniform="save")
        self.save_content.columnconfigure(1, weight=2, uniform="save")
        self.save_window = self.save_canvas.create_window((0, 0), window=self.save_content, anchor="nw")
        self.save_content.bind("<Configure>", lambda _event: self.save_canvas.configure(scrollregion=self.save_canvas.bbox("all")))
        self.save_canvas.bind("<Configure>", lambda event: self.save_canvas.itemconfigure(self.save_window, width=event.width))
        self._build_sl_checkpoint_card()
        edit_card = ttk.Frame(self.save_content, style="Card.TFrame", padding=12)
        edit_card.grid(row=1, column=0, sticky="nsew", padx=(0, 7))
        backup_card = ttk.Frame(self.save_content, style="Card.TFrame", padding=12)
        backup_card.grid(row=1, column=1, sticky="nsew", padx=(7, 0))
        ttk.Label(edit_card, text="庄园资源", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        resources = (("gold", "金币", 9999999), ("bust", "半身像", 999), ("portrait", "肖像", 999), ("deed", "地契", 999), ("crest", "徽章", 999))
        for row, (key, label, default) in enumerate(resources, start=1):
            ttk.Label(edit_card, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            variable = tk.StringVar(value=str(default))
            self._save_resource_vars[key] = variable
            spin = ttk.Spinbox(edit_card, from_=0, to=99999999, increment=1000 if key == "gold" else 1, textvariable=variable, width=16, justify="right")
            spin.grid(row=row, column=1, sticky="ew", pady=3)
            self._save_resource_widgets[key] = spin
        edit_card.columnconfigure(1, weight=1)
        self.save_clear_stress_var = tk.BooleanVar(value=False)
        self.save_clear_stress_check = ttk.Checkbutton(edit_card, text="将存档中所有英雄的压力清零", variable=self.save_clear_stress_var)
        self.save_clear_stress_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=(9, 5))
        self.save_edit_button = ttk.Button(edit_card, text="写入存档", style="Accent.TButton", command=self._edit_selected_save)
        self.save_edit_button.grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(backup_card, text="备份恢复", style="Section.TLabel").pack(anchor="w")
        ttk.Label(backup_card, text="仅显示本次会话中由写入接口返回的备份 ID。恢复所选备份会覆盖对应存档。", style="Card.TLabel", wraplength=320).pack(anchor="w", pady=(7, 9))
        self.backup_combo = ttk.Combobox(backup_card, state="disabled", values=())
        self.backup_combo.pack(fill="x")
        self.backup_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_save_controls())
        self.restore_backup_button = ttk.Button(backup_card, text="恢复所选备份", command=self._restore_selected_backup)
        self.restore_backup_button.pack(anchor="w", pady=(8, 4))
        self.backup_detail = ttk.Label(backup_card, text="当前没有可恢复的备份", style="Card.TLabel", foreground=COLORS["muted"], wraplength=320)
        self.backup_detail.pack(anchor="w", pady=(7, 0))

        status_frame = ttk.Frame(self.save_tab, style="Surface.TFrame", padding=(10, 8))
        status_frame.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        self.save_status_label = ttk.Label(status_frame, text=self._save_backend_message, style="Surface.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.save_status_label.pack(anchor="w")
        self._refresh_save_api_state()

    def _build_sl_checkpoint_card(self) -> None:
        card = ttk.Frame(self.save_content, style="Card.TFrame", padding=12)
        card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 9))
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="SL 回档点", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        flow = "退出游戏 → 保存回档点 → 游玩 → 退出游戏 → 恢复回档点"
        ttk.Label(
            card,
            text=f"操作流程：{flow}\n自动回档点定时备份已落盘进度；写入中跳过。恢复前仍需完全退出游戏；如需撤销，请从列表选择恢复前自动安全点。",
            style="Card.TLabel", wraplength=850,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 8))

        self.autosave_enabled_var = tk.BooleanVar(value=False)
        self.autosave_interval_var = tk.StringVar(value="10")
        self.autosave_enabled_check = ttk.Checkbutton(
            card, text="自动创建回档点", variable=self.autosave_enabled_var,
            command=self._on_autosave_config_change,
        )
        self.autosave_enabled_check.grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(card, text="间隔（分钟）", style="Card.TLabel").grid(row=2, column=1, sticky="e", padx=(0, 6), pady=2)
        self.autosave_interval_spin = ttk.Spinbox(
            card, from_=1, to=1440, increment=1, textvariable=self.autosave_interval_var,
            width=8, justify="right", command=self._on_autosave_config_change,
        )
        self.autosave_interval_spin.grid(row=2, column=2, sticky="e", pady=2)
        self.autosave_interval_spin.bind("<Return>", self._on_autosave_config_change)
        self.autosave_interval_spin.bind("<FocusOut>", self._on_autosave_config_change)
        self.autosave_status_label = ttk.Label(card, text="自动回档点接口尚未加载。", style="Card.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.autosave_status_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 6))

        ttk.Label(card, text="回档点名称（可选）", style="Card.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
        self.sl_checkpoint_name_var = tk.StringVar(value="")
        self.sl_checkpoint_name_entry = ttk.Entry(card, textvariable=self.sl_checkpoint_name_var)
        self.sl_checkpoint_name_entry.grid(row=4, column=1, sticky="ew", pady=3)
        self.sl_save_checkpoint_button = ttk.Button(card, text="保存回档点", style="Accent.TButton", command=self._save_selected_checkpoint)
        self.sl_save_checkpoint_button.grid(row=4, column=2, sticky="e", padx=(8, 0), pady=3)
        ttk.Label(card, text="已有回档点", style="Card.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=3)
        self.sl_checkpoint_combo = ttk.Combobox(card, state="disabled", values=())
        self.sl_checkpoint_combo.grid(row=5, column=1, sticky="ew", pady=3)
        self.sl_checkpoint_combo.bind("<<ComboboxSelected>>", self._on_sl_checkpoint_selected)
        self.sl_refresh_button = ttk.Button(card, text="刷新回档点", command=self._refresh_current_sl_checkpoints)
        self.sl_refresh_button.grid(row=5, column=2, sticky="e", padx=(8, 0), pady=3)

        filter_frame = ttk.Frame(card, style="Card.TFrame")
        filter_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(5, 2))
        ttk.Label(filter_frame, text="现实 N 天前", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.sl_days_ago_var = tk.StringVar(value="0")
        self.sl_days_ago_spin = ttk.Spinbox(filter_frame, from_=0, to=3650, increment=1, textvariable=self.sl_days_ago_var, width=6, justify="right")
        self.sl_days_ago_spin.grid(row=0, column=1, sticky="w", padx=(0, 5))
        self.sl_days_select_button = ttk.Button(filter_frame, text="选择回档点", command=self._select_checkpoint_by_days)
        self.sl_days_select_button.grid(row=0, column=2, sticky="w", padx=(0, 16))
        ttk.Label(filter_frame, text="游戏第 N 周", style="Card.TLabel").grid(row=0, column=3, sticky="w", padx=(0, 5))
        self.sl_game_week_var = tk.StringVar(value="0")
        self.sl_game_week_spin = ttk.Spinbox(filter_frame, from_=0, to=9999, increment=1, textvariable=self.sl_game_week_var, width=6, justify="right")
        self.sl_game_week_spin.grid(row=0, column=4, sticky="w", padx=(0, 5))
        self.sl_week_select_button = ttk.Button(filter_frame, text="选择回档点", command=self._select_checkpoint_by_week)
        self.sl_week_select_button.grid(row=0, column=5, sticky="w")
        filter_frame.columnconfigure(6, weight=1)

        self.sl_restore_checkpoint_button = ttk.Button(card, text="恢复所选回档点", command=self._restore_selected_checkpoint)
        self.sl_restore_checkpoint_button.grid(row=7, column=2, sticky="e", padx=(8, 0), pady=(7, 3))
        self.sl_checkpoint_detail = ttk.Label(card, text="请先扫描并选择一个存档，再刷新回档点。", style="Card.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.sl_checkpoint_detail.grid(row=7, column=0, columnspan=2, sticky="w", pady=(7, 3))
        self.sl_game_state_label = ttk.Label(card, text="正在读取游戏运行状态…", style="Card.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.sl_game_state_label.grid(row=8, column=0, columnspan=3, sticky="w", pady=(5, 2))
        self.sl_status_label = ttk.Label(card, text=self._sl_backend_message, style="Card.TLabel", foreground=COLORS["muted"], wraplength=850)
        self.sl_status_label.grid(row=9, column=0, columnspan=3, sticky="w", pady=(2, 0))

    def _refresh_save_api_state(self) -> None:
        try:
            import save_runtime  # type: ignore[import-not-found]

            self._save_module = save_runtime
        except Exception as exc:
            self._save_module = None
            self._save_backend_message = f"存档后端未加载：{type(exc).__name__}: {exc}"
        self._save_scan_ready = callable(getattr(self._save_module, "save_profiles", None))
        self._save_edit_ready = callable(getattr(self._save_module, "edit_save", None))
        self._save_restore_ready = callable(getattr(self._save_module, "restore_backup", None))
        if self._save_module is not None and not (self._save_scan_ready and self._save_edit_ready and self._save_restore_ready):
            missing = [name for name, ready in (("save_profiles", self._save_scan_ready), ("edit_save", self._save_edit_ready), ("restore_backup", self._save_restore_ready)) if not ready]
            self._save_backend_message = "存档后端尚未提供完整界面接口：" + ", ".join(missing)
        elif self._save_module is not None:
            self._save_backend_message = "存档接口已就绪；请选择存档并检查资源数值后再写入。"
        self.save_scan_button.configure(state="normal" if self._save_scan_ready else "disabled")
        self.save_status_label.configure(text=self._save_backend_message)
        self._refresh_sl_api_state()
        self._refresh_autosave_api_state()
        self._update_save_controls()

    def _refresh_sl_api_state(self) -> None:
        try:
            import sl_runtime  # type: ignore[import-not-found]

            self._sl_module = sl_runtime
        except Exception as exc:
            self._sl_module = None
            self._sl_backend_message = f"SL 回档后端未加载：{type(exc).__name__}: {exc}"
        self._sl_list_ready = callable(getattr(self._sl_module, "list_checkpoints", None))
        self._sl_create_ready = callable(getattr(self._sl_module, "create_checkpoint", None))
        self._sl_restore_ready = callable(getattr(self._sl_module, "restore_checkpoint", None))
        self._sl_select_ready = callable(getattr(self._sl_module, "select_checkpoint", None))
        missing = [name for name, ready in (
            ("list_checkpoints", self._sl_list_ready),
            ("create_checkpoint", self._sl_create_ready),
            ("restore_checkpoint", self._sl_restore_ready),
        ) if not ready]
        if self._sl_module is not None and missing:
            self._sl_backend_message = "SL 回档接口尚未完整提供：" + ", ".join(missing)
        elif self._sl_module is not None and not self._sl_select_ready:
            self._sl_backend_message = "SL 回档接口已就绪；按现实日期/游戏周定位当前不可用。"
        elif self._sl_module is not None:
            self._sl_backend_message = "SL 回档接口已就绪；保存和恢复前请完全退出游戏。"
        if hasattr(self, "sl_status_label"):
            self.sl_status_label.configure(text=self._sl_backend_message)

    def _autosave_status_snapshot(self) -> dict[str, Any]:
        controller = self._autosave_controller
        if controller is None:
            return {}
        try:
            status = getattr(controller, "status", {})
            if callable(status):
                status = status()
            return status if isinstance(status, dict) else {}
        except Exception as exc:
            self._record_error("读取自动回档状态失败", exc)
            return {"last_error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _autosave_profile_paths(profiles: Any) -> list[str]:
        paths: list[str] = []
        if isinstance(profiles, (list, tuple, set)):
            for item in profiles:
                raw = item.get("path") if isinstance(item, dict) else item
                if raw is not None and str(raw).strip():
                    paths.append(str(raw))
        return paths

    def _refresh_autosave_api_state(self) -> None:
        if self.smoke:
            self._autosave_ready = False
            self._autosave_error = "烟雾测试模式：自动回档控制器未启动。"
            self._set_autosave_status(self._autosave_error, "normal", log=False)
            self._update_save_controls()
            return
        if self._autosave_controller is not None:
            self._autosave_ready = True
            self._render_autosave_status()
            return
        try:
            import autosave_runtime  # type: ignore[import-not-found]

            self._autosave_module = autosave_runtime
            controller_factory = getattr(autosave_runtime, "AutoBackupController", None)
            if not callable(controller_factory):
                raise RuntimeError("autosave_runtime 尚未提供 AutoBackupController")
            manager = getattr(self._sl_module, "_default_manager", None)
            if manager is None and callable(getattr(self._sl_module, "CheckpointManager", None)):
                manager = self._sl_module.CheckpointManager()
            self._autosave_controller = controller_factory(manager=manager)
            status = self._autosave_status_snapshot()
            self._autosave_profiles = self._autosave_profile_paths(status.get("profiles", []))
            try:
                interval = max(1, min(1440, int(status.get("interval_minutes", 10))))
            except (TypeError, ValueError):
                interval = 10
            self._autosave_updating_vars = True
            self.autosave_enabled_var.set(bool(status.get("enabled", False)))
            self.autosave_interval_var.set(str(interval))
            self._autosave_updating_vars = False
            starter = getattr(self._autosave_controller, "start", None)
            if callable(starter):
                starter()
            self._autosave_ready = True
            self._autosave_error = None
            if self._autosave_after is None:
                self._autosave_after = self.root.after(400, self._poll_autosave_events)
            self._render_autosave_status()
        except Exception as exc:
            self._autosave_controller = None
            self._autosave_ready = False
            self._autosave_error = f"自动回档不可用：{type(exc).__name__}: {exc}"
            self._autosave_updating_vars = True
            self.autosave_enabled_var.set(False)
            self._autosave_updating_vars = False
            self._set_autosave_status(self._autosave_error, "error", log=False)
            self._update_save_controls()

    def _set_autosave_status(self, message: str, level: str = "normal", *, log: bool = False) -> None:
        if not hasattr(self, "autosave_status_label"):
            return
        color = {"success": COLORS["green"], "warning": "#f1b45f", "error": COLORS["red"]}.get(level, COLORS["muted"])
        self.autosave_status_label.configure(text=message, foreground=color)
        if log:
            self._log(message, level=level)

    def _autosave_target_label(self) -> str | None:
        profile = self._selected_save_profile()
        if profile is not None and self._autosave_profiles and self._same_profile_path(profile.get("path"), self._autosave_profiles[0]):
            return str(profile.get("label") or Path(str(profile.get("path"))).name or profile.get("path"))
        if not self._autosave_profiles:
            return None
        for item in self._save_profiles:
            if self._same_profile_path(item.get("path"), self._autosave_profiles[0]):
                return str(item.get("label") or Path(str(item.get("path"))).name or item.get("path"))
        return Path(self._autosave_profiles[0]).name or self._autosave_profiles[0]

    def _render_autosave_status(self) -> None:
        if self._autosave_error:
            self._set_autosave_status(self._autosave_error, "error", log=False)
            self._update_save_controls()
            return
        if not self._autosave_ready:
            self._set_autosave_status("自动回档接口尚未加载。", "warning", log=False)
            self._update_save_controls()
            return
        status = self._autosave_status_snapshot()
        if status.get("settings_error"):
            message = f"自动回档设置异常：{status['settings_error']}"
            level = "error"
        elif status.get("last_error"):
            message = f"自动回档错误：{status['last_error']}"
            level = "error"
        elif self._autosave_last_message:
            message = self._autosave_last_message
            level = self._autosave_last_level
        elif status.get("last_message"):
            message = str(status.get("last_message"))
            level = "normal"
        elif not status.get("enabled", self.autosave_enabled_var.get()):
            message = "自动回档已关闭。"
            level = "normal"
        elif not self._autosave_profiles:
            message = "自动回档已启用；请扫描存档以选择需要监控的存档槽。"
            level = "warning"
        elif status.get("waiting_for_game"):
            target = self._autosave_target_label() or "已选存档槽"
            message = f"自动回档已启用，监控“{target}”；游戏运行中按间隔备份已落盘进度，写入中跳过。"
            level = "normal"
        else:
            try:
                minutes = int(status.get("interval_minutes", self.autosave_interval_var.get()))
            except (TypeError, ValueError):
                minutes = 10
            target = self._autosave_target_label() or "已选存档槽"
            message = f"自动回档已启用，监控“{target}”：每 {minutes} 分钟检查已落盘进度；写入中跳过。"
            level = "success"
        self._set_autosave_status(message, level, log=False)
        self._update_save_controls()

    def _on_autosave_config_change(self, _event: Any = None) -> None:
        if self._autosave_updating_vars or not self._autosave_ready or self._autosave_controller is None:
            return
        try:
            raw_interval = self.autosave_interval_var.get().strip()
            if not raw_interval.isdigit():
                raise ValueError("间隔必须是 1 到 1440 的整数分钟")
            interval = int(raw_interval)
            if not 1 <= interval <= 1440:
                raise ValueError("间隔范围为 1 到 1440 分钟")
            selected_profile = self._selected_save_profile()
            profile_paths = [str(selected_profile["path"])] if selected_profile and selected_profile.get("path") else (
                list(self._autosave_profiles) if not self._save_profiles_loaded else []
            )
            result = self._autosave_controller.configure(
                enabled=bool(self.autosave_enabled_var.get()),
                interval_minutes=interval,
                profiles=profile_paths,
                backup_while_running=True,
            )
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise RuntimeError(str(result.get("error") or result.get("message") or "自动回档配置未保存"))
            self._autosave_profiles = profile_paths
            self._autosave_error = None
            self._autosave_last_message = ""
            self._autosave_last_level = "normal"
            self._render_autosave_status()
        except Exception as exc:
            self._autosave_error = f"自动回档配置失败：{type(exc).__name__}: {exc}"
            self._record_error("保存自动回档配置失败", exc)
            self._render_autosave_status()

    def _sync_autosave_profiles(self) -> None:
        if not self._autosave_ready or self._autosave_controller is None:
            return
        try:
            interval = int(self.autosave_interval_var.get())
            selected = self._selected_save_profile()
            profiles = [str(selected["path"])] if selected and selected.get("path") else []
            result = self._autosave_controller.configure(
                enabled=bool(self.autosave_enabled_var.get()),
                interval_minutes=interval,
                profiles=profiles,
                backup_while_running=True,
            )
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise RuntimeError(str(result.get("error") or result.get("message") or "存档槽配置未保存"))
            self._autosave_profiles = profiles
            self._autosave_error = None
            self._autosave_last_message = ""
            self._autosave_last_level = "normal"
            self._render_autosave_status()
        except Exception as exc:
            self._autosave_error = f"更新自动回档存档槽失败：{type(exc).__name__}: {exc}"
            self._record_error("更新自动回档存档槽失败", exc)
            self._render_autosave_status()

    def _preferred_save_profile_index(self, profiles: list[dict[str, Any]], preserve_path: Any = None) -> int:
        preferred_path = preserve_path
        if preferred_path is None and self._autosave_profiles:
            preferred_path = self._autosave_profiles[0]
        if preferred_path is not None:
            for index, profile in enumerate(profiles):
                if self._same_profile_path(profile.get("path"), preferred_path):
                    return index
        return 0

    def _poll_autosave_events(self) -> None:
        self._autosave_after = None
        if self.closing:
            return
        self._drain_autosave_events()
        if self._autosave_ready:
            self._autosave_after = self.root.after(400, self._poll_autosave_events)

    def _drain_autosave_events(self) -> int:
        controller = self._autosave_controller
        get_events = getattr(controller, "get_events", None) if controller is not None else None
        if not callable(get_events):
            return 0
        try:
            events = get_events(limit=100)
            if not isinstance(events, (list, tuple)):
                return 0
        except Exception as exc:
            self._record_error("读取自动回档事件失败", exc)
            return 0
        processed = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            processed += 1
            event_type = str(event.get("type", "status"))
            message = str(event.get("message") or "").strip()
            if event_type == "checkpoint_created":
                checkpoint = event.get("checkpoint") or event.get("metadata") or event
                profile_path = event.get("profile") or (checkpoint.get("profile") if isinstance(checkpoint, dict) else None)
                if not isinstance(checkpoint, dict) or checkpoint.get("id") is None:
                    self._record_error("自动回档事件缺少回档点信息", ValueError(repr(event)))
                    continue
                if profile_path is not None and not self._same_profile_path(profile_path, checkpoint.get("profile", profile_path)):
                    self._record_error("自动回档事件存档槽不匹配", ValueError(str(profile_path)))
                    continue
                profile_path = str(profile_path or checkpoint.get("profile") or "")
                known_paths = list(self._autosave_profiles)
                if self._save_profiles_loaded:
                    known_paths.extend(str(item.get("path", "")) for item in self._save_profiles if item.get("path"))
                if known_paths and not any(self._same_profile_path(profile_path, path) for path in known_paths):
                    self._record_error("忽略未配置存档槽的自动回档事件", ValueError(profile_path))
                    continue
                current = self._selected_sl_checkpoint()
                current_id = current.get("id") if current and self._same_profile_path(current.get("profile"), profile_path) else None
                selected_profile = self._selected_save_profile()
                if selected_profile and self._same_profile_path(selected_profile.get("path"), profile_path):
                    self._refresh_sl_checkpoints(preserve_id=current_id, quiet=True)
                label = str(checkpoint.get("label") or "自动回档点")
                message = message or f"已创建自动回档点“{label}”。"
                self._autosave_last_message = message
                self._autosave_last_level = "success"
                self._set_autosave_status(message, "success", log=True)
            elif event_type == "unchanged":
                message = message or "存档内容未变化，本轮自动回档点已跳过。"
                self._autosave_last_message = message
                self._autosave_last_level = "normal"
                self._set_autosave_status(message, "normal", log=False)
            elif event_type == "waiting_game":
                target = self._autosave_target_label() or "已选存档槽"
                message = message or f"游戏运行中；自动回档正在监控“{target}”的稳定落盘进度。"
                self._autosave_last_message = message
                self._autosave_last_level = "normal"
                self._set_autosave_status(message, "normal", log=False)
            elif event_type == "error":
                message = message or "自动回档发生错误。"
                self._autosave_last_message = message
                self._autosave_last_level = "error"
                self._set_autosave_status(message, "error", log=True)
            elif message:
                self._autosave_last_message = message
                self._autosave_last_level = "normal"
                self._set_autosave_status(message, "normal", log=False)
        self._render_autosave_status()
        return processed

    def _stop_autosave_controller(self) -> None:
        controller = self._autosave_controller
        stopper = getattr(controller, "stop", None) if controller is not None else None
        if callable(stopper):
            stopper()

    def _update_save_controls(self) -> None:
        profile = self._selected_save_profile()
        profile_selected = profile is not None
        game_closed = self.game_running is False
        values_state = "normal" if profile_selected and self._save_edit_ready and game_closed else "disabled"
        for key in ("gold", "bust", "portrait", "deed", "crest"):
            widget = self._save_resource_widgets.get(key)
            if widget is not None:
                widget.configure(state=values_state)
        self.save_clear_stress_check.configure(state=values_state)
        self.save_edit_button.configure(state="normal" if values_state == "normal" else "disabled")
        self.restore_backup_button.configure(state="normal" if self._save_restore_ready and self.backup_combo.current() >= 0 and game_closed else "disabled")
        if self._save_scan_ready:
            self.save_profile_combo.configure(state="readonly" if self._save_profiles else "disabled")
        if self._backup_records:
            self.backup_combo.configure(state="readonly")
        else:
            self.backup_combo.configure(state="disabled")
        if hasattr(self, "sl_game_state_label"):
            if self.game_running is True:
                self.sl_game_state_label.configure(text="检测到游戏仍在运行；保存或恢复存档前请完全退出游戏。当前写入操作已禁用。", foreground=COLORS["red"])
            elif self.game_running is False:
                self.sl_game_state_label.configure(text="已确认游戏未运行，可以保存或恢复回档点。", foreground=COLORS["green"])
            else:
                self.sl_game_state_label.configure(text="无法确认游戏已完全退出；保存或恢复操作已禁用。", foreground="#f1b45f")
        if hasattr(self, "sl_checkpoint_combo"):
            has_profile = profile_selected and self._sl_list_ready
            self.sl_checkpoint_combo.configure(state="readonly" if has_profile and self._sl_checkpoints else "disabled")
            self.sl_refresh_button.configure(state="normal" if has_profile else "disabled")
            self.sl_checkpoint_name_entry.configure(state="normal" if profile_selected and self._sl_create_ready and game_closed else "disabled")
            self.sl_save_checkpoint_button.configure(state="normal" if profile_selected and self._sl_create_ready and game_closed else "disabled")
            selector_state = "normal" if profile_selected and self._sl_select_ready else "disabled"
            self.sl_days_ago_spin.configure(state=selector_state)
            self.sl_days_select_button.configure(state=selector_state)
            self.sl_game_week_spin.configure(state=selector_state)
            self.sl_week_select_button.configure(state=selector_state)
            checkpoint = self._selected_sl_checkpoint()
            checkpoint_matches = bool(profile and checkpoint and self._same_profile_path(checkpoint.get("profile"), profile.get("path")))
            self.sl_restore_checkpoint_button.configure(state="normal" if checkpoint_matches and self._sl_restore_ready and game_closed else "disabled")
        if hasattr(self, "autosave_enabled_check"):
            state = "normal" if self._autosave_ready else "disabled"
            self.autosave_enabled_check.configure(state=state)
            self.autosave_interval_spin.configure(state=state)

    def _selected_save_profile(self) -> dict[str, Any] | None:
        if not hasattr(self, "save_profile_combo"):
            return None
        index = self.save_profile_combo.current()
        if index < 0 or index >= len(self._save_profiles):
            return None
        return self._save_profiles[index]

    @staticmethod
    def _same_profile_path(first: Any, second: Any) -> bool:
        if first is None or second is None:
            return False
        try:
            return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))
        except Exception:
            return str(first) == str(second)

    def _selected_sl_checkpoint(self) -> dict[str, Any] | None:
        if not hasattr(self, "sl_checkpoint_combo"):
            return None
        index = self.sl_checkpoint_combo.current()
        if index < 0 or index >= len(self._sl_checkpoints):
            return None
        return self._sl_checkpoints[index]

    @staticmethod
    def _checkpoint_week_text(checkpoint: dict[str, Any]) -> str:
        value = checkpoint.get("game_week")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return f"第 {value} 周"
        return "周数未知"

    def _clear_sl_checkpoint_selection(self, message: str) -> None:
        self.sl_checkpoint_combo.set("")
        self.sl_checkpoint_detail.configure(text=message)
        self._update_save_controls()

    def _choose_sl_checkpoint(self, *, days_ago: int | None = None, game_week: int | None = None) -> None:
        profile = self._selected_save_profile()
        if profile is None:
            self._set_sl_status("请先扫描并选择一个存档槽。", "warning")
            return
        if not self._sl_select_ready or self._sl_module is None:
            self._set_sl_status("当前回档后端没有提供按日期/游戏周筛选接口。", "warning")
            return
        profile_path = str(profile.get("path", ""))
        if not profile_path:
            self._set_sl_status("所选存档缺少路径，无法查找候选回档点。", "error")
            return
        try:
            if days_ago is not None:
                candidate = self._sl_module.select_checkpoint(profile_path, days_ago=days_ago)
                criterion = f"现实 {days_ago} 天前"
            else:
                assert game_week is not None
                candidate = self._sl_module.select_checkpoint(profile_path, game_week=game_week)
                criterion = f"游戏第 {game_week} 周"
            if candidate is None:
                self._clear_sl_checkpoint_selection(f"没有符合“{criterion}”的回档点；未恢复或修改存档。")
                self._set_sl_status(f"当前存档槽没有符合“{criterion}”的候选回档点。", "warning", log=False)
                return
            if not isinstance(candidate, dict) or candidate.get("id") is None:
                raise TypeError("select_checkpoint() 必须返回回档点字典或 None")
            if not self._same_profile_path(candidate.get("profile"), profile_path):
                raise RuntimeError("筛选接口返回了其他存档槽的回档点，已拒绝选择。")
            if game_week is not None:
                actual_week = candidate.get("game_week")
                if not isinstance(actual_week, int) or isinstance(actual_week, bool) or actual_week != game_week:
                    raise RuntimeError("筛选接口返回的回档点周数与所选周数不匹配。")
            checkpoint_id = candidate["id"]
            if not self._refresh_sl_checkpoints(preserve_id=checkpoint_id, quiet=True):
                raise RuntimeError("无法刷新当前存档槽的回档点列表。")
            selected = self._selected_sl_checkpoint()
            if selected is None or str(selected.get("id")) != str(checkpoint_id):
                raise RuntimeError("筛选结果未出现在当前存档槽的可用回档点列表中。")
            self._show_sl_checkpoint_detail(selected)
            detail = f"日期：{self._format_sl_datetime(selected.get('created_at'))}；游戏周：{self._checkpoint_week_text(selected)}。"
            self._set_sl_status(f"已选中符合“{criterion}”的候选回档点。{detail}尚未恢复；确认后再点“恢复所选回档点”。", "success")
        except Exception as exc:
            self._record_error("筛选 SL 回档点失败", exc)
            self._set_sl_status(f"筛选候选回档点失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _select_checkpoint_by_days(self) -> None:
        try:
            raw = self.sl_days_ago_var.get().strip()
            if not raw.isdigit():
                raise ValueError("天数必须是非负整数")
            days = int(raw)
            if not 0 <= days <= 3650:
                raise ValueError("天数范围为 0 到 3650")
            self._choose_sl_checkpoint(days_ago=days)
        except Exception as exc:
            self._set_sl_status(f"现实日期筛选值无效：{exc}", "warning", log=False)

    def _select_checkpoint_by_week(self) -> None:
        try:
            raw = self.sl_game_week_var.get().strip()
            if not raw.isdigit():
                raise ValueError("游戏周数必须是非负整数，0 周有效")
            week = int(raw)
            if not 0 <= week <= 9999:
                raise ValueError("游戏周数范围为 0 到 9999")
            self._choose_sl_checkpoint(game_week=week)
        except Exception as exc:
            self._set_sl_status(f"游戏周筛选值无效：{exc}", "warning", log=False)

    def _set_save_status(self, message: str, level: str = "normal", *, log: bool = True) -> None:
        self._save_backend_message = message
        color = {"success": COLORS["green"], "warning": "#f1b45f", "error": COLORS["red"]}.get(level, COLORS["muted"])
        self.save_status_label.configure(text=message, foreground=color)
        if log:
            self._log(message, level=level)

    def _scan_save_profiles(self, preserve_path: Any = None, *, quiet: bool = False) -> None:
        self._refresh_save_api_state()
        if not self._save_scan_ready or self._save_module is None:
            self._set_save_status(self._save_backend_message, "warning", log=not quiet)
            return
        try:
            result = self._save_module.save_profiles()
            if not isinstance(result, (list, tuple)):
                raise TypeError("save_profiles() 必须返回存档字典列表")
            profiles = [profile for profile in result if isinstance(profile, dict) and profile.get("path") is not None]
            self._save_profiles = profiles
            self._save_profiles_loaded = True
            self._save_profile_by_label.clear()
            labels: list[str] = []
            counts: dict[str, int] = {}
            for profile in profiles:
                label = str(profile.get("label") or Path(str(profile["path"])).name or profile["path"])
                counts[label] = counts.get(label, 0) + 1
                display = label if counts[label] == 1 else f"{label} ({counts[label]})"
                labels.append(display)
                self._save_profile_by_label[display] = profile
            self.save_profile_combo.configure(values=tuple(labels))
            if profiles:
                selected = self._preferred_save_profile_index(profiles, preserve_path)
                self.save_profile_combo.current(selected)
                self._on_save_profile_selected()
                self._set_save_status(f"已扫描到 {len(profiles)} 个存档。检查文件和数值后再写入。", "success", log=not quiet)
            else:
                self.save_profile_combo.set("")
                self.save_profile_detail.configure(text="未找到可用存档。")
                self._refresh_sl_checkpoints(quiet=True)
                self._update_save_controls()
                self._sync_autosave_profiles()
                self._set_save_status("扫描完成：没有找到可用存档。", "warning", log=not quiet)
        except Exception as exc:
            self._record_error("扫描存档失败", exc)
            self._set_save_status(f"扫描存档失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _on_save_profile_selected(self, _event: Any = None) -> None:
        index = self.save_profile_combo.current()
        if index < 0 or index >= len(self._save_profiles):
            self.save_profile_detail.configure(text="尚未选择存档。")
            self._refresh_sl_checkpoints(quiet=True)
            self._update_save_controls()
            return
        profile = self._save_profiles[index]
        wallet = profile.get("wallet") if isinstance(profile.get("wallet"), dict) else {}
        for key in ("gold", "bust", "portrait", "deed", "crest"):
            default = OPTION_BY_KEY[key]["default"]
            self._save_resource_vars[key].set(str(wallet.get(key, default)))
        heroes = profile.get("heroes", [])
        if isinstance(heroes, (list, tuple)):
            names = []
            for hero in heroes:
                if isinstance(hero, dict):
                    name = hero.get("name") or hero.get("hero_name") or hero.get("heroClass")
                    if name:
                        names.append(str(name))
            hero_summary = f"英雄：{len(heroes)} 人"
            if names:
                hero_summary += " · " + "、".join(names[:6])
                if len(names) > 6:
                    hero_summary += "…"
        else:
            hero_summary = "英雄：信息不可用"
        path = str(profile.get("path", ""))
        label = str(profile.get("label") or Path(path).name or path)
        wallet_summary = " · ".join(f"{key} {wallet.get(key, '—')}" for key in ("gold", "bust", "portrait", "deed", "crest"))
        self.save_profile_detail.configure(text=f"{label}  |  {path}\n{hero_summary}  |  {wallet_summary}")
        self._refresh_sl_checkpoints(quiet=True)
        self._sync_autosave_profiles()
        self._update_save_controls()

    def _set_sl_status(self, message: str, level: str = "normal", *, log: bool = True) -> None:
        color = {"success": COLORS["green"], "warning": "#f1b45f", "error": COLORS["red"]}.get(level, COLORS["muted"])
        self.sl_status_label.configure(text=message, foreground=color)
        if log:
            self._log(message, level=level)

    @staticmethod
    def _format_sl_datetime(value: Any) -> str:
        if isinstance(value, (int, float)):
            try:
                parsed = _datetime.datetime.fromtimestamp(value).astimezone()
                return parsed.strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return str(value)
        raw = str(value or "").strip()
        if not raw:
            return "日期未知"
        try:
            parsed = _datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone()
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return raw

    @staticmethod
    def _format_sl_bytes(value: Any) -> str:
        try:
            size = max(0, int(value))
        except (TypeError, ValueError):
            return "大小未知"
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MiB"
        if size >= 1024:
            return f"{size / 1024:.1f} KiB"
        return f"{size} B"

    def _sl_checkpoint_display(self, checkpoint: dict[str, Any]) -> str:
        created = self._format_sl_datetime(checkpoint.get("created_at"))
        label = str(checkpoint.get("label") or "未命名回档点")
        try:
            file_count = int(checkpoint.get("file_count", 0))
        except (TypeError, ValueError):
            file_count = 0
        raw_kind = checkpoint.get("kind")
        kind = {
            "before_restore": "自动保留",
            "automatic": "自动回档点",
            "manual": "手动回档点",
        }.get(raw_kind, "回档点")
        return f"{created} · {label} · {file_count} 个文件 · {kind} · {self._checkpoint_week_text(checkpoint)}"

    def _show_sl_checkpoint_detail(self, checkpoint: dict[str, Any] | None) -> None:
        if checkpoint is None:
            count = len(self._sl_checkpoints)
            text = f"已找到 {count} 个回档点，请选择一个查看详情。" if count else "当前存档没有回档点。"
            self.sl_checkpoint_detail.configure(text=text)
            return
        profile = self._selected_save_profile() or {}
        profile_label = str(profile.get("label") or Path(str(profile.get("path", ""))).name or "当前存档")
        raw_kind = checkpoint.get("kind")
        kind = {
            "before_restore": "自动保留点（恢复前）",
            "automatic": "自动回档点",
            "manual": "手动回档点",
        }.get(raw_kind, "回档点")
        try:
            file_count = int(checkpoint.get("file_count", 0))
        except (TypeError, ValueError):
            file_count = 0
        text = (
            f"日期：{self._format_sl_datetime(checkpoint.get('created_at'))}  |  名称：{checkpoint.get('label') or '未命名回档点'}\n"
            f"整槽文件数：{file_count}  |  总大小：{self._format_sl_bytes(checkpoint.get('bytes'))}  |  类型：{kind}  |  游戏周：{self._checkpoint_week_text(checkpoint)}  |  存档槽：{profile_label}"
        )
        self.sl_checkpoint_detail.configure(text=text)

    def _refresh_sl_checkpoints(self, preserve_id: Any = None, *, quiet: bool = False) -> bool:
        profile = self._selected_save_profile()
        self._sl_checkpoints = []
        self._sl_checkpoint_labels = []
        self.sl_checkpoint_combo.configure(values=())
        self.sl_checkpoint_combo.set("")
        if profile is None:
            self.sl_checkpoint_detail.configure(text="请先扫描并选择一个存档，再刷新回档点。")
            self._update_save_controls()
            if not quiet:
                self._set_sl_status("请先扫描并选择一个存档。", "warning", log=False)
            return False
        if self._sl_module is None or not self._sl_list_ready:
            self.sl_checkpoint_detail.configure(text="当前无法读取回档点列表。")
            self._update_save_controls()
            self._set_sl_status(self._sl_backend_message, "warning", log=not quiet)
            return False
        profile_path = str(profile.get("path", ""))
        if not profile_path:
            self.sl_checkpoint_detail.configure(text="所选存档缺少路径。")
            self._update_save_controls()
            self._set_sl_status("所选存档缺少路径，无法查询回档点。", "error", log=not quiet)
            return False
        try:
            result = self._sl_module.list_checkpoints(profile_path)
            if not isinstance(result, (list, tuple)):
                raise TypeError("list_checkpoints() 必须返回回档点字典列表")
            wrong_profile = 0
            records: list[dict[str, Any]] = []
            for checkpoint in result:
                if not isinstance(checkpoint, dict) or checkpoint.get("id") is None:
                    continue
                checkpoint_profile = checkpoint.get("profile")
                if not self._same_profile_path(checkpoint_profile, profile_path):
                    wrong_profile += 1
                    continue
                records.append(dict(checkpoint))
            self._sl_checkpoints = records
            labels = [self._sl_checkpoint_display(item) for item in records]
            counts: dict[str, int] = {}
            unique_labels: list[str] = []
            for label, checkpoint in zip(labels, records):
                counts[label] = counts.get(label, 0) + 1
                unique_labels.append(label if counts[label] == 1 else f"{label} · {checkpoint['id']}")
            self._sl_checkpoint_labels = unique_labels
            self.sl_checkpoint_combo.configure(values=tuple(unique_labels))
            preserved_index = next((index for index, item in enumerate(records) if preserve_id is not None and str(item.get("id")) == str(preserve_id)), -1)
            if preserved_index >= 0:
                self.sl_checkpoint_combo.current(preserved_index)
            self._show_sl_checkpoint_detail(self._selected_sl_checkpoint())
            self._update_save_controls()
            if wrong_profile:
                self._set_sl_status(f"已忽略 {wrong_profile} 个不属于当前存档槽的回档点。", "warning", log=not quiet)
            elif not quiet:
                self._set_sl_status(f"已刷新当前存档槽的 {len(records)} 个回档点。", "success", log=True)
            return True
        except Exception as exc:
            self._record_error("刷新 SL 回档点失败", exc)
            self.sl_checkpoint_detail.configure(text="回档点列表读取失败。")
            self._update_save_controls()
            self._set_sl_status(f"刷新回档点失败：{type(exc).__name__}: {exc}", "error", log=False)
            return False

    def _refresh_current_sl_checkpoints(self) -> None:
        checkpoint = self._selected_sl_checkpoint()
        preserve_id = checkpoint.get("id") if checkpoint is not None else None
        self._refresh_sl_checkpoints(preserve_id=preserve_id)

    def _on_sl_checkpoint_selected(self, _event: Any = None) -> None:
        checkpoint = self._selected_sl_checkpoint()
        self._show_sl_checkpoint_detail(checkpoint)
        self._update_save_controls()

    def _sl_game_is_closed(self, action: str) -> bool:
        if self.game_running is False:
            return True
        if self.game_running is True:
            self._set_sl_status(f"请先完全退出游戏，再{action}。当前操作已禁用。", "warning", log=False)
        else:
            self._set_sl_status(f"无法确认游戏已完全退出，暂不能{action}。请等待运行状态检测完成。", "warning", log=False)
        return False

    def _save_selected_checkpoint(self) -> None:
        profile = self._selected_save_profile()
        if profile is None:
            self._set_sl_status("请先扫描并选择一个存档。", "warning")
            return
        if self._sl_module is None or not self._sl_create_ready or not self._sl_list_ready:
            self._set_sl_status(self._sl_backend_message, "warning")
            return
        if not self._sl_game_is_closed("保存回档点"):
            return
        profile_path = str(profile.get("path", ""))
        label = self.sl_checkpoint_name_var.get().strip()
        try:
            result = self._sl_module.create_checkpoint(profile_path, label=label)
            if not isinstance(result, dict) or result.get("id") is None:
                raise RuntimeError(str(result.get("message") if isinstance(result, dict) else "后端未返回回档点 ID"))
            if result.get("success") is False or result.get("error"):
                raise RuntimeError(str(result.get("error") or result.get("message") or "后端报告保存失败"))
            if not self._same_profile_path(result.get("profile"), profile_path):
                raise RuntimeError("后端返回的回档点不属于当前存档槽，已拒绝在界面中选用。")
            checkpoint_id = result["id"]
            listed = self._refresh_sl_checkpoints(preserve_id=checkpoint_id, quiet=True)
            saved_label = str(result.get("label") or label or "手动回档点")
            try:
                file_count = int(result.get("file_count", 0))
            except (TypeError, ValueError):
                file_count = 0
            suffix = "" if listed else "；回档点列表刷新失败，请稍后点击刷新回档点"
            self._set_sl_status(f"已保存回档点“{saved_label}”，包含整槽 {file_count} 个文件。{suffix}", "success" if listed else "warning")
        except Exception as exc:
            self._record_error("保存 SL 回档点失败", exc)
            self._set_sl_status(f"保存回档点失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _restore_selected_checkpoint(self) -> None:
        profile = self._selected_save_profile()
        checkpoint = self._selected_sl_checkpoint()
        if profile is None or checkpoint is None:
            self._set_sl_status("请先选择存档和回档点。", "warning")
            return
        if not self._same_profile_path(checkpoint.get("profile"), profile.get("path")):
            self._set_sl_status("所选回档点不属于当前存档槽；请刷新列表后重试。", "error")
            return
        if self._sl_module is None or not self._sl_restore_ready or not self._sl_list_ready:
            self._set_sl_status(self._sl_backend_message, "warning")
            return
        if not self._sl_game_is_closed("恢复回档点"):
            return
        try:
            result = self._sl_module.restore_checkpoint(checkpoint["id"])
            if not isinstance(result, dict) or result.get("success") is not True:
                message = result.get("message") if isinstance(result, dict) else None
                raise RuntimeError(str(message or "后端未确认回档成功"))
            safety_id = result.get("safety_checkpoint_id")
            restored_id = result.get("checkpoint_id", checkpoint["id"])
            refreshed = self._refresh_sl_checkpoints(preserve_id=restored_id, quiet=True)
            if safety_id:
                safety_found = refreshed and any(str(item.get("id")) == str(safety_id) and item.get("kind") == "before_restore" for item in self._sl_checkpoints)
                if safety_found:
                    self._set_sl_status(
                        f"{result.get('message') or '回档完成。'} 回档前进度已保存为“{next((item.get('label') for item in self._sl_checkpoints if str(item.get('id')) == str(safety_id)), '自动安全点')}”；如需撤销，请从列表选择该自动保留点。",
                        "success",
                    )
                else:
                    self._set_sl_status(
                        f"{result.get('message') or '回档完成。'} 后端返回了自动安全点 ID {safety_id}，但当前列表未找到它；请刷新列表确认后再操作。",
                        "warning",
                    )
            else:
                self._set_sl_status(str(result.get("message") or "回档完成。恢复前安全点未返回，请检查回档点列表。"), "warning")
        except Exception as exc:
            self._record_error("恢复 SL 回档点失败", exc)
            self._set_sl_status(f"恢复回档点失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _save_response(self, result: Any, action: str) -> tuple[bool, str]:
        if result is False:
            return False, f"{action}失败：后端返回失败。"
        if result is None:
            return False, f"{action}已调用，但后端没有返回确认结果。"
        if isinstance(result, dict):
            if result.get("ok") is False or result.get("success") is False or result.get("error"):
                return False, f"{action}失败：{result.get('error') or result.get('message') or '后端报告失败'}"
            message = str(result.get("message") or result.get("detail") or "")
            if result.get("ok") is True or result.get("success") is True:
                return True, message or f"{action}完成。"
            if any(key in result for key in ("backup_id", "backupid", "restored", "written", "changes", "changed")):
                return True, message or f"{action}完成。"
            return False, f"{action}已调用，但后端未提供成功标记。"
        if result is True:
            return True, f"{action}完成。"
        return False, f"{action}已调用，返回值无法确认结果。"

    def _register_backup(self, result: Any, profile: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            return
        backup = result.get("backup") if isinstance(result.get("backup"), dict) else result
        backup_id = backup.get("backup_id", backup.get("backupid", backup.get("id")))
        if backup_id is None:
            return
        if any(str(item.get("backup_id")) == str(backup_id) for item in self._backup_records):
            return
        profile_label = str(profile.get("label") or Path(str(profile.get("path", ""))).name)
        suffix = backup.get("label") or backup.get("created_at") or backup.get("timestamp") or backup_id
        display = f"{profile_label} · {suffix}"
        record = {"backup_id": backup_id, "label": display, "path": profile.get("path")}
        self._backup_records.append(record)
        values = tuple(item["label"] for item in self._backup_records)
        self.backup_combo.configure(values=values)
        self.backup_combo.current(len(values) - 1)
        self.backup_detail.configure(text=f"备份 ID：{backup_id}\n{display}")
        self._update_save_controls()

    def _edit_selected_save(self) -> None:
        if self._save_module is None or not self._save_edit_ready:
            self._set_save_status("存档编辑接口尚未就绪。", "warning")
            return
        if self.game_running is not False:
            self._set_save_status("请先完全退出游戏；当前无法确认游戏已关闭，存档写入已禁用。", "warning")
            return
        index = self.save_profile_combo.current()
        if index < 0 or index >= len(self._save_profiles):
            self._set_save_status("请先扫描并选择一个存档。", "warning")
            return
        profile = self._save_profiles[index]
        try:
            resources = {key: int(self._save_resource_vars[key].get().strip()) for key in ("gold", "bust", "portrait", "deed", "crest")}
            if any(value < 0 for value in resources.values()):
                raise ValueError("资源值不能小于零")
            result = self._save_module.edit_save(profile["path"], resources, clear_stress=bool(self.save_clear_stress_var.get()))
            ok, message = self._save_response(result, "存档写入")
            self._set_save_status(message, "success" if ok else "warning" if "未" in message or "没有返回" in message else "error")
            if ok:
                self._register_backup(result, profile)
                self._scan_save_profiles(preserve_path=profile["path"], quiet=True)
        except Exception as exc:
            self._record_error("写入存档失败", exc)
            self._set_save_status(f"写入存档失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _restore_selected_backup(self) -> None:
        if self._save_module is None or not self._save_restore_ready:
            self._set_save_status("备份恢复接口尚未就绪。", "warning")
            return
        if self.game_running is not False:
            self._set_save_status("请先完全退出游戏；当前无法确认游戏已关闭，备份恢复已禁用。", "warning")
            return
        index = self.backup_combo.current()
        if index < 0 or index >= len(self._backup_records):
            self._set_save_status("请先选择一个备份。", "warning")
            return
        record = self._backup_records[index]
        try:
            result = self._save_module.restore_backup(record["backup_id"])
            ok, message = self._save_response(result, "备份恢复")
            self._set_save_status(message, "success" if ok else "warning" if "未" in message or "没有返回" in message else "error")
            if ok:
                self._scan_save_profiles(preserve_path=record.get("path"), quiet=True)
        except Exception as exc:
            self._record_error("恢复存档备份失败", exc)
            self._set_save_status(f"恢复存档备份失败：{type(exc).__name__}: {exc}", "error", log=False)

    def _log(self, message: str, level: str = "normal") -> None:
        stamp = _datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-150:]
        if hasattr(self, "log_text"):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n", level if level in {"warning", "error", "success"} else "normal")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _record_error(self, context: str, exc: BaseException) -> None:
        detail = f"{context}：{type(exc).__name__}: {exc}"
        now = time.monotonic()
        if now - self._last_errors.get(detail, 0.0) > 10.0:
            self._log(detail, level="error")
            self._last_errors[detail] = now

    def _poll_status(self) -> None:
        self._status_after = None
        if self.closing:
            return
        self._refresh_status()
        self._status_after = self.root.after(POLL_MS, self._poll_status)

    def _refresh_status(self) -> None:
        if self.engine is None:
            self.engine_status = {}
            self.capabilities = {}
            self.connected = False
            self.game_running = None
            self._render_status()
            return
        try:
            status = self.engine.status()
            if not isinstance(status, dict):
                raise TypeError("Engine.status() 必须返回字典")
            self.engine_status = status
            self.capabilities = _normalize_capabilities(status.get("capabilities", {}))
            self.connected = _read_flag(status, "connected", "attached", "is_attached")
            running_names = ("game_running", "running", "process_running", "game_detected")
            if any(isinstance(status.get(name), (bool, int, float)) for name in running_names):
                self.game_running = _read_flag(status, *running_names)
            else:
                self.game_running = None
            self._engine_error = None
        except Exception as exc:
            self._record_error("读取引擎状态失败", exc)
            self.engine_status = {}
            self.capabilities = {}
            self.connected = False
            self.game_running = None
        self._render_status()

    def _render_status(self) -> None:
        if self.engine is None:
            if self.smoke:
                self.connection_label.configure(text="● 自检模式", foreground=COLORS["accent"])
                self.status_label.configure(text="烟雾测试模式：未加载或连接游戏后端", foreground=COLORS["muted"])
            else:
                self.connection_label.configure(text="● 引擎不可用", foreground=COLORS["red"])
                self.status_label.configure(text=self._engine_error or "等待修改引擎", foreground=COLORS["red"])
            self.attach_button.configure(state="disabled")
        elif self.connected:
            self._last_attach_error = None
            self.connection_label.configure(text="● 已连接", foreground=COLORS["green"])
            self.status_label.configure(text=self._describe_game_status("已连接到游戏进程"), foreground=COLORS["green"])
            self.attach_button.configure(text="已连接", state="disabled")
        elif failure := self._connection_failure_text():
            self.connection_label.configure(text="● 连接失败", foreground=COLORS["red"])
            self.status_label.configure(text=f"连接失败：{failure}", foreground=COLORS["red"])
            retry_state = "normal" if self.game_running is not False else "disabled"
            self.attach_button.configure(text="重试连接", state=retry_state)
        elif self.game_running is True:
            self.connection_label.configure(text="● 游戏已运行", foreground=COLORS["accent"])
            self.status_label.configure(text=self._describe_game_status("已检测到游戏，点击“连接游戏”后使用受支持的功能"), foreground=COLORS["accent"])
            self.attach_button.configure(text="连接游戏", state="normal")
        elif self.game_running is False:
            self.connection_label.configure(text="● 游戏未运行", foreground=COLORS["muted"])
            self.status_label.configure(text=self._describe_game_status("未检测到目标游戏进程"), foreground=COLORS["muted"])
            self.attach_button.configure(text="连接游戏", state="disabled")
        else:
            self.connection_label.configure(text="● 状态未知", foreground=COLORS["muted"])
            self.status_label.configure(text=self._describe_game_status("引擎未报告游戏运行状态"), foreground=COLORS["muted"])
            self.attach_button.configure(text="连接游戏", state="normal")

        self.version_label.configure(text=self._version_text())
        for key, widgets in self._option_controls.items():
            supported = self.capabilities.get(key, False)
            state = "normal" if (self.connected and supported) else "disabled"
            widgets["check"].configure(state=state)
            if widgets["spin"] is not None:
                widgets["spin"].configure(state=state)
            widgets["label"].configure(foreground=COLORS["text"] if self.connected and supported else COLORS["dim"])
        self.close_all_button.configure(state="normal" if self.connected else "disabled")
        self._sync_hotkey_context()
        if hasattr(self, "sl_game_state_label"):
            self._update_save_controls()

    def _sync_hotkey_context(self) -> None:
        raw_pid = self.engine_status.get("pid")
        pid: int | None = None
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid
        elif isinstance(raw_pid, str):
            try:
                parsed = int(raw_pid.strip(), 0)
                if parsed > 0:
                    pid = parsed
            except ValueError:
                pass
        self.hotkey_manager.set_context(self.connected, pid, self.capabilities)

    def _poll_hotkey_events(self) -> None:
        self._hotkey_after = None
        if self.closing:
            return
        while True:
            try:
                event = self.hotkey_manager.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "state":
                state = event.get("state")
                color = COLORS["green"] if state == "available" else COLORS["red"] if state in {"conflict", "error"} else COLORS["accent"] if state == "paused" else COLORS["dim"]
                self.hotkey_status_label.configure(text=f"热键状态：{event.get('message', '状态未知')}", foreground=color)
            elif kind == "log":
                self._log(str(event.get("message", "全局热键状态变化")), level=str(event.get("level", "normal")))
            elif kind == "pause":
                self._log("全局热键已暂停。" if event.get("paused") else "全局热键已恢复。", level="warning" if event.get("paused") else "success")
            elif kind == "action":
                self._toggle_option_from_hotkey(str(event.get("key", "")), str(event.get("label", "快捷键")))
        self._hotkey_after = self.root.after(80, self._poll_hotkey_events)

    def _foreground_pid_matches(self) -> bool:
        if os.name != "nt" or not self.connected:
            return False
        raw_pid = self.engine_status.get("pid")
        try:
            expected_pid = int(raw_pid.strip(), 0) if isinstance(raw_pid, str) else int(raw_pid)
        except (TypeError, ValueError):
            return False
        if expected_pid <= 0:
            return False
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            foreground_pid = ctypes.c_uint32(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(foreground_pid))
            return int(foreground_pid.value) == expected_pid
        except Exception:
            return False

    def _toggle_option_from_hotkey(self, key: str, label: str) -> None:
        if key not in self._option_controls or not self.connected or not self.capabilities.get(key, False):
            self._log(f"快捷键 {label} 未执行：该功能当前未连接或后端未报告支持。", level="warning")
            return
        if not self._foreground_pid_matches():
            return
        control = self._option_controls[key]
        control["check_var"].set(not bool(control["check_var"].get()))
        self._on_option_toggle(key)

    def _describe_game_status(self, default: str) -> str:
        return str(self.engine_status.get("message") or self.engine_status.get("status_text") or default)

    def _connection_attempt_architecture(self) -> str | None:
        attempt = self.engine_status.get("connection_attempt")
        if not isinstance(attempt, dict):
            return None
        raw = attempt.get("architecture")
        if raw is None or str(raw).strip() == "":
            return None
        value = str(raw).strip()
        normalized = value.lower().replace(" ", "")
        if normalized in {"x86_64", "amd64", "x64", "64", "64bit", "64-bit"} or "x86_64" in normalized:
            return "x64"
        if "arm64" in normalized or "aarch64" in normalized:
            return "ARM64"
        if normalized in {"x86", "i386", "i686", "32", "32bit", "32-bit"} or normalized.startswith("x86"):
            return "x86"
        if "64" in normalized:
            return "x64"
        if "32" in normalized:
            return "x86"
        return value

    def _connection_failure_text(self) -> str | None:
        if self.connected:
            return None
        status = self.engine_status
        attempt = status.get("connection_attempt")
        if not isinstance(attempt, dict):
            attempt = None
        status_message = str(status.get("message") or status.get("status_text") or "").strip()
        attempt_message = str(attempt.get("message") or "").strip() if attempt is not None else ""
        for raw in (status.get("last_error"), self._last_attach_error):
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        if attempt is not None:
            for field in ("last_error", "error", "reason", "failure"):
                raw = attempt.get(field)
                if raw is not None and str(raw).strip():
                    return str(raw).strip()

        failed = False
        if attempt is not None:
            for field in ("ok", "success", "validated", "validation_passed", "compatible"):
                if field in attempt and attempt[field] is False:
                    failed = True
                    break
            if status.get("tested") is False:
                failed = True
        architecture = self._connection_attempt_architecture()
        if architecture == "x86":
            failed = True
        failure_words = ("失败", "错误", "未通过", "不匹配", "不支持", "failed", "error", "mismatch", "unsupported", "rejected")
        for candidate in (attempt_message, status_message):
            if any(word in candidate.lower() for word in failure_words):
                return candidate
        if failed:
            if architecture == "x86":
                return "目标要求 64 位，但探测到 x86 架构；版本校验未通过"
            return attempt_message or "目标版本或指令校验未通过"
        return None

    def _version_text(self) -> str:
        version = self.engine_status.get("game_version", self.engine_status.get("version"))
        tested = self.engine_status.get("tested")
        failure = self._connection_failure_text()
        architecture = self._connection_attempt_architecture()
        architecture_text = f" · 实际架构：{architecture}" if architecture else ""
        validated = (tested is True or (self.connected and tested is not False)) and failure is None
        if validated:
            display_version = str(version) if version else TARGET_VERSION
            version_state = "版本校验：通过" if tested is True else "连接验证：通过"
            identity = f"已校验目标版本：{display_version}"
        else:
            identity = f"目标版本：{TARGET_VERSION}"
            version_state = "版本校验：未通过" if failure else "版本校验：待验证"
        return f"{identity}{architecture_text} · {version_state} · 完整游戏流程：待实测"

    def _attach(self) -> None:
        if self.engine is None:
            self._log("修改引擎不可用，无法连接。", level="warning")
            return
        self.attach_button.configure(state="disabled")
        try:
            result = self.engine.attach()
            if isinstance(result, dict):
                if result.get("ok") is False or result.get("success") is False:
                    detail = str(result.get("error") or result.get("message") or "连接失败")
                    self._last_attach_error = detail
                    self._log(detail, level="error")
                else:
                    self._last_attach_error = None
                    self._log(str(result.get("message") or "已请求连接游戏。"), level="success")
            elif result is False:
                self._last_attach_error = "连接失败"
                self._log("连接失败。", level="error")
            else:
                self._last_attach_error = None
                self._log("已请求连接游戏。", level="success")
        except Exception as exc:
            self._last_attach_error = str(exc)
            self._record_error("连接游戏失败", exc)
        self._refresh_status()

    def _parse_value(self, key: str) -> int | float:
        raw = self._option_controls[key]["value_var"].get().strip()
        meta = OPTION_BY_KEY[key]
        value = float(raw) if meta["kind"] == "float" else int(raw)
        if value < meta["from"] or value > meta["to"]:
            raise ValueError(f"{meta['label']}须在 {meta['from']} 至 {meta['to']} 之间")
        if meta["kind"] == "int":
            return int(value)
        return round(float(value), 3)

    def _on_option_toggle(self, key: str) -> None:
        if self._updating_controls:
            return
        control = self._option_controls[key]
        enabled = bool(control["check_var"].get())
        try:
            value: int | float | bool | None
            parsed = self._parse_value(key) if enabled and OPTION_BY_KEY[key]["kind"] != "toggle" else None
            value = _option_value_for_toggle(key, enabled, parsed)
            result = self.engine.set_option(key, value)  # type: ignore[union-attr]
            if result is False:
                raise RuntimeError("后端返回设置失败")
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise RuntimeError(str(result.get("error") or result.get("message") or "后端拒绝该设置"))
            self._log(f"{OPTION_BY_KEY[key]['label']}：{'已启用' if enabled else '已关闭'}", level="success")
        except Exception as exc:
            self._updating_controls = True
            control["check_var"].set(not enabled)
            self._updating_controls = False
            self._record_error(f"设置 {key} 失败", exc)

    def _on_value_change(self, key: str) -> None:
        if self._updating_controls or not self.connected:
            return
        control = self._option_controls[key]
        if not control["check_var"].get():
            return
        try:
            value = self._parse_value(key)
            result = self.engine.set_option(key, value)  # type: ignore[union-attr]
            if result is False:
                raise RuntimeError("后端返回设置失败")
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise RuntimeError(str(result.get("error") or result.get("message") or "后端拒绝该设置"))
            self._log(f"{OPTION_BY_KEY[key]['label']}数值已更新为 {value}")
        except (ValueError, tk.TclError):
            # A user may still be typing in the Spinbox; validate on Return/focus-out.
            return
        except Exception as exc:
            self._record_error(f"更新 {key} 数值失败", exc)

    def _close_all(self) -> None:
        if self.engine is None:
            return
        for key, control in self._option_controls.items():
            if control["check_var"].get() and self.capabilities.get(key, False):
                try:
                    result = self.engine.set_option(key, None)
                    if result is False:
                        raise RuntimeError("后端返回关闭失败")
                    if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                        raise RuntimeError(str(result.get("error") or result.get("message") or "后端拒绝关闭"))
                except Exception as exc:
                    self._record_error(f"关闭 {key} 失败", exc)
            control["check_var"].set(False)
        try:
            result = self.engine.detach()
            if result is False:
                raise RuntimeError("后端返回断开失败")
            if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                raise RuntimeError(str(result.get("error") or result.get("message") or "后端拒绝断开"))
            self._log("已关闭所有修改并请求断开连接。", level="success")
        except Exception as exc:
            self._record_error("关闭所有修改时断开引擎失败", exc)
        self._refresh_status()

    def _diagnostics(self) -> dict[str, Any]:
        return {
            "application": {"name": APP_TITLE, "version": APP_VERSION, "target_version": TARGET_VERSION, "target_steam_id": "25463639"},
            "timestamp": _datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "host": {"platform": platform.platform(), "python": sys.version.split()[0], "tk": tk.TkVersion},
            "game": {
                "running": self.game_running,
                "connected": self.connected,
                "engine_status": _safe_json(self.engine_status),
                "capabilities": _safe_json(self.capabilities),
            },
            "controls": [
                {"key": meta["key"], "supported": self.capabilities.get(meta["key"], False), "enabled": bool(self._option_controls[meta["key"]]["check_var"].get()), "value": self._option_controls[meta["key"]]["value_var"].get() if meta["kind"] != "toggle" else None}
                for meta in OPTIONS
            ],
            "hotkeys": {
                "implemented": True,
                "status": self.hotkey_manager.snapshot(),
                "bindings": [
                    {"key": label, "option": key, "virtual_key": vk, "modifiers": modifiers | HOTKEY_MOD_NOREPEAT}
                    for label, key, vk, modifiers in HOTKEY_OPTION_BINDINGS
                ] + [{"key": HOTKEY_PAUSE_BINDING[0], "action": "pause", "virtual_key": HOTKEY_PAUSE_BINDING[1], "modifiers": HOTKEY_PAUSE_BINDING[2] | HOTKEY_MOD_NOREPEAT}],
            },
            "sl_rollback": {
                "list_available": self._sl_list_ready,
                "create_available": self._sl_create_ready,
                "restore_available": self._sl_restore_ready,
                "select_available": self._sl_select_ready,
                "checkpoint_count": len(self._sl_checkpoints),
                "game_confirmed_closed": self.game_running is False,
                "status": self.sl_status_label.cget("text"),
            },
            "automatic_checkpoints": {
                "available": self._autosave_ready,
                "settings": _safe_json(self._autosave_status_snapshot()),
                "status": self.autosave_status_label.cget("text"),
            },
            "save_backend": self._save_backend_message,
            "tested": self.engine_status.get("tested"),
            "recent_log": list(self._log_lines),
        }

    def _export_diagnostics(self) -> None:
        timestamp = _datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output = Path(__file__).resolve().parent / f"trainer_diagnostics_{timestamp}.json"
        try:
            output.write_text(json.dumps(self._diagnostics(), ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"诊断文件已导出：{output}", level="success")
            self.status_label.configure(text=f"诊断文件已导出：{output.name}")
        except Exception as exc:
            self._record_error("导出诊断失败", exc)

    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self._status_after is not None:
            try:
                self.root.after_cancel(self._status_after)
            except tk.TclError:
                pass
            self._status_after = None
        if self._hotkey_after is not None:
            try:
                self.root.after_cancel(self._hotkey_after)
            except tk.TclError:
                pass
            self._hotkey_after = None
        if self._autosave_after is not None:
            try:
                self.root.after_cancel(self._autosave_after)
            except tk.TclError:
                pass
            self._autosave_after = None
        try:
            self._stop_autosave_controller()
        except Exception as exc:
            self._record_error("窗口关闭时停止自动回档失败", exc)
        if self.engine is not None:
            try:
                result = self.engine.detach()
                if result is False:
                    raise RuntimeError("后端返回断开失败")
                if isinstance(result, dict) and (result.get("ok") is False or result.get("success") is False):
                    raise RuntimeError(str(result.get("error") or result.get("message") or "后端拒绝断开"))
                self._log("窗口关闭时已请求断开并恢复引擎钩子。", level="success")
            except Exception as exc:
                self._record_error("窗口关闭时恢复引擎失败", exc)
        self.hotkey_manager.stop()
        self.root.destroy()

    def snapshot_controls(self) -> dict[str, Any]:
        self.root.update_idletasks()
        controls = []
        for meta in OPTIONS:
            item = self._option_controls[meta["key"]]
            controls.append({
                "key": meta["key"], "label": meta["label"],
                "checkbox": item["check"].winfo_class(),
                "spinbox": item["spin"].winfo_class() if item["spin"] is not None else None,
                "enabled": bool(item["check_var"].get()),
                "supported": self.capabilities.get(meta["key"], False),
                "widget_state": str(item["check"].cget("state")),
                "default": meta.get("default"),
            })
        return {
            "window": {"title": self.root.title(), "geometry": self.root.geometry(), "width": self.root.winfo_width(), "height": self.root.winfo_height()},
            "tabs": [self.notebook.tab(tab, "text") for tab in self.notebook.tabs()],
            "controls": controls,
            "hotkey_status": self.hotkey_manager.snapshot(),
            "hotkeys": [
                {"key": label, "action": OPTION_BY_KEY[key]["label"], "option": key, "enabled": label in self.hotkey_manager.snapshot().get("registered", [])}
                for label, key, _vk, _modifiers in HOTKEY_OPTION_BINDINGS
            ] + [{"key": HOTKEY_PAUSE_BINDING[0], "action": "暂停/恢复热键", "enabled": self.hotkey_manager.snapshot().get("pause_registered", False)}],
            "engine_available": self.engine is not None,
            "save_page": {
                "scan_available": getattr(self, "_save_scan_ready", False),
                "edit_available": getattr(self, "_save_edit_ready", False),
                "restore_available": getattr(self, "_save_restore_ready", False),
                "profile_count": len(self._save_profiles),
                "resource_fields": list(self._save_resource_vars),
                "backup_count": len(self._backup_records),
                "status": self._save_backend_message,
            },
            "sl_rollback": {
                "list_available": self._sl_list_ready,
                "create_available": self._sl_create_ready,
                "restore_available": self._sl_restore_ready,
                "select_available": self._sl_select_ready,
                "checkpoint_count": len(self._sl_checkpoints),
                "game_confirmed_closed": self.game_running is False,
                "status": self.sl_status_label.cget("text"),
                "selected_checkpoint_id": (self._selected_sl_checkpoint() or {}).get("id"),
            },
            "automatic_checkpoints": {
                "available": self._autosave_ready,
                "settings": _safe_json(self._autosave_status_snapshot()),
                "status": self.autosave_status_label.cget("text"),
            },
        }


def _run_sl_mock_smoke(app: TrainerApp) -> dict[str, Any]:
    """Exercise SL UI callbacks using in-memory profiles and checkpoints only."""
    class MockSLRuntime:
        def __init__(self, profile_paths: tuple[str, ...]) -> None:
            self.checkpoints: dict[str, list[dict[str, Any]]] = {path: [] for path in profile_paths}
            self.calls: list[tuple[str, Any]] = []
            for index, path in enumerate(profile_paths, start=1):
                self.checkpoints[path].append({
                    "id": f"mock-existing-{index}", "label": f"模拟旧回档点 {index}",
                    "profile": path, "created_at": "2026-10-13T10:00:00+08:00",
                    "file_count": 4 + index, "bytes": 12000 * index, "kind": "manual",
                })

        def list_checkpoints(self, profile_path: str) -> list[dict[str, Any]]:
            self.calls.append(("list", profile_path))
            return [dict(record) for record in self.checkpoints.get(profile_path, [])]

        def create_checkpoint(self, profile_path: str, label: str = "") -> dict[str, Any]:
            self.calls.append(("create", profile_path))
            checkpoint = {
                "id": f"mock-created-{len(self.calls)}", "label": label or "未命名模拟回档点",
                "profile": profile_path, "created_at": "2026-10-13T11:00:00+08:00",
                "file_count": 7, "bytes": 65432, "kind": "manual",
            }
            self.checkpoints[profile_path].insert(0, checkpoint)
            return dict(checkpoint)

        def restore_checkpoint(self, checkpoint_id: Any) -> dict[str, Any]:
            self.calls.append(("restore", checkpoint_id))
            checkpoint = next((item for records in self.checkpoints.values() for item in records if str(item["id"]) == str(checkpoint_id)), None)
            if checkpoint is None:
                return {"success": False, "message": "mock checkpoint not found"}
            safety = {
                "id": "mock-before-restore", "label": "模拟恢复前安全点",
                "profile": checkpoint["profile"], "created_at": "2026-10-13T11:30:00+08:00",
                "file_count": 7, "bytes": 65432, "kind": "before_restore",
            }
            self.checkpoints[checkpoint["profile"]].insert(0, safety)
            return {"success": True, "checkpoint_id": checkpoint_id, "safety_checkpoint_id": safety["id"], "message": "模拟回档完成。"}

    profile_paths = ("MOCK_SLOT_A", "MOCK_SLOT_B")
    profiles = [
        {"path": profile_paths[0], "label": "模拟存档槽 A", "wallet": {"gold": 10}, "heroes": []},
        {"path": profile_paths[1], "label": "模拟存档槽 B", "wallet": {"gold": 20}, "heroes": []},
    ]
    backend = MockSLRuntime(profile_paths)
    saved = {
        "save_profiles": app._save_profiles,
        "profile_values": app.save_profile_combo.cget("values"),
        "profile_text": app.save_profile_combo.get(),
        "profile_detail": app.save_profile_detail.cget("text"),
        "resource_values": {key: variable.get() for key, variable in app._save_resource_vars.items()},
        "sl_module": app._sl_module,
        "sl_flags": (app._sl_list_ready, app._sl_create_ready, app._sl_restore_ready, app._sl_select_ready),
        "sl_checkpoints": [dict(item) for item in app._sl_checkpoints],
        "sl_checkpoint_labels": list(app._sl_checkpoint_labels),
        "sl_combo_values": app.sl_checkpoint_combo.cget("values"),
        "sl_combo_text": app.sl_checkpoint_combo.get(),
        "sl_name": app.sl_checkpoint_name_var.get(),
        "sl_status": app.sl_status_label.cget("text"),
        "sl_status_foreground": app.sl_status_label.cget("foreground"),
        "sl_detail": app.sl_checkpoint_detail.cget("text"),
        "game_running": app.game_running,
    }
    try:
        app._sl_module = backend
        app._sl_list_ready = app._sl_create_ready = app._sl_restore_ready = True
        app._save_profiles = profiles
        app.game_running = False
        app.save_profile_combo.configure(values=tuple(profile["label"] for profile in profiles), state="readonly")
        app.save_profile_combo.current(0)
        app._on_save_profile_selected()
        if any(record.get("profile") != profile_paths[0] for record in app._sl_checkpoints):
            raise AssertionError("选择 A 槽后混入了其他槽的回档点")

        app.sl_checkpoint_name_var.set("模拟首次进度")
        app._save_selected_checkpoint()
        created = next((item for item in app._sl_checkpoints if item.get("label") == "模拟首次进度"), None)
        if created is None or not any(action == "create" and path == profile_paths[0] for action, path in backend.calls):
            raise AssertionError("保存回档点回调未将名称和所选 A 槽传给后端")

        create_count = sum(action == "create" for action, _value in backend.calls)
        app.game_running = True
        app._update_save_controls()
        if str(app.sl_save_checkpoint_button.cget("state")) != "disabled" or str(app.sl_restore_checkpoint_button.cget("state")) != "disabled":
            raise AssertionError("游戏运行时 SL 保存/恢复按钮没有禁用")
        app._save_selected_checkpoint()
        if sum(action == "create" for action, _value in backend.calls) != create_count:
            raise AssertionError("检测到游戏运行时仍调用了保存回档点接口")
        app.game_running = False

        app.save_profile_combo.current(1)
        app._on_save_profile_selected()
        app._refresh_current_sl_checkpoints()
        if not backend.calls or backend.calls[-1] != ("list", profile_paths[1]):
            raise AssertionError("刷新回档点未使用当前 B 槽路径")
        if any(record.get("profile") != profile_paths[1] for record in app._sl_checkpoints):
            raise AssertionError("选择 B 槽后混入了 A 槽回档点")

        app.save_profile_combo.current(0)
        app._on_save_profile_selected()
        created_index = next(index for index, item in enumerate(app._sl_checkpoints) if str(item.get("id")) == str(created["id"]))
        app.sl_checkpoint_combo.current(created_index)
        app._on_sl_checkpoint_selected()
        app._restore_selected_checkpoint()
        selected = app._selected_sl_checkpoint()
        if selected is None or selected.get("id") != created["id"] or selected.get("kind") == "before_restore":
            raise AssertionError("恢复后未保持原目标回档点选中")
        if not any(item.get("id") == "mock-before-restore" and item.get("kind") == "before_restore" for item in app._sl_checkpoints):
            raise AssertionError("恢复后自动安全点未保留在回档点列表中")
        if not any(action == "restore" for action, _value in backend.calls):
            raise AssertionError("恢复回档点回调未调用模拟后端")
        return {"ok": True, "mock_only": True, "calls": len(backend.calls), "slot_isolation": True, "original_checkpoint_remains_selected": True, "safety_point_listed": True}
    finally:
        app._save_profiles = saved["save_profiles"]
        app.save_profile_combo.configure(values=saved["profile_values"])
        app.save_profile_combo.set(saved["profile_text"])
        app.save_profile_detail.configure(text=saved["profile_detail"])
        for key, value in saved["resource_values"].items():
            app._save_resource_vars[key].set(value)
        app._sl_module = saved["sl_module"]
        app._sl_list_ready, app._sl_create_ready, app._sl_restore_ready, app._sl_select_ready = saved["sl_flags"]
        app._sl_checkpoints = saved["sl_checkpoints"]
        app._sl_checkpoint_labels = saved["sl_checkpoint_labels"]
        app.game_running = saved["game_running"]
        app.sl_checkpoint_combo.configure(values=saved["sl_combo_values"])
        app.sl_checkpoint_combo.set(saved["sl_combo_text"])
        app.sl_checkpoint_name_var.set(saved["sl_name"])
        app.sl_checkpoint_detail.configure(text=saved["sl_detail"])
        app.sl_status_label.configure(text=saved["sl_status"], foreground=saved["sl_status_foreground"])
        app._update_save_controls()


def _run_autosave_mock_smoke(app: TrainerApp) -> dict[str, Any]:
    """Exercise autosave settings/events and selectors without touching disk saves."""
    class MockSLRuntime:
        def __init__(self, profile_paths: tuple[str, ...], now: _datetime.datetime) -> None:
            self.profile_paths = profile_paths
            self.calls: list[tuple[str, Any]] = []
            self.points: dict[str, list[dict[str, Any]]] = {}
            self.now = now
            fixtures = (
                ("mock-date-3", "现实三天前", 3, None, "manual"),
                ("mock-week-0", "游戏第零周", 5, 0, "manual"),
                ("mock-week-4", "游戏第四周", 1, 4, "automatic"),
            )
            for index, profile in enumerate(profile_paths):
                self.points[profile] = []
                for checkpoint_id, label, days, week, kind in fixtures:
                    self.points[profile].append({
                        "id": f"{checkpoint_id}-{index}", "label": label, "profile": profile,
                        "created_at": (now - _datetime.timedelta(days=days)).isoformat(timespec="seconds"),
                        "file_count": 5, "bytes": 8192, "kind": kind, "game_week": week,
                    })
                self.points[profile].sort(key=lambda item: item["created_at"], reverse=True)

        def list_checkpoints(self, profile_path: str) -> list[dict[str, Any]]:
            self.calls.append(("list", profile_path))
            return [dict(item) for item in self.points.get(profile_path, [])]

        def select_checkpoint(self, profile_path: str, *, days_ago: int | None = None, game_week: int | None = None, **_kwargs: Any) -> dict[str, Any] | None:
            self.calls.append(("select", profile_path, {"days_ago": days_ago, "game_week": game_week}))
            records = self.points.get(profile_path, [])
            if game_week is not None:
                return next((dict(item) for item in records if item.get("game_week") == game_week), None)
            if days_ago is None:
                return None
            cutoff = self.now - _datetime.timedelta(days=days_ago)
            eligible = []
            for item in records:
                created = _datetime.datetime.fromisoformat(item["created_at"])
                if created <= cutoff:
                    eligible.append(item)
            chosen = max(eligible, key=lambda item: item["created_at"]) if eligible else None
            return dict(chosen) if chosen is not None else None

    class MockAutoBackupController:
        def __init__(self) -> None:
            self.status = {
                "running": False, "enabled": False, "interval_minutes": 10,
                "profiles": [], "waiting_for_game": True, "last_error": None,
            }
            self.configure_calls: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True
            self.status["running"] = True

        def configure(self, **values: Any) -> dict[str, Any]:
            self.configure_calls.append(dict(values))
            self.status.update(values)
            return {"success": True}

        def get_events(self, limit: int = 100) -> list[dict[str, Any]]:
            output = self.events[:limit]
            del self.events[:limit]
            return output

        def stop(self) -> None:
            self.stopped = True
            self.status["running"] = False

    profile_paths = ("MOCK_AUTOSAVE_SLOT_A", "MOCK_AUTOSAVE_SLOT_B")
    profiles = [
        {"path": profile_paths[0], "label": "模拟自动备份槽 A", "wallet": {}, "heroes": []},
        {"path": profile_paths[1], "label": "模拟自动备份槽 B", "wallet": {}, "heroes": []},
    ]
    now = _datetime.datetime.now().astimezone().replace(microsecond=0)
    sl_backend = MockSLRuntime(profile_paths, now)
    auto_backend = MockAutoBackupController()
    saved = {
        "save_profiles": app._save_profiles,
        "save_profiles_loaded": app._save_profiles_loaded,
        "profile_values": app.save_profile_combo.cget("values"),
        "profile_text": app.save_profile_combo.get(),
        "profile_detail": app.save_profile_detail.cget("text"),
        "resource_values": {key: variable.get() for key, variable in app._save_resource_vars.items()},
        "sl_module": app._sl_module,
        "sl_flags": (app._sl_list_ready, app._sl_create_ready, app._sl_restore_ready, app._sl_select_ready),
        "sl_checkpoints": [dict(item) for item in app._sl_checkpoints],
        "sl_labels": list(app._sl_checkpoint_labels),
        "sl_combo_values": app.sl_checkpoint_combo.cget("values"),
        "sl_combo_text": app.sl_checkpoint_combo.get(),
        "sl_detail": app.sl_checkpoint_detail.cget("text"),
        "sl_status": app.sl_status_label.cget("text"),
        "sl_status_fg": app.sl_status_label.cget("foreground"),
        "autosave_controller": app._autosave_controller,
        "autosave_ready": app._autosave_ready,
        "autosave_error": app._autosave_error,
        "autosave_profiles": list(app._autosave_profiles),
        "autosave_message": app._autosave_last_message,
        "autosave_level": app._autosave_last_level,
        "autosave_enabled": app.autosave_enabled_var.get(),
        "autosave_interval": app.autosave_interval_var.get(),
        "autosave_status": app.autosave_status_label.cget("text"),
        "autosave_status_fg": app.autosave_status_label.cget("foreground"),
        "days_ago": app.sl_days_ago_var.get(),
        "game_week": app.sl_game_week_var.get(),
        "game_running": app.game_running,
    }
    try:
        app._save_profiles = profiles
        app._save_profiles_loaded = True
        app._autosave_profiles = [profile_paths[1]]
        if app._preferred_save_profile_index(profiles) != 1:
            raise AssertionError("重新扫描时没有优先保留已保存的自动备份存档槽")
        app._sl_module = sl_backend
        app._sl_list_ready = app._sl_select_ready = True
        app._sl_create_ready = app._sl_restore_ready = True
        app._sl_checkpoints = []
        app._sl_checkpoint_labels = []
        app.sl_checkpoint_combo.configure(values=())
        app.sl_checkpoint_combo.set("")
        app.save_profile_combo.configure(values=tuple(profile["label"] for profile in profiles), state="readonly")
        app.save_profile_combo.current(0)
        app.game_running = False
        app._on_save_profile_selected()

        app.sl_days_ago_var.set("3")
        app._select_checkpoint_by_days()
        selected_days = app._selected_sl_checkpoint()
        if selected_days is None or selected_days["id"] != "mock-date-3-0":
            raise AssertionError("现实天数筛选没有选中符合日期的可用回档点")
        if not any(call[0] == "select" and call[1] == profile_paths[0] and call[2]["days_ago"] == 3 for call in sl_backend.calls):
            raise AssertionError("现实天数筛选未将天数和当前存档槽传给后端")

        app.sl_game_week_var.set("0")
        app._select_checkpoint_by_week()
        selected_week = app._selected_sl_checkpoint()
        if selected_week is None or selected_week["id"] != "mock-week-0-0" or selected_week.get("game_week") != 0:
            raise AssertionError("游戏第 0 周筛选无效或错误地把 0 当成未知")
        if "第 0 周" not in app.sl_checkpoint_detail.cget("text"):
            raise AssertionError("已选中的游戏周没有展示在详情中")

        app.sl_game_week_var.set("42")
        app._select_checkpoint_by_week()
        if app._selected_sl_checkpoint() is not None or "没有符合" not in app.sl_checkpoint_detail.cget("text"):
            raise AssertionError("无可用周数候选时没有清除选择并提示用户")
        if app._checkpoint_week_text({}) != "周数未知":
            raise AssertionError("缺失游戏周的旧手动回档点未显示周数未知")
        auto_label = app._sl_checkpoint_display(sl_backend.points[profile_paths[0]][0])
        if "自动回档点" not in auto_label:
            raise AssertionError("automatic 类型没有显示为自动回档点")

        app._autosave_controller = auto_backend
        app._autosave_ready = True
        app._autosave_error = None
        app._autosave_profiles = []
        auto_backend.start()
        app._autosave_updating_vars = True
        app.autosave_enabled_var.set(True)
        app.autosave_interval_var.set("15")
        app._autosave_updating_vars = False
        app._on_autosave_config_change()
        configured = auto_backend.configure_calls[-1]
        if configured.get("enabled") is not True or configured.get("interval_minutes") != 15:
            raise AssertionError("自动备份启用状态或间隔没有持久化到控制器")
        if configured.get("profiles") != [profile_paths[0]] or configured.get("backup_while_running") is not True:
            raise AssertionError("自动备份没有只监控当前存档槽或启用运行中备份")

        app.save_profile_combo.current(1)
        app._on_save_profile_selected()
        if auto_backend.status.get("profiles") != [profile_paths[1]]:
            raise AssertionError("切换所选存档槽后没有同步自动备份目标")
        app.save_profile_combo.current(0)
        app._on_save_profile_selected()
        if auto_backend.status.get("profiles") != [profile_paths[0]]:
            raise AssertionError("切回 A 槽后没有恢复单槽自动备份目标")

        automatic_point = {
            "id": "mock-automatic-new", "label": "模拟自动回档点", "profile": profile_paths[0],
            "created_at": now.isoformat(timespec="seconds"), "file_count": 5,
            "bytes": 8192, "kind": "automatic", "game_week": 5,
        }
        sl_backend.points[profile_paths[0]].insert(0, automatic_point)
        auto_backend.events.append({
            "type": "checkpoint_created", "profile": profile_paths[0],
            "checkpoint": dict(automatic_point), "message": "模拟自动回档点已创建。",
        })
        if app._drain_autosave_events() != 1:
            raise AssertionError("自动备份事件队列没有被 UI 消费")
        if not any(item.get("id") == automatic_point["id"] for item in app._sl_checkpoints):
            raise AssertionError("当前存档槽列表未刷新出自动回档点")
        if not auto_backend.configure_calls or auto_backend.status.get("profiles") != [profile_paths[0]]:
            raise AssertionError("控制器状态未保留自动备份存档槽配置")
        app._stop_autosave_controller()
        if not auto_backend.stopped:
            raise AssertionError("关闭流程未停止自动备份控制器")
        return {
            "ok": True, "mock_only": True, "controller_configured": True, "single_selected_slot": True,
            "days_candidate_selected": True, "week_zero_selected": True,
            "unknown_week_supported": True, "automatic_event_refreshed": True,
            "controller_stopped": True,
        }
    finally:
        app._save_profiles = saved["save_profiles"]
        app._save_profiles_loaded = saved["save_profiles_loaded"]
        app.save_profile_combo.configure(values=saved["profile_values"])
        app.save_profile_combo.set(saved["profile_text"])
        app.save_profile_detail.configure(text=saved["profile_detail"])
        for key, value in saved["resource_values"].items():
            app._save_resource_vars[key].set(value)
        app._sl_module = saved["sl_module"]
        app._sl_list_ready, app._sl_create_ready, app._sl_restore_ready, app._sl_select_ready = saved["sl_flags"]
        app._sl_checkpoints = saved["sl_checkpoints"]
        app._sl_checkpoint_labels = saved["sl_labels"]
        app.sl_checkpoint_combo.configure(values=saved["sl_combo_values"])
        app.sl_checkpoint_combo.set(saved["sl_combo_text"])
        app.sl_checkpoint_detail.configure(text=saved["sl_detail"])
        app.sl_status_label.configure(text=saved["sl_status"], foreground=saved["sl_status_fg"])
        app._autosave_controller = saved["autosave_controller"]
        app._autosave_ready = saved["autosave_ready"]
        app._autosave_error = saved["autosave_error"]
        app._autosave_profiles = saved["autosave_profiles"]
        app._autosave_last_message = saved["autosave_message"]
        app._autosave_last_level = saved["autosave_level"]
        app._autosave_updating_vars = True
        app.autosave_enabled_var.set(saved["autosave_enabled"])
        app.autosave_interval_var.set(saved["autosave_interval"])
        app._autosave_updating_vars = False
        app.autosave_status_label.configure(text=saved["autosave_status"], foreground=saved["autosave_status_fg"])
        app.sl_days_ago_var.set(saved["days_ago"])
        app.sl_game_week_var.set(saved["game_week"])
        app.game_running = saved["game_running"]
        app._update_save_controls()


def _capture_window_bmp(hwnd: int, output_path: Path) -> None:
    """Capture a visible Windows window to a BMP using built-in GDI calls."""
    if os.name != "nt":
        raise OSError("窗口截图仅支持 Windows")
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
        ]

    rect = RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        raise OSError("无法读取窗口区域")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise OSError("窗口没有可截图区域")
    window_dc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_obj = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, 0x00CC0020):
            raise OSError("窗口截图 BitBlt 失败")
        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height  # top-down rows
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = 0
        info.biSizeImage = width * height * 4
        pixels = ctypes.create_string_buffer(info.biSizeImage)
        lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0)
        if lines != height:
            raise OSError("读取截图像素失败")
        file_size = 14 + ctypes.sizeof(BITMAPINFOHEADER) + info.biSizeImage
        bmp_file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 14 + ctypes.sizeof(BITMAPINFOHEADER))
        dib_header = struct.pack("<IiiHHIIiiII", info.biSize, width, -height, 1, 32, 0, info.biSizeImage, 0, 0, 0, 0)
        output_path.write_bytes(bmp_file_header + dib_header + pixels.raw)
    finally:
        gdi32.SelectObject(memory_dc, old_obj)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(ctypes.c_void_p(hwnd), window_dc)


def _self_test() -> int:
    keys = [item["key"] for item in OPTIONS]
    expected = {"health", "stress", "light", "items", "gold", "bust", "portrait", "deed", "crest", "one_hit", "damage", "defense", "shop", "speed"}
    toggle_keys = {"health", "stress", "light", "items", "one_hit", "shop"}
    numeric_keys = {"gold", "bust", "portrait", "deed", "crest", "damage", "defense", "speed"}
    failures: list[str] = []
    if len(keys) != 14 or set(keys) != expected:
        failures.append("14 项修改器键名不完整或重复")
    if {item["key"] for item in OPTIONS if item["kind"] == "toggle"} != toggle_keys:
        failures.append("开关项语义不完整")
    if {item["key"] for item in OPTIONS if item["kind"] != "toggle"} != numeric_keys:
        failures.append("数值项语义不完整")
    if OPTION_BY_KEY["gold"]["default"] != 9999999:
        failures.append("金币默认值不正确")
    if any(OPTION_BY_KEY[key]["default"] != 999 for key in ("bust", "portrait", "deed", "crest")):
        failures.append("资源默认值不正确")
    if OPTION_BY_KEY["damage"]["default"] != 2.0 or OPTION_BY_KEY["defense"]["default"] != 2.0 or OPTION_BY_KEY["speed"]["default"] != 2.5:
        failures.append("倍率或速度默认值不正确")
    if len({key for key, _ in HOTKEYS}) != len(HOTKEYS):
        failures.append("快捷键存在重复")
    expected_hotkey_options = {
        "Num 1": "health", "Num 2": "stress", "Num 3": "light", "Num 4": "items",
        "Num 5": "gold", "Num 6": "bust", "Num 7": "portrait", "Num 8": "deed",
        "Num 9": "crest", "Num 0": "one_hit", "Num .": "damage", "Num +": "defense",
        "Ctrl + Num 1": "shop", "Ctrl + Num 2": "speed",
    }
    actual_hotkey_options = {label: key for label, key, _vk, _modifiers in HOTKEY_OPTION_BINDINGS}
    if actual_hotkey_options != expected_hotkey_options:
        failures.append("快捷键与 14 项功能的映射不正确")
    if len(actual_hotkey_options) != len(HOTKEY_OPTION_BINDINGS):
        failures.append("快捷键映射存在重复")
    if HOTKEY_PAUSE_BINDING != ("Ctrl + Shift + Home", 0x24, HOTKEY_MOD_CONTROL | HOTKEY_MOD_SHIFT):
        failures.append("暂停组合键映射不正确")
    if any(vk != HOTKEY_VK_NUMPAD[label.split(" + ")[-1]] for label, _key, vk, _mods in HOTKEY_OPTION_BINDINGS):
        failures.append("数字键的虚拟键码不正确")
    if any(modifiers != HOTKEY_MOD_CONTROL for label, _key, _vk, modifiers in HOTKEY_OPTION_BINDINGS if label.startswith("Ctrl")):
        failures.append("Ctrl 数字键的修饰键不正确")
    if any(modifiers != 0 for label, _key, _vk, modifiers in HOTKEY_OPTION_BINDINGS if not label.startswith("Ctrl")):
        failures.append("普通数字键不应带额外修饰键")
    mock_action_parameters: dict[str, Any] = {}
    for _label, key, _vk, _modifiers in HOTKEY_OPTION_BINDINGS:
        meta = OPTION_BY_KEY[key]
        default_parameter = True if meta["kind"] == "toggle" else meta["default"]
        mock_action_parameters[key] = _option_value_for_toggle(key, True, None if meta["kind"] == "toggle" else default_parameter)
        if _option_value_for_toggle(key, False, default_parameter) is not None:
            failures.append(f"{key} 关闭时没有映射到 None")
        if mock_action_parameters[key] != default_parameter:
            failures.append(f"{key} 快捷键默认参数不正确")
    if mock_action_parameters.get("stress") is not True or mock_action_parameters.get("speed") != 2.5:
        failures.append("Num 2 应清零压力，Ctrl + Num 2 应使用默认速度 2.5")
    version_probe = object.__new__(TrainerApp)
    version_probe.connected = False
    version_probe.engine_status = {}
    version_probe._last_attach_error = None
    pending_version = version_probe._version_text()
    if "目标版本：28063 (64-bit)" not in pending_version or "检测版本" in pending_version or "已校验目标" in pending_version:
        failures.append("未验证时必须显示目标版本，不能冒称检测版本")
    version_probe.engine_status = {"tested": False, "connection_attempt": {"architecture": "x86"}}
    failed_version = version_probe._version_text()
    failure_text = version_probe._connection_failure_text() or ""
    if "实际架构：x86" not in failed_version or "未通过" not in failed_version or "x86" not in failure_text:
        failures.append("失败校验应显示实际架构并提示未通过")
    version_probe.engine_status = {"tested": True, "version": TARGET_VERSION}
    validated_version = version_probe._version_text()
    if "已校验目标版本" not in validated_version or "版本校验：通过" not in validated_version:
        failures.append("只有已校验时才能显示已校验目标版本")
    if failures:
        print(json.dumps({"ok": False, "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"ok": True, "options": len(OPTIONS), "hotkeys": len(HOTKEYS), "mock_action_parameters": mock_action_parameters, "game_started": False, "engine_attached": False}, ensure_ascii=False, indent=2))
    return 0


def _run_smoke(output_dir: Path) -> int:
    _enable_dpi_awareness()
    root = tk.Tk()
    # Smoke mode checks layout without importing or constructing a game backend.
    app = TrainerApp(root, smoke=True, load_engine=False)
    root.update_idletasks()
    root.deiconify()
    root.update()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "trainer_ui_smoke.json"
    image_path = output_dir / "trainer_ui_smoke.bmp"
    save_image_path = output_dir / "trainer_ui_save_smoke.bmp"

    def finish() -> None:
        try:
            app.notebook.select(app.live_tab)
            app.root.update_idletasks()
            app.root.update()
            _capture_window_bmp(root.winfo_id(), image_path)
            app.notebook.select(app.save_tab)
            app.root.update_idletasks()
            app.root.update()
            _capture_window_bmp(root.winfo_id(), save_image_path)
            sl_mock_test = _run_sl_mock_smoke(app)
            autosave_mock_test = _run_autosave_mock_smoke(app)
            snapshot = app.snapshot_controls()
            snapshot["sl_mock_test"] = sl_mock_test
            snapshot["autosave_mock_test"] = autosave_mock_test
            snapshot["screenshot"] = image_path.name
            snapshot["screenshots"] = {"live": image_path.name, "save": save_image_path.name}
            json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"ok": True, "controls_json": str(json_path), "screenshots": [str(image_path), str(save_image_path)], "game_started": False}, ensure_ascii=False))
            app.closing = True
            if app._status_after is not None:
                root.after_cancel(app._status_after)
                app._status_after = None
            if app._hotkey_after is not None:
                root.after_cancel(app._hotkey_after)
                app._hotkey_after = None
            if app._autosave_after is not None:
                root.after_cancel(app._autosave_after)
                app._autosave_after = None
            app._stop_autosave_controller()
            app.hotkey_manager.stop()
            root.destroy()
        except Exception:
            traceback.print_exc()
            try:
                app.closing = True
                if app._status_after is not None:
                    root.after_cancel(app._status_after)
                if app._hotkey_after is not None:
                    root.after_cancel(app._hotkey_after)
                if app._autosave_after is not None:
                    root.after_cancel(app._autosave_after)
                app._stop_autosave_controller()
                app.hotkey_manager.stop()
                root.destroy()
            except Exception:
                pass
            raise

    root.after(350, finish)
    root.mainloop()
    return 0 if json_path.exists() and image_path.exists() and save_image_path.exists() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Darkest Dungeon I 单机修改器")
    parser.add_argument("--self-test", action="store_true", help="执行静态自检，不连接或启动游戏")
    parser.add_argument("--smoke", action="store_true", help="打开界面、生成控件 JSON 和窗口截图后退出")
    parser.add_argument("--smoke-dir", type=Path, default=Path(__file__).resolve().parent, help="烟雾截图和 JSON 的输出目录")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.smoke:
        return _run_smoke(args.smoke_dir)
    _enable_dpi_awareness()
    root = tk.Tk()
    TrainerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
