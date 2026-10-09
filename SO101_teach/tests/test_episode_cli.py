import importlib.util
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from copy import deepcopy

spec = importlib.util.spec_from_file_location('episode_cli', Path(__file__).parents[1] / 'integration/episode_cli.py')
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)


class FakeLink:
    error = None
    lease = 'test'
    def __init__(self):
        self.mode = 'READ_ONLY'; self.calls = []; self.request = None; self.completed = None
        self.index = 0; self.pending = False; self.stale = False; self.auto_finish = True; self.frame = {}
    def state(self):
        return {'running': True, 'error': None, 'state': self.mode, 'program_active': self.pending,
                'command_pending': False, 'completed_request_id': self.completed, 'request_id': self.request,
                'index': self.index, 'latest': {'calibration_matches': True,
                    'monotonic': time.monotonic() - (5 if self.stale else 0), 'ticks':{str(i):2048 for i in range(6)},
                    'telemetry': {str(i): {'moving': 0, 'torque': 0 if self.mode == 'READ_ONLY' else 1} for i in range(6)}}}
    def rpc(self, method, args=None):
        args = args or {}; self.calls.append((method, deepcopy(args)))
        if method == 'command':
            action = args['action']
            if action == 'release': self.mode = 'READ_ONLY'; self.pending = False
            elif action == 'hold': self.mode = 'HOLD'; self.pending = False
            elif action == 'arm': self.mode = 'HOLD'
            else:
                self.request = len(self.calls); self.index = 0
                self.mode = 'HOLD' if self.auto_finish else 'MOVING'; self.pending = not self.auto_finish
                if self.auto_finish: self.completed = self.request; self.index = len(args['targets'])
            return {'request_id': self.request}
        if method == 'plan': return {'plan': [{'ticks': s['ticks']} for s in args['steps']]}
        return {}
    def post(self, endpoint, body=None):
        if endpoint == '/camera': return self.frame
        if endpoint == '/state': return {'follower': self.state()}
        return self.rpc(body['method'], body['args'])


class FakeLinear:
    error = None
    def __init__(self): self.calls = []; self.phase = 'TIMED_COMPLETE'; self.target = 100.
    def call(self, op, **args):
        self.calls.append((op, args))
        if op == 'ensure': self.target = args['target_mm']
        if op == 'cancel': self.phase = 'UNKNOWN'
        return {'phase': self.phase, 'target_mm': self.target, 'remaining_s': 0.}


class DirectEpisodeTests(unittest.TestCase):
    def test_explicit_completion_follows_step_id_after_rename(self):
        link=FakeLink();link.mode='HOLD';run=self.runner(link)
        steps=[{'id':'first','name':'이름 변경','ticks':{}},{'id':'last','name':'하단 : 다른 동작','ticks':{}}]
        run.move('play',[{},{}],steps,{'LOWER':'first'})
        self.assertEqual(self.events.count('STAGE_DONE:LOWER'),1)
        self.assertLess(self.events.index('STAGE_DONE:LOWER'),self.events.index('STEP_DONE:2/2:하단 : 다른 동작'))
    def test_empty_explicit_completion_disables_legacy_name_rule(self):
        link=FakeLink();link.mode='HOLD';run=self.runner(link)
        run.move('play',[{}],[{'id':'first','name':'하단 : 마지막','ticks':{}}],{})
        self.assertFalse(any('STAGE_DONE' in event for event in self.events))
    def runner(self, link=None):
        self.events = []; self.control = cli.Control(); self.linear = FakeLinear()
        return cli.Runner(link, self.events.append, self.control, 20, linear=self.linear)

    def clock(self):
        clock = [100.]
        return clock, patch.object(cli.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(cli.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def test_complete_requests_release_without_waiting_for_torque_confirmation(self):
        link = FakeLink(); run = self.runner(link)
        ep = {'steps': [{'name': 'safe', 'ticks': {}, 'safe_boundary': 'start'}, {'name': '하단 : 놓고 들기', 'ticks': {}}, {'name': 'safe', 'ticks': {}, 'safe_boundary': 'end'}]}
        clock, monotonic, sleep = self.clock()
        with monotonic, sleep: run.execute(ep, {}, 400, 100.)
        self.assertEqual(self.events[-2:], ['EPISODE_DONE', 'DONE'])
        self.assertLess(self.events.index('LINEAR_READY:target_mm=100:position_measured=false'), self.events.index('EPISODE_STARTED'))
        self.assertIn('STAGE_DONE:LOWER', self.events)
        self.assertLess(clock[0],101.)
        self.assertFalse(any(m=='command' and a.get('action')=='release' for m,a in link.calls))

    def test_completion_returns_while_release_telemetry_is_still_on(self):
        link = FakeLink(); run = self.runner(link); original = link.rpc
        def dispatch(method, args=None):
            if method == 'command' and args['action'] == 'release':
                link.calls.append((method, deepcopy(args))); return {}
            return original(method, args)
        link.rpc = dispatch
        ep = {'steps': [{'name': 'safe', 'ticks': {}, 'safe_boundary': 'end'}]}
        run.execute(ep, {}, 400, 100.)
        self.assertEqual(self.events[-1], 'DONE')
        self.assertNotIn('TORQUE_OFF_CONFIRMED', self.events)
        self.assertFalse(cli.torque_off(link.state()))
        self.assertEqual(sum(m == 'command' and a['action'] == 'release' for m,a in link.calls), 0)

    def test_completion_does_not_issue_a_late_direct_release(self):
        link = FakeLink(); run = self.runner(link); original = link.rpc
        def dispatch(method, args=None):
            if method == 'command' and args['action'] == 'release':
                raise ConnectionError('release rejected')
            return original(method, args)
        link.rpc = dispatch
        ep = {'steps': [{'name': 'safe', 'ticks': {}, 'safe_boundary': 'end'}]}
        run.execute(ep, {}, 400, 100.)
        self.assertIn('DONE', self.events)
        self.assertNotIn('TORQUE_OFF_REQUESTED', self.events)

    def test_resume_keeps_remaining_target_without_replaying_finished_steps(self):
        link = FakeLink(); link.mode = 'HOLD'; link.auto_finish = False; run = self.runner(link)
        original = link.rpc
        def dispatch(method, args=None):
            value = original(method, args)
            if method == 'command' and args['action'] == 'play' and len(args['targets']) == 3:
                self.control.pause.set()
            return value
        link.rpc = dispatch
        def paused():
            link.index = 1; link.mode = 'HOLD'; link.pending = False
            state = link.state(); self.control.pause.clear(); link.auto_finish = True
            return state
        with patch.object(run, 'await_pause', side_effect=paused):
            run.move('play', [{'n': 1}, {'n': 2}, {'n': 3}])
        self.assertEqual([a['targets'] for m, a in link.calls if m == 'command'], [[{'n': 1}, {'n': 2}, {'n': 3}], [{'n': 2}, {'n': 3}]])

    def test_completed_request_at_estop_is_not_replayed(self):
        link = FakeLink(); link.mode = 'HOLD'; run = self.runner(link); original = link.rpc
        def dispatch(method, args=None):
            result = original(method, args)
            if method == 'command': self.control.pause.set()
            return result
        link.rpc = dispatch
        def paused(): self.control.pause.clear(); return link.state()
        with patch.object(run, 'await_pause', side_effect=paused): run.move('play', [{}])
        self.assertEqual(len([m for m, _ in link.calls if m == 'command']), 1)

    def test_estop_holds_no_safe_return_and_restart_resumes(self):
        link = FakeLink(); link.mode = 'MOVING'; link.pending = True; run = self.runner(link)
        self.control.command('estop')
        def wake(_):
            if self.control.paused: self.control.command('restart')
        with patch.object(cli.time, 'sleep', side_effect=wake): run.await_pause()
        self.assertIn('PAUSED', self.events); self.assertIn('RESUMED', self.events)
        self.assertTrue(all(a['action'] == 'hold' for m, a in link.calls if m == 'command'))

    def test_reset_only_accepted_when_paused(self):
        control = cli.Control()
        for command in ('restart', 'reset'):
            with self.assertRaisesRegex(ValueError, 'NOT_PAUSED'): control.command(command)
        control.paused = True; self.assertEqual(control.command('reset'), 'RESET_REQUESTED')

    def test_reset_wait_loop_raises_then_safe_return_and_off(self):
        link = FakeLink(); link.mode = 'HOLD'; run = self.runner(link); run.safe_ticks = {'safe': 1}
        self.control.pause.set()
        def reset(_):
            if self.control.paused: self.control.command('reset')
        with patch.object(cli.time, 'sleep', side_effect=reset):
            with self.assertRaises(cli.ResetRequested): run.await_pause()
        run.reset_to_safe()
        commands = [a for m, a in link.calls if m == 'command']
        self.assertEqual(commands[-1:], [{'action': 'move', 'targets': [{'safe': 1}]}])
        self.assertEqual(self.events[-1], 'RESET_DONE')

    def test_missing_safe_pose_does_not_release(self):
        link = FakeLink(); link.mode = 'HOLD'; run = self.runner(link)
        with self.assertRaisesRegex(RuntimeError, '안전 자세'): run.reset_to_safe()
        self.assertFalse(any(a.get('action') == 'release' for _, a in link.calls))

    def test_stale_or_enabled_motor_not_torque_off(self):
        link = FakeLink(); self.assertTrue(cli.torque_off(link.state()))
        link.stale = True; self.assertFalse(cli.torque_off(link.state()))
        link.stale = False; state = link.state(); state['latest']['telemetry']['0']['torque'] = 1
        self.assertFalse(cli.torque_off(state))

    def test_measurement_timeout_does_not_reuse_saved_reference(self):
        link = FakeLink(); run = self.runner(link)
        clock, mono, sleep = self.clock()
        with mono, sleep:
            with self.assertRaisesRegex(RuntimeError, '지그 측정 실패'):
                run.measure([{'jig_id': 'j'}], {'acquisition_seconds': 1., 'acquisition_attempts': 1, 'hold_seconds': 5})
        self.assertEqual(link.calls[-1][0], 'camera_stop')

    def test_confirmed_jig_pose_logs_exact_plan_input_once(self):
        link = FakeLink(); run = self.runner(link)
        bundle = {'acquisition_seconds': 5., 'acquisition_attempts': 3,
                  'hold_seconds': 5., 'mesh_hashes': {'j': 'abc'}}
        link.frame = {'at': 100., 'detection': {'by_jig': {
            'j': {'selected': {'metric': {'center_xy_mm': [123.456, -67.891],
                                        'yaw_deg': -12.345, 'mesh_yaw_offset_deg': 90}},
                  'pose_measured_at': 100.}}}}
        with patch.object(cli.time, 'monotonic', return_value=100.):
            current = run.measure([{'jig_id': 'j'}], bundle)
        self.assertEqual(current['j']['pose'], [123.456, -67.891, -12.345])
        poses = [event for event in self.events if event.startswith('JIG_POSE:')]
        self.assertEqual(poses, ['JIG_POSE:j:x_mm=123.46:y_mm=-67.89:yaw_deg=-12.35'])
        self.assertLess(self.events.index('JIG_CONFIRMED:j'), self.events.index(poses[0]))

    def test_per_stage_completion_includes_b_lower_adjustment(self):
        link = FakeLink(); link.mode = 'HOLD'; run = self.runner(link)
        steps = [{'name': n, 'ticks': {}} for n in ('하단 : 놓기', '하단 : 조정 완료', '중단 : 놓고 들기', '상단 : 놓고 들기')]
        run.move('play', [{}] * 4, steps)
        self.assertLess(self.events.index('STEP_DONE:2/4:하단 : 조정 완료'), self.events.index('STAGE_DONE:LOWER'))
        self.assertEqual([e for e in self.events if e.startswith('STAGE_DONE')], ['STAGE_DONE:LOWER','STAGE_DONE:MIDDLE','STAGE_DONE:UPPER'])

    def test_check_never_connects_hardware(self):
        job = (None, {'name': 'test', 'steps': [{}]}, {}, 400, 900)
        with patch.object(cli, 'load_job', return_value=job), patch.object(cli, 'Link') as link:
            self.assertEqual(cli.main(['build_a', '--check']), 0); link.assert_not_called()

    def test_standalone_linear_check_never_connects(self):
        with patch.object(cli, 'Link') as link:
            self.assertEqual(cli.main(['slide_load', '--check']), 0); link.assert_not_called()

    def test_repeat_estop_clears_queued_restart(self):
        control = cli.Control(); control.paused = True
        control.command('restart'); control.command('estop')
        self.assertFalse(control.resume.is_set()); self.assertTrue(control.pause.is_set())


class RuntimeContractTest(unittest.TestCase):
    def test_real_runtime_pause_index_and_resume_keep_only_remaining_targets(self):
        import tempfile, uuid
        from types import SimpleNamespace
        from tests.fixtures import DATA, load_profile
        from tests.test_remote import FakeMotion
        from so101_teach.configuration import JigCatalog
        from so101_teach.vision import PoseLatch
        from so101_teach.domain import Snapshot
        from so101_teach.remote_config import configuration_bundle
        from so101_teach.remote_server import Runtime
        p, cal, ref = load_profile(); p['mode'] = 'follower'
        app = SimpleNamespace(profile=p, data_dir=DATA, catalog=JigCatalog(DATA), reference=ref, pose_latch=PoseLatch(), active_jig='pallet')
        control = cli.Control(); submissions = []; events = []
        class Motion(FakeMotion):
            def request(self, action, targets=None):
                request = super().request(action, targets)
                if action in ('move', 'play'):
                    self.commands.get_nowait(); self.command_pending.clear(); self.active_request_id = request
                    submissions.append(deepcopy(targets))
                    if len(submissions) == 1:
                        self.index = 1; self.state = 'MOVING'; control.pause.set()
                    else:
                        self.index = len(targets); self.state = 'HOLD'; self.program_active.clear(); self.completed_request_id = request
                return request
        with tempfile.TemporaryDirectory() as temp:
            runtime = Runtime(temp, motion_factory=Motion); lease = runtime.acquire()['lease']
            try:
                runtime.configure(configuration_bundle(app)); runtime.call('connect', {'speed': 400})
                runtime.session.state = 'HOLD'
                class Adapter:
                    error = None
                    def __init__(self): self.lease = lease
                    def rpc(self, method, args=None):
                        runtime.heartbeat(lease, True)
                        value = runtime.rpc({'lease': lease, 'id': uuid.uuid4().hex, 'method': method, 'args': args or {}})
                        if not value['ok']: raise RuntimeError(value['error'])
                        return value['value']
                    def state(self):
                        runtime.heartbeat(lease, True); s = runtime.session
                        if s.hold_requested.is_set():
                            s.hold_requested.clear(); s.program_active.clear(); s.command_pending.clear(); s.state = 'HOLD'
                        s.latest = Snapshot('follower', ref.middle, {n: {'moving': 0, 'torque': 1} for n in ref.middle}, time.monotonic(), time.time(), cal.sha256, True, 'fake')
                        return runtime.state(lease)['follower']
                    def post(self, endpoint, body=None): return self.rpc(body['method'], body['args'])
                def emit(event):
                    events.append(event)
                    if event == 'PAUSED': control.command('restart')
                runner = cli.Runner(Adapter(), emit, control, 20)
                targets = [dict(ref.middle) for _ in range(3)]
                runner.move('play', targets)
                self.assertEqual([len(v) for v in submissions], [3, 2])
                self.assertIn('PAUSED', events); self.assertIn('RESUMED', events)
                self.assertFalse(runtime.session.command_log)
            finally: runtime.close()


class ParallelPreparationTests(unittest.TestCase):
    def setup_job(self, detect_after=2., linear_seconds=8.):
        self.now = 100.; self.events = []; self.control = cli.Control()
        outer = self
        class Linear:
            error = None
            def __init__(self): self.calls=[]; self.end=None; self.target=None
            def call(self, op, **args):
                self.calls.append(op)
                if op == 'ensure':
                    self.end=outer.now+linear_seconds; self.target=args['target_mm']
                if op not in ('ensure','status'): raise AssertionError('unexpected linear control: '+op)
                return {'phase':'TIMED_COMPLETE' if outer.now >= self.end else 'MOVING',
                        'target_mm':self.target,'remaining_s':max(0.,self.end-outer.now)}
        class Link(FakeLink):
            def __init__(self): super().__init__(); self.ready=None; self.generation=0; self.move_times=[]
            def rpc(self, method, args=None):
                if method=='detect_clear':
                    self.ready=outer.now+detect_after; self.generation+=1
                if method=='command' and args['action'] in ('arm','move','play'):
                    self.move_times.append(outer.now)
                return super().rpc(method,args)
            def post(self, endpoint, body=None):
                if endpoint!='/camera': return super().post(endpoint,body)
                result={}
                if self.ready is not None and outer.now>=self.ready:
                    result={'j':{'selected':{'metric':{'center_xy_mm':[self.generation,0],'yaw_deg':0}},
                                 'pose_measured_at':outer.now}}
                return {'at':outer.now,'detection':{'by_jig':result}}
        self.link=Link(); self.linear=Linear()
        self.runner=cli.Runner(self.link,self.events.append,self.control,1000,linear=self.linear)
        self.episode={'steps':[{'name':'start','ticks':{},'safe_boundary':'start'},
                               {'name':'work','ticks':{},'jig_id':'j'},
                               {'name':'end','ticks':{},'safe_boundary':'end'}]}
        self.bundle={'acquisition_seconds':5.,'acquisition_attempts':3,'hold_seconds':5.,'mesh_hashes':{'j':'abc'}}

    def execute(self, sleep_hook=None):
        def sleep(seconds):
            self.now+=seconds
            if sleep_hook: sleep_hook()
        with patch.object(cli.time,'monotonic',side_effect=lambda:self.now), patch.object(cli.time,'sleep',side_effect=sleep):
            self.runner.execute(self.episode,self.bundle,400,100.)

    def test_detection_finishes_first_arm_waits_for_linear(self):
        self.setup_job()
        self.execute()
        self.assertLess(self.events.index('JIG_CONFIRMED:j'), self.events.index('LINEAR_DONE:TIME_BASED:position_measured=false'))
        self.assertGreaterEqual(min(self.link.move_times),108.)
        self.assertLess(min(self.link.move_times),108.2)
        self.assertEqual(self.linear.calls.count('ensure'),1)
        self.assertFalse(any('remaining_s' in event or 'LINEAR_PROGRESS' in event for event in self.events))

    def test_linear_finishes_first_arm_waits_for_detection(self):
        self.setup_job(detect_after=10.)
        self.execute()
        self.assertLess(self.events.index('LINEAR_DONE:TIME_BASED:position_measured=false'),self.events.index('JIG_CONFIRMED:j'))
        self.assertGreaterEqual(min(self.link.move_times),110.)

    def test_detection_failure_never_arms_or_moves_robot(self):
        self.setup_job(detect_after=100.)
        with self.assertRaisesRegex(RuntimeError,'지그 측정 실패'): self.execute()
        self.assertEqual(self.link.move_times,[])

    def test_linear_timeout_never_arms_or_moves_robot(self):
        self.setup_job(linear_seconds=100.)
        with self.assertRaisesRegex(RuntimeError,'리니어 완료 대기 시간 초과'): self.execute()
        self.assertEqual(self.link.move_times,[])

    def test_estop_leaves_linear_running_and_rechecks_pre_detected_jig(self):
        self.setup_job(); sent=False; restarted=False
        def controls():
            nonlocal sent,restarted
            if not sent and self.now>=103.:
                sent=True;self.control.command('estop')
            if self.control.paused and self.now>=112. and not restarted:
                restarted=True;self.control.command('restart')
        self.execute(controls)
        self.assertIn('PAUSED',self.events);self.assertIn('RESUMED',self.events)
        self.assertIn('JIG_RECHECK_AFTER_RESUME',self.events)
        self.assertEqual(self.linear.end,108.)
        self.assertEqual(self.linear.calls.count('ensure'),1)
        self.assertGreaterEqual(min(self.link.move_times),114.)
        plan=[args for method,args in self.link.calls if method=='plan'][0]
        self.assertEqual(plan['current']['j']['pose'][0],2)

    def test_reset_waits_for_linear_before_safe_return(self):
        self.setup_job();sent=False
        def controls():
            nonlocal sent
            if not sent and self.now>=101.:
                sent=True;self.control.command('estop')
            if self.control.paused:self.control.command('reset')
        with self.assertRaises(cli.ResetRequested):self.execute(controls)
        with patch.object(cli.time,'monotonic',side_effect=lambda:self.now), patch.object(cli.time,'sleep',side_effect=lambda s:setattr(self,'now',self.now+s)):
            self.runner.reset_to_safe()
        self.assertGreaterEqual(min(self.link.move_times),108.)
        self.assertIn('RESET_WAIT_LINEAR_COMPLETE',self.events)
        self.assertEqual(self.linear.calls.count('ensure'),1)
