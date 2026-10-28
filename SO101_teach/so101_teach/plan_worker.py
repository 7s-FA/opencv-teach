"""Pure geometry in a fresh interpreter so IK cannot starve servo I/O threads."""
import os
import pickle
import subprocess
import sys
import select
import struct
import tempfile
import threading
import time
from .domain import ROOT


def calculate_plan(inputs):
    from .geometry import corrected_plan
    kin,steps,current,meshes,calibration,bundle=inputs
    return {'plan':corrected_plan(kin,steps,current,lambda key:meshes[key]),
            'calibration_sha256':calibration,'bundle_hash':bundle}


def isolated_plan(inputs):
    # Private parent/child pipe only: no client-supplied pickle is accepted.
    try:
        result=subprocess.run([sys.executable,'-B','-m','so101_teach.plan_worker'],
            input=pickle.dumps(inputs),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            cwd=ROOT,env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'},timeout=35)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('경로 계산 시간 초과 (35초)') from exc
    if result.returncode:
        raise RuntimeError('경로 계산 실패: '+result.stderr.decode(errors='replace')[-2000:])
    return pickle.loads(result.stdout)


class PlanWorker:
    """One lazy pure-compute child per runtime, never a motor owner.

    Requests remain private copied values. A timeout/EOF discards the child;
    only a later explicit request may start another one, never replay this one.
    """
    limit=64*1024*1024
    def __init__(self):
        self.lock=threading.Lock();self.lifecycle=threading.Lock()
        self.closed=threading.Event();self.process=None;self.errors=None
    def _discard(self):
        process,self.process=self.process,None
        if process is not None:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.5)
            process.stdin.close();process.stdout.close()
        if self.errors is not None:self.errors.close();self.errors=None
    def close(self):
        self.closed.set()
        with self.lifecycle:self._discard()
    def calculate(self,inputs,*,timeout=35.):
        deadline=time.monotonic()+timeout
        if not self.lock.acquire(timeout=timeout):raise RuntimeError('경로 계산 시간 초과 (35초)')
        try:
            payload=pickle.dumps(inputs)
            if len(payload)>self.limit:raise ValueError('경로 입력 크기 제한 초과')
            with self.lifecycle:
                if self.closed.is_set():raise RuntimeError('경로 계산 작업자 종료됨')
                if self.process is None:
                    self.errors=tempfile.TemporaryFile()
                    self.process=subprocess.Popen([sys.executable,'-B','-m','so101_teach.plan_worker','--persistent'],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.errors,bufsize=0,cwd=ROOT,
                        env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'})
                    os.set_blocking(self.process.stdin.fileno(),False);os.set_blocking(self.process.stdout.fileno(),False)
                process=self.process
                send_fd=process.stdin.fileno();receive_fd=process.stdout.fileno()
            outgoing=memoryview(struct.pack('!Q',len(payload))+payload);sent=0;received=bytearray();size=None
            while True:
                if self.closed.is_set():raise RuntimeError('경로 계산 작업자 종료됨')
                left=deadline-time.monotonic()
                if left<=0:raise RuntimeError('경로 계산 시간 초과 (35초)')
                readable,writable,_=select.select([receive_fd],[send_fd] if sent<len(outgoing) else [],[],min(.05,left))
                if writable:
                    try:sent+=os.write(send_fd,outgoing[sent:sent+65536])
                    except BlockingIOError:pass
                if readable:
                    chunk=os.read(receive_fd,65536)
                    if not chunk:raise RuntimeError('경로 계산 작업자 응답 종료')
                    received.extend(chunk)
                    if size is None and len(received)>=8:
                        size=struct.unpack('!Q',received[:8])[0]
                        if not 0<size<=self.limit:raise RuntimeError('경로 응답 크기 오류')
                    if size is not None and len(received)>=8+size:
                        if len(received)!=8+size:raise RuntimeError('경로 응답 경계 오류')
                        reply=pickle.loads(received[8:])
                        if not reply['ok']:raise RuntimeError('경로 계산 실패: '+reply['error'])
                        return reply['value']
        except Exception:
            with self.lifecycle:self._discard()
            raise
        finally:self.lock.release()


def persistent():
    source=sys.stdin.buffer;target=sys.stdout.buffer
    while True:
        header=source.read(8)
        if not header:return
        if len(header)!=8:raise ValueError('경로 요청 헤더 오류')
        size=struct.unpack('!Q',header)[0]
        if not 0<size<=PlanWorker.limit:raise ValueError('경로 입력 크기 제한 초과')
        data=source.read(size)
        if len(data)!=size:raise ValueError('경로 요청 잘림')
        try:reply={'ok':True,'value':calculate_plan(pickle.loads(data))}
        except Exception as exc:reply={'ok':False,'error':str(exc)}
        payload=pickle.dumps(reply)
        target.write(struct.pack('!Q',len(payload)));target.write(payload);target.flush()


if __name__=='__main__':
    try:
        if sys.argv[1:]==['--persistent']:persistent()
        else:
            value=calculate_plan(pickle.load(sys.stdin.buffer))
            sys.stdout.buffer.write(pickle.dumps(value))
    except Exception as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
