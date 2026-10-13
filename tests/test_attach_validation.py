import contextlib
import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trainer_engine
from trainer_engine import Engine, SHA256, pe_architecture


def synthetic_pe(machine,optional_magic=None):
    data=bytearray(0x200)
    data[:2]=b'MZ'
    struct.pack_into('<I',data,0x3c,0x80)
    data[0x80:0x84]=b'PE\0\0'
    struct.pack_into('<H',data,0x84,machine)
    if optional_magic is None:optional_magic=0x10b if machine==0x014c else 0x20b
    struct.pack_into('<H',data,0x94,0xe0 if optional_magic==0x10b else 0xf0)
    struct.pack_into('<H',data,0x98,optional_magic)
    return bytes(data)


class RecordingProcess:
    def __init__(self,pid,path):
        self.pid=pid;self.path=str(path);self.base=0x100000
        self.read_calls=0;self.allocate_calls=0;self.write_calls=0
        self.pause_calls=0;self.closed=False
    def alive(self):return True
    def read(self,*args):self.read_calls+=1;raise AssertionError('unexpected remote read')
    def allocate(self,*args,**kwargs):self.allocate_calls+=1;raise AssertionError('unexpected remote allocation')
    def write(self,*args):self.write_calls+=1;raise AssertionError('unexpected remote write')
    @contextlib.contextmanager
    def paused(self):
        self.pause_calls+=1
        yield
    def close(self,free=False):self.closed=True


class ExitedProcess:
    def __init__(self):self.pid=9001;self.close_calls=[]
    def alive(self):return False
    def close(self,free=False):self.close_calls.append(free)


class AttachValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def _attempt(self,machine,data=None,process_path=None,expected_sha=None):
        path=self.root/'Darkest.exe'
        path.write_bytes(data if data is not None else synthetic_pe(machine))
        process=RecordingProcess(27182,process_path or path)
        process_factory=Mock(return_value=process)
        engine=Engine()
        engine._backup_saves=Mock()
        with patch.object(trainer_engine,'find_game_pids',return_value=[27182]), \
             patch.object(trainer_engine,'query_process_image',return_value=str(path)), \
             patch.object(trainer_engine,'Process',process_factory):
            if expected_sha is None:
                with self.assertRaises(ValueError) as caught:engine.attach()
            else:
                with patch.object(trainer_engine,'SHA256',expected_sha):
                    with self.assertRaises(ValueError) as caught:engine.attach()
            status=engine.status()
        return engine,process,process_factory,status,caught.exception,str(path)

    def test_pe_architecture_recognizes_amd64_and_x86_and_rejects_malformed(self):
        self.assertEqual(pe_architecture(synthetic_pe(0x8664)),'AMD64')
        self.assertEqual(pe_architecture(synthetic_pe(0x014c)),'x86')
        self.assertEqual(pe_architecture(synthetic_pe(0xaa64)),'ARM64')
        with self.assertRaisesRegex(ValueError,'PE 头'):
            pe_architecture(b'MZ'+bytes(30))
        with self.assertRaisesRegex(ValueError,r'不是 PE32\+'):
            pe_architecture(synthetic_pe(0x8664,0x10b))

    def test_x86_rejection_keeps_diagnostics_and_has_no_remote_side_effects(self):
        engine,process,process_factory,status,error,path=self._attempt(0x014c)
        attempt=status['connection_attempt']
        self.assertIn('Steam Win64',str(error))
        self.assertEqual(attempt['pid'],27182)
        self.assertEqual(attempt['actual_path'],path)
        self.assertEqual(attempt['architecture'],'x86')
        self.assertEqual(attempt['actual_sha256'],hashlib.sha256(Path(path).read_bytes()).hexdigest())
        self.assertEqual(attempt['expected_sha256'],SHA256)
        self.assertIn('32 位 x86',attempt['failure_reason'])
        self.assertEqual(status['last_error'],attempt['failure_reason'])
        self.assertFalse(status['connected'])
        self.assertEqual((process.read_calls,process.allocate_calls,process.write_calls,process.pause_calls),(0,0,0,0))
        process_factory.assert_not_called()
        engine._backup_saves.assert_not_called()

    def test_unknown_amd64_hash_is_distinguished_and_rejected_before_backup_or_writes(self):
        engine,process,process_factory,status,error,path=self._attempt(0x8664)
        attempt=status['connection_attempt']
        self.assertIn('版本更新或程序来源不同',str(error))
        self.assertEqual(attempt['architecture'],'AMD64')
        self.assertNotEqual(attempt['actual_sha256'],attempt['expected_sha256'])
        self.assertIn('AMD64 64 位',attempt['failure_reason'])
        self.assertTrue(status['last_error'])
        self.assertEqual((process.read_calls,process.allocate_calls,process.write_calls,process.pause_calls),(0,0,0,0))
        process_factory.assert_not_called()
        engine._backup_saves.assert_not_called()

    def test_other_machine_architecture_is_rejected_before_process_access(self):
        engine,process,process_factory,status,error,path=self._attempt(0xaa64)
        attempt=status['connection_attempt']
        self.assertIn('不支持的 PE 架构 ARM64',str(error))
        self.assertEqual(attempt['architecture'],'ARM64')
        self.assertEqual(attempt['actual_sha256'],hashlib.sha256(Path(path).read_bytes()).hexdigest())
        self.assertEqual(attempt['expected_sha256'],SHA256)
        process_factory.assert_not_called()
        engine._backup_saves.assert_not_called()
        self.assertEqual((process.read_calls,process.allocate_calls,process.write_calls,process.pause_calls),(0,0,0,0))

    def test_malformed_pe_keeps_actual_hash_and_fails_closed(self):
        malformed=b'MZ'+bytes(90)
        engine,process,process_factory,status,error,path=self._attempt(0x8664,data=malformed)
        attempt=status['connection_attempt']
        self.assertIn('PE 架构',str(error))
        self.assertEqual(attempt['architecture'],'unknown')
        self.assertEqual(attempt['actual_sha256'],hashlib.sha256(malformed).hexdigest())
        self.assertEqual(attempt['expected_sha256'],SHA256)
        self.assertTrue(attempt['failure_reason'])
        self.assertEqual((process.read_calls,process.allocate_calls,process.write_calls,process.pause_calls),(0,0,0,0))
        process_factory.assert_not_called()
        engine._backup_saves.assert_not_called()

    def test_process_path_mismatch_is_rejected_before_memory_or_backup_access(self):
        data=synthetic_pe(0x8664)
        expected=hashlib.sha256(data).hexdigest()
        other=self.root/'Other.exe'
        engine,process,process_factory,status,error,verified_path=self._attempt(
            0x8664,data=data,process_path=other,expected_sha=expected
        )
        attempt=status['connection_attempt']
        self.assertIn('路径不一致',str(error))
        self.assertEqual(attempt['verified_path'],verified_path)
        self.assertEqual(attempt['process_path'],str(other))
        self.assertEqual(attempt['actual_path'],str(other))
        self.assertEqual(attempt['actual_sha256'],expected)
        self.assertEqual(attempt['expected_sha256'],expected)
        process_factory.assert_called_once_with(27182)
        self.assertEqual((process.read_calls,process.allocate_calls,process.write_calls,process.pause_calls),(0,0,0,0))
        engine._backup_saves.assert_not_called()
        self.assertTrue(process.closed)

    def test_process_creation_failure_is_retained_in_status(self):
        engine=Engine()
        path=self.root/'Darkest.exe';data=synthetic_pe(0x8664);path.write_bytes(data)
        expected=hashlib.sha256(data).hexdigest()
        with patch.object(trainer_engine,'find_game_pids',return_value=[731]), \
             patch.object(trainer_engine,'query_process_image',return_value=str(path)), \
             patch.object(trainer_engine,'SHA256',expected), \
             patch.object(trainer_engine,'Process',side_effect=OSError('access denied')):
            with self.assertRaisesRegex(OSError,'access denied'):engine.attach()
            status=engine.status()
        attempt=status['connection_attempt']
        self.assertEqual(attempt['pid'],731)
        self.assertEqual(attempt['actual_path'],str(path))
        self.assertEqual(attempt['architecture'],'AMD64')
        self.assertEqual(attempt['actual_sha256'],expected)
        self.assertEqual(attempt['expected_sha256'],expected)
        self.assertIn('access denied',attempt['failure_reason'])
        self.assertEqual(status['last_error'],attempt['failure_reason'])
        self.assertFalse(status['connected'])

    def test_rejected_reconnect_clears_stale_verified_connection_state(self):
        data=synthetic_pe(0x8664)
        path=self.root/'Darkest.exe';path.write_bytes(data)
        engine=Engine();stale=ExitedProcess();old_stop=engine._stop
        engine.process=stale;engine.config=0x1234;engine.patches=[object()]
        engine._installed=True;engine._verified=True;engine.options['health']=True
        process_factory=Mock()
        with patch.object(trainer_engine,'find_game_pids',side_effect=[[27182],[27182]]), \
             patch.object(trainer_engine,'query_process_image',return_value=str(path)), \
             patch.object(trainer_engine,'Process',process_factory):
            with self.assertRaisesRegex(ValueError,'版本更新或程序来源不同'):
                engine.attach()
            status=engine.status()
        self.assertEqual(stale.close_calls,[False])
        self.assertTrue(old_stop.is_set())
        self.assertIsNot(engine._stop,old_stop)
        self.assertIsNone(engine.process);self.assertIsNone(engine.config)
        self.assertEqual(engine.patches,[]);self.assertFalse(engine._installed)
        self.assertTrue(all(value is None for value in engine.options.values()))
        self.assertFalse(status['tested'])
        self.assertTrue(all(not enabled for enabled in status['capabilities'].values()))
        self.assertEqual(status['connection_attempt']['pid'],27182)
        self.assertIn('版本更新或程序来源不同',status['connection_attempt']['failure_reason'])
        process_factory.assert_not_called()

    def test_process_discovery_failure_is_retained_in_status(self):
        engine=Engine();engine._verified=True
        finder=Mock(side_effect=[OSError('process scan failed'),[]])
        with patch.object(trainer_engine,'find_game_pids',finder):
            with self.assertRaisesRegex(OSError,'process scan failed'):engine.attach()
            status=engine.status()
        self.assertFalse(status['tested'])
        self.assertIn('process scan failed',status['last_error'])
        self.assertEqual(status['connection_attempt']['failure_reason'],status['last_error'])
        self.assertEqual(status['connection_attempt']['expected_sha256'],SHA256)

    def test_ambiguous_process_count_is_retained_in_status(self):
        engine=Engine()
        with patch.object(trainer_engine,'find_game_pids',return_value=[10,20]):
            with self.assertRaisesRegex(ValueError,'只运行一个'):
                engine.attach()
            status=engine.status()
        attempt=status['connection_attempt']
        self.assertEqual(attempt['candidate_pids'],[10,20])
        self.assertIsNone(attempt['pid'])
        self.assertTrue(attempt['expected_sha256'])
        self.assertEqual(status['last_error'],attempt['failure_reason'])


if __name__=='__main__':unittest.main(verbosity=2)
