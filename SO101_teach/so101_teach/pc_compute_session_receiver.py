"""Register one authenticated SSH compute lease, without opening any device."""
import json
import os
from pathlib import Path
import re
import sys
import time
from .domain import ROOT
from .pc_compute import ComputeClient,PC_COMMANDS

def main():
    raw=sys.stdin.readline(16384)
    value=json.loads(raw);descriptor=value['descriptor'];command=value['command'];job_id=descriptor.get('job_id','')
    if command not in PC_COMMANDS or not re.fullmatch('[0-9a-f]{32}',job_id):raise ValueError('Invalid PC compute job')
    ComputeClient(descriptor).call('hello',timeout=3.)
    folder=ROOT/'data/pc-compute-sessions';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=folder/(job_id+'.json')
    # Exclusive registration prevents rebinding an existing business request.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump({'command':command,'descriptor':descriptor,'expires_at':time.time()+90},stream)
            stream.flush();os.fsync(stream.fileno())
        print('PC_COMPUTE_REGISTERED:'+job_id,flush=True)
        sys.stdin.read()
    finally:path.unlink(missing_ok=True)
if __name__=='__main__':main()
