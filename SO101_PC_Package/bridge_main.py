"""Explicitly started local bridge; never dispatches a job during startup."""
import argparse
from concurrent.futures import Future
import itertools
import os
from pathlib import Path
import queue
import threading
import time

from bridge_service import BridgeService, BridgeError

class BridgeLogger:
    def __init__(self,logger):self.logger=logger
    def request(self,*args):pass
    def connection(self,*args):pass
    def error(self,message):self.logger.error(message)
    def warning(self,message):self.logger.warning(message)
    def info(self,message):self.logger.info(message)

class RosRuntime:
    def __init__(self):
        self.queue=queue.PriorityQueue(128);self.urgent=queue.PriorityQueue(64);self.stops=queue.PriorityQueue(8);self.completions=queue.SimpleQueue();self.counter=itertools.count();self.stopping=threading.Event()
        self.ready=threading.Event();self.failure=None;self.thread=None
    def start(self):
        self.thread=threading.Thread(target=self.run,name='so101-ros-owner');self.thread.start()
        if not self.ready.wait(15):raise RuntimeError('ROS 중계 초기화 시간 초과')
        if self.failure:raise RuntimeError('ROS 중계 시작 실패') from self.failure
    def call(self,method,*args,priority=10):
        future=Future()
        if self.stopping.is_set() or (self.ready.is_set() and (self.thread is None or not self.thread.is_alive())):
            future.set_exception(BridgeError('UNAVAILABLE','중계 실행부가 종료되었습니다.',503));return future
        inbox=self.stops if priority==0 else self.urgent if priority<0 else self.queue
        try:inbox.put_nowait((priority,next(self.counter),time.monotonic()+2.,method,args,future))
        except queue.Full:future.set_exception(BridgeError('UNAVAILABLE','중계 요청 대기열이 가득 찼습니다.',503))
        return future
    def stop(self):
        self.stopping.set()
        if self.thread:self.thread.join(10)
    def run(self):
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from arm2_node import Arm2ActionClient,Arm3ActionClient
        from bridge_storage import DiskWriter
        os.environ.setdefault('ROS_DOMAIN_ID','40');nodes=[];executor=None;writer=None
        try:
            rclpy.init();executor=SingleThreadedExecutor();writer=DiskWriter(self.completions.put)
            for name,cls in [('arm2',Arm2ActionClient),('arm3',Arm3ActionClient)]:
                node=cls(logger_factory=BridgeLogger,monitor_jobs=False,journal_writer=writer.write,
                         challenge_sink=lambda epoch,nonce,arm=name:self.service.challenge(arm,epoch,nonce))
                nodes.append(node);executor.add_node(node)
            root=nodes[0].journal.path.parent
            self.service=BridgeService({n.arm:n for n in nodes},root,writer=writer.write);self.ready.set();next_save=0.
            while not self.stopping.is_set() and rclpy.ok():
                # Drain only a bounded batch so ROS feedback and supervision keep running.
                for _ in range(8):
                    try:priority,_,deadline,method,args,future=self.stops.get_nowait()
                    except queue.Empty:
                        try:priority,_,deadline,method,args,future=self.urgent.get_nowait()
                        except queue.Empty:future=None
                    if future is None:
                        try:self.completions.get_nowait()();continue
                        except queue.Empty:pass
                        try:priority,_,deadline,method,args,future=self.queue.get_nowait()
                        except queue.Empty:break
                    if not future.set_running_or_notify_cancel():continue
                    try:
                        if time.monotonic()>deadline:raise BridgeError('REQUEST_EXPIRED','대기 중 요청이 만료되었습니다. 자동 실행하지 않습니다.',408)
                        result=getattr(self.service,method)(*args);future.set_result(result)
                    except Exception as exc:future.set_exception(exc)
                executor.spin_once(timeout_sec=.01)
                self.service.expire()
                if time.monotonic()>=next_save:self.service.maintenance();next_save=time.monotonic()+1.
        except Exception as exc:
            import logging
            logging.exception('ROS bridge owner stopped')
            self.failure=exc;self.ready.set()
        finally:
            if hasattr(self,'service'):self.service.close(self.service.generation)
            if writer:writer.close()
            if executor:executor.shutdown(timeout_sec=2.)
            for node in reversed(nodes):node.destroy_node()
            rclpy.try_shutdown()
            for inbox in (self.stops,self.urgent,self.queue):
                while not inbox.empty():
                    future=inbox.get_nowait()[-1]
                    if not future.done():future.set_exception(BridgeError('UNAVAILABLE','중계 종료',503))

def main():
    parser=argparse.ArgumentParser(description='SO-101 WPF HTTPS/WSS 중계')
    parser.add_argument('--config',type=Path,default=Path.home()/'.config/so101-bridge')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8443)
    args=parser.parse_args()
    if args.host not in ('127.0.0.1','::1'):parser.error('현재 배포는 VM NAT에서 접근하는 호스트 loopback 바인딩만 지원합니다.')
    token_path=args.config/'token';key_path=args.config/'server.key';cert_path=args.config/'server.crt'
    for file in (token_path,key_path,cert_path):
        if not file.is_file():parser.error('연결 설정이 없습니다. 먼저 scripts/setup-bridge.sh를 실행하세요.')
    if token_path.stat().st_mode&0o077 or key_path.stat().st_mode&0o077:parser.error('토큰과 개인 키 권한은 600이어야 합니다.')
    token=token_path.read_text().strip()
    if len(token)<32:parser.error('토큰 길이 오류')
    import uvicorn
    from bridge_api import create_app
    uvicorn.run(create_app(RosRuntime(),token),host=args.host,port=args.port,ssl_keyfile=str(key_path),
                ssl_certfile=str(cert_path),access_log=False,ws_max_size=4096,ws_max_queue=8,
                timeout_graceful_shutdown=5,workers=1)
if __name__=='__main__':main()
