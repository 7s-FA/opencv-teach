"""Reproduce the actual two-arm startup against local HTTP runtimes, no hardware."""
import cProfile,io,json,pstats,tempfile,threading,time,unittest
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
    def test_profile_native_startup_with_two_remote_connections(self):
        out=ROOT/'verification/live-app-audit';out.mkdir(parents=True,exist_ok=True)
        runtime_by_arm={};servers=[];links=[];manager=None;root=None;slow=[];gaps=[]
        class LocalRuntime(Runtime):
            def state(self,*args,**kwargs):
                if self.session and self.session.running:
                    ticks=self.ref.middle
                    telemetry={n:{'torque':0,'moving':0,'voltage_v':12.,'temperature_c':25,'load_raw':0,'current_raw':0,'status':0,'goal_ticks':ticks[n]} for n in JOINTS}
                    self.session.latest=Snapshot('follower',ticks,telemetry,time.monotonic(),time.time(),self.cal.sha256,True,self.profile['port'])
                return super().state(*args,**kwargs)
        def open_link(link):
            runtime=runtime_by_arm[link.robot_id];server=next(s for s in servers if s.runtime is runtime)
            link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token
            link.health={'devices':{'serial':[],'cameras':[],'camera_details':[]}};link.instance=runtime.instance
            acquired=link.http('/acquire',{});link.lease=acquired['lease'];link.event_id=acquired['event_id']
            link.ui_heartbeat=time.monotonic();link.state_thread=threading.Thread(target=link.poll_state,daemon=True);link.state_thread.start();links.append(link)
            return link
        with tempfile.TemporaryDirectory() as temp:
            data=prepare_data(destination=Path(temp)/'app');prefs=read_json(data/'preferences.json');prefs['device_host']='pi';atomic_json(data/'preferences.json',prefs)
            for arm in ('arm2','arm3'):
                runtime=LocalRuntime(Path(temp)/arm,motion_factory=FakeMotion,camera_factory=FakeCamera,robot_id=arm);runtime_by_arm[arm]=runtime
                server=Server(('127.0.0.1',0),Handler);server.runtime=runtime;server.token='test-'+arm;servers.append(server)
                threading.Thread(target=server.serve_forever,daemon=True).start()
            original=tk.CallWrapper.__call__
            def timed(wrapper,*args):
                started=time.monotonic()
                try:return original(wrapper,*args)
                finally:
                    elapsed=time.monotonic()-started
                    if elapsed>.1:slow.append({'callback':repr(wrapper.func),'seconds':elapsed})
            profiler=cProfile.Profile()
            try:
                with patch.object(RemoteLink,'open',open_link),patch('so101_teach.pi_episode_library.PiEpisodeLibrary.refresh'),patch('so101_teach.linear_state.StateStream.start'),patch('so101_teach.motion.new_bus',side_effect=AssertionError('No hardware')),patch.object(tk.CallWrapper,'__call__',timed):
                    root=tk.Tk();root.withdraw();default_scaling=float(root.tk.call('tk','scaling'));root.tk.call('tk','scaling',4/3)
                    profiler.enable();manager=ArmWorkspaces(root,data,render=True,auto_camera=False,auto_devices=True,mode_override='follower')
                    root.geometry('1920x1043');root.deiconify();last=time.monotonic()
                    deadline=time.monotonic()+20
                    beat_job=[None]
                    def beat():
                        nonlocal last
                        now=time.monotonic();gaps.append(now-last);last=now
                        if now<deadline:beat_job[0]=root.after(50,beat)
                    beat_job[0]=root.after(50,beat)
                    root.after(20000,root.quit);root.mainloop()
                    if beat_job[0]:root.after_cancel(beat_job[0])
                    profiler.disable();a=manager.apps[manager.active]
                    root.update_idletasks();ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+1920,root.winfo_rooty()+1043)).save(out/'startup-reproduced.png')
                    report={'default_test_scaling':default_scaling,'actual_scaling':float(root.tk.call('tk','scaling')),'max_ui_gap':max(gaps,default=0),'slow_callbacks':slow,'links':[{'arm':x.robot_id,'error':x.error,'events':list(x.transport_events)} for x in links],
                            'status':{key:{'message':app.message.get(),'pi_status':app.settings.pi_panel.status.get(),'connection':app.connection.get(),'heartbeat_age':time.monotonic()-app.ui_heartbeat,'badge_width':app.badge.winfo_width(),'badge_reqwidth':app.badge.winfo_reqwidth()} for key,app in manager.apps.items()}}
                    (out/'startup-profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False),flush=True)
                    stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumtime').print_stats(30);(out/'startup-profile.txt').write_text(stream.getvalue());print(stream.getvalue(),flush=True)
                    self.assertEqual(len(links),2,report['status'])
                    self.assertTrue(all(link.error is None for link in links))
                    self.assertLess(max(gaps),1.,'The UI must keep the existing 1.5-second heartbeat fresh')
            finally:
                profiler.disable()
                if manager:
                    manager.finish_safe_close()
                    until=time.monotonic()+5
                    while time.monotonic()<until:
                        try:root.update()
                        except tk.TclError:break
                        if all(a.closed for a in manager.apps.values()):break
                        time.sleep(.02)
                for link in links:link.close(disconnect=False)
                for server in servers:server.shutdown();server.server_close()
                for runtime in runtime_by_arm.values():runtime.close()
