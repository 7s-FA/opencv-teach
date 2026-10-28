"""Teaching UI ownership for the explicit PC_BUILD_LOAD commands."""
from .pi_execution import PiExecution,execution_links,command_args
from .pc_compute_broker import configuration,ComputeBroker
from .pc_compute import PC_COMMANDS
from .episode_inspection import criteria_signature

class PcChainExecution(PiExecution):
    def __init__(self,app,command):
        if command not in PC_COMMANDS:raise ValueError('지원하지 않는 PC 협력 명령')
        self.compute_config=configuration();self.command=command;self.broker=None
        super().__init__(app)
    def make_request(self,app,*,apply_jig):
        if app.camera_only:raise ValueError('카메라 전용에서는 PC 협력 작업을 실행할 수 없습니다.')
        letter=self.command[-1]
        return {'arm':'arm2','chain_command':self.command,'apply_jig':True,'links':execution_links(app,self.manager.apps),
            'inspection_protocol':6,'inspection_criteria_sha256':criteria_signature(),
            'episode':{'id':self.command.lower(),'name':f'PC 협력 {letter} 조립·적재 · Pi 저장 레시피','product_type':letter}}
    def receiver_command(self):return command_args(self.config,receiver='pc_chain_receiver.py')
    def prepare(self):
        try:
            self.broker=ComputeBroker(self.request['execution_id'],self.command,cancel=self.cancelled,config=self.compute_config,connection=self.config)
            self.broker.start()
            self.record['pc_preparation_seconds']=self.broker.start_seconds
            self.validate_connections()
            if self.cancelled.is_set():raise RuntimeError('PC 협력 준비 취소됨')
        except Exception as exc:
            self.launch_error=str(exc);self.prepared.set();return
        super().prepare()
    def hold(self):
        errors=[]
        for key,other,link,_ in self.links:
            try:
                if other.remote is not link or not self.link_matches_request(key,link):raise RuntimeError('실행 연결 변경')
                link.rpc('command',{'action':'hold'})
            except Exception as exc:errors.append(str(exc))
        if errors:raise RuntimeError(' · '.join(errors))
    def finish(self,code,detail=''):
        was_busy=self.busy
        super().finish(code,detail)
        if was_busy and not self.busy and self.broker:
            import threading
            threading.Thread(target=self.broker.close,daemon=True,name='gui-pc-compute-close').start()
