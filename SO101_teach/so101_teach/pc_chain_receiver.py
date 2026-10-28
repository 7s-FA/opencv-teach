"""GUI PC chain adapter: execute the existing Pi A/B recipes with borrowed leases."""
import json
from pathlib import Path
import re
import sys


def validate(cli,request):
    from so101_teach.pc_compute import PC_COMMANDS
    from so101_teach.episode_inspection import criteria_signature
    if request.get('chain_command') not in PC_COMMANDS or request.get('apply_jig') is not True:raise ValueError('Invalid PC chain command')
    if request.get('arm')!='arm2' or not re.fullmatch('[0-9a-f]{32}',request.get('execution_id','')):raise ValueError('Invalid PC chain identity')
    if request.get('inspection_protocol')!=cli.INSPECTION_PROTOCOL or request.get('inspection_criteria_sha256')!=criteria_signature():raise ValueError('PC/Pi inspection criteria differ')
    if set(request.get('links',{}))!={'arm2','arm3'}:raise ValueError('Both arm leases are required')
    for link in request['links'].values():
        if type(link.get('port')) is not int or not 1<=link['port']<=65535 or not all(isinstance(link.get(k),str) and link[k] for k in ('token','lease','instance')):raise ValueError('Invalid arm lease')
    if request.get('episode',{}).get('product_type')!=request['chain_command'][-1]:raise ValueError('PC chain product mismatch')


def main():
    root=Path(sys.argv[1]).expanduser().resolve();sys.path[:0]=[str(root),str(root/'integration')]
    import episode_cli as cli
    from so101_teach.pi_execution_receiver import install_adapters
    request=json.loads(sys.stdin.readline(1024*1024));validate(cli,request)
    command=request['chain_command'].lower()
    if '--check' in sys.argv[2:]:return cli.main([command,'--check','--no-ros'])
    load=cli.load_job
    install_adapters(cli,request,None,sys.stdin)
    cli.load_job=load  # Both saved Pi recipes and their speed/thresholds remain authoritative.
    result=cli.main([command,'--supervision-job-id',request['execution_id']])
    print('GUI_RESULT:'+json.dumps({'execution_id':request['execution_id'],'exit_code':result}),flush=True)
    return result
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print('GUI_REJECTED:'+str(exc),flush=True);raise SystemExit(1)
