"""Close the GUI as soon as the Pi accepts ownership of safe shutdown."""
from copy import deepcopy
import json
import queue
import subprocess
import threading
import time

from .pi_execution import command_args
from .remote_config import configuration_bundle


class SafeShutdown:
    def __init__(self,manager):
        self.manager=manager;self.busy=True;self.events=queue.Queue();self.process=None
        self.started=time.monotonic();self.thread=None;self.cancelled=False;self.confirmed=False
        active=getattr(manager,'pi_execution',None)
        self.first=active.request['arm'] if active and active.busy else manager.active

    def start(self):
        m=self.manager
        for app in m.apps.values():
            if app.settings.job or app.settings.pi_panel.job or app.settings.worker and app.settings.worker.running:
                raise ValueError('장치 설정·연결 작업이 끝난 뒤 종료를 다시 시도하세요.')
        if m.integration:m.integration.close();m.integration=None
        if m.pi_execution and m.pi_execution.busy:m.pi_execution.cancel()
        for app in m.apps.values():
            if app.inspection_run:app.inspection_run.cancel('앱 종료 준비',hold=True)
            app.live_adjust.stop(halt=False);app.cancel_space_key();app.cancel_jig_key()
            app.stop_preview(quiet=True)
            app.hold_btn.state(['!disabled'])
            app.notice('Pi에 안전 종료 인계 중 · 접수되면 앱이 닫힙니다.')
        self.poll()

    def request(self):
        from .episode_inspection import criteria_signature
        request={'links':{},'arms':{},'first':self.first,'apply_jig':True,'speed':300.,'timeout_seconds':120.,
                 'inspection_protocol':6,'inspection_criteria_sha256':criteria_signature()}
        config=None
        if set(self.manager.apps)!={'arm2','arm3'}:raise ValueError('두 팔의 작업창이 필요합니다.')
        for arm,app in self.manager.apps.items():
            remote=app.remote
            if not app.remote_mode or not remote or remote.error or remote.stop.is_set() or not remote.lease:
                raise ValueError(arm+': Pi 연결을 확인한 뒤 종료를 다시 시도하세요.')
            if not app.session or not app.session.running:raise ValueError(arm+': 팔로워 연결이 필요합니다.')
            if config and any(config[k]!=remote.config[k] for k in ('host','port','user','app_dir')):
                raise ValueError('두 팔의 Pi 연결 대상이 다릅니다.')
            config=deepcopy(remote.config)
            episode=deepcopy(app.episode);app.store.validate(episode)
            if not episode['steps'] or episode['steps'][-1].get('safe_boundary')!='end':
                raise ValueError(arm+': 에피소드의 안전자세를 먼저 설정하세요.')
            request['arms'][arm]={'episode':episode,'bundle':configuration_bundle(app)}
            request['links'][arm]={k:getattr(remote,{'port':'server_port'}.get(k,k)) for k in ('port','token','lease','instance')}
        return config,request

    def run(self,config,request):
        try:
            if self.cancelled:self.events.put(('error','사용자가 종료를 취소했습니다.'));return
            self.process=subprocess.Popen(command_args(config,receiver='shutdown_receiver.py',dispatch=True),
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
            self.process.stdin.write(json.dumps(request,ensure_ascii=False,allow_nan=False)+'\n');self.process.stdin.flush()
            if self.cancelled:self.process.stdin.write('cancel\n');self.process.stdin.flush()
            for line in self.process.stdout:self.events.put(('line',line.strip()))
            self.events.put(('exit',self.process.wait()))
        except Exception as exc:
            if self.process:
                try:self.process.stdin.close()
                except (OSError,ValueError):pass
                try:self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.events.put(('error',str(exc)))

    def cancel(self):
        self.cancelled=True
        def send():
            try:
                if self.process and self.process.poll() is None:
                    self.process.stdin.write('cancel\n');self.process.stdin.flush()
            except (OSError,ValueError):pass
        threading.Thread(target=send,daemon=True).start()

    def poll(self):
        if not self.busy:return
        m=self.manager
        if self.thread is None:
            if self.cancelled:self.fail('사용자가 안전 종료를 중단했습니다.');return
            if m.pi_execution and m.pi_execution.busy:
                if time.monotonic()-self.started>30:self.fail('실행 중인 작업의 정지 확인 시간 초과');return
            else:
                try:config,request=self.request()
                except Exception as exc:self.fail(str(exc));return
                for app in m.apps.values():app.remote.execution_owner=self
                self.thread=threading.Thread(target=self.run,args=(config,request),daemon=True);self.thread.start()
        for _ in range(100):
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='line':
                if value.startswith('SHUTDOWN_ACCEPTED:'):
                    self.confirmed=True;self.release_ownership()
                    for app in m.apps.values():app.remote.relinquish()
                    m.finish_safe_close();return
                elif value.startswith('SHUTDOWN_FAILED:'):self.error=value.partition(':')[2]
                message=('두 팔 안전자세 도착 · 3초 정지 확인 후 토크 해제' if value=='SHUTDOWN_SETTLING'
                         else value.split(':')[1]+' 안전자세 복귀 중' if value.startswith('ARM_RESET_STARTED:') else None)
                if message:
                    for app in m.apps.values():app.notice(message)
            elif kind=='exit':
                self.fail(getattr(self,'error','Pi의 종료 작업 접수를 확인하지 못했습니다.'));return
            else:self.fail(value);return
        m.root.after(80,self.poll)

    def fail(self,message):
        self.release_ownership()
        for app in self.manager.apps.values():app.notice('종료 취소 · '+message,True)

    def release_ownership(self):
        self.busy=False
        for app in self.manager.apps.values():
            remote=getattr(app,'remote',None)
            if remote and getattr(remote,'execution_owner',None) is self:
                remote.execution_owner=None;remote.bundle_hash=None
