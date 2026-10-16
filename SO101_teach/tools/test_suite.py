"""Run each regression module in a fresh process to isolate Tk/GL and signals."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


def run(output):
    modules=sorted('tests.'+path.stem for path in (ROOT/'tests').glob('test_*.py'))
    results=[]
    for index,module in enumerate(modules,1):
        path=output/(module+'.log');started=time.monotonic()
        with path.open('w') as stream:
            process=subprocess.Popen([sys.executable,str(ROOT/'tools/test_runner.py'),module,'-v'],cwd=ROOT,
                                     stdout=stream,stderr=subprocess.STDOUT)
            try:code=process.wait(timeout=360)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                code=124
        log=path.read_text(errors='replace');count=re.search(r'Ran (\d+) tests? in',log)
        row={'module':module,'exit_code':code,'tests':int(count[1]) if count else 0,
             'seconds':round(time.monotonic()-started,3),'log':str(path)}
        results.append(row)
        print(f'[{index}/{len(modules)}] {module}: '+('PASS' if code==0 else f'FAIL ({code})')+f" · {row['tests']} tests · {row['seconds']}s",flush=True)
        if code:print(log[-30000:],flush=True)
        (output/'summary.json').write_text(json.dumps(results,indent=2))
    failed=[r for r in results if r['exit_code']]
    print(f"Modules: {len(results)}, tests: {sum(r['tests'] for r in results)}, failed modules: {len(failed)}",flush=True)
    return bool(failed)


if __name__=='__main__':
    folder=os.environ.get('SO101_TEST_REPORT_DIR')
    if folder:
        output=Path(folder).resolve();output.mkdir(parents=True,exist_ok=True);sys.exit(run(output))
    with tempfile.TemporaryDirectory(prefix='so101-regression-') as temp:sys.exit(run(Path(temp)))
