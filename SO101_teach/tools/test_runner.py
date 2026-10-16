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

class LocalTestLoader(unittest.TestLoader):
    def loadTestsFromModule(self,module,*,pattern=None):
        # Imported TestCase classes are fixtures, not another execution of the
        # source module. Explicit class/method selectors remain available.
        suite=super().loadTestsFromModule(module,pattern=pattern)
        def local(tests):
            for test in tests:
                if isinstance(test,unittest.TestSuite):yield from local(test)
                elif type(test).__module__ in (module.__name__,'unittest.loader'):yield test
        return self.suiteClass(local(suite))


class UIThreadRunner(unittest.TextTestRunner):
    resultclass=UIThreadResult

if __name__=='__main__':
    gc.disable()
    unittest.main(module=None,testRunner=UIThreadRunner,testLoader=LocalTestLoader())
