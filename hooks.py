"""Small auditable x64 stubs. No compiler, DLL injection, or download needed."""
import struct
from memory_win import absolute_jump

class Code:
    def __init__(self):self.b=bytearray();self.labels={};self.fixups=[]
    def emit(self,h):self.b.extend(bytes.fromhex(h));return self
    def raw(self,b):self.b.extend(b);return self
    def imm64(self,b):self.b.extend(struct.pack('<Q',b));return self
    def label(self,s):self.labels[s]=len(self.b);return self
    def jump(self,op,label):
        self.emit(op);p=len(self.b);self.b.extend(b'\0'*4);self.fixups.append((p,label));return self
    def finish(self):
        for p,label in self.fixups:struct.pack_into('<i',self.b,p,self.labels[label]-(p+4))
        return bytes(self.b)

# Config layout uses uint32 flags and float32 multipliers. All default flags=0.
CONFIG={'health':0,'stress':4,'one_hit':8,'damage':16,'defense':20,'items':24,'shop':28,'light':32,'speed':36}

def damage_stub(config,hero_vtable,monster_vtable,stolen,return_address,guard_address=0):
    c=Code().emit('9c 50 41 53') # preserve flags, RAX, R11
    if guard_address:
        c.emit('49 bb').imm64(guard_address).emit('41 80 3b 00').jump('0f 85','done')
    c.emit('48 83 ec 10 0f 11 04 24 0f 57 c0 0f 2f d0 0f 10 04 24 48 8d 64 24 10')
    c.jump('0f 86','done') # ignore zero, negative, unordered (healing is unaffected)
    c.emit('49 bb').imm64(config)
    c.emit('48 b8').imm64(hero_vtable).emit('48 39 01').jump('0f 85','monster')
    c.emit('41 83 3b 00').jump('0f 84','defense')
    c.emit('0f 57 d2').jump('e9','done')
    c.label('defense').emit('f3 41 0f 5e 53 14').jump('e9','done') # XMM2 / defense
    c.label('monster').emit('48 b8').imm64(monster_vtable).emit('48 39 01').jump('0f 85','done')
    c.emit('41 83 7b 08 00').jump('0f 84','multiply')
    c.emit('b8 80 96 18 4b 66 0f 6e d0').jump('e9','done') # 10,000,000.0f
    c.label('multiply').emit('f3 41 0f 59 53 10')
    c.label('done').emit('41 5b 58 9d').raw(stolen).raw(absolute_jump(return_address))
    return c.finish()

def stress_stub(config,hero_vtable,stolen,return_address,guard_address=0):
    c=Code().emit('9c 50 41 53')
    if guard_address:
        c.emit('49 bb').imm64(guard_address).emit('41 80 3b 00').jump('0f 85','done')
    c.emit('49 bb').imm64(config)
    c.emit('41 83 7b 04 00').jump('0f 84','done')
    c.emit('48 b8').imm64(hero_vtable).emit('48 39 01').jump('0f 85','done')
    c.emit('c7 81 8c 12 00 00 00 00 00 00 0f 57 d2')
    c.label('done').emit('41 5b 58 9d').raw(stolen).raw(absolute_jump(return_address))
    return c.finish()

def inventory_stub(config,return_address,guard_address=0):
    # At the site: RBP=inventory, RBX=item, EDX=old count, EDI=min(count,request).
    # Skip only the subtraction. The caller still handles flags/events/requests.
    c=Code().emit('9c 41 53')
    if guard_address:
        c.emit('49 bb').imm64(guard_address).emit('41 80 3b 00').jump('0f 85','normal')
    c.emit('49 bb').imm64(config)
    c.emit('81 7d 20 44 37 07 01').jump('0f 84','raid')
    c.emit('81 7d 20 69 40 f1 72').jump('0f 84','shop')
    c.emit('81 7d 20 4d c5 54 18').jump('0f 84','shop').jump('e9','normal')
    c.label('raid').emit('41 83 7b 18 00').jump('0f 85','freeze').jump('e9','normal')
    c.label('shop').emit('41 83 7b 1c 00').jump('0f 85','freeze')
    c.label('normal').emit('41 5b 9d 2b d7 89 13 85 d2').raw(absolute_jump(return_address))
    c.label('freeze').emit('41 5b 9d 89 13 85 d2').raw(absolute_jump(return_address))
    return c.finish()

def near_jump(source,target,size=6):
    delta=target-(source+5)
    if not -(1<<31)<=delta<(1<<31):raise ValueError('库存跳板必须在指令的2GB范围内')
    return b'\xe9'+struct.pack('<i',delta)+b'\x90'*(size-5)
