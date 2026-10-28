"""Prepare and hand off resources while the ordinary job owns the workcell."""
import time
from copy import deepcopy


class WorkcellPreparation:
    def __init__(self,job,cli):
        self.job=job;self.cli=cli;self.generations={};self.committed=False
        self.links={job.link.arm:job.link,job.peer.arm:job.peer}
        self.records=job.standby_records
    @staticmethod
    def records(job,cli):
        if job.slide_only:
            jobs={arm:{letter:cli.load_job(arm,letter,timing=job.control.startup_timing)
                       for letter in ('A','B')} for arm in ('arm2','arm3')}
            records=cli.chain_start_records({arm:values['A'] for arm,values in jobs.items()})
            other=cli.chain_start_records({arm:values['B'] for arm,values in jobs.items()})
            saved=None
            for arm in records:
                if jobs[arm]['A'][2]!=jobs[arm]['B'][2]:raise ValueError(arm+': 준비 중 현재 설정이 변경되었습니다.')
                if records[arm]['safe_ticks']!=other[arm]['safe_ticks']:
                    from episode_recovery import workcell_records
                    if saved is None:saved=workcell_records(cli.recovery_context())
                    # Use only the validated saved pose, with the current ROI,
                    # calibration, model and recipe configuration loaded above.
                    records[arm]['safe_ticks']=deepcopy(saved[arm]['safe_ticks'])
            return records
        jobs={job.arm:(job.data,job.episode,job.bundle,job.speed,job.timeout)}
        peer='arm3' if job.arm=='arm2' else 'arm2'
        jobs[peer]=job.next_job if job.chained else cli.load_job(peer,job.letter,timing=job.control.startup_timing)
        return cli.chain_start_records(jobs)
    def begin(self):
        j=self.job
        for arm,link in self.links.items():
            reply=link.rpc('standby_begin',{'client_id':getattr(j.args,'supervision_client_id',None)})
            self.generations[arm]=reply['generation'];link.keep_prepared=True
    def prepare(self):
        from episode_recovery import prepare_chain_start
        from so101_teach.motion import SPEED_PRESETS
        j=self.job;runner=j.runner
        runner.emit('WORKCELL_PREPARING')
        speed=getattr(j,'speed',min(SPEED_PRESETS.values()))
        receipts=prepare_chain_start(self.cli.recovery_context(),self.records,self.links,j.linear,j.control,
            runner.emit,active=runner,speed=speed,on_started=lambda:runner.start_linear(j.target,defer_pause=True))
        j.chain_start={'records':self.records,'links':self.links,'receipts':receipts}
        if not j.slide_only:
            runner.chain_start=j.chain_start;runner.prepared_start=receipts[j.arm]
        self.warm()
        if j.slide_only:runner.wait_linear()
        runner.emit('WORKCELL_READY')
    def warm(self):
        j=self.job
        for arm in ('arm2','arm3'):
            j.runner.check()
            self.links[arm].rpc('detect_freeze',{'enabled':False})
            self.links[arm].rpc('detect_clear')
            self.links[arm].rpc('inspection_prepare',{'profile':self.records[arm]['bundle']['profile']})
        previous=j.control.emergency
        try:
            # Start the physical owner first. The second arm receives its original
            # lossless captures and retains its own detector/calibration context.
            for arm in ('arm2','arm3'):
                runner=self.cli.Runner(self.links[arm],j.status.emit,j.control,120,
                                      peer=self.links['arm3' if arm=='arm2' else 'arm2'],linear=j.linear)
                runner.prepare_camera();runner.await_camera_ready()
                deadline=time.monotonic()+60
                while True:
                    runner.check()
                    state=self.links[arm].rpc('inspection_status')
                    if state.get('error'):raise RuntimeError('검사 모델 준비 실패: '+state['error'])
                    if state.get('ready'):break
                    if time.monotonic()>=deadline:raise TimeoutError('검사 모델 준비 시간 초과')
                    time.sleep(.05)
        finally:j.control.emergency=previous

    def finish(self):
        j=self.job
        j.runner.check()
        self.begin()  # A resumed, normally completed job starts a fresh generation.
        # PC computation is job-bound. Detach its descriptor before that broker
        # exits; the normal Pi models remain ready for the next arbitrary command.
        if j.pc_command:
            for arm,link in self.links.items():
                link.compute=None
                link.rpc('configure',{'bundle':self.records[arm]['bundle']})
        self.warm()
        completed_at=time.monotonic()
        for arm,link in self.links.items():
            j.runner.check()
            link.rpc('standby_complete',{'generation':self.generations[arm],'completed_at':completed_at})
        self.committed=True
        j.status.emit('STANDBY_READY:seconds=180')
    def cancel(self):
        for link in self.links.values():
            link.keep_prepared=False
            try:link.rpc('standby_cancel')
            except Exception:pass
