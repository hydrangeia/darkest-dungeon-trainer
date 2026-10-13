"""Prepare a portable testing release; excludes local saves and research files."""
from pathlib import Path
import datetime, hashlib, json, os, shutil, subprocess, zipfile

root = Path(__file__).resolve().parent
dist = root / 'dist'
names = ('trainer_ui.py','trainer_engine.py','memory_win.py','hooks.py','save_engine.py','save_runtime.py','probe_game.py','sl_runtime.py','autosave_runtime.py')
for name in names:
    shutil.copy2(root/name, dist/'app'/name)
for source, target in (('README.md','使用说明.md'),('测试清单.md','测试清单.md')):
    shutil.copy2(root/source, dist/target)
shutil.copy2(root/'游戏程序检查.cmd',dist/'游戏程序检查.cmd')
shutil.copy2(root/'LICENSE',dist/'LICENSE')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

test_statistics=json.loads((root/'test_results.json').read_text(encoding='utf-8'))
test_statistics['skip_reasons']=[
    'Windows symlink creation unavailable (WinError 1314)' if 'WinError 1314' in reason else reason
    for reason in test_statistics.get('skip_reasons',[])
]

report = {
    'release':'0.3.0 testing', 'generated_at':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
    'connection_diagnostics':'Actual executable path, PE architecture, hashes and failure reason retained; no validation relaxed.',
    'version_label_correction':{'previous_label':'27987','correct_label':'28063',
                               'exe_sha256_unchanged':True,'memory_offsets_unchanged':True},
    'target':{'game_build':'28063','steam_build_id':'25463639','platform':'Steam Windows x64',
              'exe_sha256':'4d78fbfaa65b7d002d2208d02da809200f4a5723119521180e30100d6f1a75f2'},
    'implemented_options':14,
    'save_load':{'whole_profile':True,'named_checkpoints':True,'persistent_history':True,
                 'safety_checkpoint_before_restore':True,'requires_game_closed':True,
                 'automatic_disk_snapshots':True,'auto_backup_while_game_running':True,
                 'select_by_days_ago':True,'select_by_game_week':True,
                 'portable_create_restore_undo':'passed', 'ui_backend_integration':'passed',
                 'test_scope':'Workspace fixtures; real game save files were not written by SL tests.'},
    'automated_tests':test_statistics,
    'ui_self_test':'passed: 14 options, 15 hotkey mappings, defaults',
    'portable_runtime':'passed: imports, UI self-test and Tk smoke',
    'live_process_checks':{'tested_on':'2026-10-08','component':'memory trainer',
                           'attach':'passed','speed_memory_enable_disable':'passed: 1.0 to 2.5 to 1.0',
                           'patch_restore':'passed: original bytes at all 4 sites restored'},
    'gameplay_verified':False,
    'pending':['full combat','town resources and stress UI','torch UI','consumable use and stack merge',
               'shop purchases','global hotkey activation in foreground game'],
    'inventory_test_limit':'merge helper is a ret stub in native function-copy tests',
    'privacy':'Release contains no game saves, backups or account identifiers.',
}
(dist/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

payload = sorted(p for p in dist.rglob('*') if p.is_file() and p != dist/'SHA256.json')
for path in payload:
    relative = path.relative_to(dist)
    if any(part.lower() in ('backups','__pycache__','research','tests') for part in relative.parts):
        raise RuntimeError('Private/development file in release: '+str(relative))
    if path.name == 'autosave_settings.json' or path.name.startswith('trainer_diagnostics_'):
        raise RuntimeError('Local settings or diagnostic file in release: '+str(relative))
manifest = {str(p.relative_to(dist)).replace('\\','/'):digest(p) for p in payload}
(dist/'SHA256.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')

archive = root/'DarkestDungeon-28063-Trainer-v0.3.0-AutoSL.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
    for path in sorted(p for p in dist.rglob('*') if p.is_file()):
        bundle.write(path, Path('DarkestDungeon-28063-Trainer')/path.relative_to(dist))
with zipfile.ZipFile(archive) as bundle:
    if bundle.testzip() is not None:
        raise RuntimeError('Archive integrity check failed')
print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'sha256':digest(archive),
                  'files':len(manifest)+1},ensure_ascii=False))
