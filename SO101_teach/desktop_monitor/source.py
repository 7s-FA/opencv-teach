"""Pi observation and bounded camera preview subscriptions; stdlib only.

The executor's `inspect` and runtime `/camera` read evidence. `/preview` keeps
camera-only viewing alive; it cannot acquire a robot lease or send motion RPCs.
Never import/start an executor, ensure a server, or change device configuration.
"""
import json
import hashlib
from pathlib import Path
import re
import socket
import sys
import time
import uuid
from urllib.request import Request, urlopen

MAX_FILE_BYTES=24*1024*1024
MAX_EVENTS=400

class Source:
    def __init__(self,root):
        self.root=Path(root).expanduser();self.cache={};self.cursors={};self.paths=[];self.scan_at=0
        self.names={};self.names_at=0;self.preview=None
        self.last_events={};self.record_digest=None
        self.visual_cursor=None;self.watch_at={};self.state_cache=None;self.state_checked=0.
        self.viewer=uuid.uuid4().hex;self.watched={}
        if (self.root/'so101_teach').is_dir():sys.path.insert(0,str(self.root))
        try:
            from so101_teach.execution_records import RunReader
            from so101_teach.run_directory import RunDirectoryWatch
            self.run_reader=RunReader();self.run_changes=RunDirectoryWatch(self.root/'data/episode-cli-runs')
        except ImportError:
            # A new passive PC viewer can still read an older Pi's legacy logs.
            self.run_reader=None;self.run_changes=None

    def preview_request(self,arm,info,token,operation):
        now=time.monotonic()
        if operation=='watch' and self.watched.get(arm)==(info,token) and now-self.watch_at.get(arm,float('-inf'))<1:return
        request=Request(f"http://127.0.0.1:{int(info['port'])}/preview",
            data=json.dumps({'operation':operation,'viewer':self.viewer}).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urlopen(request,timeout=.7) as response:reply=json.load(response)
        if reply.get('ok') and operation=='watch':self.watched[arm]=(info,token);self.watch_at[arm]=now

    def release_preview(self,except_arm=None):
        for arm,(info,token) in list(self.watched.items()):
            if arm==except_arm:continue
            try:self.preview_request(arm,info,token,'release')
            except (OSError,ValueError):pass
            self.watched.pop(arm,None);self.watch_at.pop(arm,None)

    def read(self,path,default=None):
        path=Path(path)
        try:
            stat=path.stat();stamp=(stat.st_mtime_ns,stat.st_size)
            if stat.st_size>MAX_FILE_BYTES:raise ValueError('기록 크기 제한 초과: '+path.name)
            old=self.cache.get(path)
            if old and old[0]==stamp:return old[1]
            value=json.loads(path.read_text())
            if isinstance(value,dict) and value.get('record_kind')=='execution_journal':
                if self.run_reader is None:raise ValueError('실행 기록 읽기 버전이 일치하지 않습니다.')
                value=self.run_reader.read(path,document=value,copy=False)
            self.cache[path]=(stamp,value)
            return value
        except FileNotFoundError:return default

    def control(self):
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(.35);client.connect(str(self.root/'data/episode-cli.sock'))
                client.sendall(b'inspect');raw=b''
                while b'\n' not in raw and len(raw)<16384:
                    part=client.recv(4096)
                    if not part:break
                    raw+=part
            return {'known':True,**json.loads(raw)}
        except (FileNotFoundError,ConnectionRefusedError):return {'known':True,'info':None,'paused':False}
        except (OSError,ValueError) as exc:return {'known':False,'error':str(exc),'info':None}

    def records(self,control):
        now=time.monotonic();folder=self.root/'data/episode-cli-runs'
        if now-self.scan_at>=1:
            changed,rescan=self.run_changes.poll() if self.run_changes else (set(),True)
            candidates=folder.glob('*.json') if rescan else dict.fromkeys([*self.paths,*changed])
            stamped=[]
            for path in candidates:
                try:stamped.append((path.stat().st_mtime_ns,path))
                except FileNotFoundError:continue
            if not rescan and changed:
                stamps=[stamp for stamp,_ in stamped]
                # Legacy ordering preserves directory order for equal mtimes.
                # New ties or a backdated run need the full candidate window.
                reorder=any(path in changed and (stamps.count(stamp)>1 or
                    path in self.cache and stamp<self.cache[path][0][0]) for stamp,path in stamped)
                if reorder:
                    stamped=[]
                    for path in folder.glob('*.json'):
                        try:stamped.append((path.stat().st_mtime_ns,path))
                        except FileNotFoundError:continue
            self.paths=[path for _,path in sorted(stamped,key=lambda row:row[0],reverse=True)[:48]]
            # A notified rewrite can retain the same size and filesystem mtime.
            for path in changed:self.cache.pop(path,None)
            self.scan_at=now
        paths=set(self.paths)
        for arm in ('arm2','arm3'):
            current=self.read(self.root/'data'/('episode-cli-'+arm+'.json'),{})
            key=current.get('run_id','')
            if re.fullmatch('[0-9a-f]{32}',key):paths.add(folder/(key+'.json'))
        key=(control.get('info') or {}).get('run_id','')
        if re.fullmatch('[0-9a-f]{32}',key):paths.add(folder/(key+'.json'))
        events=[];runs=[];errors=[]
        for path in paths:
            try:doc=self.read(path,{})
            except (OSError,ValueError,TypeError) as exc:errors.append(str(exc));continue
            rows=doc.get('events',[])
            if not isinstance(rows,list):continue
            key=doc.get('run_id',path.stem);analysis=doc.get('analysis') or {}
            rows=[row for row in rows if isinstance(row,dict) and isinstance(row.get('at'),(int,float)) and isinstance(row.get('status'),str)]
            for i,row in enumerate(rows[-MAX_EVENTS:],max(0,len(rows)-MAX_EVENTS)):
                events.append({k:row[k] for k in ('at','arm','status','event','elapsed_s','job_id','timing_summary') if k in row}|{'run_id':key,'sequence':row.get('event_seq',i+1)})
            if rows and (doc.get('record_kind')=='execution' or analysis or any(re.match(r'^(A|B|SLIDE|HOME)_(ACCEPTED|STEP_DONE|JIG_)',r['status']) for r in rows)):
                # Return only display evidence; no motor telemetry/large plans.
                contexts=analysis.get('configurations',{})
                runs.append({'run_id':key,'job_id':doc.get('job_id') or rows[0].get('job_id') or key,
                    'arm':rows[-1].get('arm'),'started':rows[0]['at'],'at':rows[-1]['at'],
                    'status':rows[-1]['status'],'operation':analysis.get('operation'),
                    'expected':analysis.get('expected_inspections',{}),
                    'checks':analysis.get('inspection_by_arm',{}),
                    'poses':[{**p,'adopted_at':analysis.get('executor_started_at_unix',0)+p.get('adopted_elapsed_s',0)}
                             for p in analysis.get('jig_pose_sets',[])[-4:]],
                    'motions':[{field:r.get(field) for field in ('arm','action','context_id','plan_id','requests','finished_elapsed_s')}
                               for r in analysis.get('motion_calls',[])[-3:]],
                    'plans':[{field:r.get(field) for field in ('arm','plan_id','context_id')}|
                             {'steps':[{'id':s.get('id'),'name':s.get('name')} for s in r.get('steps',[])]}
                             for r in analysis.get('plans',[])[-2:]],
                    'contexts':{k:{'episode':{'name':(v.get('episode') or {}).get('name'),
                        'steps':[{field:s.get(field) for field in ('id','name','inspection')} for s in (v.get('episode') or {}).get('steps',[])]}}
                        for k,v in contexts.items()},
                    'follower':analysis.get('follower_observations',[])[-2:]})
        keep=paths|{p for p in self.cache if p.parent!=folder}
        self.cache={p:v for p,v in self.cache.items() if p in keep}
        return sorted(events,key=lambda r:(r['at'],r['run_id'],r['sequence']))[-MAX_EVENTS:],sorted(runs,key=lambda r:r['at'],reverse=True)[:20],errors

    def display_names(self):
        if time.monotonic()-self.names_at<5:return self.names
        names={}
        for data in (self.root/'data',self.root/'data/arm3-runtime'):
            for key,value in (self.read(data/'jigs.json',{}) or {}).items():names[key]=value.get('name',key)
            for path in (data/'episodes').glob('*.json'):
                doc=self.read(path,{})
                for step in doc.get('steps',[]):names[step['id']]=step.get('name',step['id'])
        self.names=names;self.names_at=time.monotonic();return names

    def camera(self,preferred):
        self.release_preview(except_arm=preferred)
        order=[preferred,*[a for a in ('arm2','arm3') if a!=preferred]]
        errors=[]
        for arm in order:
            if arm not in ('arm2','arm3'):continue
            data=self.root/('data' if arm=='arm2' else 'data/arm3-runtime')
            info=self.read(data/'remote-server.json',{})
            if not info:continue
            try:
                token=(data/'remote-token').read_text().strip();port=int(info['port'])
                if not 1<=port<=65535:raise ValueError('잘못된 카메라 포트')
                try:self.preview_request(arm,info,token,'watch')
                except (OSError,ValueError):pass
                identity=(arm,info.get('instance'));after=self.cursors.get(identity,{})
                req=Request(f'http://127.0.0.1:{port}/camera',data=json.dumps(after).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urlopen(req,timeout=.45) as response:reply=json.load(response)
                if not reply.get('ok'):raise ValueError(reply.get('error','영상 조회 실패'))
                value=reply['value']
                if not value.get('running'):continue
                now=value.get('server_now',time.monotonic());at=value.get('preview_at',value.get('at'))
                image=value.get('preview_image') or value.get('image')
                if value.get('preview_image'):at=value['preview_at']
                elif value.get('image'):at=value.get('at')
                if value.get('image'):after['after_at']=value.get('at')
                if value.get('preview_image') or value.get('at')==value.get('preview_at'):after['after_preview_at']=value.get('preview_at',value.get('at'))
                self.cursors[identity]=after
                # Use only the frame's own Pi monotonic clock; PC clock is unrelated.
                age=now-at if isinstance(at,(float,int)) else None
                result={'arm':arm,'identity':list(identity),'at':at,'age_s':age,'running':True,'error':value.get('error')}
                frozen={}
                detection=value.get('detection') or {}
                for key,item in detection.get('by_jig',{}).items():
                    selected=item.get('selected') or {};metric=selected.get('metric') or {}
                    if item.get('pose_frozen') and selected.get('quad'):
                        frozen[key]={'quad':selected['quad'],'center_xy_mm':metric.get('center_xy_mm'),'yaw_deg':metric.get('yaw_deg')}
                result['frozen']=frozen
                if isinstance(value.get('at'),(int,float)) and 0<=now-value['at']<1:
                    result['acquiring']={key:{k:item[k] for k in ('status','acquisition_completed_attempts',
                        'acquisition_attempts','stable_candidate_seconds','acquisition_seconds','error') if k in item}
                        for key,item in detection.get('by_jig',{}).items()}
                if image and age is not None and 0<=age<1:result['image']=image
                self.release_preview(except_arm=arm)
                return result
            except (OSError,ValueError,KeyError) as exc:errors.append(type(exc).__name__)
        return {'running':False,'error':' / '.join(errors) or '실행기가 제공하는 영상 대기'}

    def visual(self,control,runs):
        info=control.get('info') or {}
        run=next((r for r in runs if r['run_id']==info.get('run_id')),None) if info else (runs[0] if runs else None)
        if not run or run.get('arm') not in ('arm2','arm3'):return {}
        try:
            value=self.read(self.root/'data'/('work-visual-'+run['arm']+'.json'),{})
            if (value.get('schema')!=1 or value.get('run_id')!=run['run_id']
                or value.get('job_id')!=run['job_id'] or value.get('arm')!=run['arm']):return {}
            value={**value};now=time.monotonic();age=now-value['captured_at']
            if age<0:return {}
            value['age_s']=age
            value['checks']={key:{**row,'age_s':now-row['captured_at']} for key,row in value.get('checks',{}).items()}
            identity=(value['instance'],value['sequence'])
            if identity==self.visual_cursor:value.pop('image',None)
            self.visual_cursor=identity
            return value
        except (OSError,ValueError,KeyError,TypeError):return {}

    def snapshot(self):
        # Faster preview must not multiply executor/socket and journal reads.
        now=time.monotonic()
        if self.state_cache is None or now-self.state_checked>=.5:
            control=self.control();events,runs,errors=self.records(control)
            self.state_cache={'control':control,'events':events,'runs':runs,'names':self.display_names(),'errors':errors}
            self.state_checked=now
        state=self.state_cache;control=state['control'];runs=state['runs']
        arm=(control.get('info') or {}).get('arm','arm2')
        return {'schema':1,'at':time.time(),**state,'camera':self.camera(arm),'visual':self.visual(control,runs)}

    def packet(self):
        value=self.snapshot();events=value.pop('events')
        current={(r['run_id'],r['sequence']):r for r in events}
        value['events']=[r for key,r in current.items() if self.last_events.get(key)!=r]
        value['events_reset']=self.record_digest is None;self.last_events=current
        records={key:value[key] for key in ('runs','names','errors')}
        signature=hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()
        if signature==self.record_digest:
            for key in records:value.pop(key)
        self.record_digest=signature
        return value


def main(root):
    source=Source(root)
    try:
        for line in sys.stdin:
            try:
                if json.loads(line)!={'operation':'snapshot'}:raise ValueError('조회 요청만 허용됩니다.')
                reply={'ok':True,'value':source.packet()}
            except Exception as exc:reply={'ok':False,'error':type(exc).__name__+': '+str(exc)}
            print(json.dumps(reply,ensure_ascii=False,allow_nan=False),flush=True)
    finally:source.release_preview()

if __name__=='__main__':main(sys.argv[1])
