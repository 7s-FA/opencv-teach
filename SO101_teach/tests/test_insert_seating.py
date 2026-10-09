"""User-labelled camera frames and controlled slot-pose displacement."""
from copy import deepcopy
import json,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.inspection_geometry import anchors_for
from so101_teach.shape_inspection import ShapeInspector
from tests import test_ui as ui_fixtures

FOLDER=ROOT/'tests/fixtures/b-housing-seating'

def scene(index=0):
    cfg=json.loads((FOLDER/'fixture.json').read_text());frame=cv2.imread(str(FOLDER/f'camera-{index}.jpg'))
    anchors,_=anchors_for(frame,{},cfg['adopted'],cfg['catalog'],cfg['profile'],cfg['reference'],'운반용 지그','전체',{})
    return frame,cfg,anchors

class InsertSeatingTests(unittest.TestCase):
    def test_carrier_margins_accept_normal_frames_but_reject_real_displacement(self):
        folder=ROOT/'tests/fixtures/carrier-seating-margin';cfg=json.loads((folder/'fixture.json').read_text());inspector=ShapeInspector()
        for index in range(3):
            frame=cv2.imread(str(folder/f'frame-{index}.jpg'))
            anchors,_=anchors_for(frame,{},cfg['adopted'],cfg['catalog'],cfg['profile'],cfg['reference'],'운반용 지그','전체',{})
            rows={r['id']:r for r in inspector.inspect(frame,anchors,cfg['profile'])}
            for name in ('a_ball','b_tpu','a_housing','b_housing'):self.assertEqual(rows[name]['quality'],'normal',(index,name,rows[name]['reason']))
        for name,shift,quality in [('a_ball',4,'normal'),('a_ball',8,'abnormal'),('a_housing',-2,'normal'),('a_housing',-8,'abnormal'),('b_housing',-2,'normal'),('b_housing',-8,'abnormal')]:
            anchor=deepcopy(next(a for a in anchors if a['id']==name))
            anchor['camera_from_local'][:3,3]+=anchor['camera_from_local'][:3,:3]@np.array([shift,0.,0.])
            row=inspector.inspect(frame,[anchor],cfg['profile'])[0]
            self.assertEqual(row['quality'],quality,(name,shift,row['reason']))

    def test_user_confirmed_normal_insert_in_three_frames(self):
        folder=ROOT/'tests/fixtures/insert-normal';cfg=json.loads((folder/'fixture.json').read_text());inspector=ShapeInspector()
        for index in range(3):
            frame=cv2.imread(str(folder/f'camera-{index}.png'))
            anchors,_=anchors_for(frame,{},cfg['adopted'],cfg['catalog'],cfg['profile'],cfg['reference'],'운반용 지그','전체',{})
            rows={r['id']:r for r in inspector.inspect(frame,anchors,cfg['profile'])}
            with self.subTest(frame=index):
                self.assertEqual(rows['b_tpu']['quality'],'normal')
                self.assertEqual(rows['b_tpu']['seating'],'inside')
                self.assertLessEqual(rows['b_tpu']['center_error_mm'],2)
                self.assertEqual(rows['a_ball']['quality'],'normal')
    def test_real_normal_housing_and_displaced_insert_three_frames(self):
        inspector=ShapeInspector()
        for index in range(3):
            frame,cfg,anchors=scene(index)
            rows={r['id']:r for r in inspector.inspect(frame,anchors,cfg['profile'])}
            with self.subTest(frame=index):
                self.assertEqual((rows['b_housing']['state'],rows['b_housing']['quality']),('present','normal'))
                self.assertEqual(rows['a_ball']['quality'],'normal')
                self.assertEqual(rows['b_tpu']['quality'],'abnormal')
                # Wider silhouette allowance still rejects actual center displacement.
                self.assertEqual(rows['b_tpu']['seating'],'inside')
                self.assertEqual(rows['b_tpu']['center_tolerance_mm'],3.)
                self.assertGreater(rows['b_tpu']['center_error_mm'],4)
                self.assertIn('슬롯 중심 이탈',rows['b_tpu']['reason'])
    def test_A_ball_displacement_is_rejected_without_round_part_yaw(self):
        frame,cfg,anchors=scene();anchor=deepcopy(next(a for a in anchors if a['id']=='a_ball'))
        anchor['camera_from_local'][:3,3]+=anchor['camera_from_local'][:3,:3]@np.array([6.,0,0])
        row=ShapeInspector().inspect(frame,[anchor],cfg['profile'])[0]
        self.assertEqual(row['state'],'present');self.assertEqual(row['quality'],'abnormal')
        self.assertGreater(row['center_error_mm'],4);self.assertNotIn('rotation_error_deg',row)
    def test_round_ROI_stays_fixed_across_candidate_types(self):
        frame,cfg,anchors=scene();inspector=ShapeInspector()
        for anchor in (a for a in anchors if a['part']=='insert'):
            _,_,templates=inspector.projected(anchor,cfg['profile'],frame.shape)
            reference=templates[0][1]['roi_polygon']
            for _,masks in templates:self.assertEqual(masks['roi_polygon'],reference)
            # Different/wider candidate parts cannot enlarge the allowed slot.
            self.assertLess(cv2.contourArea(np.array(reference,np.float32)),4000)

    def test_moved_carrier_and_finished_pallet_three_frames(self):
        from so101_teach.occupied_pallet import locate
        from so101_teach.inspection_tracking import PalletReference
        folder=ROOT/'tests/fixtures/carrier-moved';cfg=json.loads((folder/'fixture.json').read_text());inspector=ShapeInspector()
        tracker=PalletReference(ROOT/'tests/fixtures/inspection-all/reference')
        for index in range(3):
            frame=cv2.imread(str(folder/f'camera-{index}.png'))
            anchors,_=anchors_for(frame,{},cfg['adopted'],cfg['catalog'],cfg['profile'],cfg['reference'],'운반용 지그','전체',{})
            rows={r['id']:r for r in inspector.inspect(frame,anchors,cfg['profile'])}
            with self.subTest(frame=index):
                self.assertEqual(rows['b_housing']['quality'],'normal')
                self.assertEqual(rows['b_tpu']['quality'],'abnormal')
                self.assertEqual(rows['a_ball']['quality'],'normal')
                self.assertIsNone(tracker.locate(frame,cfg['profile'],cfg['catalog']['pallet']))
                found=locate(frame,cfg['catalog']['pallet'],cfg['profile']);self.assertIsNotNone(found)
                anchors,_=anchors_for(frame,{'pallet':found},{},cfg['catalog'],cfg['profile'],cfg['reference'],'완성품 팔레트','전체',cfg['placement'])
                row=inspector.inspect(frame,anchors,cfg['profile'])[0]
                self.assertEqual((row['state'],row['product'],row['quality']),('cap_added','A','normal'))

class InsertSeatingUITests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_actual_camera_layout_and_small_middle_regions(self):
        frame,cfg,_=scene()
        self.check_screen(frame,cfg,'updated-1180x760.png')
    def test_reacquires_moved_pallet_with_existing_empty_reference(self):
        import shutil
        folder=ROOT/'tests/fixtures/carrier-moved';cfg=json.loads((folder/'fixture.json').read_text())
        shutil.copytree(ROOT/'tests/fixtures/inspection-all/reference',self.app.data_dir/'inspection_reference')
        self.check_screen(cv2.imread(str(folder/'camera-0.png')),cfg,'moved-1180x760.png',moved=True)
    def test_correctly_seated_middle_is_green(self):
        folder=ROOT/'tests/fixtures/insert-normal';cfg=json.loads((folder/'fixture.json').read_text())
        self.check_screen(cv2.imread(str(folder/'camera-0.png')),cfg,'normal-1180x760.png',normal_insert=True)
    def check_screen(self,frame,cfg,name,moved=False,normal_insert=False):
        a=self.app;p=a.camera_inspection
        insert_quality='normal' if normal_insert else 'abnormal'
        a.profile=cfg['profile'];a.catalog.items=cfg['catalog'];a.catalog.revision+=1;a.workcell_preview=cfg['placement']
        data={'live_by_jig':{}}
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.show_page('camera');a.camera_tabs.select(p);self.root.geometry('1180x760');self.root.update()
        with patch.object(a,'camera_adoption_results',return_value=cfg['adopted']):
            deadline=time.monotonic()+7
            while time.monotonic()<deadline:
                a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
                ready='b_tpu' in p.table.get_children() and p.table.item('b_tpu','tags')==(insert_quality,) and p.table.item('b_housing','tags')==('normal',)
                if ready and (not moved or ('finished' in p.table.get_children() and p.table.item('finished','tags')==('normal',))):break
                time.sleep(.1)
            self.assertEqual(p.table.item('b_tpu','tags'),(insert_quality,))
            self.assertEqual(p.table.item('b_housing','tags'),('normal',))
            self.assertEqual(p.table.item('a_ball','tags'),('normal',))
            if moved:
                self.assertEqual(p.table.item('finished','tags'),('normal',))
                self.assertIn('현재 외곽 재탐색',p.position.get())
            from PIL import ImageGrab
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(ROOT/'verification/b-housing-seating'/name)
