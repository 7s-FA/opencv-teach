"""Opt-in ROS topic process with bounded queues; all app calls run in Tk."""
import fcntl,json,os,queue,subprocess,threading
from pathlib import Path
from .domain import ROOT
from .integration import EpisodeCoordinator


class IntegrationBridge:
    def __init__(self,manager,config_path,*,domain=40):
        self.manager=manager;self.closed=False;self.inbox=queue.Queue(100);self.outbox=queue.Queue(100);self.job=None;self.error=None
        self.lock_file=open(manager.data_dir/'integration-receiver.lock','a+')
        try:fcntl.flock(self.lock_file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close();raise ValueError('통합 명령 수신 앱이 이미 실행 중입니다.') from None
        config=json.loads(Path(config_path).read_text())
        self.coordinator=EpisodeCoordinator(manager.apps,manager.data_dir,config,emit=self.send_status)
        self.log=open(manager.data_dir/'integration-ros.log','ab')
        environment={**os.environ,'ROS_DOMAIN_ID':str(domain),'RCUTILS_LOGGING_USE_STDOUT':'0','PYTHONUNBUFFERED':'1'}
        environment.pop('PYTHONPATH',None);environment.pop('PYTHONHOME',None)
        self.process=subprocess.Popen(['bash','-c','source /opt/ros/jazzy/setup.bash && exec /usr/bin/python3 -m so101_teach.integration_ros'],
            cwd=ROOT,env=environment,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1)
        threading.Thread(target=self.read,daemon=True).start();threading.Thread(target=self.write,daemon=True).start();self.poll()
    def read(self):
        try:
            for line in self.process.stdout:
                if len(line)>8192:raise ValueError('ROS command too large')
                self.inbox.put(json.loads(line),timeout=1.)
        except Exception as exc:self.error=str(exc)
    def write(self):
        try:
            while not self.closed:
                try:value=self.outbox.get(timeout=1.)
                except queue.Empty:continue
                if value is None:return
                self.process.stdin.write(json.dumps(value,separators=(',',':'))+'\n');self.process.stdin.flush()
        except (OSError,ValueError) as exc:self.error=str(exc)
    def send_status(self,arm,text):
        try:self.outbox.put_nowait({'arm':arm,'text':text})
        except queue.Full:self.error='ROS 상태 전송 지연'
    def poll(self):
        if self.closed:return
        c=self.coordinator
        try:
            if self.error or self.process.poll() is not None:
                c.enabled=False
                if c.active:c.cancel('RECEIVER_ERROR')
                if not self.error:self.error='ROS 수신부 종료 · integration-ros.log 확인'
            if c.enabled:
                for _ in range(20):
                    try:message=self.inbox.get_nowait()
                    except queue.Empty:break
                    if message.get('kind')=='command':c.command(message.get('arm'),message.get('command',''))
            c.poll()
        except Exception as exc:
            self.error=str(exc);c.enabled=False
            if c.active:c.cancel('BRIDGE_ERROR')
        if self.error and not getattr(self,'reported_error',False):
            self.reported_error=True
            for a in self.manager.apps.values():a.notice('통합 수신 오류: '+self.error,True)
        self.job=self.manager.root.after(100,self.poll)
    def close(self):
        if self.closed:return
        if self.coordinator.active:self.coordinator.cancel('RECEIVER_CLOSED')
        self.closed=True
        if self.job:self.manager.root.after_cancel(self.job)
        try:self.outbox.put_nowait(None)
        except queue.Full:pass
        self.process.terminate()
        try:self.process.wait(timeout=2.)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2.)
        self.log.close()
        self.lock_file.close()
