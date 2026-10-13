"""User-triggered, backed-up save editing. Never runs while Darkest.exe is open."""
from pathlib import Path
import os, json, hashlib, shutil, uuid, datetime, tempfile
from save_engine import DsonDocument, DsonError
from memory_win import find_game_pids

ROOT=Path(__file__).resolve().parent
RESOURCES=('gold','bust','portrait','deed','crest')

def sha(data):return hashlib.sha256(data).hexdigest()

class SaveManager:
    def __init__(self,backup_root=None):
        self.backup_root=Path(backup_root) if backup_root else ROOT/'backups'/'edited-saves'
    def roots(self):
        steam=Path(r'F:\steam')
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Valve\Steam') as k:
                steam=Path(winreg.QueryValueEx(k,'SteamPath')[0])
        except (OSError,ImportError):pass
        paths=[]
        users=steam/'userdata'
        if users.is_dir():
            for user in users.iterdir():
                remote=user/'262060'/'remote'
                if remote.is_dir():paths.append(remote)
        # SHGetFolderPath respects redirected Documents folders.
        try:
            import ctypes
            buf=ctypes.create_unicode_buffer(32768)
            if ctypes.windll.shell32.SHGetFolderPathW(None,5,None,0,buf)==0:
                local=Path(buf.value)/'Darkest'
                if local.is_dir():paths.append(local)
        except (OSError,AttributeError):pass
        return paths
    def profiles(self):
        out=[]
        for root in self.roots():
            for p in sorted(root.glob('profile_*')):
                if not p.is_dir() or p.is_symlink():continue
                estate=p/'persist.estate.json';roster=p/'persist.roster.json'
                if not estate.is_file() or not roster.is_file():continue
                try:
                    d=DsonDocument.from_bytes(estate.read_bytes());r=DsonDocument.from_bytes(roster.read_bytes())
                    stress=list(r.find_named_fields('m_Stress'))
                    out.append({'path':str(p.resolve()),'label':p.name+' · '+str(root.parent.parent.name),'wallet':d.read_wallet(),'heroes':len(stress),'stress_values':[r.read_scalar(f.path,'float32') for f in stress]})
                except (OSError,DsonError):continue
        return out
    def checked_profile(self,path):
        p=Path(path).resolve()
        if str(p) not in {x['path'] for x in self.profiles()}:raise ValueError('请选择自动识别的有效存档')
        return p
    def guard(self):
        if find_game_pids():raise ValueError('请先完全退出游戏，再编辑或恢复存档')
    def backup(self,profile,updates):
        self.backup_root.mkdir(parents=True,exist_ok=True)
        ident=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        folder=self.backup_root/ident;folder.mkdir()
        # Copy the whole slot, including game's own backup, before any edit.
        for p in profile.rglob('*'):
            if p.is_symlink():raise ValueError('存档中有符号链接，拒绝修改')
        shutil.copytree(profile,folder/'profile')
        metadata={'id':ident,'profile':str(profile),'created_at':datetime.datetime.now().isoformat(),'files':{name:{'before':sha((profile/name).read_bytes()),'after':sha(data)} for name,data in updates.items()}}
        (folder/'manifest.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        return ident,folder,metadata
    @staticmethod
    def atomic(path,data):
        fd,name=tempfile.mkstemp(prefix='.ddtrainer-',suffix='.tmp',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def atomic_batch(self,profile,updates,originals,expected_hashes=None):
        """Replace several save files as one recoverable transaction.

        Each replacement is atomic on its own. If a later replacement fails,
        restore every file this batch attempted to change, including a file
        whose atomic() call replaced it before raising. Never overwrite a file
        that no longer matches either the transaction's original or target.
        """
        expected_hashes=expected_hashes or {name:sha(data) for name,data in originals.items()}
        attempted=[]
        try:
            for name,data in updates.items():
                path=profile/name
                if sha(path.read_bytes())!=expected_hashes[name]:
                    raise ValueError('存档被其他程序改动，操作已取消')
                attempted.append(name)
                self.atomic(path,data)
        except Exception as write_error:
            rollback_errors=[]
            for name in reversed(attempted):
                path=profile/name
                original=originals[name]
                target=updates[name]
                try:
                    current=path.read_bytes()
                    current_hash=sha(current)
                    if current_hash==sha(original):
                        continue
                    if current_hash!=sha(target):
                        rollback_errors.append(f'{name}: 内容已被其他程序改动，未覆盖')
                        continue
                    try:
                        self.atomic(path,original)
                    except Exception as rollback_error:
                        # atomic() may report an error after os.replace has
                        # completed. Trust the resulting bytes if they verify.
                        try:
                            if sha(path.read_bytes())==sha(original):
                                continue
                        except OSError:
                            pass
                        rollback_errors.append(f'{name}: {rollback_error}')
                        continue
                    if sha(path.read_bytes())!=sha(original):
                        rollback_errors.append(f'{name}: 回滚后校验失败')
                except Exception as rollback_error:
                    rollback_errors.append(f'{name}: {rollback_error}')
            if rollback_errors:
                detail='；'.join(rollback_errors)
                raise RuntimeError(f'写入失败，且事务回滚未能完全完成：{detail}') from write_error
            raise
    def edit(self,path,resources,clear_stress):
        self.guard();p=self.checked_profile(path)
        if not isinstance(resources,dict) or set(resources)-set(RESOURCES):raise ValueError('资源字段无效')
        updates={};changes=[]
        if resources:
            d=DsonDocument.from_bytes((p/'persist.estate.json').read_bytes())
            for key,value in resources.items():
                if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=99999999:raise ValueError('资源数量必须为0～99999999的整数')
                changes.append(d.write_wallet(key,value))
            updates['persist.estate.json']=d.to_bytes()
        if clear_stress:
            d=DsonDocument.from_bytes((p/'persist.roster.json').read_bytes())
            changed=d.write_named_float('m_Stress',0.0)
            if not changed:raise ValueError('未找到英雄压力字段，未改动存档')
            changes.extend(changed);updates['persist.roster.json']=d.to_bytes()
        if not updates:raise ValueError('没有选择修改内容')
        ident,folder,meta=self.backup(p,updates)
        originals={name:(folder/'profile'/name).read_bytes() for name in updates}
        self.guard()
        self.atomic_batch(p,updates,originals,{name:meta['files'][name]['before'] for name in updates})
        return {'success':True,'backup_id':ident,'backup_path':str(folder),'changed_fields':len(changes),'message':'存档修改完成；备份 '+ident}
    def restore(self,ident):
        self.guard()
        if not isinstance(ident,str) or not ident or Path(ident).name!=ident:raise ValueError('备份编号无效')
        folder=(self.backup_root/ident).resolve()
        if folder.parent!=self.backup_root.resolve():raise ValueError('备份路径无效')
        meta=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        p=self.checked_profile(meta['profile']);updates={}
        for name,hashes in meta['files'].items():
            if name not in ('persist.estate.json','persist.roster.json'):raise ValueError('备份文件名无效')
            data=(folder/'profile'/name).read_bytes()
            if sha(data)!=hashes['before']:raise ValueError('备份内容校验失败')
            if sha((p/name).read_bytes())!=hashes['after']:raise ValueError('存档已产生新进度；请手动选择备份恢复，防止覆盖新进度')
            updates[name]=data
        originals={name:(p/name).read_bytes() for name in updates}
        expected_hashes={name:meta['files'][name]['after'] for name in updates}
        self.guard()
        self.atomic_batch(p,updates,originals,expected_hashes)
        return {'success':True,'message':'已恢复修改前的数据','backup_id':ident}

_default_manager=SaveManager()
def save_profiles():return _default_manager.profiles()
def edit_save(path,resources,clear_stress=False):return _default_manager.edit(path,resources,clear_stress)
def restore_backup(backup_id):return _default_manager.restore(backup_id)
