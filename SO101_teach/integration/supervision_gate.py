"""Bound control admission and retain an emergency signal across child startup."""
import os
import threading
import uuid


class Ticket:
    def __init__(self,job_id=None,client_id=None):
        self.job_id=job_id or uuid.uuid4().hex;self.client_id=client_id;self.link_lost=False
        self.read_fd,self.write_fd=os.pipe()
        os.set_blocking(self.write_fd,False)
        self.stopped=False;self.started=False;self.finished=False

    def stop(self):
        self.stopped=True
        if self.write_fd is not None:
            try:os.write(self.write_fd,b'!')
            except (BlockingIOError,BrokenPipeError):pass

    def close_reader(self):
        if self.read_fd is not None:os.close(self.read_fd);self.read_fd=None

    def close(self):
        self.finished=True;self.close_reader()
        if self.write_fd is not None:os.close(self.write_fd);self.write_fd=None


class SupervisionGate:
    def __init__(self):
        self.lock=threading.RLock();self.admission_lock=threading.RLock();self.generation=0;self.context_generation=0;self.active=None;self.controls=set();self.cancelled_controls=set()

    def stop_active(self):
        with self.lock:
            self.generation+=1
            self.cancelled_controls.update(self.controls-{'EMER_STOP'})
            if self.active:self.active.stop()
            return self.active

    def accept_control(self,command):
        with self.lock:
            # One ordinary control and one emergency callback at most, leaving
            # capacity for goal callbacks even while a job callback is blocked.
            if command=='EMER_STOP':
                if command in self.controls:return False
            elif self.controls:return False
            self.cancelled_controls.discard(command)
            self.controls.add(command);return True

    def release_control(self,command):
        with self.lock:
            self.controls.discard(command);self.cancelled_controls.discard(command)

    def cancelled(self,command):
        with self.lock:return command in self.cancelled_controls

    def reserve(self,job_id=None,client_id=None):
        with self.lock:
            if self.active is not None:raise RuntimeError('BUSY: dispatch already reserved')
            self.active=Ticket(job_id,client_id);return self.active

    def retire(self,ticket):
        if ticket is None:return
        with self.lock:
            ticket.close()
            if self.active is ticket:
                self.active=None
                # Requests created before this job ended cannot become new idle controls.
                self.context_generation+=1
