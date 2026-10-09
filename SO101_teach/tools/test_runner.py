"""Keep Tcl cyclic cleanup on the UI thread during repeated native-window tests."""
import gc
from pathlib import Path
import sys
import unittest

# Match `python -m unittest` imports when invoked through run_tests.sh.
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class UIThreadResult(unittest.TextTestResult):
    def startTest(self,test):
        # Earlier test cases have now been released by TestSuite. Tk variables
        # can form cycles; collecting them in a camera/pool thread aborts Tcl.
        if type(test) is not getattr(self,'previous_test_class',None):
            gc.collect()
            self.previous_test_class=type(test)
        super().startTest(test)

class UIThreadRunner(unittest.TextTestRunner):
    resultclass=UIThreadResult

if __name__=='__main__':
    gc.disable()
    unittest.main(module=None,testRunner=UIThreadRunner)
