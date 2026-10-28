"""PC-only process/tunnel lifecycle. Preparation cannot send a robot command."""
import json
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
import time
from .domain import ROOT
from .pc_compute import PC_COMMANDS


def configuration(*,require_enabled=True):
    path=ROOT/'data/pc-compute.json'
    if not path.is_file():raise ValueError('PC 협력 실행 환경이 아직 준비되지 않았습니다.')
    value=json.loads(path.read_text())
    if require_enabled and value.get('physical_execution_enabled') is not True:
        raise ValueError('PC 협력 실행은 저장 영상 검증 단계입니다. 비교 결과 확인 후 실물 비교를 활성화합니다.')
    for key in ('python','model','reference'):
        if not isinstance(value.get(key),str) or not Path(value[key]).exists():raise ValueError('PC 연산 경로 없음: '+key)
    return value

class ComputeBroker:
    def __init__(self,job_id,command,*,cancel=None,config=None,connection=None):
        if command not in PC_COMMANDS or not re.fullmatch('[0-9a-f]{32}',job_id):raise ValueError('Invalid PC compute command/job')
        self.job_id=job_id;self.command=command;self.cancel=cancel or threading.Event()
        self.config=config or configuration();self.connection=connection
        self.service=None;self.tunnel=None;self.descriptor=None;self.closed=False
        self.lines=queue.Queue();self.errors=[];self.start_seconds=None;self._lock=threading.RLock()
    def _reader(self,process,stream,tag):
        try:
            for line in stream:
                # Only selected stdout/SSH allocation responses enter the queue.
                if tag=='stderr':self.errors.append(line.rstrip()[-500:]);self.errors=self.errors[-12:]
                else:
                    if tag=='ssh':self.errors.append(line.rstrip()[-500:]);self.errors=self.errors[-12:]
                    self.lines.put((tag,line.rstrip()))
        except (OSError,ValueError):pass
    def _spawn(self,args,tag,**options):
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1,**options)
        threading.Thread(target=self._reader,args=(process,process.stdout,tag),daemon=True).start()
        threading.Thread(target=self._reader,args=(process,process.stderr,'ssh' if tag=='tunnel' else 'stderr'),daemon=True).start()
        return process
    def _wait(self,tag,parse,deadline):
        while time.monotonic()<deadline:
            if self.cancel.is_set() or self.closed:raise RuntimeError('PC 연산 준비 취소됨')
            if any(p is not None and p.poll() is not None for p in (self.service,self.tunnel)):raise RuntimeError('PC 연산 준비 프로세스 종료'+(' · '+self.errors[-1] if self.errors else ''))
            try:source,line=self.lines.get(timeout=.05)
            except queue.Empty:continue
            if source!=tag:continue
            result=parse(line)
            if result is not None:return result
        raise TimeoutError('PC 연산 준비 시간 초과')
    def start(self):
        from .remote_client import ssh_args
        from .pi_connection import load,validate
        began=time.monotonic();deadline=began+70
        try:
            config=self.config;connection=validate(self.connection or load(ROOT/'data'),True)
            env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'4','YOLO_AUTOINSTALL':'False'}
            env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
            self.service=self._spawn([config['python'],'-B','-u','-m','so101_teach.pc_compute_service','--model',config['model'],'--reference',config['reference'],'--job-id',self.job_id],'service',cwd=ROOT,env=env)
            def parse(line):
                try:value=json.loads(line)
                except ValueError:return None
                return value if isinstance(value,dict) and value.get('job_id')==self.job_id else None
            local=self._wait('service',parse,deadline)
            script='import os,sys;os.chdir(os.path.expanduser(sys.argv[1]));sys.path.insert(0,os.getcwd());from so101_teach.pc_compute_session_receiver import main;main()'
            command=shlex.join([connection['python'],'-B','-u','-c',script,connection['app_dir']])
            args=ssh_args(connection)+['-o','ControlMaster=no','-o','ControlPath=none','-o','ExitOnForwardFailure=yes','-R',f"127.0.0.1:0:127.0.0.1:{local['port']}",'-l',connection['user'],connection['host'],command]
            self.tunnel=self._spawn(args,'tunnel')
            def allocation(line):
                match=re.search(r'Allocated port (\d+) for remote forward',line)
                return int(match[1]) if match else None
            port=self._wait('ssh',allocation,deadline)
            self.descriptor={**local,'port':port}
            self.tunnel.stdin.write(json.dumps({'command':self.command,'descriptor':self.descriptor})+'\n');self.tunnel.stdin.flush()
            self._wait('tunnel',lambda line:True if line=='PC_COMPUTE_REGISTERED:'+self.job_id else None,deadline)
            if self.cancel.is_set():raise RuntimeError('PC 연산 준비 취소됨')
            self.start_seconds=time.monotonic()-began
            return self
        except BaseException:self.close();raise
    def alive(self):
        return not self.closed and all(p is not None and p.poll() is None for p in (self.service,self.tunnel))
    def close(self):
        self.cancel.set()
        with self._lock:
            self.closed=True
            for process in (self.tunnel,self.service):
                if process is None:continue
                try:process.stdin.close()
                except (OSError,ValueError):pass
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                for stream in (process.stdout,process.stderr):
                    try:stream.close()
                    except (OSError,ValueError):pass
