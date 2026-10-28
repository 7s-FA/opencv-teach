"""Opening the offline work-monitor UI must not import Linux device locks."""
import importlib.abc
from pathlib import Path
import subprocess,sys,unittest

class MonitorImportTests(unittest.TestCase):
    def test_monitor_imports_without_linux_only_lock_and_terminal_modules(self):
        root=Path(__file__).resolve().parents[1]
        code='''import sys,importlib.abc
class BlockLinux(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in ('fcntl','termios'):raise ModuleNotFoundError('Linux-only module blocked: '+fullname)
sys.meta_path.insert(0,BlockLinux())
sys.modules.pop('fcntl',None);sys.modules.pop('termios',None)
from desktop_monitor.ui import WorkMonitorPanel
from desktop_monitor.model import summary
assert summary({},None)['phase']
'''
        result=subprocess.run([sys.executable,'-c',code],cwd=root/'SO101_teach',capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

if __name__=='__main__':unittest.main()
