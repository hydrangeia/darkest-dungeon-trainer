import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from save_runtime import SaveManager
from save_engine import DsonDocument
from tests.test_sl_runtime import _estate_bytes, _roster_bytes

class SaveTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1]))
        root=Path(self.temp.name);self.remote=root/'remote';self.profile=self.remote/'profile_8'
        self.profile.mkdir(parents=True)
        (self.profile/'persist.estate.json').write_bytes(_estate_bytes())
        (self.profile/'persist.roster.json').write_bytes(_roster_bytes())
        self.m=SaveManager(root/'backups');self.m.roots=lambda:[self.remote];self.m.guard=lambda:None
        self.before={n:(self.profile/n).read_bytes() for n in ['persist.estate.json','persist.roster.json']}
    def tearDown(self):self.temp.cleanup()
    def test_atomic_edit_and_restore_are_byte_exact(self):
        result=self.m.edit(str(self.profile),{'gold':0,'bust':999,'portrait':500,'deed':998,'crest':997},True)
        d=DsonDocument.from_bytes((self.profile/'persist.estate.json').read_bytes())
        self.assertEqual(d.read_wallet()['gold'],0)
        r=DsonDocument.from_bytes((self.profile/'persist.roster.json').read_bytes())
        self.assertTrue(all(r.read_scalar(f.path,'float32')==0 for f in r.find_named_fields('m_Stress')))
        self.m.restore(result['backup_id'])
        self.assertTrue(all((self.profile/n).read_bytes()==b for n,b in self.before.items()))
    def test_restore_refuses_overwriting_new_progress(self):
        result=self.m.edit(str(self.profile),{'gold':42},False)
        path=self.profile/'persist.estate.json';d=DsonDocument.from_bytes(path.read_bytes());d.write_wallet('gold',43);path.write_bytes(d.to_bytes())
        with self.assertRaisesRegex(ValueError,'新进度'):self.m.restore(result['backup_id'])
        self.assertEqual(DsonDocument.from_bytes(path.read_bytes()).read_wallet()['gold'],43)
    def test_restore_rolls_back_all_files_when_later_replace_reports_failure(self):
        result=self.m.edit(str(self.profile),{'gold':42},True)
        current={name:(self.profile/name).read_bytes() for name in self.before}
        original_atomic=self.m.atomic

        def fail_after_roster_replace(path,data):
            original_atomic(path,data)
            if path.name=='persist.roster.json' and data==self.before['persist.roster.json']:
                raise OSError('simulated failure after atomic replacement')

        self.m.atomic=fail_after_roster_replace
        with self.assertRaisesRegex(OSError,'after atomic replacement'):
            self.m.restore(result['backup_id'])
        self.assertEqual(
            {name:(self.profile/name).read_bytes() for name in self.before},
            current,
        )
    def test_invalid_amount_does_not_write(self):
        with self.assertRaises(ValueError):self.m.edit(str(self.profile),{'gold':-1},False)
        self.assertEqual((self.profile/'persist.estate.json').read_bytes(),self.before['persist.estate.json'])

if __name__=='__main__':unittest.main()
