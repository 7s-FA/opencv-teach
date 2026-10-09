from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import time,unittest
from tests import test_ui as fixtures
from so101_teach.domain import Snapshot,JOINTS
from so101_teach.motion import MotionSession
from so101_teach.integration import EpisodeCoordinator


class IntegrationUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        self.app.workspace_manager=None;self.app.session=None;fixtures.UITests.tearDown(self)
    def test_external_command_uses_real_safe_entry_and_episode_execution_path(self):
        a=self.app;doc=a.store.new('통합 시험');ticks=a.reference.middle.copy()
        first=a.store.step(ticks,'시작');first['safe_boundary']='start'
        middle=a.store.step({**ticks,'shoulder_pan':ticks['shoulder_pan']+10},'작업')
        last=a.store.step(ticks,'종료');last['safe_boundary']='end'
        doc['steps']=[first,middle,last];a.store.save(doc);a.episode=deepcopy(doc)
        s=MotionSession('fake',a.calibration);s.running=True;s.state='HOLD'
        sample=Snapshot('follower',ticks.copy(),{n:{'moving':0} for n in JOINTS},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
        s.latest=sample;a.latest=sample;a.session=s;calls=[]
        def request(action,targets=None):
            self.assertIn(action,('move','play'))
            calls.append((action,deepcopy(targets)));s.active_request_id=len(calls);s.state='MOVING';s.program_active.set();s.command_pending.set();return len(calls)
        s.request=request;messages=[]
        c=EpisodeCoordinator({'arm2':a},self.data,{'recipes':{'arm2':{'A':{'episode_id':doc['id'],'timeout_seconds':60}}}},emit=lambda arm,text:messages.append(text))
        a.workspace_manager=SimpleNamespace(integration=SimpleNamespace(coordinator=c))
        c.command('arm2','A');self.assertEqual(calls[0],('move',[ticks]));self.assertIsNotNone(a.safe_entry)
        s.completed_request_id=1;s.state='HOLD';s.program_active.clear();s.command_pending.clear()
        a.poll_extended()
        self.assertIsNone(a.safe_entry);self.assertEqual(calls[1],('play',[middle['ticks'],last['ticks']]))
        c.poll();self.assertIn('A_STARTED',messages);self.assertNotIn('A_DONE',messages)
        s.completed_request_id=2;s.state='HOLD';s.program_active.clear();s.command_pending.clear();c.poll()
        self.assertEqual(messages[-1],'A_DONE')
