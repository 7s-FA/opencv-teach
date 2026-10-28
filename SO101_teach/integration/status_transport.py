"""Ordered, bounded status I/O; never owns motor or supervision controls."""
import json
import os
import queue
import select
import socket
import threading
import time
import sys


def write_pipe(fd,payload,timeout=2.):
    raw=memoryview((json.dumps(payload,ensure_ascii=False)+'\n').encode());sent=0;until=time.monotonic()+timeout
    while sent<len(raw):
        left=until-time.monotonic()
        if left<=0:raise TimeoutError('ROS 상태 발행 파이프 지연')
        if select.select([],[fd],[],left)[1]:
            try:sent+=os.write(fd,raw[sent:])
            except BlockingIOError:pass


class StatusOutput:
    """Transport ownership survives a Status/arm handoff without draining I/O."""
    def __init__(self,*,process=None,client=None):self.process=process;self.client=client;self.lost=False
    def send(self,payload):
        if self.lost:return
        try:
            if self.client:self.client.send(payload)
            elif self.process:write_pipe(self.process.stdin.fileno(),payload)
        except (BrokenPipeError,ConnectionResetError):
            self.lost=True
            print('EXECUTOR_WARNING:ROS 상태 발행 종료: 터미널·JSON 상태를 확인하세요.',file=sys.stderr,flush=True)


class OrderedStatusWriter:
    def __init__(self,send,*,capacity=32):
        self.send=send;self.jobs=queue.Queue(capacity);self.changed=threading.Condition()
        self.submitted=0;self.completed=0;self.error=None;self.closed=False
        self.thread=threading.Thread(target=self.run,name='episode-status-io',daemon=True);self.thread.start()
    def submit(self,payload):
        with self.changed:
            if self.error:raise self.error
            if self.closed:raise RuntimeError('ROS 상태 발행 종료됨')
            sequence=self.submitted+1
            try:self.jobs.put_nowait((sequence,dict(payload)))
            except queue.Full:raise RuntimeError('ROS 상태 발행 대기열 포화') from None
            self.submitted=sequence
    def flush(self,timeout=4.):
        until=time.monotonic()+timeout
        with self.changed:
            target=self.submitted
            while self.completed<target:
                if self.error:raise self.error
                left=until-time.monotonic()
                if left<=0:raise TimeoutError('ROS 상태 발행 완료 대기 시간 초과')
                self.changed.wait(left)
            if self.error:raise self.error
    def close(self):
        with self.changed:self.closed=True
        try:self.jobs.put_nowait(None)
        except queue.Full:pass
        self.thread.join(2.2)
    def run(self):
        while True:
            try:item=self.jobs.get(timeout=.1)
            except queue.Empty:
                if self.closed:return
                continue
            if item is None:return
            sequence,payload=item
            try:self.send(payload)
            except Exception as exc:
                with self.changed:self.error=exc;self.changed.notify_all()
                return
            with self.changed:self.completed=sequence;self.changed.notify_all()


class StatusSocketClient:
    def __init__(self,fd):
        self.socket=socket.socket(fileno=os.dup(fd));self.socket.settimeout(2.)
        self.sequence=0;self.buffer=b''
    def send(self,payload):
        self.sequence+=1
        self.socket.sendall((json.dumps({**payload,'sequence':self.sequence},ensure_ascii=False)+'\n').encode())
        while b'\n' not in self.buffer:
            part=self.socket.recv(4096)
            if not part:raise BrokenPipeError('ROS 상태 인계 연결 종료')
            self.buffer+=part
            if len(self.buffer)>65536:raise ValueError('ROS 상태 인계 응답 크기 오류')
        line,self.buffer=self.buffer.split(b'\n',1);reply=json.loads(line)
        if reply!={'sequence':self.sequence,'ok':True}:raise RuntimeError('ROS 상태 인계 응답 불일치')
    def close(self):self.socket.close()


class StatusSocketRelay:
    """A private inherited descriptor, with no externally reachable listener."""
    def __init__(self,publish):
        self.parent,self.child=socket.socketpair();self.publish=publish;self.error=None
        self.thread=threading.Thread(target=self.run,name='action-status-relay',daemon=True);self.thread.start()
    def run(self):
        buffer=b'';expected=1
        try:
            while True:
                part=self.parent.recv(4096)
                if not part:return
                buffer+=part
                if len(buffer)>65536:raise ValueError('ROS 상태 인계 크기 오류')
                while b'\n' in buffer:
                    line,buffer=buffer.split(b'\n',1);value=json.loads(line)
                    if (not isinstance(value,dict) or value.get('arm') not in ('arm2','arm3')
                            or not isinstance(value.get('status'),str) or len(value['status'])>32768
                            or value.get('sequence')!=expected):raise ValueError('ROS 상태 인계 순서·형식 오류')
                    self.publish(value['arm'],value['status'])
                    self.parent.sendall((json.dumps({'sequence':expected,'ok':True})+'\n').encode());expected+=1
        except (OSError,ValueError,RuntimeError) as exc:self.error=exc
        finally:self.parent.close()
    def close(self):
        self.child.close()
        try:self.parent.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        self.thread.join(2.2);self.parent.close()
