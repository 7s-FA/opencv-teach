import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.episode_inspection_runtime import PreparedInspector


class PreparedInspectorTests(unittest.TestCase):
    def session(self,inspector):
        with patch('so101_teach.ai_inspection.inspector_for',return_value=inspector):
            session=PreparedInspector({})
            self.assertTrue(session.ready.wait(2))
        self.addCleanup(session.close)
        return session

    def test_warmup_is_discarded_and_borrowers_use_fresh_frames(self):
        inspector=Mock()
        inspector.inspect.side_effect=lambda frame,anchors,profile:[{'value':int(frame[0,0,0])}]
        session=self.session(inspector)
        inspector.provider.prepare.assert_called_once()
        inspector.inspect.assert_not_called()
        first,second=session.borrow(),session.borrow()
        first.stop=threading.Event();second.stop=threading.Event()
        self.assertEqual(first.inspect(np.ones((2,2,3),np.uint8),[],{}),[{'value':1}])
        first.close();first.stop.set()
        self.assertEqual(second.inspect(np.full((2,2,3),2,np.uint8),[],{}),[{'value':2}])
        self.assertIs(inspector.stop,second.stop)
        inspector.close.assert_not_called()
        session.close();inspector.close.assert_called_once()

    def test_cancelled_waiter_cannot_cancel_active_or_next_inspection(self):
        entered=threading.Event();release=threading.Event()
        inspector=Mock()
        def inspect(*args):
            entered.set();release.wait(2)
            self.assertFalse(inspector.stop.is_set())
            return ['fresh']
        inspector.inspect.side_effect=inspect
        session=self.session(inspector)
        a,b=session.borrow(),session.borrow();a.stop=threading.Event();b.stop=threading.Event()
        task=threading.Thread(target=lambda:a.inspect(None,[],{}));task.start()
        self.assertTrue(entered.wait(1));b.stop.set()
        self.assertEqual(b.inspect(None,[],{}),[])
        release.set();task.join(2);self.assertFalse(task.is_alive())
        self.assertEqual(inspector.inspect.call_count,1)

    def test_failed_warmup_never_returns_an_observation(self):
        inspector=Mock();inspector.provider.prepare.side_effect=ValueError('model failure')
        session=self.session(inspector)
        with self.assertRaisesRegex(RuntimeError,'model failure'):session.borrow().inspect(None,[],{})
        inspector.inspect.assert_not_called()

    def test_changed_profile_is_rejected_instead_of_using_wrong_model(self):
        inspector=Mock();session=self.session(inspector)
        with self.assertRaisesRegex(ValueError,'프로필'):session.borrow().inspect(None,[],{'changed':True})
        inspector.inspect.assert_not_called()

    def test_opencv_profile_prepares_without_an_ai_provider(self):
        inspector=SimpleNamespace(inspect=Mock(return_value=['geometry']),stop=None)
        session=self.session(inspector)
        self.assertEqual(session.borrow().inspect(None,[],{}),['geometry'])

    def test_runner_prepares_before_device_connection_and_closes_on_failure(self):
        from tests.test_episode_cli import cli,FakeLink
        run=cli.Runner(FakeLink(),lambda message:None,cli.Control(),20)
        episode={'id':'test','name':'test','product_type':'A','steps':[{'safe_boundary':'end','ticks':{}}]}
        session=Mock()
        with patch('so101_teach.episode_inspection.validate_inspection'),patch('so101_teach.episode_inspection.inspection_schedule'),patch('so101_teach.episode_inspection.has_inspections',return_value=True),patch('so101_teach.episode_inspection.startup_steps',return_value=[]),patch('so101_teach.episode_inspection_runtime.PreparedInspector',return_value=session),patch.object(cli,'atomic_json'):
            def failed_connection(*args):
                self.assertIs(run.prepared_inspector,session)
                raise RuntimeError('connection failed')
            with patch.object(run,'prepare_follower',side_effect=failed_connection):
                with self.assertRaisesRegex(RuntimeError,'connection failed'):run.execute(episode,{'profile':{}},400,100.)
        session.close.assert_called_once();self.assertIsNone(run.prepared_inspector)

    def test_runner_uses_prewarmed_load_inspector_and_closes_it_on_failure(self):
        from tests.test_episode_cli import cli,FakeLink
        run=cli.Runner(FakeLink(),lambda message:None,cli.Control(),20)
        session=Mock();run.prepared_inspector=session
        episode={'id':'load','product_type':'A','steps':[{'safe_boundary':'end','ticks':{}}]}
        with patch('so101_teach.episode_inspection.validate_inspection'),patch('so101_teach.episode_inspection.inspection_schedule'),patch('so101_teach.episode_inspection.has_inspections',return_value=True),patch('so101_teach.episode_inspection.startup_steps',return_value=[]),patch('so101_teach.episode_inspection_runtime.PreparedInspector') as create,patch.object(cli,'atomic_json'),patch.object(run,'prepare_follower',side_effect=RuntimeError('stop')):
            with self.assertRaisesRegex(RuntimeError,'stop'):run.execute(episode,{'profile':{}},400,1.5)
        create.assert_not_called();session.close.assert_called_once()
