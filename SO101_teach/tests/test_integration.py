from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json,tempfile,time,unittest
from tests.fixtures import load_profile
from so101_teach.domain import EpisodeStore,Snapshot,JOINTS
from so101_teach.integration import EpisodeCoordinator


class Variable:
    def __init__(self,value=''):self.value=value
    def set(self,value):self.value=value
    def get(self):return self.value


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.data=Path(self.temp.name)
        self.clock=100.;self.apps={};self.config={'recipes':{}};self.messages=[]
        _,cal,ref=load_profile()
        for arm in ('arm2','arm3'):
            store=EpisodeStore(self.data/'episodes',cal,arm);mapping={}
            for command in ('A','B'):
                doc=store.new(arm+' '+command);doc['steps']=[store.step(ref.middle,'작업')];store.save(doc)
                mapping[command]={'episode_id':doc['id'],'timeout_seconds':60};initial=doc
            self.config['recipes'][arm]=mapping
            sample=Snapshot('follower',ref.middle.copy(),{n:{'moving':0} for n in JOINTS},time.monotonic(),time.time(),cal.sha256,True,'fake')
            s=SimpleNamespace(running=True,error=None,state='HOLD',latest=sample,command_pending=Event(),program_active=Event(),active_request_id=None,completed_request_id=None)
            a=SimpleNamespace(closed=False,playing=False,pending_execution=None,camera_task=None,safe_entry=None,remote_plan_job=None,
                live_adjust=SimpleNamespace(owner=None),settings=SimpleNamespace(worker=None),session=s,latest=sample,remote=None,
                store=store,episode=deepcopy(initial),episode_name=Variable(),follow_jig=Variable(False),refresh_steps=Mock(),refresh_library=Mock(),message=Variable(),last_motion_stop=None)
            a.execute_episode=Mock(side_effect=lambda a=a:setattr(a,'pending_execution',{'action':'play'}))
            def hold(action,a=a):
                self.assertEqual(action,'hold');a.pending_execution=None;a.safe_entry=None;a.remote_plan_job=None
                a.session.state='HOLD';a.session.command_pending.clear();a.session.program_active.clear()
            a.motion_request=Mock(side_effect=hold);self.apps[arm]=a
        self.c=EpisodeCoordinator(self.apps,self.data,self.config,emit=lambda a,t:self.messages.append((a,t)),clock=lambda:self.clock)
    def start(self,arm='arm2',command='A'):
        self.c.command(arm,command);self.assertIsNotNone(self.c.active);return self.apps[arm]
    def complete(self,arm='arm2',request_id=2):
        a=self.apps[arm];a.pending_execution=None
        self.c.submitted(arm,'play',request_id,[a.episode['steps'][-1]['ticks']])
        a.session.completed_request_id=request_id;a.session.active_request_id=request_id;self.c.poll()
    def test_full_pi_job_waits_for_runner_result_and_manual_job_blocks_commands(self):
        a=self.apps['arm2'];job=SimpleNamespace(busy=True,record={'state':'running'})
        a.execute_episode=Mock(side_effect=lambda:setattr(a,'workspace_manager',SimpleNamespace(pi_execution=job)))
        self.start();self.c.poll();self.assertIsNotNone(self.c.active)
        self.assertIn(('arm2','A_STARTED'),self.messages)
        job.busy=False;job.record['state']='done';self.c.poll()
        self.assertIsNone(self.c.active);self.assertEqual(self.messages[-1],('arm2','A_DONE'))
        job.busy=True;self.c.command('arm3','B')
        self.assertEqual(self.messages[-1],('arm3','B_REJECTED:BUSY'))

    def test_routes_each_arm_and_episode_through_button_method(self):
        for arm in self.apps:
            for command in ('A','B'):
                a=self.start(arm,command)
                self.assertEqual(a.episode['id'],self.config['recipes'][arm][command]['episode_id'])
                self.assertEqual(a.execute_episode.call_count,1 if command=='A' else 2)
                self.complete(arm);self.assertIn((arm,command+'_DONE'),self.messages)
    def test_duplicate_while_busy_is_rejected_without_second_execution(self):
        a=self.start();self.c.command('arm2','A');self.c.command('arm3','B')
        a.execute_episode.assert_called_once();self.apps['arm3'].execute_episode.assert_not_called()
        self.assertIn(('arm2','A_REJECTED:BUSY'),self.messages)
        self.assertIn(('arm3','B_REJECTED:BUSY'),self.messages)
    def test_safe_entry_completion_is_not_episode_completion(self):
        a=self.start();a.pending_execution=None;a.safe_entry={'steps':[{}]};self.c.submitted('arm2','move',1,[a.latest.ticks])
        a.session.completed_request_id=1;self.c.poll();self.assertIsNotNone(self.c.active)
        self.assertFalse(any(t.endswith('_DONE') for _,t in self.messages))
    def test_failure_after_jig_measurement_does_not_send_done(self):
        a=self.start();a.pending_execution=None;a.message.set('지그 측정 실패');self.c.poll()
        self.assertIsNone(self.c.active);self.assertIn(('arm2','A_FAILED:지그 측정 실패'),self.messages)
    def test_waits_for_fresh_stopped_feedback_after_final_request(self):
        a=self.start();a.session.latest=replace(a.latest,monotonic=time.monotonic()-2)
        self.complete();self.assertIsNotNone(self.c.active)
        a.session.latest=replace(a.latest,monotonic=time.monotonic());self.c.poll()
        self.assertIsNone(self.c.active);self.assertEqual(self.messages[-1],('arm2','A_DONE'))
    def test_stop_holds_and_never_reports_done(self):
        a=self.start();self.c.command('arm2','STOP');a.motion_request.assert_called_once_with('hold');self.c.poll()
        self.assertEqual(self.messages[-1],('arm2','A_STOPPED:OPERATOR_STOP'))
    def test_timeout_requests_hold_and_reports_failure(self):
        a=self.start();self.clock+=61;self.c.poll();a.motion_request.assert_called_once_with('hold');self.c.poll()
        self.assertEqual(self.messages[-1],('arm2','A_FAILED:TASK_TIMEOUT'))
    def test_read_only_rejects_without_automatic_torque_enable(self):
        a=self.apps['arm2'];a.session.state='READ_ONLY';self.c.command('arm2','A')
        a.execute_episode.assert_not_called();a.motion_request.assert_not_called();self.assertIn('FOLLOWER_NOT_READY',self.messages[-1][1])
    def test_unmapped_command_is_rejected(self):
        self.c.command('arm2','C');self.assertIn('UNKNOWN_COMMAND',self.messages[-1][1])
    def test_demo_feedback_cannot_report_a_real_episode_completion(self):
        a=self.apps['arm2'];a.session.latest=replace(a.latest,role='demo');self.c.command('arm2','A')
        a.execute_episode.assert_not_called();self.assertIn('FOLLOWER_NOT_READY',self.messages[-1][1])
    def test_wrong_arm_recipe_is_rejected(self):
        self.c.config['recipes']['arm2']['A']['episode_id']=self.config['recipes']['arm3']['A']['episode_id']
        self.c.command('arm2','A');self.apps['arm2'].execute_episode.assert_not_called()
    def test_unsaved_episode_is_preserved(self):
        a=self.apps['arm2'];a.episode['name']='unsaved';self.c.command('arm2','A')
        self.assertEqual(a.episode['name'],'unsaved');self.assertIn('UNSAVED_EPISODE',self.messages[-1][1])
    def test_record_failure_prevents_start(self):
        with patch('so101_teach.integration.atomic_json',side_effect=OSError('disk full')):self.c.command('arm2','A')
        self.apps['arm2'].execute_episode.assert_not_called();self.assertIsNone(self.c.active)
    def test_restart_marks_incomplete_without_replay(self):
        a=self.start();c=EpisodeCoordinator(self.apps,self.data,self.config)
        self.assertIsNone(c.active);a.execute_episode.assert_called_once()
        record=json.loads(next((self.data/'integration-runs').glob('*.json')).read_text());self.assertEqual(record['state'],'UNKNOWN')
    def test_old_completed_request_is_not_success_for_current_episode(self):
        a=self.start();a.pending_execution=None;self.c.submitted('arm2','play',9,[a.latest.ticks]);a.session.completed_request_id=8;self.c.poll()
        self.assertTrue(self.messages[-1][1].startswith('A_FAILED'))
