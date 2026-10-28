"""Summarize existing execution JSON once per job; never connects to a robot."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'integration'))
sys.path.insert(0,str(ROOT))
from episode_accounting import select_jobs,summarize
from so101_teach.execution_records import read_run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path,help='episode-cli-runs directory, or a read-only capture JSON containing runs')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();records=[];errors=[]
    if args.source.is_dir():
        for path in sorted(args.source.glob('*.json')):
            try:records.append(read_run(path))
            except (OSError,ValueError) as exc:errors.append({'file':path.name,'error':type(exc).__name__})
    else:
        capture=json.loads(args.source.read_text())
        records=[r['data'] for r in capture['runs']]
    jobs,selection=select_jobs(records);conflicts=set(selection['conflicting_terminal_jobs'])
    rows=[{'source_run_id':r.get('run_id'),'operation':r['analysis'].get('operation'),
           'configuration_ids':r['analysis'].get('active_contexts',{}),
           'runtime_sha256':r['analysis'].get('environment',{}).get('runtime_sha256'),
           'terminal_conflict':job in conflicts,
           **summarize(r['analysis'],r.get('timing_summary'))} for job,r in sorted(jobs.items())]
    result={'schema_version':1,'source':str(args.source),'input_records':len(records),
        'selection':selection,'read_errors':errors,'unique_jobs':len(rows),
        'outcomes':dict(Counter(r['outcome'] for r in rows)),
        'successful_production_jobs':sum(r['successful_production'] and not r['terminal_conflict'] for r in rows),
        'jobs':rows,
        'note':'No pooled cycle-time mean across different configurations. Missing older fields remain unavailable.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({k:result[k] for k in ('input_records','unique_jobs','successful_production_jobs','selection')},ensure_ascii=False))
    return int(bool(errors or conflicts))


if __name__=='__main__':sys.exit(main())
