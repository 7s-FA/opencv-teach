import time
import unittest
from copy import deepcopy
from pathlib import Path
from tests import test_camera_overview as fixtures
from so101_teach.inspection_overlay import detection_summary, overlay


class InspectionSummaryTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_stale_or_missing_results_are_not_reported_as_detection(self):
        catalog={'jig':{'name':'운반용 지그'}}
        data={'jig':{'selected':{'orientation_verified':True}}}
        self.assertIn('검출됨',detection_summary(data,catalog,fresh=True))
        self.assertNotIn('검출됨',detection_summary(data,catalog,fresh=False))
        self.assertIn('검출 결과 없음',detection_summary({},catalog,fresh=True))
        data['jig']['pose_held']=True
        self.assertIn('이전 위치 유지',detection_summary(data,catalog,fresh=True))
    def test_projected_region_is_distinguished_from_detected_part(self):
        frame,data=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);p.station.set('완성품 팔레트');self.root.update()
        a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic())
        self.assertIn('완제품 팔레트 · 검출됨',p.detected.get())
        self.assertIn('완성품 팔레트',p.parts.get());self.assertNotIn('PASS',p.parts.get())
        report={};overlay(frame,a.camera_view_results(data),a.catalog.items,a.profile,p.reference.data,p.station.get(),'B',report=report)
        self.assertEqual(report['projected_parts'],['finished'])
        p.update_frame(frame,{},detection_fresh=False)
        self.assertIn('갱신 대기',p.detected.get());self.assertNotIn('검출됨',p.detected.get());self.assertNotIn('합격',p.parts.get())
    def test_three_part_summary_and_legend_fit_small_window(self):
        from PIL import ImageGrab
        frame,data=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);p.station.set('운반용 지그');p.product.set('전체');self.root.geometry('1180x760');self.root.update()
        a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
        for part in ('A 하단','A 중단','A 상단','B 하단','B 중단','B 상단'):self.assertIn(part,p.parts.get())
        self.assertGreater(p.canvas.winfo_height(),180)
        self.assertLessEqual(p.legend.winfo_rooty()+p.legend.winfo_height(),p.summary.winfo_rooty()+p.summary.winfo_height())
        out=Path(__file__).parents[1]/'verification/inspection-summary';out.mkdir(exist_ok=True)
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/'carrier-1180x760.png')
