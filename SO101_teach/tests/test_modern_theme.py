"""Native input contracts and full-size demo evidence for the soft teal theme."""
import time
import tkinter as tk
from tkinter import ttk
import unittest
from PIL import ImageGrab
from so101_teach.ui import styles
from so101_teach.ui_scroll import AutoScrollbar
from so101_teach.domain import ROOT
from tests import test_demo_workspace as demo


class NativeThemeTests(unittest.TestCase):
    def test_click_keeps_button_glyphs_and_tab_bounds_fixed(self):
        from tkinter import font
        r=tk.Tk();r.withdraw();styles(r);style=ttk.Style(r)
        try:
            def font_metrics(name,state):
                f=font.Font(r,font=style.lookup(name,'font',state))
                return (f.actual(),f.measure('스텝 추가·편집'),f.metrics('linespace'))
            for name in ('TButton','Primary.TButton','Compact.TButton','Compact.Primary.TButton','Shell.TButton','Shell.Primary.TButton','Nav.TButton'):
                b=ttk.Button(r,text='스텝 추가·편집',style=name);b.pack();r.deiconify();r.update()
                before=(b.winfo_reqwidth(),b.winfo_reqheight(),font_metrics(name,b.state()))
                b.event_generate('<Enter>');b.event_generate('<ButtonPress-1>',x=8,y=8);r.update()
                self.assertEqual((b.winfo_reqwidth(),b.winfo_reqheight(),font_metrics(name,b.state())),before,name)
                b.event_generate('<ButtonRelease-1>',x=8,y=8);r.update()
                self.assertEqual((b.winfo_reqwidth(),b.winfo_reqheight(),font_metrics(name,b.state())),before,name)
                b.destroy()
            for name in ('TNotebook','Card.TNotebook'):
                n=ttk.Notebook(r,style=name);n.pack()
                for title in ('스텝 추가·편집','에피소드 조정'):n.add(ttk.Frame(n,width=400,height=60),text=title)
                r.update()
                def tab_bounds():
                    # Hit-test actual drawn tabs, including their selected-state padding.
                    bounds={}
                    for y in range(42):
                        for x in range(280):
                            index=r.tk.call(n,'identify','tab',x,y)
                            if index=='':continue
                            old=bounds.get(index,(x,y,x,y));bounds[index]=(min(x,old[0]),min(y,old[1]),max(x,old[2]),max(y,old[3]))
                    return bounds
                before=tab_bounds();second=before[1];x=(second[0]+second[2])//2;y=(second[1]+second[3])//2
                n.event_generate('<ButtonPress-1>',x=x,y=y);n.event_generate('<ButtonRelease-1>',x=x,y=y);r.update()
                self.assertEqual(n.index(n.select()),1)
                self.assertEqual(tab_bounds(),before,name)
                self.assertEqual(font_metrics(name+'.Tab',()),font_metrics(name+'.Tab',('selected',)))
                n.destroy()
        finally:r.destroy()

    def test_native_drag_keyboard_focus_and_widget_states(self):
        root=tk.Tk();root.withdraw();styles(root)
        try:
            frame=ttk.Frame(root,style='Surface.TFrame',padding=16);frame.pack(fill='both',expand=True)
            scale=ttk.Scale(frame,from_=0,to=100);scale.grid(row=0,column=0,sticky='ew')
            tree=ttk.Treeview(frame,height=4);tree.grid(row=1,column=0)
            scroll=AutoScrollbar(frame,command=tree.yview);scroll.grid(row=1,column=1,sticky='ns')
            tree.configure(yscrollcommand=scroll.set)
            for n in range(60):tree.insert('', 'end',text=str(n))
            entry=ttk.Entry(frame);entry.grid(row=2,column=0)
            combo=ttk.Combobox(frame,values=('A','B'),state='readonly');combo.grid(row=3,column=0);combo.current(0)
            spin=ttk.Spinbox(frame,from_=0,to=10);spin.grid(row=4,column=0);spin.set('2')
            clicks=[];button=ttk.Button(frame,text='선택',command=lambda:clicks.append(1));button.grid(row=5,column=0)
            root.deiconify();root.update();scale.set(0)
            scale.event_generate('<Button-1>',x=10,y=10);scale.event_generate('<B1-Motion>',x=180,y=10);scale.event_generate('<ButtonRelease-1>',x=180,y=10);root.update()
            self.assertGreater(scale.get(),40)
            entry.focus_force();entry.event_generate('<KeyPress-a>');root.update();self.assertEqual(entry.get(),'a')
            combo.focus_force();combo.event_generate('<KeyPress-Down>');root.update()
            combo.event_generate('<KeyPress-Down>');combo.event_generate('<KeyPress-Return>');root.update()
            self.assertEqual(combo.get(),'B')
            spin.event_generate('<Button-1>',x=spin.winfo_width()-8,y=7);spin.event_generate('<ButtonRelease-1>',x=spin.winfo_width()-8,y=7);root.update()
            self.assertEqual(spin.get(),'3')
            self.assertTrue(scroll.winfo_ismapped());start=tree.yview()[0]
            scroll.event_generate('<Button-1>',x=6,y=10);scroll.event_generate('<B1-Motion>',x=6,y=100);scroll.event_generate('<ButtonRelease-1>',x=6,y=100);root.update()
            self.assertGreater(tree.yview()[0],start)
            button.state(['disabled']);button.invoke();self.assertEqual(clicks,[])
            button.state(['!disabled']);button.focus_force();button.event_generate('<KeyPress-space>');button.event_generate('<KeyRelease-space>');root.update();self.assertEqual(clicks,[1])
            tree.delete(*tree.get_children());root.update();self.assertFalse(scroll.winfo_ismapped())
        finally:root.destroy()


class ModernDemoTests(unittest.TestCase):
    setUp=demo.DemoWorkspaceTests.setUp
    tearDown=demo.DemoWorkspaceTests.tearDown
    wait_for=demo.DemoWorkspaceTests.wait_for

    def test_safe_edit_click_keeps_compact_button_height(self):
        a=self.app;r=self.root;r.geometry('1920x1043');r.update()
        before=(a.safe_edit_btn.winfo_height(),a.delete_step_btn.winfo_height(),a.steps.winfo_height())
        a.safe_edit_btn.invoke();r.update();self.assertTrue(a.safe_editing)
        self.assertEqual((a.safe_edit_btn.winfo_height(),a.delete_step_btn.winfo_height(),a.steps.winfo_height()),before)
        a.safe_edit_btn.invoke();r.update();self.assertFalse(a.safe_editing)
        self.assertEqual((a.safe_edit_btn.winfo_height(),a.delete_step_btn.winfo_height(),a.steps.winfo_height()),before)

    def test_status_fields_and_dynamic_labels_keep_layout_fixed(self):
        a=self.app;r=self.root;r.geometry('1920x1043');r.update()
        def bounds(widgets):
            r.update_idletasks()
            return [(w.winfo_rootx(),w.winfo_rooty(),w.winfo_width(),w.winfo_height()) for w in widgets]
        controls=[a.body,a.editor_card,a.steps,a.preview_canvas,a.arm_btn,a.follow_btn,a.jig_pause_btn,a.teach_reread_jig_btn,a.connect_btn]
        before=bounds(controls)
        for connection,follow,jig,linear in (
            ('모터 미연결','리더 연결 중','전체 지그 채택 중','리니어: 상태 미확인 · 기준 배치'),
            ('현재 자세 유지 · 토크 ON · 집게 접촉 유지','따라가기 준비 중','● 갱신 정지 중 · 재개','리니어: 조립 위치로 이동 중 · 위치 미측정'),
            ('읽기 연결 · 토크 OFF','리더 다시 연결','자동 갱신 정지','리니어: 하차 위치 목표 · 위치 미측정')):
            a.connection.set(connection);a.follow_btn.configure(text=follow);a.jig_pause_btn.configure(text=jig);a.linear_status.set(linear)
            for button in a.arm_quick_buttons.values():button.configure(text='로봇팔3 · 실행 준비')
            self.assertEqual(bounds(controls),before)
        a.open_episode_adjust();r.update();q=a.episode_adjust_panel
        controls=[q.marker_button,q.reset_button,q.tabs,a.preview_canvas]
        before=bounds(controls)
        for text in ('짧은 이름','긴 스텝 이름과 검사 근거를 반복해도 옆의 입력칸과 미리보기를 밀지 않습니다. '*8,''):
            q.selected.set(text);q.draft_status.set(text);q.inspection.product_status.set(text)
            self.assertEqual(bounds(controls),before)
        a.show_page('settings');s=a.settings;s.tabs.select(s.pages['calibration']);r.update();c=s.calibration_panel
        controls=[c.canvas,c.start,c.negative_input,c.positive_input]
        before=bounds(controls)
        for text in ('—','-180.0°'):
            c.angle_value.set(text);c.caption.set('보정 상태와 모델 설명이 길어져도 화면을 밀지 않습니다. '*6);s.cal_status.set('상태 설명 '*35)
            self.assertEqual(bounds(controls),before)

    def test_all_pages_and_two_arm_navigation(self):
        a=self.app;r=self.root;r.geometry('1920x1043');r.update();out=ROOT/'verification/live-app-audit';out.mkdir(parents=True,exist_ok=True)
        def capture(name):
            r.update();x,y=r.winfo_rootx(),r.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+r.winfo_width(),y+r.winfo_height())).save(out/(name+'.png'))
        a.render_enabled=True;a.workcell_view();self.wait_for(lambda:a.last_rgb is not None,20);capture('teaching')
        a.open_episode_adjust();r.update();capture('episode-adjust')
        for idx,tab in enumerate(a.episode_adjust_panel.tabs.tabs()[1:],1):
            a.episode_adjust_panel.tabs.select(tab);r.update();capture('episode-tab-'+str(idx))
        a.nav_buttons['devices'].invoke();r.update();self.assertEqual(a.page,'devices');capture('devices')
        a.nav_buttons['camera'].invoke();a.camera_tabs.select(a.camera_inspection)
        self.wait_for(lambda:a.camera_inspection.worker.result and len(a.camera_inspection.worker.result['rows'])==9 and all(row.get('state')!='checking' for row in a.camera_inspection.worker.result['rows']),15)
        a.draw_camera_frame(time.monotonic());capture('inspection')
        a.camera_tabs.select(a.camera_tabs.tabs()[0]);r.update();a.draw_camera_frame(time.monotonic());capture('camera')
        a.nav_buttons['settings'].invoke();a.settings.tabs.select(a.settings.pages['tcp']);r.update()
        self.wait_for(lambda:a.settings.model_views['tcp'].image is not None,15);capture('settings-tcp')
        a.settings.tabs.select(a.settings.pages['robot']);r.update()
        canvas=a.settings.scroll_canvases[str(a.settings.pages['robot'])]
        # The normal form fits at 1080p; exercise overflow without changing app data.
        body=canvas.winfo_children()[0];extra=ttk.Label(body,text='스크롤 확인\n'*80)
        extra.grid(row=99,column=0);r.update()
        a.settings.scroll_page(10);r.update();self.assertGreater(canvas.yview()[0],0)
        extra.destroy();canvas.yview_moveto(0);r.update();capture('settings-robot')
        for key in ('calibration','jigs','camera','pi','help'):
            a.settings.tabs.select(a.settings.pages[key]);r.update()
            if key=='calibration':self.wait_for(lambda:a.settings.calibration_panel.image is not None,15)
            if key=='jigs':self.wait_for(lambda:a.settings.model_views['jigs'].image is not None,15)
            capture('settings-'+key)
        a.nav_buttons['model'].invoke();r.update();self.wait_for(lambda:bool(a.model_canvas.find_withtag('image')),15);capture('model')
        a.nav_buttons['teach'].invoke();self.assertEqual(a.page,'teach')
        self.manager.select('arm3');r.update();self.assertEqual(self.manager.active,'arm3')
        other=self.manager.apps['arm3'];other.render_enabled=True;other.workcell_view()
        self.wait_for(lambda:other.last_rgb is not None,20);capture('arm3')
        self.assertTrue(all(app.demo_mode for app in self.manager.apps.values()))
