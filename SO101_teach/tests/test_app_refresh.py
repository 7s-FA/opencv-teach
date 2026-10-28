"""Stable layout and incremental drawing without relaxing live-data checks."""
import unittest
from unittest.mock import patch
from PIL import Image
from tests import test_ui as fixtures


class AppRefreshTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def settle(self):
        self.root.geometry('1920x1080');self.root.update_idletasks()

    def test_long_status_does_not_move_teaching_or_monitor_controls(self):
        a=self.app;self.settle()
        sizes=[]
        for message in ('Pi 저장본 대기','전송 결과 확인 · 연결 복구를 기다립니다. '*40,'Pi 저장본 대기'):
            a.pi_episode_status.set(message);self.root.update_idletasks()
            sizes.append((a.steps.winfo_rooty(),a.steps.winfo_height()))
        self.assertEqual(len(set(sizes)),1)
        a.show_page('devices');self.root.update_idletasks();positions=[]
        card=a.dual_monitor.cards['arm2']
        for message in ('최신 수신','연결 오류 · 장치 상태를 확인하세요. '*40,'최신 수신'):
            card['status'].set(message);card['message'].set(message);self.root.update_idletasks()
            positions.append((card['tree'].winfo_rooty(),a.dual_monitor.winfo_reqheight()))
        self.assertEqual(len(set(positions)),1)

    def test_full_status_is_readable_scrollable_and_identical_updates_preserve_scroll(self):
        self.settle();a=self.app;area=a.pi_episode_status_label
        text='\n'.join('진단 내용 '+str(i) for i in range(40));a.pi_episode_status.set(text);self.root.update_idletasks()
        self.assertEqual(area.text.get('1.0','end-1c'),text)
        self.assertEqual(str(area.text.cget('state')),'disabled')
        self.assertFalse(any(w.winfo_class()=='TScrollbar' for w in area.winfo_children()))
        area.text.yview_moveto(1);self.root.update_idletasks();position=area.text.yview()
        a.pi_episode_status.set(text);self.assertEqual(area.text.yview(),position);self.assertGreater(position[0],0)
        callbacks=len(a.pi_episode_status.trace_info());area.destroy()
        self.assertEqual(len(a.pi_episode_status.trace_info()),callbacks-1)
        a.pi_episode_status.set('종료 후 갱신도 정상')

    def test_hidden_scene_work_is_skipped_but_poll_heartbeat_continues(self):
        a=self.app;self.settle()
        for page in ('camera','devices','settings'):
            a.show_page(page);self.root.update_idletasks()
            with patch.object(a.catalog,'mesh',side_effect=AssertionError('Hidden 3D work')):
                self.root.after_cancel(a.render_job);a.render_tick()
            previous=a.ui_heartbeat
            self.root.after_cancel(a.job);a.poll();self.assertGreaterEqual(a.ui_heartbeat,previous)
        a.show_page('teach')
        with patch.object(a.catalog,'mesh',wraps=a.catalog.mesh) as mesh:
            self.root.after_cancel(a.render_job);a.render_tick();self.assertTrue(mesh.called)

    def test_caption_is_written_once_only_when_final_content_changes(self):
        a=self.app;self.settle();a.preview_caption.set('이전 내용')
        with patch.object(a.preview_caption,'set',wraps=a.preview_caption.set) as update:
            self.root.after_cancel(a.render_job);a.render_tick();self.assertEqual(update.call_count,1)
            self.root.after_cancel(a.render_job);a.render_tick();self.assertEqual(update.call_count,1)

    def test_same_image_and_size_reuses_photo_but_resize_replaces_it(self):
        a=self.app;self.settle();image=Image.new('RGB',(640,480),'gray')
        a.fit_image(a.preview_canvas,image);photo=a.preview_canvas.photo;item=a.preview_canvas.find_withtag('image')
        a.fit_image(a.preview_canvas,image);self.assertIs(a.preview_canvas.photo,photo)
        a.preview_canvas.configure(width=700);self.root.geometry('1800x1000');self.root.update_idletasks()
        a.fit_image(a.preview_canvas,image);self.assertIsNot(a.preview_canvas.photo,photo)
        self.assertEqual(a.preview_canvas.find_withtag('image'),item)
        new=Image.new('RGB',(640,480),'blue');photo=a.preview_canvas.photo;a.fit_image(a.preview_canvas,new)
        self.assertIsNot(a.preview_canvas.photo,photo)

    def test_countdown_does_not_relayout_or_rebuild_comparison_rows(self):
        from so101_teach.jig_comparison import comparison_groups
        a=self.app;fixtures.UITests.fake_jig_camera(self);a.follow_jig.set(True);a.commit_target()
        panel=a.jig_comparison;groups=comparison_groups(a.episode['steps'],a.catalog.items)
        panel.show(groups,'유지 5.0초',live=True)
        with patch.object(panel,'update_visibility') as layout,patch.object(panel,'draw') as draw:
            panel.show(groups,'유지 4.9초',live=True);layout.assert_not_called();draw.assert_not_called()
        self.assertIn('4.9',panel.caption.get())

    def test_inspection_reason_length_does_not_move_legend(self):
        a=self.app;self.settle();a.show_page('camera');p=a.camera_inspection;a.camera_tabs.select(p);self.root.update_idletasks()
        geometry=[]
        for text in ('영상 대기','판정 근거를 확인하세요. '*80,'영상 대기'):
            p.details.set(text);self.root.update_idletasks()
            geometry.append((p.legend.winfo_rooty(),p.canvas.winfo_height()))
            self.assertEqual(p.detail_label.get('1.0','end-1c'),text)
        self.assertEqual(len(set(geometry)),1)

    def test_same_model_error_does_not_restart_or_flash_the_preview(self):
        a=self.app;view=a.settings.model_views['tcp']
        view.set_spec(None,'모델 파일 확인 필요');version=view.version
        with patch.object(view,'paint') as paint:
            view.set_spec(None,'모델 파일 확인 필요');paint.assert_not_called()
        self.assertEqual(view.version,version)
        view.set_spec({'kind':'tcp','tcp':a.profile['tcp']});self.assertGreater(view.version,version)

    def test_settings_model_messages_keep_canvas_height(self):
        a=self.app;self.settle();a.show_page('settings');a.settings.tabs.select(a.settings.pages['tcp']);self.root.update_idletasks()
        view=a.settings.model_views['tcp'];sizes=[]
        for text in ('모델 읽는 중…','좌표 계산 기준을 확인하세요. '*45,'모델 표시 완료'):
            view.caption.set(text);view.details.set(text);self.root.update_idletasks()
            sizes.append(view.canvas.winfo_height())
        self.assertEqual(len(set(sizes)),1)

    def test_button_disable_survives_later_status_widget_failure(self):
        a=self.app
        for button in (a.arm_btn,a.move_btn,a.execute_btn):button.state(['!disabled'])
        self.assertIsNone(a.session)
        with patch.object(a.motion_speed_choice,'configure',side_effect=RuntimeError('status widget failed')):
            self.root.after_cancel(a.job);a.poll()
        for button in (a.arm_btn,a.move_btn,a.execute_btn):self.assertTrue(button.instate(['disabled']))
        self.assertIn('status widget failed',a.message.get())

    def test_observation_mode_overrides_pi_hold_permission(self):
        a=self.app;a.camera_only=True
        with patch('so101_teach.pi_execution.active_execution',return_value=True):
            a.apply_command_permissions({})
        for button in (a.arm_btn,a.hold_btn,a.release_btn,a.execute_btn):self.assertTrue(button.instate(['disabled']))
        a.camera_only=False
        with patch('so101_teach.pi_execution.active_execution',return_value=True):
            a.apply_command_permissions({})
        self.assertTrue(a.hold_btn.instate(['!disabled']))
        self.assertTrue(a.execute_btn.instate(['disabled']))

    def test_adjustment_off_toggle_precedes_final_pi_restrictions(self):
        a=self.app
        for pi_active in (False,True):
            with patch.object(a.live_adjust,'poll',side_effect=lambda:a.move_btn.state(['!disabled'])), \
                 patch('so101_teach.pi_execution.active_execution',return_value=pi_active):
                self.root.after_cancel(a.job);a.poll()
            self.assertEqual(a.move_btn.instate(['disabled']),pi_active)
