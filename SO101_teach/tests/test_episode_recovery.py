import json,tempfile,unittest,fcntl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from tests.fixtures import load_profile
import episode_recovery as recovery
import linear_client

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.profile,self.cal,self.ref=load_profile();self.profile['robot_id']='arm2';self.safe=self.ref.middle.copy()
        self.episode=dict(id='e',calibration_sha256=self.cal.sha256)
        recovery.save_failure(self.root,'arm2','B',self.episode,{},'failed-run',self.safe,'안착 불합격')
        recovery.save_ready(self.root,'arm3','A',self.episode,{},'arm3-ready',self.safe)
        self.links=[]
        class Link(FakeLink):
            def open(this):this.lease='lease'
            def close(this):this.lease=None
        class Linear(FakeLinear):
            def open(this):pass
            def close(this):pass
        self.Link=Link;self.linear=Linear()
    def run_reset(self,**changes):
        def link(arm):
            value=self.Link();self.links.append(value);return value
        with patch.object(cli,'ROOT',self.root),patch.object(cli,'Link',side_effect=link),patch.object(recovery,'load_profile',side_effect=lambda data:({**self.profile,'robot_id':'arm3' if data.name=='arm3-runtime' else 'arm2'},self.cal,self.ref)),patch.object(recovery.signal,'signal'),patch.object(linear_client,'LinearClient',return_value=self.linear):
            return recovery.run(cli,changes.get('arm','arm2'))
    def test_explicit_reset_after_exit_returns_saved_safe_pose_then_releases(self):
        self.assertEqual(self.run_reset(),0)
        commands=[a for m,a in self.links[0].calls if m=='command']
        self.assertEqual([c['action'] for c in commands],['arm','move'])
        self.assertTrue(all(any(m=='schedule_idle_release' for m,_ in link.calls) for link in self.links))
        self.assertEqual(commands[1]['targets'],[self.safe]);self.assertFalse(any(m=='camera_start' for m,_ in self.links[0].calls))
        self.assertEqual(json.loads(recovery.path(self.root,'arm2').read_text())['state'],'completed')
        self.assertEqual(json.loads((self.root/'data/episode-cli-arm2.json').read_text())['status'],'B_RESET_DONE')
    def test_both_arms_return_sequentially_and_only_then_report_done(self):
        order=[];original=self.Link.rpc
        def request(link,method,args=None):
            if method=='command':order.append((self.links.index(link),args['action']))
            return original(link,method,args)
        with patch.object(self.Link,'rpc',request):self.assertEqual(self.run_reset(),0)
        self.assertEqual(order,[(0,'arm'),(0,'move'),(1,'arm'),(1,'move')])
        latest=json.loads((self.root/'data/episode-cli-arm2.json').read_text())
        events=json.loads((self.root/'data/episode-cli-runs'/(latest['run_id']+'.json')).read_text())['events']
        details=[e['status'] for e in events]
        self.assertEqual(sum(x.endswith('_RESET_DONE') for x in details),1)
        self.assertLess(details.index('B_ARM_RESET_DONE:arm3'),details.index('B_RESET_DONE'))
        self.assertEqual(recovery.reset_context(self.root,'arm3')['state'],'completed')

    def test_missing_peer_safe_pose_prevents_both_arm_movements(self):
        recovery.path(self.root,'arm3').unlink()
        self.assertEqual(self.run_reset(),1);self.assertFalse(self.links)

    def test_second_arm_failure_never_reports_whole_workcell_success(self):
        original=self.Link.rpc
        def request(link,method,args=None):
            if self.links.index(link)==1 and method=='command' and args['action']=='move':raise RuntimeError('arm3 blocked')
            return original(link,method,args)
        with patch.object(self.Link,'rpc',request):self.assertEqual(self.run_reset(),1)
        latest=json.loads((self.root/'data/episode-cli-arm2.json').read_text())
        self.assertIn('FAILED:RESET:arm3 blocked',latest['status'])
        self.assertEqual(recovery.reset_context(self.root,'arm2')['state'],'completed')
        self.assertEqual(recovery.reset_context(self.root,'arm3')['state'],'ready')

    def test_arm3_endpoint_also_resets_both_arms(self):
        self.assertEqual(self.run_reset(arm='arm3'),0)
        for link in self.links:
            self.assertEqual([a['action'] for m,a in link.calls if m=='command'],['arm','move'])
        self.assertEqual(json.loads((self.root/'data/episode-cli-arm3.json').read_text())['status'],'A_RESET_DONE')

    def test_active_arm3_returns_first_and_control_events_remain_observable(self):
        arm2=self.Link();arm3=self.Link();events=[];control=cli.Control()
        runner=cli.Runner(arm3,events.append,control,120,peer=arm2,linear=self.linear)
        records={arm:recovery.reset_context(self.root,arm) for arm in ('arm2','arm3')}
        original=runner.reset_to_safe
        def active_reset():
            runner.emit('PAUSED');runner.emit('RESUMED');original()
        with patch.object(cli,'ROOT',self.root),patch.object(runner,'reset_to_safe',side_effect=active_reset),patch.object(runner,'prepare_follower',side_effect=AssertionError('must preserve active session')):
            recovery.reset_workcell(cli,records,{'arm2':arm2,'arm3':arm3},self.linear,control,events.append,active=runner,first='arm3')
        self.assertLess(events.index('ARM_RESET_DONE:arm3'),events.index('ARM_RESET_STARTED:arm2'))
        self.assertIn('PAUSED',events);self.assertIn('RESUMED',events);self.assertEqual(events[-1],'RESET_DONE')
        self.assertEqual([a['action'] for m,a in arm2.calls if m=='command'],['arm','move'])

    def test_changed_calibration_rejected_before_devices_open(self):
        file=recovery.path(self.root,'arm2');value=json.loads(file.read_text());value['calibration_sha256']='different';file.write_text(json.dumps(value))
        self.assertEqual(self.run_reset(),1);self.assertFalse(self.links)
    def test_another_job_lock_blocks_recovery(self):
        with (self.root/'data/episode-cli.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(self.run_reset(),1)
        self.assertFalse(self.links)
    def test_unconfirmed_linear_state_never_moves_arm(self):
        self.linear.phase='UNKNOWN';self.assertEqual(self.run_reset(),1)
        self.assertFalse(any(m=='command' and a['action']=='move' for m,a in self.links[0].calls))
        self.assertEqual(recovery.pending(self.root,'arm2')['state'],'pending')
    def test_new_job_invalidates_old_recovery(self):
        recovery.invalidate(self.root,'arm2')
        with self.assertRaises(ValueError):recovery.pending(self.root,'arm2')
    def test_cli_reset_without_active_socket_uses_recovery(self):
        with patch.object(cli,'send_control',side_effect=FileNotFoundError),patch('action_protocol.record_notice'),patch.object(recovery,'run',return_value=0) as recover:
            self.assertEqual(cli.main(['reset','--recovery-arm','arm3','--no-ros']),0)
        self.assertEqual(recover.call_args.args[1],'arm3')

    def test_legacy_failure_requires_matching_inspection_record(self):
        recovery.path(self.root,'arm2').unlink();self.episode['steps']=[dict(safe_boundary='end',ticks=self.safe)]
        (self.root/'data/episode-cli-arm2.json').write_text(json.dumps(dict(status='B_FAILED:안착 불합격',at=100,run_id='old')))
        folder=self.root/'data/diagnostics';folder.mkdir();(folder/'episode-inspection-1.json').write_text(json.dumps(dict(episode_id='e',checks=[dict(result='FAIL',error='안착 불합격',at=99)])))
        with patch.object(cli,'ROOT',self.root),patch.object(cli,'load_job',return_value=(None,self.episode,{},120,120)):
            recovery.restore_legacy_failure(cli,'arm2')
        self.assertEqual(recovery.pending(self.root,'arm2')['safe_ticks'],self.safe)
    def test_return_failure_preserves_recovery_and_holds(self):
        original=self.Link.rpc
        def request(link,method,args=None):
            if method=='command' and args['action']=='move':raise RuntimeError('return failed')
            return original(link,method,args)
        with patch.object(self.Link,'rpc',request):self.assertEqual(self.run_reset(),1)
        self.assertEqual(recovery.pending(self.root,'arm2')['state'],'pending')
        self.assertTrue(any(m=='command' and a['action']=='hold' for m,a in self.links[0].calls))

    def test_check_only_reset_never_sends_control_or_opens_devices(self):
        with patch.object(cli,'send_control',side_effect=AssertionError('no commands')),patch.object(recovery,'run',side_effect=AssertionError('no recovery')):
            self.assertEqual(cli.main(['reset','--check','--no-ros']),0)

    def test_recovery_speed_passes_real_motion_session_validation(self):
        from so101_teach.motion import MotionSession,SPEED_PRESETS
        original=self.Link.rpc;cal=self.cal;drivers=[]
        def request(link,method,args=None):
            if method=='connect':drivers.append(MotionSession('unused',cal,rate_ticks_s=args['speed']))
            if method=='speed':drivers[-1].set_speed(args['rate'])
            return original(link,method,args)
        with patch.object(self.Link,'rpc',request),patch.object(MotionSession,'start',side_effect=AssertionError('no hardware')):
            self.assertEqual(self.run_reset(),0)
        self.assertEqual(drivers[0].rate_ticks_s,min(SPEED_PRESETS.values()))

    def test_completed_reset_can_return_again_after_torque_off(self):
        self.assertEqual(self.run_reset(),0)
        self.assertEqual(self.run_reset(),0)
        commands=[a['action'] for m,a in self.links[2].calls if m=='command']
        self.assertEqual(commands,['arm','move'])

    def test_normal_job_safe_pose_is_available_without_failure(self):
        recovery.save_ready(self.root,'arm2','B',self.episode,{},'normal-job',self.safe)
        self.assertEqual(recovery.reset_context(self.root,'arm2')['state'],'ready')
        self.assertEqual(self.run_reset(),0)

    def test_active_reset_with_torque_off_arms_before_return(self):
        link=FakeLink();events=[];run=cli.Runner(link,events.append,cli.Control(),30,linear=FakeLinear());run.safe_ticks=self.safe
        run.reset_to_safe()
        self.assertEqual([a['action'] for m,a in link.calls if m=='command'],['arm','move'])
        self.assertLess(events.index('RESET_TORQUE_ON_CONFIRMED'),events.index('SAFE_RETURN_STARTED'))
        self.assertEqual(events[-1],'RESET_DONE')

    def test_missing_safe_pose_with_torque_off_never_enables_motors(self):
        link=FakeLink();run=cli.Runner(link,lambda _:None,cli.Control(),30)
        with self.assertRaisesRegex(RuntimeError,'안전 자세'):run.reset_to_safe()
        self.assertFalse(link.calls)

    def test_legacy_completed_job_requires_unchanged_recipe_before_repeated_reset(self):
        import time,os
        recovery.path(self.root,'arm2').unlink();self.episode['steps']=[dict(safe_boundary='end',ticks=self.safe)]
        (self.root/'integration').mkdir();recipe=self.root/'integration/recipes.json';recipe.write_text('{}')
        (self.root/'data/episodes').mkdir();(self.root/'data/episodes/e.json').write_text('{}')
        at=time.time()+1;(self.root/'data/episode-cli-arm2.json').write_text(json.dumps(dict(status='B_DONE',at=at,run_id='old-complete')))
        with patch.object(cli,'ROOT',self.root),patch.object(cli,'load_job',return_value=(self.root/'data',self.episode,{},300,120)):
            recovery.restore_legacy_failure(cli,'arm2')
            self.assertEqual(recovery.reset_context(self.root,'arm2')['state'],'ready')
            recovery.path(self.root,'arm2').unlink();os.utime(recipe,(at+1,at+1));recovery.restore_legacy_failure(cli,'arm2')
            with self.assertRaises(ValueError):recovery.reset_context(self.root,'arm2')
