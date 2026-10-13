"""Collect game-image diagnostics without opening game memory or installing hooks."""
from pathlib import Path
import argparse, datetime, hashlib, json, struct
from memory_win import find_game_pids, query_process_image

EXPECTED_SHA256='4d78fbfaa65b7d002d2208d02da809200f4a5723119521180e30100d6f1a75f2'

def inspect_file(path):
    path=Path(path)
    with path.open('rb') as image:
        header=image.read(64)
        if len(header)!=64 or header[:2]!=b'MZ':raise ValueError('程序不是有效的 PE 文件')
        pe=struct.unpack_from('<I',header,60)[0]
        if pe<64 or pe>path.stat().st_size-26:raise ValueError('PE 文件头无效')
        image.seek(pe);signature=image.read(26)
        if signature[:4]!=b'PE\0\0':raise ValueError('PE 标识无效')
        machine=struct.unpack_from('<H',signature,4)[0]
        magic=struct.unpack_from('<H',signature,24)[0]
        architecture={0x8664:'x64',0x14c:'x86'}.get(machine,hex(machine))
        image.seek(0);digest=hashlib.file_digest(image,'sha256').hexdigest()
    supported=machine==0x8664 and magic==0x20b and digest==EXPECTED_SHA256
    reason=('与适配的 Steam 28063/64位程序完全匹配' if supported else
            '当前运行的是32位程序；请在Steam选择 Win 64-bit version' if machine==0x14c else
            '64位程序哈希不匹配，需要进一步核对游戏来源或更新版本')
    return {'architecture':architecture,'pe_machine':hex(machine),'pe_magic':hex(magic),
            'sha256':digest,'expected_sha256':EXPECTED_SHA256,'supported':supported,'reason':reason}

def collect():
    result={'collector_version':'0.1.2','timestamp':datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'read_only':True,'game_memory_opened':False,'target_build':'28063','processes':[]}
    try:pids=find_game_pids()
    except Exception as error:
        result['error']=str(error);return result
    for pid in pids:
        row={'pid':pid}
        try:
            row['path']=query_process_image(pid);row.update(inspect_file(row['path']))
        except Exception as error:row['error']=str(error)
        result['processes'].append(row)
    return result

def main():
    parser=argparse.ArgumentParser(description='只读采集正在运行的暗黑地牢程序路径、位数与SHA-256')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();report=collect()
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    output=args.output or Path(__file__).resolve().parent/('trainer_connection_probe_'+stamp+'.json')
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not report['processes']:print('未检测到 Darkest.exe。请先打开游戏，再运行本检查。')
    for row in report['processes']:
        print(row.get('reason') or row.get('error') or '状态未知')
        if row.get('path'):print('程序：'+row['path'])
    print('只读报告已保存：'+str(output))
    return 0

if __name__=='__main__':raise SystemExit(main())
