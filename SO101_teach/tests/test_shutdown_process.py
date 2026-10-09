"""Real detached process/lock lifecycle, with motor work replaced at its boundary."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


class ShutdownProcessTests(unittest.TestCase):
    def test_pi_worker_outlives_client_and_done_means_next_job_can_lock(self):
        package=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            child=root/'child.py';parent=root/'parent.py'
            child.write_text('''import sys,os,time,json
from pathlib import Path
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'integration')]
from so101_teach import domain
domain.ROOT=Path(sys.argv[2])
from so101_teach import shutdown_receiver,shutdown_worker
def motor_substitute(cli,request,stream,emit,*,lock_fd,on_accept,**kwargs):
 with os.fdopen(lock_fd,'a') as lock:
  on_accept()
  time.sleep(1.2)
  emit('SHUTDOWN_DONE')
shutdown_receiver.park=motor_substitute
sys.exit(shutdown_worker.run(Path(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5])))
''')
            parent.write_text('''import sys,subprocess
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from so101_teach import shutdown_worker
original=subprocess.Popen
def start(args,**kwargs):
 assert kwargs['start_new_session'] and kwargs['stdin']==subprocess.DEVNULL
 return original([sys.executable,sys.argv[3],sys.argv[1],sys.argv[2],*args[-3:]],**kwargs)
shutdown_worker.subprocess.Popen=start
shutdown_worker.dispatch(SimpleNamespace(ROOT=Path(sys.argv[2])),{'first':'arm2'})
''')
            client=subprocess.run([sys.executable,str(parent),str(package),str(root),str(child)],capture_output=True,text=True,timeout=10)
            self.assertEqual(client.returncode,0,client.stderr);self.assertIn('SHUTDOWN_ACCEPTED:',client.stdout)
            task=next((root/'data/shutdown-jobs').iterdir())
            self.assertFalse((task/'request.json').exists())
            with (root/'data/episode-cli.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            until=time.monotonic()+5
            while time.monotonic()<until:
                status=json.loads((task/'status.json').read_text())
                if status['status']=='SHUTDOWN_DONE':break
                time.sleep(.05)
            self.assertEqual(status['status'],'SHUTDOWN_DONE')
            with (root/'data/episode-cli.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
