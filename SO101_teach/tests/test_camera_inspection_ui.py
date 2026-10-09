import json,time,unittest
from copy import deepcopy
from pathlib import Path
from tests import test_camera_overview as camera_fixtures
from so101_teach.domain import ROOT
from so101_teach.inspection_overlay import overlay

class CameraInspectionUITests(unittest.TestCase):
    setUp=camera_fixtures.CameraOverviewTests.setUp
    tearDown=camera_fixtures.CameraOverviewTests.tearDown
    camera=camera_fixtures.CameraOverviewTests.camera
    def test_toolbar_controls_share_height_and_stay_inside_maximized_page(self):
        a=self.app;self.root.geometry('1920x1080');a.show_page('camera');a.camera_tabs.select(a.camera_inspection);self.root.update()
        p=a.camera_inspection;controls=(p.mode_choice,p.endpoint_choice,p.connect_button,p.save_button,p.folder_button)
        self.assertEqual(len({w.winfo_height() for w in controls}),1)
        self.assertEqual(len({w.winfo_rooty() for w in controls}),1)
        self.assertLess(p.mode_choice.winfo_rootx()+p.mode_choice.winfo_width(),p.endpoint_choice.winfo_rootx())
        self.assertLessEqual(p.folder_button.winfo_rootx()+p.folder_button.winfo_width(),a.pages['camera'].winfo_rootx()+a.pages['camera'].winfo_width())
        p.mode.set('CAD 설명');p.change();self.root.update();self.assertTrue(p.reference.winfo_ismapped())
        p.mode.set('카메라 영상');p.change();self.root.update();self.assertTrue(p.canvas.winfo_ismapped())
    def test_capture_preserves_display_raw_and_diagnostics_and_rejects_stale(self):
        import cv2,numpy as np
        frame,_=self.camera();a=self.app;p=a.camera_inspection
        a.camera_tabs.select(p);a.draw_camera_frame(time.monotonic())
        saved=p.capture_snapshot;self.assertIsNotNone(saved)
        path=p.save_capture();metadata=json.loads((path.parent.parent/'판정 기록'/path.with_suffix('.json').name).read_text())
        np.testing.assert_array_equal(cv2.imread(str(path)),saved['image'])
        np.testing.assert_array_equal(cv2.imread(str(path.parent.parent/metadata['raw_image'])),saved['raw'])
        self.assertIn('anchors',metadata);self.assertIn('profile',metadata);self.assertIn('display_rows',metadata)
        p.capture_snapshot['at']-=2
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()
        p.mode.set('CAD 설명');p.change()
        self.assertTrue(p.save_button.instate(['disabled']))
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()

    def test_results_remain_during_refresh_then_replace_expire_and_clear_on_context_change(self):
        p=self.app.camera_inspection
        first={'x':dict(state='housing_seated',label='B 하단 안착 형상',quality='normal',score=81,reason='정상')}
        shown,retained=p.display_rows(first,'same',10,10);self.assertFalse(retained)
        shown,retained=p.display_rows({'x':dict(state='checking')},'same',10.4,10.4)
        self.assertTrue(retained);self.assertEqual(shown['x']['label'],'B 하단 안착 형상');self.assertEqual(shown['x']['quality'],'normal')
        self.assertIn('갱신 중',shown['x']['reason']);self.assertEqual(first['x']['quality'],'normal')
        shown,_=p.display_rows({},'same',15.1);self.assertNotIn('x',shown)
        p.display_rows(first,'same',20,20)
        changed={'x':dict(state='cap_added',label='상단 결합',quality='normal',reason='새 결과')}
        shown,retained=p.display_rows(changed,'same',20.3,20.3)
        self.assertFalse(retained);self.assertEqual(shown['x']['state'],'cap_added')
        shown,_=p.display_rows({},'other-arm',20.5);self.assertFalse(shown)

    def test_target_names_fit_and_selected_reason_is_not_truncated(self):
        import tkinter.font as font
        a=self.app;p=a.camera_inspection;self.root.geometry('1920x1080');a.show_page('camera');a.camera_tabs.select(p);self.root.update()
        from tkinter import ttk
        configured=ttk.Style().lookup('Inspection.Treeview','font')
        textfont=font.Font(font=configured)
        self.assertLess(textfont.measure('리니어 우측 B용 팔레트')+12,p.table.column('part','width'))
        detail='판정 상세 · '+('위치와 경계 기준 설명 '*15)
        p.table.insert('','end',iid='detail-test',values=('대상','판정','80%'));p.row_details['detail-test']=detail
        p.table.selection_set('detail-test');p.select_row();self.assertEqual(p.details.get(),detail);self.assertEqual(p.detail_label.get('1.0','end-1c'),detail)

    def test_detector_gap_preserves_result_and_region_in_real_table_updates(self):
        from unittest.mock import patch
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        a.camera_tabs.select(p);p.update_frame(frame,a.camera_view_results(data),frame_at=time.monotonic())
        context=p.rows_context
        anchors=[anchor for _,anchor in p.previous_outlines.values()]
        self.assertTrue(anchors)
        anchor=anchors[0];key=anchor['id'];now=time.monotonic()
        p.previous_rows={key:(now,dict(id=key,state='present',label='확인된 부품 결과',quality='normal',score=88,reason='검증된 이전 결과'))}
        p.worker.result=None
        messages={name:'지그 검출 대기' for name in ('운반용 지그','리니어 조립','완성품 팔레트')}
        with patch('so101_teach.camera_inspection_ui.all_anchors',return_value=([],messages)),patch.object(p.worker,'submit') as submit:
            for fresh in (False,True):
                p.last_submit_at=0
                p.update_frame(frame,{},detection_fresh=fresh,frame_at=time.monotonic())
                self.assertEqual(p.rows_context,context)
                self.assertIn(key,p.table.get_children())
                self.assertEqual(p.table.item(key,'values')[1],'확인된 부품 결과')
                self.assertEqual(p.table.item(key,'tags'),('normal',))
                self.assertIn('정상 1',p.title.get())
                self.assertIn('갱신 중',p.status.get())
                self.assertEqual(submit.call_args.args[3],[])
        p.clear();self.assertFalse(p.previous_rows);self.assertFalse(p.previous_outlines)

    def test_small_pose_updates_and_detection_gaps_do_not_reset_worker_context(self):
        from unittest.mock import patch
        from so101_teach.inspection_geometry import all_anchors
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        anchors,messages=all_anchors(frame,a.camera_view_results(data),{},a.catalog.items,a.profile,p.reference.data,a.workcell_preview,p.endpoint.get())
        changed=deepcopy(anchors)
        for anchor in changed:anchor['camera_from_local'][0,3]+=.1
        keys=[]
        with patch.object(p.worker,'submit') as submit:
            for values in (anchors,changed,[],anchors):
                p.last_submit_at=0
                with patch('so101_teach.camera_inspection_ui.all_anchors',return_value=(values,messages)):
                    p.update_frame(frame,{},frame_at=time.monotonic())
                keys.append(submit.call_args.args[0])
            self.assertTrue(all(key==keys[0] for key in keys))
            p.endpoint.set('전진 목표');p.last_submit_at=0
            with patch('so101_teach.camera_inspection_ui.all_anchors',return_value=(anchors,messages)):p.update_frame(frame,{},frame_at=time.monotonic())
            self.assertNotEqual(submit.call_args.args[0],keys[0])

    def test_inspection_view_accepts_delayed_jig_result_with_original_timestamp(self):
        from unittest.mock import patch
        from so101_teach.devices import CameraSession
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        now=time.monotonic();camera=CameraSession('unused');camera.running=True;camera.preview_frame=(frame,now);camera.observation=(frame,data,now-2)
        obs=camera.preview_observation
        self.assertEqual(obs[1]['detection_frame_at'],now-2)
        self.assertEqual(obs[1]['preview_detection_age_s'],2)
        a.camera_tabs.select(p)
        with patch.object(a,'camera_view_observation',return_value=obs),patch.object(p,'update_frame') as update:
            a.draw_camera_frame(now);self.assertTrue(update.call_args.kwargs['detection_fresh'])
            a.draw_camera_frame(now+1.01);update.reset_mock()
        camera.observation=(frame,data,now-3.1)
        self.assertFalse(camera.preview_observation[1]['live_by_jig'])
        camera.running=False

    def test_completed_two_second_inspection_is_shown_instead_of_discarded(self):
        from unittest.mock import patch
        from so101_teach.inspection_geometry import all_anchors
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        anchors,messages=all_anchors(frame,a.camera_view_results(data),{},a.catalog.items,a.profile,p.reference.data,a.workcell_preview,p.endpoint.get())
        self.assertTrue(anchors)
        with patch('so101_teach.camera_inspection_ui.all_anchors',return_value=(anchors,messages)),patch.object(p.worker,'submit'):
            p.update_frame(frame,{},frame_at=time.monotonic());now=time.monotonic()
            item=anchors[0];row=dict(id=item['id'],state='present',quality='normal',label='검사 완료',score=88,reason='2초 처리 완료')
            p.worker.result=dict(key=p.context,at=now-5.2,completed=now-.1,anchors=anchors,rows=[row],error=None)
            p.update_frame(frame,{},frame_at=now)
            self.assertEqual(p.table.item(item['id'],'values')[1],'검사 완료')
            self.assertEqual(p.table.item(item['id'],'tags'),('normal',))
            self.assertTrue(p.capture_snapshot['metadata']['inspection_valid'])

    def test_tabs_overlay_stale_and_cad_switch(self):
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        self.assertEqual([a.camera_tabs.tab(t,'text') for t in a.camera_tabs.tabs()],['지그 검출','부품·조립 검사'])
        self.assertNotIn('inspection',a.pages)
        a.camera_tabs.select(p);p.station.set('완성품 팔레트');p.change();a.root.update()
        self.assertIn('전체 영역 자동 비교',p.status.get());self.assertTrue(p.canvas.find_withtag('image'))
        p.mode.set('CAD 설명');p.change();a.root.update();self.assertTrue(p.reference.winfo_ismapped())
        p.reference.choice.current(1);p.reference.select();self.assertEqual(p.reference.choice.current(),1)
        p.mode.set('카메라 영상');p.change();self.assertIn('고정 좌표 없음',p.status.get())
        a.camera.observation=(frame,data,time.monotonic()-2);a.draw_camera_frame(time.monotonic())
        self.assertFalse(p.canvas.find_withtag('image'));self.assertIn('최신',p.status.get())
    def test_overlay_rejects_held_direction_and_calibration_mismatch(self):
        import numpy as np
        frame,data=self.camera();a=self.app;ref=a.camera_inspection.reference.data
        results=deepcopy(a.camera_view_results(data))
        results['pallet']['pose_held']=True
        image,message=overlay(frame,results,a.catalog.items,a.profile,ref,'완성품 팔레트','B')
        np.testing.assert_array_equal(image,frame);self.assertIn('감지 대기',message)
        results=deepcopy(a.camera_view_results(data));profile=deepcopy(a.profile);profile['intrinsics']['size']=[1,1]
        _,message=overlay(frame,results,a.catalog.items,profile,ref,'완성품 팔레트','B');self.assertIn('보정',message)
        jid=ref['stations']['carrier']['jig_id'];results[jid]['selected']['orientation_verified']=False
        _,message=overlay(frame,results,a.catalog.items,a.profile,ref,'운반용 지그','B');self.assertIn('방향 확인',message)
    def test_projection_applies_mesh_rotation_and_part_height(self):
        import numpy as np
        from unittest.mock import patch
        frame=np.zeros((600,800,3),dtype=np.uint8)
        ref=json.loads((ROOT/'inspection/roi_reference.json').read_text());jid=ref['stations']['carrier']['jig_id']
        profile={'table_z_mm':0,'intrinsics':{'size':[800,600],'K':[[500,0,400],[0,500,300],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,1000],[0,0,0,1]]}}
        results={jid:{'selected':{'orientation_verified':True,'metric':{'center_xy_mm':[100,200],'yaw_deg':0,'symmetry_deg':360,'mesh_yaw_offset_deg':180}}}}
        from so101_teach.vision import project_plane
        with patch('so101_teach.inspection_overlay.project_plane',wraps=project_plane) as project:
            _,message=overlay(frame,results,{jid:{'support_height_mm':10}},profile,ref,'운반용 지그','B')
            self.assertIn('3개',message)
            np.testing.assert_allclose(project.call_args_list[0].args[0][0],[200,192])
            self.assertAlmostEqual(project.call_args_list[0].args[2],41.35)
    def test_actual_middle_stage_table_adopted_coordinates_and_background_render(self):
        import cv2
        from PIL import ImageGrab
        from types import SimpleNamespace
        a=self.app;p=a.camera_inspection;folder=ROOT/'tests/fixtures/part-inspection'
        fixture=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'insert-added.jpg'))
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,{},time.monotonic()),close=lambda:None,join=lambda n:True)
        a.profile=fixture['profile'];a.workcell_preview=fixture['placement'];a.show_page('camera');a.camera_tabs.select(p)
        p.station.set('리니어 조립');p.product.set('전체');p.change();self.root.geometry('1180x760');self.root.update()
        # Keep actual live-video timestamps moving while the background inspector runs.
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            a.camera.observation=(frame,{},time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
            if all('중단 삽입' in p.table.item(k,'values')[1] for k in ('linear_1.5_0','linear_1.5_1')):break
            time.sleep(.12)
        self.assertEqual(len(p.table.get_children()),9)
        self.assertTrue(all('중단 삽입' in p.table.item(k,'values')[1] for k in ('linear_1.5_0','linear_1.5_1')))
        self.assertIn('후진 목표',p.status.get());self.assertIn('전체 9',p.title.get())
        self.assertGreater(p.canvas.winfo_height(),180)
        self.assertLessEqual(p.legend.winfo_rooty()+p.legend.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        out=ROOT/'verification/part-inspection';out.mkdir(exist_ok=True)
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/'middle-stage-1180x760.png')
        p.endpoint.set('전진 목표');p.change();p.update_frame(frame,{},frame_at=time.monotonic())
        self.assertIn('미리보기',p.status.get())
        self.assertTrue(all(p.table.item(k,'values')[1]=='분석 중' for k in ('linear_100_0','linear_100_1')))
        self.assertNotIn('중단 삽입',p.parts.get())
        p.clear();self.assertFalse(p.table.get_children());self.assertFalse(p.canvas.find_withtag('image'))
    def test_adopted_jig_stays_visible_when_live_detection_expires(self):
        from unittest.mock import patch
        frame,data=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);p.station.set('완성품 팔레트');self.root.update()
        accepted=deepcopy(a.camera_view_results(data));accepted['pallet']['pose_held']=True;accepted['pallet']['pose_measured_at']=time.monotonic()
        with patch.object(a,'camera_adoption_results',return_value=accepted):p.update_frame(frame,{},detection_fresh=False,frame_at=time.monotonic())
        self.assertIn('채택 위치',p.position.get());self.assertIn('X ',p.position.get());self.assertIn('검출 갱신 대기',p.status.get())
        with patch.object(a,'camera_adoption_results',return_value={}):p.update_frame(frame,{},detection_fresh=False,frame_at=time.monotonic())
        self.assertNotIn('채택 위치',p.position.get());self.assertIn('이전 검출',p.position.get())
        p.previous_outlines={key:(at-6,anchor) for key,(at,anchor) in p.previous_outlines.items()}
        with patch.object(a,'camera_adoption_results',return_value={}):p.update_frame(frame,{},detection_fresh=False,frame_at=time.monotonic())
        self.assertTrue(all(p.table.item(k,'values')[1]=='위치 대기' for k in p.table.get_children()))
    def test_confirmed_carrier_heading_and_adoption_reach_inspection(self):
        import cv2
        from types import SimpleNamespace
        from unittest.mock import patch
        from PIL import ImageGrab
        from so101_teach.vision_service import MultiDetector
        a=self.app;p=a.camera_inspection;folder=ROOT/'tests/fixtures/part-inspection'
        a.profile=json.loads((folder/'fixture.json').read_text())['profile']
        a.catalog.items=json.loads((folder/'carrier-reference.json').read_text())['jigs'];a.catalog.revision+=1
        detector=MultiDetector(a.catalog,a.profile);frame=cv2.imread(str(ROOT/'tests/fixtures/vision-lens/input.jpg'))
        now=time.monotonic();base=now-3.1
        for delta in (0,.8,1.6,2.4,3.05):
            with patch('so101_teach.vision_service.time.monotonic',return_value=base+delta):data=detector.process(frame)
        jid=next(iter(a.catalog.items));chosen=data['by_jig'][jid]['selected']
        self.assertIsNotNone(chosen);self.assertTrue(chosen['orientation_verified']);self.assertEqual(chosen['metric']['symmetry_deg'],360)
        a.detector=detector;a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.show_page('camera');a.camera_tabs.select(p);p.station.set('운반용 지그');p.product.set('전체');p.change();self.root.geometry('1180x760');self.root.update()
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic());self.root.update()
            if all(p.table.item(k,'values')[1] not in ('위치 대기','분석 중','확인 중') for k in ('a_cap','a_ball','a_housing','b_cap','b_tpu','b_housing')):break
            time.sleep(.12)
        self.assertEqual(len(p.table.get_children()),9);self.assertTrue(all(p.table.item(k,'values')[1] not in ('위치 대기','분석 중','확인 중') for k in ('a_cap','a_ball','a_housing','b_cap','b_tpu','b_housing')))
        self.assertIn('채택 위치',p.position.get());self.assertNotIn('방향 확인 대기',p.parts.get())
        p.status.set('기록 영상 검증 · 현재 확정된 지그 형상·검출 방식 적용 · 실시간 화면 아님');self.root.update_idletasks()
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(ROOT/'verification/part-inspection/carrier-recording-current-config.png')
