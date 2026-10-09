"""Bounded serial recovery. Retry transport failures, never servo alarms or logic errors."""
from collections import deque
from contextlib import contextmanager
import time

ATTEMPTS=3
READ_BUDGET=.25
WRITE_BUDGET=.4

class CommunicationCancelled(RuntimeError):pass

class Communication:
    def __init__(self):
        self.cancel=None;self.cleaning=False;self.deadline=None
        self.events=deque(maxlen=100);self.recoveries=0
    def check_cancel(self):
        if self.cancel and not self.cleaning:self.cancel()
    @contextmanager
    def budget(self,seconds):
        old=self.deadline;self.deadline=min(old,time.monotonic()+seconds) if old is not None else time.monotonic()+seconds
        try:yield
        finally:self.deadline=old
    @contextmanager
    def cleanup(self):
        old=self.cleaning;self.cleaning=True
        try:yield
        finally:self.cleaning=old
    def record(self,operation,attempts,outcome,error=None):
        self.events.append({'at':time.time(),'operation':operation,'attempts':attempts,'outcome':outcome,'error':str(error) if error else None})
        if outcome in ('recovered','verified'):self.recoveries+=1
    def drain(self,bus):
        port=getattr(bus,'port_handler',None);serial=getattr(port,'ser',None)
        reset=getattr(serial,'reset_input_buffer',None)
        if reset:
            try:reset()
            except OSError:pass
    def pause(self,bus,attempt):
        self.check_cancel();self.drain(bus)
        end=min(time.monotonic()+.01*attempt,self.deadline)
        while time.monotonic()<end:
            self.check_cancel();time.sleep(min(.005,max(0,end-time.monotonic())))
        self.check_cancel()
    def retry_read(self,bus,operation,read):
        with self.budget(READ_BUDGET):
            for attempt in range(1,ATTEMPTS+1):
                self.check_cancel()
                try:result=read()
                except OSError as exc:
                    if attempt==ATTEMPTS or time.monotonic()>=self.deadline:
                        self.record(operation,attempt,'failed',exc)
                        raise ConnectionError(f'{operation}: 통신 재시도 {attempt}회 실패 · {exc}') from exc
                    self.pause(bus,attempt)
                    if time.monotonic()>=self.deadline:
                        self.record(operation,attempt,'failed',exc);raise ConnectionError(f'{operation}: 통신 복구 시간 초과 · {exc}') from exc
                else:
                    if attempt>1:self.record(operation,attempt,'recovered')
                    return result
    def verified_write(self,bus,operation,value,send,readback,*,before_retry=None,off=False):
        """Return ack/readback; only resend when a successful read shows it was not applied.

        Torque OFF is the sole exception: it can be resent when readback is unavailable.
        Each send callback must grant a fresh wire permit.
        """
        with self.budget(WRITE_BUDGET):
            last=None
            for attempt in range(1,ATTEMPTS+1):
                self.check_cancel()
                if attempt>1 and time.monotonic()>=self.deadline:break
                try:send()
                except OSError as exc:
                    last=exc;self.pause(bus,attempt)
                    if time.monotonic()>=self.deadline:break
                    try:actual=self.retry_read(bus,operation+' 읽기 확인',readback)
                    except OSError:
                        if not off:raise
                    else:
                        if actual==value:
                            self.record(operation,attempt,'verified',exc);return 'readback'
                    if attempt<ATTEMPTS and time.monotonic()<self.deadline:
                        if before_retry:before_retry()
                        continue
                    break
                else:
                    if attempt>1:self.record(operation,attempt,'recovered',last)
                    return 'ack'
            self.record(operation,attempt,'failed',last)
            raise ConnectionError(f'{operation}: 통신 복구 실패 ({attempt}회) · {last}') from last

def install_packet_recovery(bus,communication):
    """Covers SDK reads, firmware handshake, calibration reads, and raw telemetry."""
    bus.communication=communication
    handler=bus.packet_handler
    original=handler.readTxRx
    def read(port,motor,address,length):
        def once():
            result=original(port,motor,address,length);data,comm,error=result
            voltage_handler=getattr(bus.communication,'voltage_handler',None)
            if comm==0 and len(data)==length and error==1 and voltage_handler and (address,length)!=(40,31):
                voltage_handler(motor);return data,comm,0
            if comm==0 and error!=0:return result
            if comm!=0 or len(data)!=length:raise ConnectionError(f'comm={comm}, bytes={len(data)}/{length}')
            return result  # A servo alarm is NOT retried; the caller must handle it.
        return bus.communication.retry_read(bus,f'모터 {motor} 주소 {address} 읽기',once)
    handler.readTxRx=read
    if hasattr(handler,'writeTxRx'):
        original_write=handler.writeTxRx
        def write(port,motor,*args,**kwargs):
            comm,error=original_write(port,motor,*args,**kwargs)
            voltage_handler=getattr(bus.communication,'voltage_handler',None)
            if comm==0 and error==1 and voltage_handler:voltage_handler(motor);return comm,0
            return comm,error
        handler.writeTxRx=write
    original_ping=handler.ping
    def ping(port,motor):
        def once():
            result=original_ping(port,motor)
            if result[1]!=0:raise ConnectionError(f'comm={result[1]}')
            return result
        return bus.communication.retry_read(bus,f'모터 {motor} 연결 확인',once)
    handler.ping=ping
    if hasattr(bus,'sync_read'):
        original_sync=bus.sync_read
        def sync_read(*args,**kwargs):
            kwargs['num_retry']=0
            return bus.communication.retry_read(bus,'모터 동기 위치 읽기',lambda:original_sync(*args,**kwargs))
        bus.sync_read=sync_read


def install_calibration_write_recovery(bus,communication):
    # Only idempotent absolute configuration registers admitted by calibration_gate.
    original=bus.write
    def write(register,motor,value,*,normalize=True,num_retry=0):
        allowed=register in ('Homing_Offset','Min_Position_Limit','Max_Position_Limit','Operating_Mode','Lock') or register=='Torque_Enable' and value==0
        if not allowed:return original(register,motor,value,normalize=normalize,num_retry=0)
        return communication.verified_write(bus,str(motor)+' '+register,value,
            lambda:original(register,motor,value,normalize=normalize,num_retry=0),
            lambda:bus.read(register,motor,normalize=False,num_retry=0),off=register=='Torque_Enable' and value==0)
    bus.write=write
