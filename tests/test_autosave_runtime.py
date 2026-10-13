import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autosave_runtime import AutoBackupController, AutoBackupSettingsStore
import autosave_runtime
from save_runtime import SaveManager
from sl_runtime import CheckpointManager, SnapshotCancelledError, SnapshotNotQuietError
from tests.test_sl_runtime import _build_dson, _estate_bytes, _roster_bytes


def _campaign_bytes(week):
    return _build_dson({"base_root": {"total_weeks": ("int32", week)}})


class FakeClock:
    def __init__(self, value=None):
        self.value = float(value if value is not None else time.time())
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.value

    def advance(self, seconds):
        with self._lock:
            self.value += seconds

    def sleep(self, seconds):
        self.advance(seconds)


class AutoBackupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "steam-user" / "262060" / "remote"
        self.remote.mkdir(parents=True)
        self.profile0 = self._make_profile("profile_0", b"slot0", 0)
        self.profile1 = self._make_profile("profile_1", b"slot1", 2)
        self.checkpoint_root = self.root / "backups" / "checkpoints"
        self.settings_path = self.root / "backups" / "autosave.json"
        self.saves = SaveManager(self.root / "unused-edit-backups")
        self.saves.roots = lambda: [self.remote]
        self.saves.guard = lambda: None
        self.manager = CheckpointManager(self.saves, self.checkpoint_root)
        self.clock = FakeClock()
        self._make_tree_old()

    def _make_profile(self, name, payload, week):
        profile = self.remote / name
        profile.mkdir(parents=True)
        (profile / "persist.estate.json").write_bytes(_estate_bytes())
        (profile / "persist.roster.json").write_bytes(_roster_bytes())
        (profile / "persist.campaign_log.json").write_bytes(_campaign_bytes(week))
        (profile / "persist.game.json").write_bytes(
            _build_dson({"base_root": {"fixture_marker": ("int32", 1)}})
        )
        (profile / "campaign.bin").write_bytes(payload)
        return profile

    def _make_tree_old(self):
        old_time = self.clock.value - 120.0
        for profile in (self.profile0, self.profile1):
            for path in sorted(profile.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                os.utime(path, (old_time, old_time))
            os.utime(profile, (old_time, old_time))

    def _store(self, settings):
        store = AutoBackupSettingsStore(self.settings_path)
        store.save(settings)
        return store

    def _controller(self, *, game_running=lambda: False, settings=None, **kwargs):
        store = self._store(settings or {
            "enabled": True,
            "interval_minutes": 1,
            "profiles": [str(self.profile0)],
            "backup_while_running": True,
        })
        return AutoBackupController(
            self.manager,
            settings_store=store,
            clock=self.clock,
            game_running=game_running,
            sleeper=self.clock.sleep,
            quiet_seconds=kwargs.pop("quiet_seconds", 0),
            poll_interval=kwargs.pop("poll_interval", 0.05),
            backoff_seconds=kwargs.pop("backoff_seconds", 5),
            **kwargs,
        )

    def test_automatic_snapshot_is_read_only_and_works_while_game_is_running(self):
        self.saves.guard = lambda: (_ for _ in ()).throw(ValueError("game is running"))
        controller = self._controller(game_running=lambda: True)
        controller.tick()
        checkpoints = self.manager.list_checkpoints(str(self.profile0))
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["kind"], "automatic")
        self.assertEqual(checkpoints[0]["game_week"], 0)
        self.assertTrue(controller.status["waiting_for_game"])

    def test_recent_tree_change_during_quiet_window_is_skipped(self):
        campaign = self.profile0 / "campaign.bin"
        os.utime(campaign, (self.clock.value - 1, self.clock.value - 1))

        def write_during_quiet(seconds):
            self.clock.advance(seconds)
            campaign.write_bytes(b"changed during quiet wait")
            stamp = self.clock.value
            os.utime(campaign, (stamp, stamp))

        with self.assertRaises(SnapshotNotQuietError):
            self.manager.create_automatic_checkpoint(
                str(self.profile0), quiet_seconds=4, clock=self.clock, sleeper=write_during_quiet
            )
        self.assertFalse(self.checkpoint_root.exists())

    def test_tree_change_during_copy_is_not_published(self):
        original_copy = self.manager._copy_tree
        campaign = self.profile0 / "campaign.bin"

        def copy_then_change(source, destination, mtime_ns=None, *, destination_owned=False):
            original_copy(source, destination, mtime_ns, destination_owned=destination_owned)
            if source == self.profile0:
                campaign.write_bytes(b"changed while copied")

        with patch.object(self.manager, "_copy_tree", side_effect=copy_then_change):
            with self.assertRaises(SnapshotNotQuietError):
                self.manager.create_automatic_checkpoint(
                    str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep
                )
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(any(path.name.startswith(".staging-") for path in self.checkpoint_root.iterdir()))

    def test_corrupt_core_dson_is_rejected_without_publishing(self):
        (self.profile0 / "persist.estate.json").write_bytes(b"broken DSON")
        with self.assertRaises(ValueError):
            self.manager.create_automatic_checkpoint(
                str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep
            )
        self.assertFalse(self.checkpoint_root.exists())

    def test_corrupt_non_core_persist_dson_is_rejected_without_publishing(self):
        (self.profile0 / "persist.game.json").write_bytes(b"truncated DSON")
        with self.assertRaises(SnapshotNotQuietError):
            self.manager.create_automatic_checkpoint(
                str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep
            )
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(any(path.name.startswith(".staging-") for path in self.checkpoint_root.iterdir()))

    def test_missing_top_level_total_weeks_is_unknown_not_a_failure(self):
        (self.profile0 / "persist.campaign_log.json").write_bytes(
            _build_dson({"base_root": {"other_value": ("int32", 7)}})
        )
        checkpoint = self.manager.create_automatic_checkpoint(
            str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep
        )
        self.assertIsNone(checkpoint["game_week"])

    def test_cancel_during_final_hash_scan_prevents_publish(self):
        original_scan = self.manager._scan_tree
        cancelled = [False]
        profile_scans = [0]

        def scan_then_cancel(path):
            result = original_scan(path)
            if Path(path) == self.profile0:
                profile_scans[0] += 1
                # The third source scan is the pre-publish content hash, after
                # the copy-time source and snapshot have both been verified.
                if profile_scans[0] == 3:
                    cancelled[0] = True
            return result

        with patch.object(self.manager, "_scan_tree", side_effect=scan_then_cancel):
            with self.assertRaises(SnapshotCancelledError):
                self.manager.create_automatic_checkpoint(
                    str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep,
                    cancelled=lambda: cancelled[0],
                )
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(any(path.name.startswith(".staging-") for path in self.checkpoint_root.iterdir()))

    def test_source_signature_runs_after_final_snapshot_hash(self):
        original_scan = self.manager._scan_tree
        snapshot_scans = [0]

        def mutate_after_final_snapshot_scan(path):
            result = original_scan(path)
            if Path(path).name == "snapshot":
                snapshot_scans[0] += 1
                if snapshot_scans[0] == 2:
                    campaign = self.profile0 / "campaign.bin"
                    campaign.write_bytes(b"changed after source content scan")
            return result

        with patch.object(self.manager, "_scan_tree", side_effect=mutate_after_final_snapshot_scan):
            with self.assertRaises(SnapshotNotQuietError):
                self.manager.create_automatic_checkpoint(
                    str(self.profile0), quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep
                )
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(any(path.name.startswith(".staging-") for path in self.checkpoint_root.iterdir()))

    def test_auto_checkpoint_deduplicates_and_captures_final_state_after_game_exit(self):
        running = [True]
        controller = self._controller(game_running=lambda: running[0])
        controller.tick()
        self.assertEqual(len(self.manager.list_checkpoints(str(self.profile0))), 1)

        # The game advances on-disk progress during play; the next interval records it.
        (self.profile0 / "campaign.bin").write_bytes(b"later progress")
        (self.profile0 / "persist.campaign_log.json").write_bytes(_campaign_bytes(3))
        controller.clock.advance(61)
        controller.tick()
        points = self.manager.list_checkpoints(str(self.profile0))
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["game_week"], 3)

        # On quit the final check is immediate but identical data does not duplicate.
        running[0] = False
        controller.tick()
        self.assertEqual(len(self.manager.list_checkpoints(str(self.profile0))), 2)
        self.assertFalse(controller.status["waiting_for_game"])

    def test_disabled_during_play_waits_for_exit_then_captures_final_point(self):
        running = [True]
        controller = self._controller(
            game_running=lambda: running[0],
            settings={"enabled": True, "interval_minutes": 1, "profiles": [str(self.profile0)],
                      "backup_while_running": False},
        )
        controller.tick()
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        running[0] = False
        controller.tick()
        self.assertEqual(len(self.manager.list_checkpoints(str(self.profile0))), 1)

    def test_settings_are_atomic_persistent_and_cover_multiple_slots(self):
        controller = self._controller(game_running=lambda: False,
                                      settings={"enabled": False, "interval_minutes": 30,
                                                "profiles": [], "backup_while_running": True})
        configured = controller.configure(enabled=False, interval_minutes=17,
                                           profiles=[str(self.profile0), str(self.profile1)],
                                           backup_while_running=False)
        restarted = AutoBackupSettingsStore(self.settings_path).load()
        self.assertEqual(configured, restarted)
        self.assertEqual(restarted["interval_minutes"], 17)
        self.assertFalse(restarted["backup_while_running"])

        defaults = AutoBackupSettingsStore(self.root / "fresh-settings.json").load()
        self.assertTrue(defaults["backup_while_running"])
        self.assertEqual(defaults["interval_minutes"], 10)

    def test_settings_replace_failure_keeps_previous_file_and_cleans_temp(self):
        store = AutoBackupSettingsStore(self.settings_path)
        previous = store.save({"enabled": False, "interval_minutes": 12,
                               "profiles": [str(self.profile0)], "backup_while_running": True})
        changed = {**previous, "interval_minutes": 45}
        with patch.object(autosave_runtime.os, "replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                store.save(changed)
        self.assertEqual(store.load(), previous)
        self.assertFalse(list(self.settings_path.parent.glob(".autosave-settings-*.tmp")))

        active = self._controller(game_running=lambda: True,
                                  settings={"enabled": True, "interval_minutes": 1,
                                            "profiles": [str(self.profile0), str(self.profile1)],
                                            "backup_while_running": True})
        active.tick()
        self.assertEqual(len(self.manager.list_checkpoints(str(self.profile0))), 1)
        self.assertEqual(len(self.manager.list_checkpoints(str(self.profile1))), 1)

    def test_missing_profile_reports_error_without_creating_path(self):
        missing = self.remote / "profile_missing"
        controller = self._controller(settings={"enabled": True, "interval_minutes": 1,
                                                "profiles": [str(missing)], "backup_while_running": True})
        controller.tick()
        self.assertFalse(missing.exists())
        self.assertTrue(controller.status["last_error"])
        self.assertTrue(any(event["type"] == "error" for event in controller.get_events()))

    def test_game_probe_errors_respect_backoff_and_clear_after_recovery(self):
        probes = [0]

        def flaky_probe():
            probes[0] += 1
            if probes[0] == 1:
                raise RuntimeError("probe unavailable")
            return False

        controller = self._controller(
            game_running=flaky_probe,
            settings={"enabled": True, "interval_minutes": 1, "profiles": [],
                      "backup_while_running": True},
            backoff_seconds=5,
        )
        controller.tick()
        self.assertEqual(probes[0], 1)
        self.assertEqual(controller.status["last_error"], "probe unavailable")
        controller.tick()
        self.assertEqual(probes[0], 1)

        self.clock.advance(5)
        controller.tick()
        self.assertEqual(probes[0], 2)
        self.assertIsNone(controller.status["last_error"])
        self.assertNotIn("<controller>", controller._retry_at)

    def test_stop_prevents_later_background_check_from_writing(self):
        running = [True]
        controller = self._controller(game_running=lambda: running[0],
                                      settings={"enabled": True, "interval_minutes": 1,
                                                "profiles": [str(self.profile0)],
                                                "backup_while_running": False})
        controller.start()
        deadline = time.monotonic() + 2
        while not controller.status["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        controller.stop()
        running[0] = False
        controller.tick()
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(controller.status["running"])

    def test_disabling_during_copy_cancels_publish(self):
        controller = self._controller(game_running=lambda: False)
        original_copy = self.manager._copy_tree

        def copy_then_disable(source, destination, mtime_ns=None, *, destination_owned=False):
            original_copy(source, destination, mtime_ns, destination_owned=destination_owned)
            if source == self.profile0:
                controller.configure(enabled=False)

        with patch.object(self.manager, "_copy_tree", side_effect=copy_then_disable):
            controller.tick()
        self.assertEqual(self.manager.list_checkpoints(str(self.profile0)), [])
        self.assertFalse(controller.settings["enabled"])

    def test_real_date_and_game_week_selection_uses_only_recorded_data(self):
        utc = datetime.timezone.utc
        day0 = datetime.datetime(2025, 1, 1, 12, tzinfo=utc)
        points = []
        for offset, week, payload in ((0, 0, b"week zero"), (4, 2, b"week two early"),
                                     (10, 2, b"week two late")):
            (self.profile0 / "persist.campaign_log.json").write_bytes(_campaign_bytes(week))
            (self.profile0 / "campaign.bin").write_bytes(payload)
            points.append(self.manager.create_automatic_checkpoint(
                str(self.profile0), created_at=day0 + datetime.timedelta(days=offset),
                quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep,
            ))
        selected = self.manager.select_checkpoint(
            str(self.profile0), days_ago=6, now=day0 + datetime.timedelta(days=15)
        )
        self.assertEqual(selected["id"], points[1]["id"])
        self.assertEqual(self.manager.select_checkpoint(
            str(self.profile0), days_ago=20, now=day0 + datetime.timedelta(days=15)
        ), None)
        self.assertEqual(self.manager.select_checkpoint(str(self.profile0), game_week=0)["id"], points[0]["id"])
        self.assertEqual(self.manager.select_checkpoint(str(self.profile0), game_week=2)["id"], points[2]["id"])
        self.assertIsNone(self.manager.select_checkpoint(str(self.profile0), game_week=1))

    def test_old_manifest_week_is_derived_and_unknown_week_is_never_guessed(self):
        old_point = self.manager.create_automatic_checkpoint(
            str(self.profile0), created_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
            quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep,
        )
        manifest_path = self.checkpoint_root / old_point["id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("game_week", None)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.manager.select_checkpoint(str(self.profile0), game_week=0)["id"], old_point["id"])

        (self.profile0 / "persist.campaign_log.json").unlink()
        unknown = self.manager.create_automatic_checkpoint(
            str(self.profile0), created_at=datetime.datetime(2025, 1, 2, tzinfo=datetime.timezone.utc),
            quiet_seconds=0, clock=self.clock, sleeper=self.clock.sleep,
        )
        self.assertIsNone(unknown.get("game_week"))
        self.assertIsNone(self.manager.select_checkpoint(str(self.profile0), game_week=99))


if __name__ == "__main__":
    unittest.main(verbosity=2)
