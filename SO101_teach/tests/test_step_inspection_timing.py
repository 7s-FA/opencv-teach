from copy import deepcopy
from types import SimpleNamespace
import unittest
import numpy as np
from so101_teach.episode_inspection import migrate_inspection_timing,inspection_schedule
from so101_teach.workcell_preview import teaching_placement
from so101_teach.inspection import project_markers

class StepInspectionTimingTests(unittest.TestCase):
    def test_legacy_checks_and_deferred_events_move_once_without_changing_trigger(self):
        check={'station':'linear','target':'2','expected':'housing_seated','timeout_seconds':10}
        old={'steps':[{'id':'a','name':'하단 : 조정 완료','inspection':check},{'id':'b','name':'중단 : 잡기 위치'}],'completion_events':{'LOWER':'a'}}
        before=deepcopy(old);new=migrate_inspection_timing(old)
        self.assertEqual(old,before);self.assertNotIn('inspection',new['steps'][0]);self.assertEqual(new['steps'][1]['inspection'],check)
        self.assertEqual(inspection_schedule(new['steps'])[0][0],2);self.assertEqual(new['completion_events'],{'LOWER':'b'})
        self.assertEqual(migrate_inspection_timing(new),new)
    def test_adjacent_checks_keep_independent_stages(self):
        old={'steps':[{'id':'a','name':'a','inspection':{'expected':'housing_seated'}},{'id':'b','name':'b','inspection':{'expected':'insert_added'}},{'id':'c','name':'c'}],'completion_events':{'LOWER':'a','MIDDLE':'b'}}
        new=migrate_inspection_timing(old)
        self.assertEqual(new['completion_events'],{'LOWER':'b','MIDDLE':'c'})
        self.assertEqual([n for n,s in inspection_schedule(new['steps'])],[2,3])
    def test_arm_specific_preview_endpoint_does_not_mutate_live_placement(self):
        live={'linear_stage':{'stroke_mm':12,'startup_state':{'known':True,'commanded_mm':12},'endpoint_reference':{'forward':{'commanded_mm':100},'retracted':{'commanded_mm':1.5}}}}
        before=deepcopy(live)
        for arm,expected in [('arm2',100),('arm3',1.5)]:
            shown=teaching_placement(live,{'robot_id':arm})
            self.assertEqual(shown['linear_stage']['stroke_mm'],expected)
            self.assertEqual(shown['linear_stage']['startup_state']['commanded_mm'],expected)
        self.assertEqual(live,before)
    def test_markers_use_the_image_aspect_without_vertical_shift(self):
        camera=SimpleNamespace(pos=np.zeros(3),forward=np.array([0,0,-1]),up=np.array([0,1,0]))
        scene=SimpleNamespace(camera=[camera,camera]);points={'center':[0,0,-2],'right':[1,0,-2],'up':[0,1,-2]}
        wide=project_markers(scene,points,90,aspect=16/9);classic=project_markers(scene,points,90)
        np.testing.assert_allclose(wide['center'],[.5,.5]);np.testing.assert_allclose(wide['right'],[.640625,.5])
        self.assertLess(wide['right'][0],classic['right'][0]);np.testing.assert_allclose(wide['up'],classic['up'])
