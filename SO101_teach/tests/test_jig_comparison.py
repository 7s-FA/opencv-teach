import unittest
import time
from types import SimpleNamespace
from copy import deepcopy
from unittest.mock import patch
from so101_teach.jig_comparison import comparison_groups
from so101_teach.domain import read_json,ROOT
from tests import test_ui as fixtures


class ComparisonTests(unittest.TestCase):
    def test_detected_pose_updates_only_after_hold_time_and_freezes_during_execution(self):
        from so101_teach.vision_service import MultiDetector
        from tests.test_pose_hold import result
        catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda k:{'sha256':'a'*64})
        import numpy as np
        frame=np.zeros((48,64,3),np.uint8)
        detector=MultiDetector(catalog,{})
        def observe(x,t):
            with patch('so101_teach.vision_service.detect',return_value=result(x,strong=True)),patch('so101_teach.vision_service.time.monotonic',return_value=t):return detector.process(frame)
        for t in (10,11,12,13):first=observe(100,t)
        self.assertIsNotNone(first['selected'])
        for t in (20,21,22):second=observe(140,t)
        self.assertEqual(second['selected']['metric']['center_xy_mm'][0],100)
        third=observe(140,23);self.assertEqual(third['selected']['metric']['center_xy_mm'][0],140)
        detector.freeze(True);frozen=observe(160,35)
        self.assertEqual(frozen['selected']['metric']['center_xy_mm'][0],140)
        self.assertNotIn('live_reference',second)

    def test_multiple_jigs_and_different_step_references_keep_separate_deltas(self):
        ref={'pose':[100,200,89],'symmetry_deg':90,'stl_sha256':'a'*64}
        steps=[{'id':'1','name':'잡기','jig_id':'a','jig_reference':ref},
               {'id':'2','name':'들기','jig_id':'a','jig_reference':deepcopy(ref)},
               {'id':'3','name':'놓기','jig_id':'b','jig_reference':{**ref,'pose':[300,400,10]}},
               {'id':'4','name':'다른 기준','jig_id':'a','jig_reference':{**ref,'pose':[105,200,89]}},
               {'id':'5','name':'안전 자세'}]
        before=deepcopy(steps);current={'a':{**ref,'pose':[110,195,1]},'b':{**ref,'pose':[302,403,13]}}
        out=comparison_groups(steps,{},current)
        self.assertEqual(len(out),3);self.assertEqual(out[0]['delta'],[10,-5,2])
        self.assertEqual(len(out[0]['steps']),2);self.assertEqual(out[1]['delta'],[2,3,3]);self.assertEqual(out[2]['delta'],[5,-5,2])
        self.assertEqual(steps,before);self.assertEqual(out[0]['measured']['pose'][2],1)


class ComparisonUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def teach(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.apply_target(read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks'])
        a.follow_jig.set(True);a.commit_target();return a

    def test_saved_teaching_basis_and_execution_delta_remain_visible_and_persist(self):
        a=self.teach();a.jig_comparison.view.set('최근 실행');a.refresh_jig_comparison();original=deepcopy(a.episode);self.root.update()
        self.assertTrue(a.jig_comparison.winfo_ismapped());self.assertIsNone(a.jig_comparison.groups[0]['measured'])
        a.play();self.fake_jig_camera((233,-136,70));a.check_pending_execution()
        g=a.jig_comparison.groups[0];self.assertEqual(g['reference']['pose'],[228,-138,67]);self.assertEqual(g['delta'],[5,2,3])
        cells=[a.jig_comparison.table.item(k,'values') for k in a.jig_comparison.table.get_children()]
        self.assertEqual(tuple(cells[0][1:]),('138.0','136.0','-2.0'))
        self.assertEqual(tuple(cells[1][1:]),('228.0','233.0','+5.0'))
        paths=list((self.data/'episode_runs'/a.episode['id']).glob('*.json'));self.assertEqual(len(paths),1)
        saved=read_json(paths[0]);self.assertEqual(saved['state'],'preview');self.assertEqual(saved['groups'][0],g)
        self.assertEqual(a.episode,original);a.stop_preview();a.show_page('camera');a.show_page('teach');self.assertEqual(a.jig_comparison.groups[0],g)
        a.comparison_reports={};a.refresh_jig_comparison();self.assertEqual(a.jig_comparison.groups[0],g)
        a.episode['steps'][0]['jig_reference']['pose'][0]+=1;a.refresh_steps();self.assertIsNone(a.jig_comparison.groups[0]['measured'])

    def test_execution_dispatch_records_same_measurement_and_ticks_as_motion(self):
        a=self.teach();current={**a.episode['steps'][0]['jig_reference'],'pose':[233,-136,70]}
        with patch.object(a,'motion_request',return_value=42) as move:
            a.begin_execution('play',a.episode['steps'],{'pallet':current})
        record=read_json(next((self.data/'episode_runs'/a.episode['id']).glob('*.json')))
        self.assertEqual(record['request_id'],42);self.assertEqual(record['state'],'submitted')
        self.assertEqual(record['groups'][0]['measured'],current);self.assertEqual([p['ticks'] for p in record['plan']],move.call_args.args[1])

    def test_failed_ik_shows_measured_delta_but_never_claims_dispatch(self):
        a=self.teach();current={**a.episode['steps'][0]['jig_reference'],'pose':[10000,10000,67]}
        with patch.object(a,'motion_request') as move:
            with self.assertRaises(ValueError):a.begin_execution('play',a.episode['steps'],current)
            move.assert_not_called()
        record=read_json(next((self.data/'episode_runs'/a.episode['id']).glob('*.json')))
        self.assertEqual(record['state'],'blocked');self.assertIn('실행 안 됨',a.jig_comparison.caption.get())

    def test_panel_does_not_cover_servo_controls_or_preview_at_1280(self):
        a=self.teach();self.root.update()
        panel=a.jig_comparison
        self.assertGreater(a.preview_canvas.winfo_height(),150)
        self.assertLessEqual(panel.winfo_rooty()+panel.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height()-45)
        self.assertGreaterEqual(panel.winfo_width(),330)
    def test_idle_comparison_uses_same_ten_second_hold_without_changing_records(self):
        from so101_teach.vision import PoseLatch
        a=self.teach();before=deepcopy(a.episode);latch=PoseLatch(10);base=time.monotonic()
        for seconds,pose,expected in [(3.1,(228,-138,67),[0,0,0]),(10.9,(243,-136,70),[0,0,0]),(13.2,(243,-136,70),[15,2,3])]:
            self.fake_jig_camera(pose);frame,out,at=a.camera.observation
            from tests.test_pose_hold import result
            raw=result(pose[0],pose[2],strong=True);raw['selected']['metric']=out['selected']['metric']
            if seconds in (3.1,10.9):
                for dt in ((3.1,2.3,1.5,.7) if seconds==3.1 else (2.9,2.1,1.3,.5)):latch.update(raw,base+seconds-dt)
            out=latch.update(raw,base+seconds)
            with patch('so101_teach.ui.time.monotonic',return_value=base+seconds):
                a.camera.observation=(frame,out,base+seconds);a.update_live_jig_comparison()
                self.assertEqual(a.jig_comparison.groups[0]['delta'],expected)
                self.assertEqual(a.jig_comparison.groups[0]['measured'],a.current_jig_reference())
        self.assertEqual(a.episode,before);self.assertFalse((self.data/'episode_runs').exists())
        with patch('so101_teach.ui.time.monotonic',return_value=base+24):a.update_live_jig_comparison()
        self.assertIsNone(a.jig_comparison.groups[0]['measured']);self.assertIn('감지 대기',a.jig_comparison.caption.get())
    def test_preview_comparison_stays_fixed_until_stop_then_live_can_update(self):
        a=self.teach();current={**a.episode['steps'][0]['jig_reference'],'pose':[233,-136,70]}
        a.begin_execution('preview',a.episode['steps'],current);saved=deepcopy(a.jig_comparison.groups)
        self.fake_jig_camera((250,-130,80));frame,out,at=a.camera.observation
        a.update_live_jig_comparison();self.assertEqual(a.jig_comparison.groups,saved)
        a.stop_preview();a.update_live_jig_comparison();self.assertEqual(a.jig_comparison.groups[0]['delta'],[22,8,13])
        a.jig_comparison.view.set('최근 실행');a.update_live_jig_comparison();self.assertEqual(a.jig_comparison.groups,saved)
