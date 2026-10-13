"""Build-28063 x64 DD1 campaign trainer. Rejects unknown binaries before writing."""
from __future__ import annotations
import struct,hashlib,threading,math,json,datetime,shutil
from pathlib import Path
import os
from memory_win import Process,Patch,MemoryError,absolute_jump,find_game_pids,query_process_image
from hooks import CONFIG,damage_stub,stress_stub,inventory_stub,near_jump
from save_runtime import SaveManager

SHA256='4d78fbfaa65b7d002d2208d02da809200f4a5723119521180e30100d6f1a75f2'
KEYS=('health','stress','light','items','gold','bust','portrait','deed','crest','one_hit','damage','defense','shop','speed')
BOOLS={'health','stress','light','items','one_hit','shop'}
WALLET={'gold','bust','portrait','deed','crest'}
ROOT=Path(__file__).resolve().parent
SITES=[
    ('hero_damage',0x47E570,bytes.fromhex('48 8b c4 48 89 58 10 4c 89 48 20 55 56 57')),
    ('monster_damage',0x5F0930,bytes.fromhex('48 8b c4 55 53 56 57 41 54 41 55 41 56 41 57')),
    ('stress',0x47BFB0,bytes.fromhex('48 8b c4 48 89 58 18 48 89 68 20 56 57 41 56')),
    ('inventory',0x5D5BFE,bytes.fromhex('2b d7 89 13 85 d2')),
]

def type_hash(s):
    out=0
    for ch in s.encode('ascii'):out=(out*53+ch)&0xffffffff
    return out
def valid_ptr(p):return 0x10000<=p<0x800000000000

def pe_architecture(data):
    """Return the PE machine architecture, rejecting truncated/non-PE files."""
    if len(data)<0x40 or data[:2]!=b'MZ':raise ValueError('文件没有有效的 DOS/PE 头')
    pe_offset=struct.unpack_from('<I',data,0x3c)[0]
    if pe_offset<0x40 or pe_offset>len(data)-26 or data[pe_offset:pe_offset+4]!=b'PE\0\0':
        raise ValueError('文件没有有效或完整的 PE 头')
    machine=struct.unpack_from('<H',data,pe_offset+4)[0]
    optional_size=struct.unpack_from('<H',data,pe_offset+20)[0]
    optional_offset=pe_offset+24
    if optional_size<2 or optional_offset+optional_size>len(data):
        raise ValueError('PE Optional Header 不完整')
    optional_magic=struct.unpack_from('<H',data,optional_offset)[0]
    if optional_magic not in (0x10b,0x20b):raise ValueError('PE Optional Header 位宽标记无效')
    if machine==0x8664 and optional_magic!=0x20b:raise ValueError('AMD64 文件的 PE Optional Header 不是 PE32+')
    if machine==0x14c and optional_magic!=0x10b:raise ValueError('x86 文件的 PE Optional Header 不是 PE32')
    return {0x8664:'AMD64',0x14c:'x86',0xaa64:'ARM64'}.get(machine,f'unknown (machine 0x{machine:04x})')

def _attempt_record(pid=None,pids=()):
    return {'pid':pid,'candidate_pids':list(pids),'actual_path':None,'verified_path':None,'process_path':None,'architecture':None,
            'actual_sha256':None,'expected_sha256':SHA256,'success':False,'failure_reason':''}

def _same_path(left,right):
    return os.path.normcase(os.path.normpath(str(left)))==os.path.normcase(os.path.normpath(str(right)))

class Engine:
    def __init__(self):
        self.process=None;self.config=None;self.patches=[];self.options={k:None for k in KEYS}
        self._lock=threading.RLock();self._stop=threading.Event();self._worker=None
        self._speed_original=None;self.last_error='';self._installed=False;self.saves=SaveManager()
        self.backup_path=None;self._verified=False;self.connection_attempt=None
    def status(self):
        with self._lock:
            p=self.process;connected=bool(p and p.alive() and self._installed)
            return {'running':bool(find_game_pids()),'connected':connected,'version':'28063 (64-bit)','build':'25463639','capabilities':{k:self._verified for k in KEYS},'tested':self._verified,'gameplay_verified':False,'options':dict(self.options),'pid':p.pid if connected else None,'sha256':SHA256 if self._verified else None,'backup_path':self.backup_path,'last_error':self.last_error,'connection_attempt':dict(self.connection_attempt) if self.connection_attempt else None,'message':self.last_error or ('已连接；在城镇/地牢中启用所需功能' if connected else '请启动64位游戏，再点击连接游戏')}
    def attach(self):
        with self._lock:
            if self.process and self._installed:
                try:
                    if self.process.alive():return {'success':True,'message':'游戏已连接'}
                except Exception:
                    pass
            old_process=self.process
            self._verified=False;self.last_error=''
            self.connection_attempt=_attempt_record(pid=getattr(old_process,'pid',None))
            # Give any worker from the old connection its own stopped event so
            # it cannot wake against a newly connected process.
            self._stop.set();self._stop=threading.Event();self._worker=None
            if old_process is not None:
                try:old_process.close(free=False)
                except Exception as exc:
                    reason=str(exc).strip() or repr(exc) or exc.__class__.__name__
                    self.last_error='无法关闭上次游戏连接：'+reason
                    self.connection_attempt['failure_reason']=self.last_error
                    raise RuntimeError(self.last_error) from exc
            self.process=None;self.config=None;self.patches=[];self._installed=False
            self.options={k:None for k in KEYS};self._speed_original=None
            try:pids=find_game_pids()
            except Exception as exc:
                reason=str(exc).strip() or repr(exc) or exc.__class__.__name__
                self.last_error=reason;self.connection_attempt['failure_reason']=reason
                raise
            self.connection_attempt['candidate_pids']=list(pids)
            if len(pids)!=1:
                reason='请只运行一个64位 Darkest.exe 实例'
                self.connection_attempt['pid']=pids[0] if len(pids)==1 else None
                self.connection_attempt['failure_reason']=reason;self.last_error=reason
                raise ValueError(reason)
            pid=pids[0];self.connection_attempt['pid']=pid
            p=None;installed=[]
            try:
                image_path=str(query_process_image(pid))
                self.connection_attempt['actual_path']=image_path
                self.connection_attempt['verified_path']=image_path
                image=Path(image_path).read_bytes()
                actual=hashlib.sha256(image).hexdigest()
                self.connection_attempt['actual_sha256']=actual
                try:architecture=pe_architecture(image)
                except ValueError as exc:
                    self.connection_attempt['architecture']='unknown'
                    raise ValueError(f'无法验证 Darkest.exe 的 PE 架构：{exc}；未写入任何指令。') from exc
                self.connection_attempt['architecture']=architecture
                if architecture!='AMD64':
                    if architecture=='x86':
                        raise ValueError('检测到 32 位 x86 Darkest.exe；当前修改器只支持 Steam Win64 版本，请确认安装并启动 Win64 游戏。未写入任何指令。')
                    raise ValueError(f'检测到不支持的 PE 架构 {architecture}；当前修改器只支持 AMD64 Steam Win64 版本。未写入任何指令。')
                if actual!=SHA256:
                    raise ValueError('检测到 AMD64 64 位程序，但 SHA-256 与已适配的 Steam build 25463639 不符；可能是游戏版本更新或程序来源不同。未写入任何指令。')
                # Only open the process for memory access after the read-only
                # image checks pass. Verify the module path and file again to
                # close the gap between the initial check and process opening.
                p=Process(pid)
                process_path=str(getattr(p,'path','') or '') or None
                self.connection_attempt['pid']=getattr(p,'pid',pid)
                self.connection_attempt['process_path']=process_path
                if not process_path or not _same_path(image_path,process_path):
                    if process_path:self.connection_attempt['actual_path']=process_path
                    raise ValueError('只读进程路径与已验证的程序路径不一致；为避免连接错误进程，未读取或写入游戏内存。')
                current_hash=hashlib.sha256(Path(process_path).read_bytes()).hexdigest()
                if current_hash!=actual:
                    self.connection_attempt['actual_sha256']=current_hash
                    raise ValueError('Darkest.exe 在校验期间发生变化；请重新启动游戏后重试，未读取或写入游戏内存。')
                for name,rva,original in SITES:
                    if p.read(p.base+rva,len(original))!=original:raise ValueError(name+' 指令不匹配，可能已开启其他修改器')
                if p.read(p.base+0x2A5C08B,1)!=b'\0':raise ValueError('请退出竞技场或特殊调试模式，进入常规战役后连接')
                self._backup_saves()
                config=p.allocate(4096,near=p.base);code=p.allocate(4096,near=p.base)
                defaults=bytearray(128);struct.pack_into('<ff',defaults,16,1.0,1.0);struct.pack_into('<f',defaults,36,1.0);p.write(config,defaults)
                hero=p.base+0xE7C638;monster=p.base+0xE9B020
                for index,(name,rva,original) in enumerate(SITES):
                    site=p.base+rva;cave=code+index*512;ret=site+len(original)
                    guard=p.base+0x2A5C08B
                    if name=='hero_damage':stub=damage_stub(config,hero,0,original,ret,guard)
                    elif name=='monster_damage':stub=damage_stub(config,0,monster,original,ret,guard)
                    elif name=='stress':stub=stress_stub(config,hero,original,ret,guard)
                    else:stub=inventory_stub(config,ret,guard)
                    if len(stub)>512:raise ValueError('跳板尺寸校验失败')
                    p.write(cave,stub)
                    jump=near_jump(site,cave,len(original)) if name=='inventory' else absolute_jump(cave)+b'\x90'*(len(original)-14)
                    installed.append(Patch(site,original,jump))
                with p.paused():
                    applied=[]
                    try:
                        for patch in installed:patch.install(p);applied.append(patch)
                    except Exception:
                        for patch in reversed(applied):patch.restore(p)
                        raise
                self.process=p;self.config=config;self.patches=installed;self._installed=True;self._verified=True
                self.last_error='';self.options={k:None for k in KEYS};self._stop.clear()
                self.connection_attempt['success']=True;self.connection_attempt['failure_reason']=''
                self._worker=threading.Thread(target=self._loop,args=(self._stop,),name='DD1 trainer',daemon=True);self._worker.start()
                return {'success':True,'message':'已连接本机 28063/64位版本；修改前存档已备份','backup_path':self.backup_path}
            except Exception as exc:
                reason=str(exc).strip() or repr(exc) or exc.__class__.__name__
                # A resumed game thread may briefly be inside a cave after rollback.
                if p is not None:
                    try:
                        if installed and p.alive():
                            with p.paused():
                                for patch in reversed(installed):
                                    if p.read(patch.address,len(patch.replacement))==patch.replacement:patch.restore(p)
                    except Exception as cleanup_error:
                        reason+=f'；清理连接失败：{cleanup_error}'
                    try:p.close(free=False)
                    except Exception as close_error:reason+=f'；关闭进程句柄失败：{close_error}'
                self.last_error=reason
                self.connection_attempt['failure_reason']=reason
                raise
    def _backup_saves(self):
        profiles=self.saves.profiles()
        if not profiles:return
        folder=ROOT/'backups'/('session-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        folder.mkdir(parents=True)
        listing=[]
        for i,prof in enumerate(profiles):
            path=Path(prof['path']);dst=folder/(str(i)+'-'+path.name)
            shutil.copytree(path,dst,symlinks=True)
            listing.append({'source':str(path),'backup':str(dst)})
        (folder/'manifest.json').write_text(json.dumps({'sha256':SHA256,'profiles':listing},ensure_ascii=False,indent=2),encoding='utf-8')
        self.backup_path=str(folder)
    def set_option(self,key,value):
        with self._lock:
            if key not in KEYS:raise ValueError('未知功能')
            p=self.process
            if not p or not p.alive() or not self._installed:raise ValueError('请先连接游戏')
            if p.read(p.base+0x2A5C08B,1)!=b'\0':raise ValueError('功能仅用于常规单人战役')
            if value is not None:
                if key in BOOLS:
                    if value is not True:raise ValueError('开关值无效')
                elif key in WALLET:
                    if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=99999999:raise ValueError('资源必须是0～99999999的整数')
                else:
                    value=float(value)
                    maximum=10.0 if key=='speed' else 100.0
                    if not math.isfinite(value) or not 0.1<=value<=maximum:raise ValueError('倍率范围应为0.1～'+str(maximum))
            if key=='speed':
                if value is not None and self._speed_original is None:self._speed_original=p.f32(p.base+0x2A5C07C)
                if value is None and self._speed_original is not None:
                    p.write(p.base+0x2A5C07C,struct.pack('<f',self._speed_original));self._speed_original=None
            if key in CONFIG:
                if key in ('damage','defense','speed'):payload=struct.pack('<f',float(value) if value is not None else 1.0)
                else:payload=struct.pack('<I',1 if value is not None else 0)
                p.write(self.config+CONFIG[key],payload)
            self.options[key]=value;self._tick()
            return {'success':True,'message':key+(' 开启' if value is not None else ' 关闭')}
    def _loop(self,stop_event=None):
        stop=stop_event or self._stop
        while not stop.wait(0.1):
            with self._lock:
                if stop.is_set():break
                try:
                    if not self.process or not self.process.alive():break
                    self._tick()
                except Exception as exc:
                    self.last_error=str(exc)
                    # Stop all modifications on a read/write failure, keep rollback available.
                    self.options={k:None for k in KEYS}
                    try:
                        self.process.write(self.config,bytes(16));self.process.write(self.config+24,bytes(12))
                        self.process.write(self.config+16,struct.pack('<ff',1,1))
                        if self._speed_original is not None:self.process.write(self.process.base+0x2A5C07C,struct.pack('<f',self._speed_original));self._speed_original=None
                    except Exception:pass
                    break
    def _tick(self):
        p=self.process
        if not p:return
        if p.read(p.base+0x2A5C08B,1)!=b'\0':
            # No writes to campaign objects while another roster/mode is active.
            if any(v is not None for v in self.options.values()):raise ValueError('已进入特殊模式，全部功能已关闭；请回到单人战役')
            return
        if self.options['speed'] is not None:p.write(p.base+0x2A5C07C,struct.pack('<f',self.options['speed']))
        campaign=p.u64(p.base+0x117D9B8);raid=p.u64(p.base+0x117DB48)
        if self.options['light'] is not None and valid_ptr(raid) and p.u32(raid+0x4B20)!=0:
            current=p.f32(raid+0x5F14)
            if not math.isfinite(current) or not 0<=current<=100:raise MemoryError('火把字段不符合已验证结构')
            if current!=100:p.write(raid+0x5F14,struct.pack('<f',100))
        if any(self.options[k] is not None for k in WALLET) and valid_ptr(campaign):
            begin=p.u64(campaign+0xE70);end=p.u64(campaign+0xE78)
            if begin==end:return
            if not valid_ptr(begin) or end<begin or (end-begin)%72 or (end-begin)//72>128:raise MemoryError('资源列表结构校验失败')
            for a in range(begin,end,72):
                data=p.read(a,72);raw=data[4:68].split(b'\0')[0]
                try:name=raw.decode('ascii')
                except UnicodeDecodeError:raise MemoryError('资源类型校验失败')
                if name in WALLET and self.options[name] is not None:
                    if struct.unpack_from('<I',data,68)[0]!=type_hash(name):raise MemoryError('资源类型哈希校验失败')
                    amount=struct.unpack_from('<i',data)[0]
                    if not 0<=amount<=2000000000:raise MemoryError('资源数量字段校验失败')
                    if amount!=self.options[name]:p.write(a,struct.pack('<i',self.options[name]))
        if self.options['stress'] is not None:
            heroes=set()
            if valid_ptr(campaign):
                for entry in self._pointer_list(campaign+0x20,campaign+0x28,512):
                    if valid_ptr(entry):heroes.add(entry+8)
            if valid_ptr(raid):heroes.update(self._pointer_list(raid+0x48D0,raid+0x48D8,128))
            for hero in heroes:
                if not valid_ptr(hero) or p.u64(hero)!=p.base+0xE7C638:continue
                stress=p.f32(hero+0x128C)
                if not math.isfinite(stress) or not -0.01<=stress<=1000:raise MemoryError('压力字段校验失败')
                if stress!=0 and p.u64(hero)==p.base+0xE7C638:p.write(hero+0x128C,bytes(4))
    def _pointer_list(self,begin_address,end_address,limit):
        p=self.process;begin=p.u64(begin_address);end=p.u64(end_address)
        if begin==end:return []
        if not valid_ptr(begin) or end<begin or (end-begin)%8 or (end-begin)//8>limit:raise MemoryError('英雄列表结构校验失败')
        return struct.unpack('<'+'Q'*((end-begin)//8),p.read(begin,end-begin))
    def detach(self):
        self._stop.set()
        # Do not join with the state lock held; the background worker may be waiting for it.
        worker=self._worker
        if worker and worker is not threading.current_thread():worker.join(timeout=1.0)
        with self._lock:
            p=self.process
            if not p:return {'success':True,'message':'已断开'}
            if p.alive():
                p.write(self.config,bytes(16));p.write(self.config+16,struct.pack('<ff',1,1));p.write(self.config+24,bytes(12))
                if self._speed_original is not None:p.write(p.base+0x2A5C07C,struct.pack('<f',self._speed_original))
                with p.paused():
                    for patch in reversed(self.patches):patch.restore(p)
            # Retain tiny remote pages until process exit; a suspended thread may be in a stub.
            p.close(free=False);self.process=None;self.config=None;self.patches=[];self._installed=False
            self._speed_original=None;self.options={k:None for k in KEYS};self._worker=None
            return {'success':True,'message':'已关闭所有功能并恢复原游戏指令'}
    def save_profiles(self):return self.saves.profiles()
    def edit_save(self,path,resources,clear_stress=False):return self.saves.edit(path,resources,clear_stress)
    def restore_backup(self,ident):return self.saves.restore(ident)
