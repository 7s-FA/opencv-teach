"""Exercise saved JSON, exact poses, lifecycle outcomes and existing device-call boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
import base64,io,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from tests.test_episode_cli import cli,FakeLink
from episode_analytics import ExecutionAnalysis
from episode_accounting import summarize,union_seconds,select_jobs


class AnalysisEvidenceTests(unittest.TestCase):
    def analysis(self,operation='build_load_b'):
        a=ExecutionAnalysis('job',100.);a.configure(operation,'job',100.);a.register_run('build','arm2');return a

    def test_configuration_is_immutable_and_poses_keep_precision_and_revision(self):
        a=self.analysis();episode={'id':'episode','steps':[{'id':'step','jig_id':'jig','name':'집기'}]}
        bundle={'profile':{'robot_id':'arm2','calibration_file':'calibration/a.json'},'jigs':{'jig':{'name':'지그'}},
                'files':{'calibration/a.json':base64.b64encode(b'{"offset":1.25}').decode(),'jig_assets/a.stl':'not-stored'}}
        context=a.context('arm2',episode,bundle,320,100.,90.)
        pose={'jig':{'pose':[192.63456789,-95.12345678,12.3456789],'stl_sha256':'mesh','symmetry_deg':180}}
        one=a.poses('arm2',pose,{'jig':{'pose_measured_at':101.123456}},100.)
        a.plan('arm2',episode['steps'],[{'ticks':{'joint':2000}}]);before=a.snapshot()
        pose['jig']['pose'][0]=201.654321;two=a.poses('arm2',pose,{},100.)
        episode['steps'][0]['name']='changed';bundle['jigs']['jig']['name']='changed'
        d=a.snapshot();self.assertNotEqual(one,two)
        self.assertEqual(d['jig_pose_sets'][0]['poses'][0]['x_mm'],192.63456789)
        self.assertIsNone(d['jig_pose_sets'][0]['poses'][0]['z_mm'])
        self.assertEqual(d['plans'][0]['pose_set_id'],one)
        self.assertEqual(d['configurations'][context]['episode']['steps'][0]['name'],'집기')
        self.assertEqual(d['configurations'][context]['calibration']['calibration/a.json']['offset'],1.25)
        self.assertEqual(d['configurations'][context]['runner_timeout_seconds'],90.)
        self.assertNotIn('not-stored',json.dumps(d));self.assertEqual(len(before['jig_pose_sets']),1)
        json.dumps(d,allow_nan=False)

    def test_build_load_and_resume_stay_one_job_and_only_chain_done_finishes(self):
        a=self.analysis()
        for kind,elapsed in [('EPISODE_STARTED',3),('ESTOP_REQUESTED',5),('PAUSED',6),('RESUMED',9),('BUILD_DONE',12)]:a.event('arm2','build',kind,1000+elapsed,elapsed)
        d=a.snapshot();self.assertFalse(d['record_complete']);self.assertTrue(d['runs'][0]['phase_completed'])
        self.assertEqual(d['pause_intervals'],[{'started_elapsed_s':6,'ended_elapsed_s':9,'ended_by':'RESUMED'}])
        a.register_run('load','arm3');a.event('arm3','load','CHAIN_LOAD_STARTED:build_load_b',1013,13)
        a.event('arm3','load','CHAIN_DONE',1020,20);d=a.snapshot()
        self.assertEqual(d['job_id'],'job');self.assertEqual(d['outcome'],'succeeded');self.assertEqual(len(d['runs']),2)
        self.assertFalse(d['completed_without_intervention'])

    def test_reset_during_production_is_cancellation_not_assembly_success(self):
        a=self.analysis();a.cancel();a.event('arm2','build','RESET_DONE',1010,10)
        self.assertEqual(a.snapshot()['outcome'],'cancelled')
        reset=self.analysis('reset');reset.event('arm2','build','RESET_DONE',1010,10)
        self.assertEqual(reset.snapshot()['category'],'recovery');self.assertEqual(reset.snapshot()['outcome'],'succeeded')

    def test_missing_terminal_and_shutdown_never_become_success(self):
        a=self.analysis();a.event('arm2','build','EPISODE_STARTED',1001,1)
        self.assertFalse(a.snapshot()['record_complete'])
        a.error(cli.ShutdownRequested('PROCESS_SHUTDOWN'),'motion');a.event('arm2','build','FAILED:PROCESS_SHUTDOWN',1003,3)
        self.assertEqual(a.snapshot()['outcome'],'interrupted')

    def test_linear_target_is_not_a_measured_position_and_overlaps_arm_interval(self):
        a=self.analysis()
        with patch('episode_analytics.time.monotonic',return_value=102.):token=a.motion_start('arm2','play',[{}])
        a.event('arm2','build','LINEAR_STARTED:target_mm=100',1003,3)
        event=a.event('arm2','build','LINEAR_DONE:TIME_BASED:target_mm=100:position_measured=false',1007,7)
        with patch('episode_analytics.time.monotonic',return_value=110.):a.motion_end(token)
        self.assertFalse(event['linear']['position_measured']);self.assertEqual(a.snapshot()['motion_calls'][0]['elapsed_seconds'],8)
        self.assertEqual(a.snapshot()['linear_intervals'][0]['ended_elapsed_s'],7)

    def test_linear_skip_is_a_zero_motion_decision_not_missing_ten_seconds(self):
        a=self.analysis()
        for suffix,t in [('LINEAR_CHECK:target_mm=100',1),('LINEAR_READY:target_mm=100',1.1),
                         ('LINEAR_CHECK:target_mm=1.5',2),('LINEAR_STARTED:target_mm=1.5',2.1),
                         ('LINEAR_DONE:TIME_BASED:target_mm=1.5',12.1),('CHAIN_DONE',14)]:
            a.event('arm2','build',suffix,1000+t,t)
        d=a.snapshot();rows=d['linear_requests']
        self.assertEqual(rows[0]['outcome'],'already_at_target');self.assertEqual(rows[0]['movement_seconds'],0)
        self.assertEqual(rows[1]['movement_seconds'],10)
        result=summarize(d);self.assertEqual(result['linear']['already_at_target'],1)
        self.assertEqual(result['time']['linear_moving_observation_union_seconds'],10)

    def test_same_preparation_profile_is_replaced_and_parallel_spans_are_not_added(self):
        a=self.analysis()
        profile={'profile_id':'p','origin_monotonic_s':100.,'intervals':[
            {'interval_id':1,'started_seconds':0.,'finished_seconds':4.},
            {'interval_id':2,'started_seconds':1.,'finished_seconds':3.}], 'metrics':{}}
        a.preparation_profile(profile);a.preparation_profile(profile)
        a.event('arm2','build','CHAIN_DONE',1010,10)
        result=summarize(a.snapshot())
        self.assertEqual(len(a.snapshot()['preparation_profiles']),1)
        self.assertEqual(result['time']['preparation_measured_span_union_seconds'],4.)
        self.assertEqual(result['time']['elapsed_not_covered_by_spans_seconds'],6.)
        self.assertEqual(union_seconds([(0,5),(2,3),(4,7)]),7)

    def test_arrivals_keep_contact_target_and_resume_offset_without_duplicate_poll_counts(self):
        a=self.analysis();target={'shoulder_pan':2000,'gripper':2200}
        state={'state':'MOVING','request_id':1,'index':0,'arrival_evidence':{
            'controller_session_id':'session','last_arrival_seq':0,'arrivals':[]}}
        a.follower('arm2',state)
        token=a.motion_start('arm2','play',[target,target,target]);a.motion_request(token,1)
        row={'controller_session_id':'session','arrival_seq':1,'request_id':1,'step_index':1,
             'target':{**target,'gripper':2300},'actual':{'shoulder_pan':1998,'gripper':2299},
             'residual_ticks':{'shoulder_pan':-2,'gripper':-1},'gripper_target_adjusted':True,'arrival_type':'settled'}
        state['arrival_evidence']['arrivals']=[row]
        a.follower('arm2',state);a.follower('arm2',state)
        a.motion_request(token,2,step_offset=1)
        state['arrival_evidence']['arrivals']=[row,{**row,'arrival_seq':2,'request_id':2,'step_index':1}]
        a.follower('arm2',state);d=a.snapshot()
        self.assertEqual(len(d['step_arrivals']),2)
        self.assertEqual([r['original_step_index'] for r in d['step_arrivals']],[1,2])
        self.assertEqual(d['step_arrivals'][0]['planned_target']['gripper'],2200)
        self.assertEqual(d['step_arrivals'][0]['target']['gripper'],2300)
        self.assertEqual(summarize(d)['arrival_coverage']['missing_original_steps'],[[token,3]])
        # A new controller can reuse the numerical request ID; old mappings
        # must not attach its evidence to this job's previous controller.
        state['arrival_evidence']={'controller_session_id':'new','arrivals':[
            {**row,'controller_session_id':'new','arrival_seq':1}]}
        a.follower('arm2',state);self.assertEqual(len(a.snapshot()['step_arrivals']),2)

    def test_terminal_controls_and_supervision_loss_exclude_uninterrupted_cohort(self):
        a=self.analysis();a.control_requests({'id':{'command':'estop'}},True)
        a.event('arm2','build','CHAIN_DONE',1010,10)
        self.assertFalse(a.snapshot()['completed_without_intervention'])

    def test_arrivals_outlive_controller_window_and_missing_packets_remain_visible(self):
        a=self.analysis();target={'joint':10,'gripper':20}
        state={'arrival_evidence':{'controller_session_id':'s','arrivals':[]}}
        a.follower('arm2',state);token=a.motion_start('arm2','play',[target]*220);a.motion_request(token,1)
        for start in range(1,221,8):
            state['arrival_evidence']['arrivals']=[{'controller_session_id':'s','arrival_seq':i,
                'request_id':1,'step_index':i,'target':target,'arrival_type':'settled'} for i in range(start,min(start+8,221))]
            a.follower('arm2',state)
        result=summarize(a.snapshot())['arrival_coverage']
        self.assertEqual(result['matched_original_steps'],220);self.assertEqual(result['transport_gaps'],0)
        # A lost window must be reported, rather than silently counted complete.
        state['arrival_evidence']['arrivals']=[{'controller_session_id':'s','arrival_seq':240,
            'request_id':99,'step_index':1,'target':target}]
        a.follower('arm2',state)
        self.assertEqual(len(a.snapshot()['arrival_transport_gaps']),1)

    def test_inspection_restarts_and_provisional_pass_are_not_final_pass(self):
        a=self.analysis()
        for suffix,t in [('INSPECTION_STARTED:s:name',1),('INSPECTION_PROVISIONAL_PASS:s',2),
                         ('INSPECTION_GROUP_RESTARTED',3),('INSPECTION_STARTED:s:name',4),
                         ('INSPECTION_PASS:s',5)]:a.event('arm2','build',suffix,1000+t,t)
        checks=a.snapshot()['inspection_attempts']
        self.assertEqual([r['outcome'] for r in checks],['restarted','passed'])
        self.assertEqual(checks[1]['finished_elapsed_s']-checks[1]['started_elapsed_s'],1)


class SavedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        p=patch.object(cli,'ROOT',self.root);p.start();self.addCleanup(p.stop)

    def status(self,operation='build_b'):
        status=cli.Status('arm2','B',False);self.addCleanup(status.close)
        status.control=cli.Control();status.timing.job_id=status.control.job_id
        status.timing.begin(operation);status.configure_analysis(operation,request_id=status.control.job_id)
        return status

    def test_index_only_failure_does_not_change_durable_completion(self):
        s=self.status();original=cli.atomic_json;output=io.StringIO()
        s.emit('EPISODE_STARTED');s.flush_records()
        def fail_index(path,value):
            if Path(path).parent.name=='episode-job-index':raise OSError('index full')
            return original(path,value)
        with patch.object(cli,'atomic_json',side_effect=fail_index),redirect_stdout(output):
            s.emit('DONE');s.flush_final();s.close()
        self.assertIn('B_DONE',output.getvalue().splitlines())
        record=json.loads(s.log.read_text())
        self.assertTrue(record['accounting']['successful_production'])
        jobs,_=select_jobs([record]);self.assertEqual(len(jobs),1)

    def test_lost_motion_ack_records_attempt_and_indeterminate_coverage(self):
        s=self.status();link=FakeLink();link.mode='HOLD';link.arm='arm2'
        run=cli.Runner(link,s.emit,s.control,30)
        with patch.object(link,'rpc',side_effect=TimeoutError('response lost')) as rpc:
            with self.assertRaises(TimeoutError):run.move('play',[{},{}])
        self.assertEqual(rpc.call_count,1)
        a=s.analysis.snapshot();self.assertEqual(a['motion_calls'][0]['requests'],[])
        coverage=summarize(a)['arrival_coverage']
        self.assertEqual(coverage['expected_original_steps'],2)
        self.assertEqual(coverage['missing_original_steps'],[[0,1],[0,2]])
        self.assertEqual(coverage['unacknowledged_send_attempts'],1)
        self.assertEqual(coverage['coverage_state'],'indeterminate')

    def test_lost_resume_ack_preserves_original_offset_without_retry(self):
        s=self.status();link=FakeLink();link.mode='HOLD';link.arm='arm2';link.auto_finish=False
        run=cli.Runner(link,s.emit,s.control,30);original=link.rpc
        def rpc(method,args=None):
            value=original(method,args)
            if len(args['targets'])==3:s.control.pause.set()
            else:raise TimeoutError('resume response lost')
            return value
        def paused():
            link.index=1;link.mode='HOLD';link.pending=False
            s.control.pause.clear();return link.state()
        with patch.object(link,'rpc',side_effect=rpc),patch.object(run,'await_pause',side_effect=paused):
            with self.assertRaises(TimeoutError):run.move('play',[{'n':1},{'n':2},{'n':3}])
        a=s.analysis.snapshot();attempts=a['motion_calls'][0]['send_attempts']
        self.assertEqual([r['step_offset'] for r in attempts],[0,1])
        self.assertIsNotNone(attempts[0]['request_id']);self.assertIsNone(attempts[1]['request_id'])
        self.assertEqual(len(link.calls),2)
        coverage=summarize(a)['arrival_coverage']
        self.assertEqual(coverage['expected_original_steps'],3)
        self.assertEqual(coverage['coverage_state'],'indeterminate')

    def test_saved_success_has_machine_fields_and_retains_original_reader_contract(self):
        s=self.status();link=FakeLink();link.mode='HOLD';link.arm='arm2'
        run=cli.Runner(link,s.emit,s.control,30)
        episode={'id':'ep','steps':[{'id':'step','name':'집기','ticks':{}}]}
        s.analysis.context('arm2',episode,{'profile':{'robot_id':'arm2'}},320,100.)
        s.emit('EPISODE_STARTED');run.move('play',[{}],episode['steps']);s.emit('DONE');s.flush_final()
        record=json.loads(s.log.read_text());a=record['analysis']
        self.assertEqual(record['schema_version'],2);self.assertEqual(record['record_kind'],'execution')
        self.assertEqual(a['outcome'],'succeeded');self.assertEqual(a['request_id'],record['job_id'])
        self.assertEqual(a['motion_calls'][0]['outcome'],'verified_return')
        self.assertEqual(a['motion_calls'][0]['requests'][0]['request_id'],link.request)
        self.assertEqual(link.calls,[('command',{'action':'play','targets':[{}]})])
        self.assertEqual([e['status'] for e in record['events']],['B_EPISODE_STARTED','B_STEP_DONE:1/1:집기','B_DONE'])
        self.assertEqual([e['event_seq'] for e in record['events']],[1,2,3]);self.assertIn('at_monotonic_s',record['events'][0])
        self.assertNotIn('_analysis',json.dumps(record));self.assertNotIn('analysis',json.loads(s.path.read_text()))

    def test_handoff_final_file_contains_both_arms_and_build_file_stays_partial(self):
        s=self.status('build_load_b');s.emit('EPISODE_STARTED');s.emit('BUILD_DONE')
        other=s.handoff('arm3','B');self.addCleanup(other.close)
        other.emit('CHAIN_LOAD_STARTED:build_load_b');other.emit('EPISODE_STARTED');other.emit('CHAIN_DONE');other.flush_final()
        first=json.loads(s.log.read_text());last=json.loads(other.log.read_text())
        self.assertEqual(first['job_id'],last['job_id']);self.assertNotEqual(first['run_id'],last['run_id'])
        self.assertFalse(first['analysis']['record_complete']);self.assertTrue(last['analysis']['record_complete'])
        self.assertEqual([r['arm'] for r in last['analysis']['runs']],['arm2','arm3'])
        index=json.loads((self.root/'data/episode-job-index'/(last['job_id']+'.json')).read_text())
        self.assertEqual(index['source_run_id'],last['run_id']);self.assertTrue(index['record_complete'])
        self.assertEqual(index['revision'],last['analysis']['event_count'])
        self.assertGreater(last['events'][0]['job_event_seq'],first['events'][-1]['job_event_seq'])
        jobs,info=select_jobs([last,first,last,{'events':[]}])
        self.assertEqual(len(jobs),1);self.assertEqual(info['ignored_non_execution_records'],1)
        self.assertTrue(next(iter(jobs.values()))['accounting']['successful_production'])

    def test_preparation_failure_is_saved_without_opening_devices(self):
        with patch.object(cli,'load_job',side_effect=ValueError('missing model')),patch.object(cli,'Link') as link,patch.object(cli.signal,'signal'),redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['build_a','--no-ros']),1)
        link.assert_not_called();paths=list((self.root/'data/episode-cli-runs').glob('*.json'));self.assertEqual(len(paths),1)
        a=json.loads(paths[0].read_text())['analysis']
        self.assertEqual(a['outcome'],'failed');self.assertEqual(a['failure']['phase'],'preparation')
        self.assertEqual(a['operation'],'build_a');self.assertEqual(a['configurations'],{})

    def test_worker_snapshots_do_not_retroactively_change_earlier_records(self):
        s=self.status();captured=[];original=s._write
        def write(record,events):captured.append(deepcopy(record['_analysis']));return original(record,events)
        with patch.object(s,'_write',side_effect=write):
            s.emit('EPISODE_STARTED');s.emit('FAILED:test');s.close()
        self.assertEqual(captured[0]['outcome'],'in_progress');self.assertEqual(captured[1]['outcome'],'failed')

    def test_measure_stores_unrounded_pose_used_by_the_existing_path(self):
        s=self.status();link=FakeLink();link.mode='HOLD';link.arm='arm2';run=cli.Runner(link,s.emit,s.control,30)
        point=[192.63456789,95.12345678]
        def frame(body):
            now=time.monotonic();return {'at':now,'detection':{'by_jig':{'jig':{'pose_measured_at':now,
                'selected':{'metric':{'center_xy_mm':point,'yaw_deg':12.3456789}},'acquisition_completed_attempts':1}}}}
        run.camera_frame=frame;run.failure_images.confirm_jigs=Mock()
        pose=run.measure([{'jig_id':'jig'}],{'acquisition_seconds':1,'acquisition_attempts':1,'hold_seconds':1,'mesh_hashes':{'jig':'mesh'}})
        s.emit('FAILED:fixture ends');s.close();record=json.loads(s.log.read_text())
        self.assertEqual(record['analysis']['jig_pose_sets'][0]['poses'][0]['x_mm'],point[0])
        self.assertEqual(pose['jig']['pose'],[*point,12.3456789])
        self.assertTrue(any('192.63' in e['status'] for e in record['events']))
