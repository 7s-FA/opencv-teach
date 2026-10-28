"""Read-only action evidence archive. Also streamed over SSH without a runtime."""
import base64
import json
import re
from pathlib import Path

RUN=re.compile(r'[0-9a-f]{32}\Z')
FOLDER=re.compile(r'\d{8}-\d{6}-[0-9a-f]{32}(?:-retry-[0-9a-f]{32})?\Z')
PICTURES={role+suffix+'.jpg' for role in ('inspection','detection','last-received','jig-confirmed') for suffix in ('','-annotated')}


def read_json(path):
    if path.is_symlink() or path.stat().st_size>4*1024*1024:raise ValueError('기록 파일 형식 또는 크기를 확인하세요.')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('기록 형식을 확인하세요.')
    return value


def safe_file(parent,name):
    path=parent/name
    if path.is_symlink() or path.resolve().parent!=parent.resolve():raise ValueError('허용하지 않는 기록 경로입니다.')
    return path


def summary(key,doc,stamp):
    events=[e for e in doc.get('events',[]) if isinstance(e,dict)]
    event=next((e for e in reversed(events) if any(x in str(e.get('status','')) for x in ('_FAILED:','_REJECTED:'))),events[-1] if events else {})
    context=doc.get('context') or {};status=doc.get('status',event.get('status',''))
    return {'key':key,'at':doc.get('failed_at',doc.get('at',event.get('at',stamp))),
            'arm':context.get('arm',doc.get('arm',event.get('arm',''))),'command':context.get('command',doc.get('command',status.partition('_')[0])),
            'error':doc.get('error') or status,'run_id':context.get('run_id',doc.get('run_id','')),
            'image_count':sum(bool(i.get('file')) for i in doc.get('images',[]))}


def list_records(data,limit=200):
    data=Path(data);rows=[];seen=set();skipped=0
    archive=data/'diagnostics/action-failures'
    paths=[p for p in archive.glob('*/error.json') if FOLDER.fullmatch(p.parent.name) and not p.parent.is_symlink()]
    for path in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)[:limit]:
        try:
            doc=read_json(path);row=summary('evidence:'+path.parent.name,doc,path.stat().st_mtime)
            rows.append(row);seen.add(row['run_id'])
        except (OSError,ValueError,TypeError,KeyError,AttributeError):skipped+=1
    # Include older failures that predate automatic image capture.
    paths=[p for p in (data/'episode-cli-runs').glob('*.json') if RUN.fullmatch(p.stem) and not p.is_symlink()]
    for path in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)[:5000]:
        if path.stem in seen:continue
        try:
            doc=read_json(path);row=summary('run:'+path.stem,doc,path.stat().st_mtime);status=row['error']
            if not isinstance(status,str) or not any(x in status for x in ('_FAILED:','_REJECTED:')):continue
            row['run_id']=path.stem;rows.append(row)
            if len(rows)>=limit*2:break
        except (OSError,ValueError,TypeError,KeyError,AttributeError):skipped+=1
    def when(row):
        try:return float(row['at'])
        except (ValueError,TypeError):return 0.
    rows.sort(key=when,reverse=True)
    return {'records':rows[:limit],'skipped':skipped,'limit':limit}


def read_record(data,key):
    data=Path(data);kind,_,name=key.partition(':');images=[];warnings=[];run=None;inspection=None
    if kind=='evidence' and FOLDER.fullmatch(name):
        folder=safe_file(data/'diagnostics/action-failures',name)
        doc=read_json(safe_file(folder,'error.json'));context=doc.get('context') or {};run_id=context.get('run_id','')
        for entry in doc.get('images',[]):
            filename=entry.get('file')
            if not filename:continue
            if filename not in PICTURES:warnings.append('허용하지 않는 이미지 이름: '+str(filename));continue
            try:
                path=safe_file(folder,filename)
                if path.stat().st_size>10*1024*1024:raise ValueError('이미지 크기 제한 초과')
                raw=path.read_bytes()
                if not raw.startswith(b'\xff\xd8'):raise ValueError('JPEG 형식 오류')
                images.append({'name':filename,'role':entry.get('role'),'variant':entry.get('variant','raw'),'source_file':entry.get('source_file'),'age':entry.get('age_at_failure_seconds'),
                               'captured_monotonic':entry.get('captured_monotonic'),'data':base64.b64encode(raw).decode()})
            except (OSError,ValueError) as exc:warnings.append(filename+' · '+str(exc))
        old=context.get('inspection_log')
        if old and re.fullmatch(r'episode-inspection-\d+\.json',Path(str(old)).name):
            path=safe_file(data/'diagnostics',Path(old).name)
            try:inspection=read_json(path)
            except (OSError,ValueError) as exc:warnings.append('검사 로그 · '+str(exc))
    elif kind=='run' and RUN.fullmatch(name):
        run_id=name;doc=read_json(safe_file(data/'episode-cli-runs',name+'.json'));run=doc
    else:raise ValueError('올바르지 않은 오류 기록입니다.')
    if run is None and isinstance(run_id,str) and RUN.fullmatch(run_id):
        path=safe_file(data/'episode-cli-runs',run_id+'.json')
        try:run=read_json(path)
        except (OSError,ValueError) as exc:warnings.append('실행 로그 · '+str(exc))
    return {'key':key,'record':doc,'run':run,'inspection_log':inspection,'images':images,'warnings':warnings}


def request(data,message):
    if message.get('operation')=='list':return list_records(data)
    if message.get('operation')=='read':return read_record(data,message.get('key',''))
    raise ValueError('지원하지 않는 조회입니다.')


if __name__=='__main__':
    import sys
    try:print(json.dumps({'ok':True,'value':request(Path(sys.argv[1]).expanduser()/'data',json.load(sys.stdin))},ensure_ascii=False))
    except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
