"""Real Tk + two HTTP runtimes, simulated motion only; no physical hardware."""
import json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from PIL import ImageGrab
from so101_teach.domain import ROOT,JOINTS,Snapshot,read_json,atomic_json
from so101_teach.demo_workspace import prepare_data
from so101_teach.arm_workspaces import ArmWorkspaces
from so101_teach.remote_server import Runtime,Server,Handler
from so101_teach.remote_client import RemoteLink
from tests.test_remote import FakeMotion,FakeCamera

class StartupProbeTests(unittest.TestCase):
    def test_readonly_startup_job_boundaries_and_explicit_reconnect(self):
        out=ROOT/'verification/manual-control-20261026';out.mkdir(parents=True,exist_ok=True)
        runtimes={};servers=[];links=[];manager=None;root=None;requests=[];external={};phases=[]
        class LocalRuntime(Runtime):
            def state(self,*args,**kwargs):
                if self.session and self.session.running:
                    ticks=self.ref.middle
                    telemetry={n:{'torque':0,'moving':0,'voltage_v':12.,'temperature_c':25,'load_raw':0,'current_raw':0,'status':0,'goal_ticks':ticks[n]} for n in JOINTS}
                    self.session.latest=Snapshot('follower',ticks,telemetry,time.monotonic(),time.time(),self.cal.sha256,True,self.profile['port'])
                return super().state(*args,**kwargs)
        def open_link(link):
            runtime=runtimes[link.robot_id];server=next(s for s in servers if s.runtime is runtime)
            link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token
            link.health={'devices':{'serial':[],'cameras':[],'camera_details':[]}};link.instance=runtime.instance
            requests.append(link.robot_id)
            acquired=link.http('/acquire',{});link.lease=acquired['lease'];link.event_id=acquired['event_id']
            link.ui_heartbeat=time.monotonic();link.state_thread=threading.Thread(target=link.poll_state,daemon=True);link.state_thread.start();links.append(link)
            return link
        def pump(seconds=.5,condition=None):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                for arm,lease in external.items():runtimes[arm].heartbeat(lease,True)
                root.update();time.sleep(.015)
                if condition and condition():return
            if condition:self.fail('Timed out: '+str({k:a.settings.pi_panel.status.get() for k,a in manager.apps.items()}))
        def record(name):
            phases.append({'phase':name,'acquire_attempts':len(requests),'teaching_links':sum(a.remote is not None and not a.remote.error for a in manager.apps.values())})
        def release_external():
            for arm,lease in external.items():
                runtime=runtimes[arm]
                if runtime.session:runtime.session.state='READ_ONLY';runtime.session.program_active.clear()
                runtime.detach(lease)
            external.clear()
        with tempfile.TemporaryDirectory() as temp:
            data=prepare_data(destination=Path(temp)/'app');prefs=read_json(data/'preferences.json');prefs['device_host']='pi';atomic_json(data/'preferences.json',prefs)
            for arm in ('arm2','arm3'):
                runtime=LocalRuntime(Path(temp)/arm,motion_factory=FakeMotion,camera_factory=FakeCamera,robot_id=arm);runtimes[arm]=runtime
                external[arm]=runtime.acquire()['lease']
                server=Server(('127.0.0.1',0),Handler);server.runtime=runtime;server.token='test-'+arm;servers.append(server)
                threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with patch.object(RemoteLink,'open',open_link),patch('so101_teach.pi_episode_library.PiEpisodeLibrary.refresh'),patch('so101_teach.linear_state.StateStream.start'),patch('so101_teach.motion.new_bus',side_effect=AssertionError('No hardware')):
                    root=tk.Tk();root.withdraw();root.tk.call('tk','scaling',4/3)
                    manager=ArmWorkspaces(root,data,render=True,auto_camera=False,auto_devices=True,mode_override='follower')
                    root.geometry('1920x1080+0+0');root.deiconify()
                    pump(2);self.assertEqual(requests,[]);record('startup_while_external_owner_active')
                    ImageGrab.grab().save(out/'readonly-startup.png')
                    # An intentional click while busy fails once per arm.
                    manager.apps[manager.active].connect_all_btn.invoke()
                    pump(5,lambda:all(a.settings.pi_panel.job is None for a in manager.apps.values()))
                    self.assertEqual(len(requests),2)
                    self.assertTrue(all(a.remote is None for a in manager.apps.values()))
                    self.assertTrue(all(runtimes[k].lease==v for k,v in external.items()))
                    record('explicit_request_rejected_by_external_owner')
                    release_external()
                    # Expired retry deadlines cannot acquire during the gap.
                    for a in manager.apps.values():a.settings.pi_panel.retry_at=0
                    pump(2);self.assertEqual(len(requests),2);record('between_jobs_no_retry')
                    external.update({arm:r.acquire()['lease'] for arm,r in runtimes.items()})
                    pump(.5);self.assertEqual(len(requests),2);record('next_external_job_acquires_both_arms')
                    release_external()
                    manager.apps[manager.active].connect_all_btn.invoke()
                    pump(10,lambda:all(a.session and a.session.running and a.latest for a in manager.apps.values()))
                    self.assertEqual(len(requests),4);record('explicit_request_connects_both_arms')
                    self.assertTrue(all(r.session.commands.empty() for r in runtimes.values()))
                    # A simulated transport loss must not trigger another open.
                    for a in manager.apps.values():a.remote.fail('simulated transport loss')
                    pump(2.5)
                    self.assertTrue(all(not a.settings.pi_panel.auto_enabled for a in manager.apps.values()))
                    self.assertEqual(len(requests),4);record('connection_loss_no_retry')
                    external.update({arm:r.acquire()['lease'] for arm,r in runtimes.items()})
                    # Model a running Pi program while the desktop stays open.
                    for r in runtimes.values():r.session.state='MOVING';r.session.program_active.set();r.session.hold_requested.clear()
                    pump(.5);self.assertEqual(len(requests),4)
                    self.assertTrue(all(r.session.state=='MOVING' and not r.session.hold_requested.is_set() for r in runtimes.values()))
                    record('external_program_undisturbed_after_desktop_loss')
                    release_external();pump(.5);self.assertEqual(len(requests),4)
                    manager.apps[manager.active].connect_all_btn.invoke()
                    pump(10,lambda:all(a.remote and not a.remote.error and a.session and a.session.running for a in manager.apps.values()))
                    self.assertEqual(len(requests),6);record('second_explicit_request_reconnects')
                    self.assertTrue(all(r.session.commands.empty() for r in runtimes.values()))
                    ImageGrab.grab().save(out/'explicit-connected.png')
                    atomic_json(out/'ownership-probe.json',{'phases':phases,'motion_commands':0,'physical_hardware':False,'status':'passed'})
            finally:
                if manager:
                    manager.finish_safe_close()
                    until=time.monotonic()+5
                    while time.monotonic()<until:
                        try:root.update()
                        except tk.TclError:break
                        if manager.finished==set(manager.apps):break
                        time.sleep(.02)
                for link in links:link.close(disconnect=False)
                for server in servers:server.shutdown();server.server_close()
                for runtime in runtimes.values():runtime.close()
