import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DIR = Path(__file__).parents[1] / 'integration'
sys.path.insert(0, str(DIR))
spec = importlib.util.spec_from_file_location('work_monitor', DIR / 'work_monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class WorkMonitorTests(unittest.TestCase):
    def test_reset_summary_keeps_each_arm_and_one_overall_completion(self):
        statuses=['GOAL_RECEIVED:RESET','CONTROL_ACCEPTED:RESET','CONTROL_RESET:REQUESTED',
            'B_ARM_RESET:arm2:FOLLOWER_READY','B_ARM_RESET_STARTED:arm2','B_ARM_RESET:arm2:RESET_DONE',
            'B_ARM_RESET_DONE:arm2','B_ARM_RESET_STARTED:arm3','B_ARM_RESET_DONE:arm3','B_RESET_DONE',
            'CONTROL_REPLY:reset:B_ARM_RESET_STARTED:arm2\nB_RESET_DONE','CONTROL_RESULT:RESET:IDLE']
        source=[dict(at=1,arm='arm2',status=s) for s in statuses]
        shown=[v for e in source if (v:=monitor.visible_event(e)) is not None]
        self.assertEqual(len(shown),6)
        self.assertEqual([e['arm'] for e in shown[1:5]],['arm2','arm2','arm3','arm3'])
        self.assertEqual(shown[-1]['status'],'전체 팔 RESET 완료')
        self.assertEqual([e['status'] for e in source],statuses)

    def test_reset_errors_remain_visible_and_details_can_show_original(self):
        import io
        from contextlib import redirect_stdout
        error=dict(at=1,arm='arm2',status='CONTROL_REPLY:reset:B_ARM_RESET_STARTED:arm3\nB_FAILED:RESET:arm3 blocked')
        self.assertIn('arm3 blocked',monitor.visible_event(error)['status'])
        detail=dict(at=1,arm='arm2',status='B_ARM_RESET:arm3:TORQUE_OFF_CONFIRMED')
        with redirect_stdout(io.StringIO()) as output:monitor.print_event(detail,details=True)
        self.assertIn(detail['status'],output.getvalue())

    def test_alarm_details_are_indented_once_without_losing_measurements(self):
        import io
        from contextlib import redirect_stdout
        reason='시작 전 · B · 하단 부품 · 안착 불합격: 경계 걸침 · 위치차 +5.6/-3.1 mm · ROI 밖 5.72%'
        failures={}
        with redirect_stdout(io.StringIO()) as output:
            for kind in ('INSPECTION_FAILED','FAILED'):
                monitor.print_event(dict(at=1,arm='arm2',run_id='same',status='B_'+kind+':'+reason),failures=failures)
        shown=output.getvalue()
        self.assertEqual(shown.count('위치차 +5.6/-3.1 mm'),1)
        self.assertIn('    - ROI 밖 5.72%',shown)
        self.assertIn('B_FAILED:',shown);self.assertIn('동일 실패 원인 참조',shown)
        with redirect_stdout(io.StringIO()) as output:
            monitor.print_event(dict(at=2,arm='arm2',run_id='different',status='B_FAILED:'+reason),failures=failures)
        self.assertIn('위치차 +5.6/-3.1 mm',output.getvalue())

    def test_routine_noise_is_folded_but_warnings_and_unknown_events_survive(self):
        for status in ('B_STEP_DONE:12/32:이동','A_INSPECTION_COUNT:item:1/2','A_JIG_POSE:jig:x_mm=10','A_LINEAR_PROGRESS:remaining_s=2','GOAL_RECEIVED:build_a'):
            self.assertIsNone(monitor.visible_event(dict(at=1,arm='arm2',status=status)))
        for status in ('B_FAILED:camera lost','B_REJECTED:BUSY','B_NEW_WARNING:unknown','EXECUTOR_OUTPUT:build_b:warning'):
            self.assertIsNotNone(monitor.visible_event(dict(at=1,arm='arm2',status=status)))
        self.assertIn('사유: BUSY',monitor.visible_event(dict(at=1,arm='arm2',status='GOAL_REJECTED:build_b:BUSY'))['status'])

    def test_stage_linear_and_pause_messages_keep_their_meaning(self):
        cases={'B_STAGE_DONE:MIDDLE':'중단','A_LINEAR_DONE:TIME_BASED:position_measured=false':'위치 실측 없음',
               'A_LINEAR_READY:target_mm=1.5:position_measured=false':'1.5 mm',
               'B_PAUSED':'작업 보류','B_RESUMED':'작업 재개',
               'B_LINEAR_CONTINUES:ESTOP_APPLIES_TO_ARM_ONLY':'계속 이동',
               'B_ARM_RESET:arm3:RESET_WAIT_LINEAR_COMPLETE':'리니어 완료 대기'}
        for raw,meaning in cases.items():
            self.assertIn(meaning,monitor.visible_event(dict(at=1,arm='arm2',status=raw))['status'])
        self.assertIn('에피소드 완료',monitor.visible_event(dict(at=1,arm='arm2',status='B_DONE'))['status'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'data/episode-cli-runs'
        self.folder.mkdir(parents=True)
        self.reader = monitor.Events(self.root)

    def write_events(self, name, statuses):
        rows = [{'at': i, 'arm': 'arm2', 'status': status} for i, status in enumerate(statuses)]
        (self.folder / name).write_text(json.dumps({'events': rows}))

    def test_idle_without_socket_does_not_query_control(self):
        with patch.object(monitor, 'control_state') as query:
            self.assertTrue(monitor.current_state(self.root).startswith('IDLE'))
            query.assert_not_called()

    def test_paused_starting_and_running_states(self):
        (self.root / 'data/episode-cli.sock').touch()
        for state, expected in [({'info': None}, 'STARTING'), ({'info': {'arm':'arm3','run_id':'x'}, 'paused':True}, 'PAUSED'), ({'info': {'arm':'arm2','run_id':'y'}, 'paused':False}, 'RUNNING')]:
            with patch.object(monitor, 'control_state', return_value=state):
                self.assertTrue(monitor.current_state(self.root).startswith(expected))

    def test_unreachable_socket_is_unknown_not_idle(self):
        (self.root / 'data/episode-cli.sock').touch()
        with patch.object(monitor, 'control_state', side_effect=OSError('unreachable')):
            self.assertTrue(monitor.current_state(self.root).startswith('UNKNOWN'))

    def test_append_and_new_job_are_reported_once(self):
        self.write_events('first.json', ['A_STARTED'])
        self.assertEqual(len(self.reader.read_new()[0]), 1)
        self.assertEqual(self.reader.read_new(), ([], []))
        self.write_events('first.json', ['A_STARTED', 'A_DONE'])
        self.write_events('second.json', ['B_STARTED'])
        self.assertEqual([e['status'] for e in self.reader.read_new()[0]], ['B_STARTED', 'A_DONE'])
        self.assertEqual(self.reader.read_new(), ([], []))

    def test_broken_record_retried_after_repair(self):
        (self.folder / 'first.json').write_text('{')
        events, errors = self.reader.read_new()
        self.assertFalse(events)
        self.assertEqual(len(errors), 1)
        self.write_events('first.json', ['A_DONE'])
        events, errors = self.reader.read_new()
        self.assertEqual(events[0]['status'], 'A_DONE')
        self.assertFalse(errors)

    def test_replaced_shorter_log_resets_cursor(self):
        self.write_events('first.json', ['A_STARTED', 'A_DONE'])
        self.reader.read_new()
        self.write_events('first.json', ['B_STARTED'])
        self.assertEqual(self.reader.read_new()[0][0]['status'], 'B_STARTED')

    def test_service_changes_reported_only_on_transition(self):
        from types import SimpleNamespace
        import io
        from contextlib import redirect_stdout
        output=io.StringIO()
        with patch.object(monitor.subprocess,'run',side_effect=[SimpleNamespace(stdout=s) for s in ('active','active','active','active','failed','active')]),redirect_stdout(output):
            state=monitor.service_status();state=monitor.service_status(state);monitor.service_status(state)
        self.assertEqual(len(output.getvalue().splitlines()),3)
        self.assertIn('workcell-actions: failed',output.getvalue())
    def test_independent_notices_join_existing_job_events_without_duplicates(self):
        from action_protocol import record_notice
        self.write_events('job.json',['A_INSPECTION_STARTED:step','A_INSPECTION_PASS:step'])
        record_notice(self.root,'arm2','GOAL_REJECTED:build_b:BUSY')
        events,errors=self.reader.read_new();self.assertFalse(errors);self.assertEqual(len(events),3)
        self.assertEqual(self.reader.read_new(),([],[]))

    def test_display_hides_internal_ids_but_keeps_coordinates_and_original_record(self):
        key='b5ebdb54807c441c8267c15d77713b6a';raw='B_JIG_POSE:'+key+':x_mm=195.18:y_mm=-134.32:yaw_deg=31.20'
        shown=monitor.display_status(raw,{key:'운반용 지그'})
        self.assertEqual(shown,'B_JIG_POSE:운반용 지그:x_mm=195.18:y_mm=-134.32:yaw_deg=31.20')
        self.assertIn(key,raw);self.assertNotIn(key,monitor.display_status(raw,{}))
        self.assertEqual(monitor.display_status('B_INSPECTION_COUNT:start-0-B-housing:1/2',{}),'B_INSPECTION_COUNT:B 하단:1/2')

    def test_action_result_ends_job_with_one_separator(self):
        import io
        from contextlib import redirect_stdout
        for result in ('IDLE','ERROR'):
            output=io.StringIO()
            with redirect_stdout(output):
                monitor.print_event(dict(at=1,arm='arm3',status='B_DONE'))
                monitor.print_event(dict(at=2,arm='arm3',status='ACTION_RESULT:load_b:'+result))
            lines=output.getvalue().splitlines()
            self.assertEqual(lines[-1],'─'*72)
            self.assertEqual(lines.count('─'*72),1)

    def test_terminal_failure_is_printed_before_idle_transition(self):
        import io
        from contextlib import redirect_stdout
        output=io.StringIO();event=dict(at=1,arm='arm3',status='B_FAILED:부품 남음')
        (self.root/'integration').mkdir();(self.root/'integration/episode_cli.py').touch()
        with patch.object(sys,'argv',['watch','--app-dir',str(self.root)]),patch.object(monitor,'service_status',return_value={}),patch.object(monitor,'current_state',side_effect=['RUNNING | arm3','IDLE | 진행 중인 CLI/Action 작업 없음']),patch.object(monitor.Events,'read_new',side_effect=[([],[]),([event],[])]),patch.object(monitor.time,'sleep',side_effect=[None,KeyboardInterrupt]),redirect_stdout(output):
            monitor.main()
        text=output.getvalue();self.assertLess(text.index('B_FAILED:'),text.index('현재: IDLE'))
