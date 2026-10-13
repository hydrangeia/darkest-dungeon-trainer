"""UI selection and configuration checks using only synthetic local fixtures."""
import unittest
from tests import test_sl_ui as sl_ui
from tests.test_sl_runtime import _build_dson, _tree


class SpyController:
    def __init__(self):
        self.status={'enabled':False,'running':False,'interval_minutes':10,
                     'profiles':[],'backup_while_running':True}
        self.calls=[]
    def configure(self,**settings):
        self.calls.append(dict(settings));self.status.update(settings)
        return dict(self.status)
    def get_events(self):return []
    def stop(self,**_kwargs):return dict(self.status)


class AutomaticSaveUiTests(unittest.TestCase):
    setUp=sl_ui.SaveLoadUiIntegrationTests.setUp
    close_window=sl_ui.SaveLoadUiIntegrationTests.close_window

    def test_configuration_targets_only_selected_slot_and_allows_live_backup(self):
        spy=SpyController()
        self.app._autosave_controller=spy;self.app._autosave_ready=True
        self.app._save_profiles.append({'path':str(self.profile.parent/'profile_1'),'label':'another slot'})
        self.app.autosave_enabled_var.set(True)
        self.app.autosave_interval_var.set('7')
        self.app._on_autosave_config_change()
        self.assertEqual(spy.calls[-1],{'enabled':True,'interval_minutes':7,
                         'profiles':[str(self.profile.resolve())],'backup_while_running':True})

    def test_week_zero_and_days_selection_only_select_without_restoring(self):
        (self.profile/'persist.campaign_log.json').write_bytes(
            _build_dson({'base_root':{'total_weeks':('int32',0)}}))
        self.app._sl_module.select_checkpoint=self.points.select_checkpoint
        self.app._sl_select_ready=True
        self.app.sl_checkpoint_name_var.set('week zero')
        self.app._save_selected_checkpoint()
        point=self.app._selected_sl_checkpoint()
        self.assertEqual(point['game_week'],0)
        (self.profile/'new-progress.bin').write_bytes(b'new progress must remain')
        before=_tree(self.profile)
        self.app.sl_game_week_var.set('0');self.app._select_checkpoint_by_week()
        self.assertEqual(self.app._selected_sl_checkpoint()['id'],point['id'])
        self.app.sl_days_ago_var.set('0');self.app._select_checkpoint_by_days()
        self.assertEqual(self.app._selected_sl_checkpoint()['id'],point['id'])
        self.assertEqual(_tree(self.profile),before)


if __name__=='__main__':unittest.main()
