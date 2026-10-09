import json,time,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.inspection_geometry import all_anchors,anchors_for,draw_linear_centers
from so101_teach.shape_inspection import ShapeInspector
from so101_teach.inspection_seating import containment,apply_quality
from so101_teach.occupied_pallet import locate
from so101_teach.inspection_tracking import PalletReference
from so101_teach.vision_service import MultiDetector
from tests import test_ui as ui_fixtures

FOLDER=ROOT/'tests/fixtures/inspection-all'

def scene(name):
    cfg=json.loads((FOLDER/'fixture.json').read_text());frame=cv2.imread(str(FOLDER/name))
    catalog=JigCatalog(ROOT/'data',profile=cfg['profile']);catalog.items=cfg['catalog']
    data=MultiDetector(catalog,cfg['profile']).process(frame)
    return frame,cfg,catalog,data

class AllInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.normal=scene('complete.jpg');cls.fault=scene('faults.jpg');cls.inspector=ShapeInspector()
    def inspect(self,case,adopted=None):
        frame,cfg,catalog,data=case
        current=deepcopy(data['live_by_jig']);result=PalletReference(FOLDER/'reference').locate(frame,cfg['profile'],catalog.items['pallet']) or locate(frame,catalog.items['pallet'],cfg['profile'])
        if result:current['pallet']=result
        anchors,_=all_anchors(frame,current,adopted or {},catalog.items,cfg['profile'],cfg['reference'],cfg['placement'])
        return anchors,self.inspector.inspect(frame,anchors,cfg['profile'])
    def test_all_nine_regions_and_two_centers(self):
        anchors,rows=self.inspect(self.normal)
        self.assertEqual(len(anchors),9);self.assertEqual(len(rows),9)
        frame,cfg,_,_=self.normal;copy=frame.copy();marks=draw_linear_centers(copy,cfg['profile'],cfg['reference'],cfg['placement'])
        self.assertEqual(len(marks),2);self.assertFalse(np.array_equal(copy,frame))
    def test_B_complete_and_incomplete_are_distinct(self):
        for case,state in ((self.normal,'cap_added'),(self.fault,'insert_added')):
            _,rows=self.inspect(case);row=next(r for r in rows if r['id']=='finished')
            self.assertEqual(row['state'],state);self.assertEqual(row['product'],'B')
            if state=='insert_added':self.assertEqual(row['quality'],'abnormal');self.assertIn('미완성',row['label'])
            else:self.assertEqual(row['quality'],'normal')
    def test_actual_wrong_slot_and_misaligned_linear_are_not_green(self):
        adopted=deepcopy(self.normal[3]['live_by_jig'])
        _,rows=self.inspect(self.fault,adopted)
        by_id={r['id']:r for r in rows}
        self.assertEqual(by_id['b_housing']['state'],'wrong_part')
        self.assertEqual(by_id['b_housing']['product'],'A')
        self.assertEqual(by_id['b_housing']['quality'],'abnormal')
        self.assertEqual(by_id['a_cap']['quality'],'abnormal')
        self.assertEqual(by_id['linear_1.5_0']['quality'],'abnormal')
    def test_boundary_and_outside_are_different(self):
        roi=np.zeros((100,100),np.uint8);roi[30:70,30:70]=255
        for bounds,expected in [((40,40,60,60),'inside'),((20,40,40,60),'boundary'),((2,2,20,20),'outside')]:
            mask=np.zeros_like(roi);x0,y0,x1,y1=bounds;mask[y0:y1,x0:x1]=255
            self.assertEqual(containment(mask,roi),expected)
            row={'state':'present','label':'형상 일치','reason':'test'};apply_quality(row,{'part':'cap'},expected)
            self.assertEqual(row['quality'],{'inside':'normal','boundary':'abnormal','outside':'outside'}[expected])
    def test_finished_never_accepts_only_housing_or_insert(self):
        for state in ('empty','housing_seated','insert_added'):
            row={'state':state,'label':state,'reason':'sample'};apply_quality(row,{'part':'finished'},'inside');self.assertEqual(row['quality'],'abnormal')
        row={'state':'cap_added','label':'B 상단 결합','reason':'내부 미확인'};apply_quality(row,{'part':'finished'},'inside');self.assertEqual(row['quality'],'normal')
        row={'state':'cap_added','label':'A 상단 결합','reason':'sample','missing_visible_parts':['insert']};apply_quality(row,{'part':'finished'},'inside');self.assertEqual(row['quality'],'abnormal')
    def test_occluded_pallet_recovery_is_inspection_only(self):
        frame,cfg,catalog,data=self.normal
        self.assertFalse(data['live_by_jig']['pallet']['selected'])
        result=locate(frame,catalog.items['pallet'],cfg['profile']);self.assertTrue(result['inspection_only'])
        self.assertTrue(result['selected']['metric']);self.assertFalse(result['selected']['orientation_verified'])

    def test_empty_reference_follows_small_move_and_rejects_incomplete(self):
        frame,cfg,catalog,_=scene('moved-incomplete.png');tracker=PalletReference(FOLDER/'reference')
        result=tracker.locate(frame,cfg['profile'],catalog.items['pallet'])
        self.assertIsNotNone(result);self.assertGreaterEqual(result['selected']['tracking_points'],6)
        self.assertGreater(result['selected']['reference_shift_px'],.5);self.assertLess(result['selected']['reference_shift_px'],5)
        anchors,_=anchors_for(frame,{'pallet':result},{},catalog.items,cfg['profile'],cfg['reference'],'완성품 팔레트','전체',cfg['placement'])
        row=self.inspector.inspect(frame,anchors,cfg['profile'])[0]
        self.assertEqual(row['state'],'insert_added');self.assertEqual(row['quality'],'abnormal');self.assertIn('미완성',row['label'])
    def test_tracking_requires_actual_rim_and_matching_camera(self):
        frame,cfg,catalog,_=self.normal;tracker=PalletReference(FOLDER/'reference')
        self.assertIsNone(tracker.locate(np.full_like(frame,170),cfg['profile'],catalog.items['pallet']))
        profile=deepcopy(cfg['profile']);profile['intrinsics']['size']=[1,1]
        self.assertIsNone(tracker.locate(frame,profile,catalog.items['pallet']))

class AllInspectionUITests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_all_rows_centers_colors_and_minimum_window(self):
        frame,cfg,catalog,data=scene('complete.jpg');a=self.app;p=a.camera_inspection
        import shutil
        shutil.copytree(FOLDER/'reference',a.data_dir/'inspection_reference')
        a.profile=cfg['profile'];a.catalog.items=catalog.items;a.catalog.revision+=1;a.workcell_preview=cfg['placement']
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.show_page('camera');a.camera_tabs.select(p);self.root.geometry('1180x760');self.root.update()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
            if 'finished' in p.table.get_children() and p.table.item('finished','values')[2]!='—' and p.table.item('finished','values')[1]!='확인 중':break
            time.sleep(.1)
        self.assertEqual(len(p.table.get_children()),9);self.assertEqual(len(p.center_marks),2)
        self.assertGreater(p.canvas.winfo_height(),180)
        self.assertLessEqual(p.legend.winfo_rooty()+p.legend.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height()-75)
        from PIL import ImageGrab
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(ROOT/'verification/inspection-all/all-1180x760.png')
        a.camera_tabs.select(a.camera_tabs.tabs()[0]);a.camera_all.set(True);a.draw_camera_frame(time.monotonic());self.assertEqual(len(a.camera_linear_centers),2)
        # Physical ROI overlays cannot poison the classifier's raw input.
        np.testing.assert_array_equal(frame,cv2.imread(str(FOLDER/'complete.jpg')))
    def test_current_fault_layout_with_empty_reference_and_adopted_carrier(self):
        frame,cfg,catalog,data=scene('moved-incomplete.png');normal=scene('complete.jpg');a=self.app;p=a.camera_inspection
        import shutil
        shutil.copytree(FOLDER/'reference',a.data_dir/'inspection_reference')
        a.profile=cfg['profile'];a.catalog.items=catalog.items;a.catalog.revision+=1;a.workcell_preview=cfg['placement']
        accepted={k:{**v,'teaching_held':True} for k,v in normal[3]['live_by_jig'].items() if v.get('selected')}
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.show_page('camera');a.camera_tabs.select(p);self.root.geometry('1180x760');self.root.update()
        deadline=time.monotonic()+5
        with patch.object(a,'camera_adoption_results',return_value=accepted):
            while time.monotonic()<deadline:
                a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
                if 'finished' in p.table.get_children() and '미완성' in p.table.item('finished','values')[1] and p.table.item('b_housing','tags')==('abnormal',):break
                time.sleep(.1)
            self.assertEqual(len(p.table.get_children()),9)
            for key in ('b_housing','a_cap','linear_1.5_0','finished'):self.assertEqual(p.table.item(key,'tags'),('abnormal',))
            self.assertIn('미완성',p.table.item('finished','values')[1]);self.assertIn('빈 팔레트 기준',p.position.get())
            self.assertEqual(len(p.center_marks),2)
            self.assertLessEqual(p.legend.winfo_rooty()+p.legend.winfo_height(),p.summary.winfo_rooty()+p.summary.winfo_height())
            self.assertLessEqual(p.coords.winfo_rootx()+p.coords.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            p.status.set('현재 오배치 사진 검증 · 빈 팔레트 기준으로 이동 추적 · 리니어 후진 목표');self.root.update_idletasks()
            from PIL import ImageGrab
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(ROOT/'verification/inspection-all/current-faults-1180x760.png')
