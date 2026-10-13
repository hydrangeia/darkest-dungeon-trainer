import os,sys,ctypes as C,struct,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from memory_win import Process
from hooks import damage_stub,stress_stub

@unittest.skipUnless(os.name=='nt' and C.sizeof(C.c_void_p)==8,'Windows x64 only')
class DamageHookTests(unittest.TestCase):
    def setUp(self):
        self.p=Process(os.getpid());self.config=self.p.allocate();self.code=self.p.allocate()
        self.buf=(C.c_uint64*1)();self.buf[0]=0x11111000
        defaults=bytearray(128);struct.pack_into('<ff',defaults,16,2.0,2.0)
        self.p.write(self.config,defaults)
        self.p.write(self.code,damage_stub(self.config,0x11111000,0x22222000,b'\x0f\x28\xc2\xc3',0))
        self.fn=C.CFUNCTYPE(C.c_float,C.c_void_p,C.c_void_p,C.c_float)(self.code)
    def tearDown(self):self.p.close()
    def apply(self,amount):return self.fn(C.addressof(self.buf),None,amount)
    def test_defense_and_healing(self):
        self.assertEqual(self.apply(20),10);self.assertEqual(self.apply(-20),-20);self.assertEqual(self.apply(0),0)
    def test_health(self):
        self.p.write(self.config,struct.pack('<I',1));self.assertEqual(self.apply(20),0)
    def test_monster_damage_and_onehit(self):
        self.buf[0]=0x22222000;self.assertEqual(self.apply(20),40)
        self.p.write(self.config+8,struct.pack('<I',1));self.assertEqual(self.apply(20),10000000)
    def test_unrelated_actor(self):
        self.buf[0]=0x33333000;self.assertEqual(self.apply(20),20)
    def test_mode_guard_keeps_damage_unmodified(self):
        guard=(C.c_ubyte*1)(1)
        self.p.write(self.code,damage_stub(self.config,0x11111000,0x22222000,b'\x0f\x28\xc2\xc3',0,C.addressof(guard)))
        self.assertEqual(self.apply(20),20)
    def test_stress_zero_and_disabled_paths(self):
        hero=C.create_string_buffer(0x1400);C.c_uint64.from_buffer(hero).value=0x11111000
        C.c_float.from_buffer(hero,0x128c).value=123.5
        self.p.write(self.code,stress_stub(self.config,0x11111000,b'\x0f\x28\xc2\xc3',0))
        fn=C.CFUNCTYPE(C.c_float,C.c_void_p,C.c_void_p,C.c_float)(self.code)
        self.assertEqual(fn(C.addressof(hero),None,25),25)
        self.assertEqual(C.c_float.from_buffer(hero,0x128c).value,123.5)
        self.p.write(self.config+4,struct.pack('<I',1))
        self.assertEqual(fn(C.addressof(hero),None,25),0)
        self.assertEqual(C.c_float.from_buffer(hero,0x128c).value,0)

if __name__=='__main__':unittest.main()
