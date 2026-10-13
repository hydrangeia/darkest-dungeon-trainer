"""Exercise SL buttons with the real file backend and synthetic save fixtures."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tkinter as tk

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from save_runtime import SaveManager
from sl_runtime import CheckpointManager
from trainer_ui import TrainerApp
from tests.test_sl_runtime import _estate_bytes, _roster_bytes, _tree


class SaveLoadUiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        directory=Path(self.temp.name);remote=directory/'remote'
        self.profile=remote/'profile_0';self.profile.mkdir(parents=True)
        (self.profile/'persist.estate.json').write_bytes(_estate_bytes())
        (self.profile/'persist.roster.json').write_bytes(_roster_bytes())
        (self.profile/'persist.game.json').write_bytes(b'raid-before-test')
        self.saves=SaveManager(directory/'edits');self.saves.roots=lambda:[remote]
        self.saves.guard=lambda:None
        self.points=CheckpointManager(self.saves,directory/'checkpoints')
        save_api=SimpleNamespace(save_profiles=self.saves.profiles,edit_save=self.saves.edit,
                                 restore_backup=self.saves.restore)
        sl_api=SimpleNamespace(create_checkpoint=self.points.create_checkpoint,
                               list_checkpoints=self.points.list_checkpoints,
                               restore_checkpoint=self.points.restore_checkpoint)
        self.modules=patch.dict(sys.modules,{'save_runtime':save_api,'sl_runtime':sl_api})
        self.modules.start();self.addCleanup(self.modules.stop)
        self.window=tk.Tk();self.window.withdraw()
        self.app=TrainerApp(self.window,smoke=True,load_engine=False)
        self.app.engine=SimpleNamespace(status=lambda:{'running':False,'connected':False,'capabilities':{}})
        self.app._refresh_status()
        self.app._scan_save_profiles(quiet=True)
        self.addCleanup(self.close_window)

    def close_window(self):
        self.app.closing=True
        self.app.hotkey_manager.stop()
        self.window.destroy()

    def test_save_restore_and_undo_through_ui_use_real_slot_snapshot(self):
        before=_tree(self.profile)
        self.app.sl_checkpoint_name_var.set('进地牢前')
        self.app._save_selected_checkpoint()
        self.assertEqual(len(self.app._sl_checkpoints),1)
        point=self.app._sl_checkpoints[0]
        self.assertEqual(point['label'],'进地牢前')
        self.assertEqual(self.app._selected_sl_checkpoint()['id'],point['id'])
        (self.profile/'persist.game.json').write_bytes(b'raid-after-test')
        (self.profile/'new-after-checkpoint.json').write_bytes(b'new progress')
        current=_tree(self.profile)
        self.app._restore_selected_checkpoint()
        self.assertEqual(_tree(self.profile),before)
        self.assertEqual(self.app._selected_sl_checkpoint()['id'],point['id'])
        safety=next(p for p in self.app._sl_checkpoints if p['kind']=='before_restore')
        self.app.sl_checkpoint_combo.current(self.app._sl_checkpoints.index(safety))
        self.app._on_sl_checkpoint_selected()
        self.app._restore_selected_checkpoint()
        self.assertEqual(_tree(self.profile),current)
        self.assertIn('回档',self.app.sl_status_label.cget('text'))


if __name__=='__main__':unittest.main()
