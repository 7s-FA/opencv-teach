"""One bounded disk writer. ROS supervision and urgent stops never wait for fsync."""
import queue
import threading
from supervision_client import atomic_record

class DiskWriter:
    def __init__(self,post):
        self.post=post;self.queue=queue.Queue(256);self.thread=threading.Thread(target=self.run,name='so101-receipt-writer',daemon=True)
        self.thread.start()
    def write(self,path,value,complete):
        try:self.queue.put_nowait((path,value,complete))
        except queue.Full:self.post(lambda:complete(OSError('기록 저장 대기열 제한 초과')))
    def run(self):
        while True:
            entry=self.queue.get()
            if entry is None:return
            path,value,complete=entry;error=None
            try:atomic_record(path,value)
            except OSError as exc:error=exc
            self.post(lambda error=error,complete=complete:complete(error))
    def close(self):
        # Keep the ClientJournal process locks until all queued disk writes finish.
        # The UI/ROS supervision has already stopped, so this never delays HOLD.
        self.queue.put(None)
        self.thread.join()
