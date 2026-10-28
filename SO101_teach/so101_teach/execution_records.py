"""Incremental durable run records, with unchanged materialized JSON output.

Only committed journal revisions are visible. Files are data, never executable
objects; legacy snapshots remain readable without a journal.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

MAX_LINE=32*1024*1024
MAX_JOURNAL=256*1024*1024


def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()


def changes(old,new,path=()):
    if old is new:return []
    if type(old) is not type(new):return [['set',list(path),new]]
    if isinstance(new,dict):
        rows=[['delete',list(path)+( [key]),None] for key in old.keys()-new.keys()]
        for key,value in new.items():
            rows.extend(changes(old[key],value,(*path,key)) if key in old else [['set',[*path,key],value]])
        return rows
    if isinstance(new,list):
        rows=[]
        for index in range(min(len(old),len(new))):rows.extend(changes(old[index],new[index],(*path,index)))
        if len(new)>len(old):rows.append(['append',list(path),new[len(old):]])
        elif len(new)<len(old):rows.append(['truncate',list(path),len(new)])
        return rows
    return [] if old==new else [['set',list(path),new]]


def apply(value,operation):
    if not isinstance(operation,list) or len(operation)!=3:raise ValueError('실행 기록 변경 형식 오류')
    kind,path,item=operation
    if not isinstance(path,list) or kind not in ('set','delete','append','truncate'):raise ValueError('실행 기록 변경 형식 오류')
    def visit(current,remaining):
        if not remaining:
            if kind=='set':return item
            if kind=='append' and isinstance(current,list) and isinstance(item,list):return current+item
            if kind=='truncate' and isinstance(current,list) and type(item) is int and 0<=item<=len(current):return current[:item]
            raise ValueError('실행 기록 변경 대상 오류')
        key,*rest=remaining
        if isinstance(current,dict) and isinstance(key,str):result=dict(current)
        elif isinstance(current,list) and type(key) is int and 0<=key<len(current):result=list(current)
        else:raise ValueError('실행 기록 변경 경로 오류')
        if not rest and kind=='delete':
            if isinstance(result,dict) and key not in result:raise ValueError('실행 기록 삭제 경로 오류')
            del result[key]
        else:result[key]=visit(current.get(key) if isinstance(current,dict) else current[key],rest)
        return result
    return visit(value,path)


class RunJournal:
    def __init__(self,path):
        self.path=Path(path);self.journal=self.path.with_suffix('.journal.jsonl')
        self.sequence=0;self.digest='';self.payload=None;self.materialized=0
    def append(self,payload,atomic):
        patch=changes(self.payload,payload)
        row={'sequence':self.sequence+1,'previous':self.digest,'changes':patch}
        digest=hashlib.sha256(encoded(row)).hexdigest();raw=encoded({**row,'sha256':digest})+b'\n'
        if len(raw)>MAX_LINE:raise ValueError('실행 기록 변경 크기 제한 초과')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        # A run UUID has one writer. Never append to an unrelated preexisting
        # journal if a fresh writer somehow reuses its path.
        mode='xb' if self.sequence==0 else 'ab'
        with self.journal.open(mode) as stream:
            if stream.tell()+len(raw)>MAX_JOURNAL:raise ValueError('실행 기록 크기 제한 초과')
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        manifest={'schema_version':3,'record_kind':'execution_journal','run_id':payload['run_id'],
                  'job_id':payload['job_id'],'journal':self.journal.name,'revision':row['sequence'],'sha256':digest}
        # The existing atomic writer fsyncs both file and directory. A crash
        # before this commit leaves an ignored tail, never a fabricated event.
        atomic(self.path,manifest)
        self.sequence=row['sequence'];self.digest=digest;self.payload=payload;self.materialized=0
    def materialize(self,atomic):
        if self.payload is not None and self.materialized!=self.sequence:
            atomic(self.path,self.payload);self.materialized=self.sequence


class RunReader:
    def __init__(self,capacity=64):self.cache=OrderedDict();self.capacity=capacity
    def read(self,path,*,document=None,copy=True):
        path=Path(path)
        doc=json.loads(path.read_text()) if document is None else document
        if not isinstance(doc,dict) or doc.get('record_kind')!='execution_journal':return doc
        if (doc.get('schema_version')!=3 or doc.get('run_id')!=path.stem
                or doc.get('journal')!=path.with_suffix('.journal.jsonl').name
                or type(doc.get('revision')) is not int or doc['revision']<1):raise ValueError('실행 기록 색인 형식 오류')
        journal=path.with_name(doc['journal'])
        if journal.is_symlink():raise ValueError('실행 기록 링크는 허용하지 않습니다.')
        with journal.open('rb') as stream:
            stat=os.fstat(stream.fileno());identity=(stat.st_dev,stat.st_ino)
            if stat.st_size>MAX_JOURNAL:raise ValueError('실행 기록 크기 제한 초과')
            cached=self.cache.get(path)
            if cached is not None and cached[0]==identity and cached[1]<=stat.st_size and cached[2]<=doc['revision']:
                _,offset,sequence,digest,value=cached;stream.seek(offset)
            else:offset=0;sequence=0;digest='';value=None
            while sequence<doc['revision']:
                raw=stream.readline(MAX_LINE+1)
                if len(raw)>MAX_LINE or not raw.endswith(b'\n'):raise ValueError('실행 기록 변경이 잘렸습니다.')
                row=json.loads(raw)
                if (not isinstance(row,dict) or set(row)!={'sequence','previous','changes','sha256'}
                        or type(row['sequence']) is not int or not isinstance(row['changes'],list)):
                    raise ValueError('실행 기록 변경 형식 오류')
                received=row.pop('sha256')
                if (row.get('sequence')!=sequence+1 or row.get('previous')!=digest
                        or hashlib.sha256(encoded(row)).hexdigest()!=received):raise ValueError('실행 기록 순서·해시 불일치')
                for operation in row['changes']:value=apply(value,operation)
                sequence+=1;digest=received;offset=stream.tell()
            if (digest!=doc.get('sha256') or not isinstance(value,dict)
                    or value.get('run_id')!=doc['run_id'] or value.get('job_id')!=doc['job_id']):raise ValueError('실행 기록 확정 상태 불일치')
        self.cache[path]=(identity,offset,sequence,digest,value);self.cache.move_to_end(path)
        while len(self.cache)>self.capacity:self.cache.popitem(last=False)
        return deepcopy(value) if copy else value


def read_run(path,*,document=None):return RunReader().read(path,document=document,copy=False)
