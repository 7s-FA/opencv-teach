"""Preparation seam for tests of episode handoff and final-status ownership.

Real preparation, clocks, resource retention and safety are tested separately in
 test_prepared_standby. This fake does not replace the episode runner under test.
"""
class PreparedResources:
    def __init__(self,job,cli):self.job=job;self.committed=False
    @staticmethod
    def records(job,cli):
        if job.chained:return cli.chain_start_records({job.arm:(job.data,job.episode,job.bundle,job.speed,job.timeout),'arm3':job.next_job})
        return {}
    def begin(self):pass
    def prepare(self):pass
    def finish(self):
        for link in (self.job.link,self.job.peer):link.rpc('standby_complete',{'generation':1})
        self.committed=True
    def cancel(self):pass
