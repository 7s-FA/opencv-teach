"""Pi connection and target selection inside the existing settings notebook."""
import time,tkinter as tk
from copy import deepcopy
from tkinter import ttk
from .ui_status import StatusText
from . import pi_connection
from .domain import Calibration

class PiSettingsPanel:
    def __init__(self,settings):
        self.s=settings;self.app=settings.app;self.job=None;self.job_kind=None;self.auto_enabled=False;self.retry_at=0.;self.retry_count=0;self.recovery_pending=False;self.restore_devices=False
        p=settings.form('pi');p.columnconfigure(1,weight=0,minsize=640)
        settings.label(p,0,'장치 연결: 팔로워·카메라는 Pi, 리더는 PC USB에 연결합니다.')
        error=None
        try:values=pi_connection.load(self.app.data_dir)
        except (ValueError,OSError,TypeError) as exc:values=dict(pi_connection.DEFAULTS);error=str(exc)
        for row,(key,label) in enumerate([('host','Pi 주소 (IP 또는 이름)'),('user','Pi 사용자 계정'),('port','SSH 포트'),('identity_file','이 PC의 SSH 키 (선택)'),('app_dir','Pi 프로그램 폴더'),('python','Pi Python 실행 파일')],1):
            settings.field(p,row,label,'pi_'+key,values[key])
        settings.button(p,4,'키 파일 선택',lambda:settings.choose('pi_identity_file',extensions=('', '.pem', '.key')))
        settings.label(p,7,'Pi에 등록한 SSH 키로 인증합니다. 비밀번호는 저장하지 않습니다.')
        actions=ttk.Frame(p,style='Card.TFrame');actions.grid(row=8,column=0,columnspan=3,sticky='w',pady=8)
        self.save_btn=self.app.button(actions,'설정 저장',self.save)
        self.probe_btn=self.app.button(actions,'SSH 연결 확인',self.probe)
        self.connect_btn=self.app.button(actions,'Pi 대상으로 선택',self.use_pi,True)
        self.local_btn=self.app.button(actions,'이 PC 사용',self.use_local)
        for button in (self.save_btn,self.probe_btn,self.connect_btn,self.local_btn):button.pack(side='left',padx=(0,8))
        self.status=tk.StringVar(value='저장 설정 읽기 실패: '+error if error else '시작은 조회 전용입니다. 제어권은 상단 전체 연결로 요청합니다.')
        StatusText(p,self.status,height=4,font=('Noto Sans CJK KR',11)).grid(row=10,column=0,columnspan=3,sticky='ew',pady=10)
        settings.label(p,11,'시작 시 조회 전용 · 전체 연결을 누를 때만 제어권 요청\nPi 에피소드는 시작 시 불러오며, 편집본은 에피소드 전송으로 반영합니다.')
        settings.label(p,12,'통신 끊김: 이동 정지·자세 유지 · 자동 재연결 없음 · 전체 연결로 다시 요청하며 중단된 동작은 자동 재개하지 않습니다.')
    def values(self):return {key:self.s.value('pi_'+key) for key in pi_connection.DEFAULTS}
    def save(self):
        pi_connection.save(self.app.data_dir,self.values());self.status.set('연결 설정 저장됨');self.app.notice('라즈베리파이 연결 설정 저장')
    def probe(self):
        if getattr(self.app,'demo_mode',False):raise ValueError('데모에서는 실제 장치 연결·보정·Pi 전송을 실행하지 않습니다.')
        if self.job:return
        values=pi_connection.validate(self.values(),True);self.job_kind='probe';self.job=self.s.pool.submit(pi_connection.probe,values)
        self.probe_btn.state(['disabled']);self.status.set(f"SSH 확인 중 · {values['user']}@{values['host']}:{values['port']}")
    def use_pi(self):
        if getattr(self.app,'demo_mode',False):raise ValueError('데모에서는 실제 장치 대상을 변경하지 않습니다.')
        if self.job:raise ValueError('연결 확인을 마친 뒤 대상을 변경하세요.')
        self.s.require_idle()
        a=self.app
        self.save()
        a.remote_mode=True;a.preferences['device_host']='pi';a.save_preferences()
        a.start_linear_state_query()
        self.status.set('Pi 대상으로 선택됨 · 제어 연결은 상단 전체 연결을 누르세요.')
        a.device_target.set('장치: Pi · 조회 전용')
    def schedule_recovery(self):
        # A lost or rejected lease requires another explicit click. Never retry
        # across an external job boundary or replay unfinished UI continuations.
        a=self.app
        if a.closed or not a.remote_mode:return
        if not self.recovery_pending:
            a.live_adjust.stop(halt=False);a.stop_preview(quiet=True)
        self.recovery_pending=True;self.auto_enabled=False;self.restore_devices=False
        self.retry_count=0;self.retry_at=0.;a.startup_devices_pending=False
        self.status.set('제어 연결이 끊겼습니다. 자동 재연결 없이 조회 전용으로 대기합니다. 전체 연결을 눌러 다시 요청하세요.')
        a.device_target.set('장치: Pi · 조회 전용 · 전체 연결 필요')
    def connect(self,automatic=False,*,control_requested=False):
        if getattr(self.app,'demo_mode',False):raise ValueError('데모에서는 실제 장치 연결·보정·Pi 전송을 실행하지 않습니다.')
        # Even a stale timer callback must never reacquire a controller.
        if automatic:return
        if not control_requested:raise ValueError('조회 전용입니다. 상단 전체 연결을 눌러 제어권을 요청하세요.')
        if self.job:return
        a=self.app
        if a.camera_only:raise ValueError('카메라 전용 실행에서는 제어권을 요청하지 않습니다.')
        if getattr(a,'work_observation_mode',False):
            if a.page=='camera' and a.work_monitor_selected():
                raise ValueError('실시간 작업은 조회 전용입니다. 티칭 화면에서 전체 연결을 누르세요.')
            a.work_observation_mode=False;a._work_monitor_passive=False
        self.s.require_idle()
        self.auto_enabled=False
        if a.closed:return
        from .remote_config import configuration_bundle
        from .remote_client import RemoteLink,RemoteLeaderSession,RemoteCameraSession
        from .motion import SPEED_PRESETS
        values=pi_connection.save(a.data_dir,self.values());bundle=configuration_bundle(a)
        a.remote_mode=True;a.preferences['device_host']='pi'
        a.save_preferences()
        a.start_linear_state_query()
        a.device_target.set('장치: Pi · 연결 중')
        restore=True
        from .arm_workspace import calibration_pending
        if calibration_pending(a.profile):restore=False
        profile=deepcopy(a.profile);rate=SPEED_PRESETS[a.motion_speed_choice.get()];want_camera=a.camera_needed()
        camera=a.camera;old=a.remote;a.remote=None
        def work():
            if camera:camera.close();camera.join(3)
            if old:
                old.close(disconnect=False)
                reader=getattr(old,'leader_client',None)
                if reader and not reader.join(3):raise ValueError('이전 PC 리더 연결 정리 대기')
            if camera and camera.running:raise ValueError('기존 카메라 연결 정리 중입니다.')
            link=RemoteLink(values);link.robot_id=profile.get('robot_id','arm2');link.heartbeat_provider=lambda:a.ui_heartbeat
            link.audit_path=a.data_dir/'diagnostics'/f'pi-link-{time.time_ns()}.json'
            try:
                link.open();link.sync_bundle(bundle)
                link.restored_leader=None;link.restored_camera=None;link.devices_prepared=restore
                # Connecting reuses a held Pi session or starts read-only. No arm,
                # follow, move or play request is made during connection.
                if restore:
                    link.rpc('connect',{'speed':rate})
                    leader=profile.get('leader') or {}
                    if profile.get('mode')=='leader' and leader.get('port') and leader.get('calibration_file'):
                        cal=Calibration(a.data_dir/leader['calibration_file'])
                        worker=RemoteLeaderSession(link,leader['port'],cal,role='leader',audit_path=a.data_dir/'diagnostics'/f'leader-session-{time.time_ns()}.json')
                        worker.poll_seconds=.02;worker.start();link.restored_leader=worker
                if want_camera:
                    # The workspace camera coordinator opens the shared device
                    # after installation; another arm may still be measuring.
                    if not getattr(a,'workspace_manager',None):
                        receiver=RemoteCameraSession(link);receiver.start();link.restored_camera=receiver
                else:link.rpc('camera_stop')
                # Obtain state before enabling controls, including an already-held arm.
                link.latest_state=link.http('/state',{'lease':link.lease,'alive':True,'after':link.event_id})
                if a.closed:raise ValueError('화면이 닫혀 Pi 연결을 취소했습니다.')
                return link
            except Exception:link.close(disconnect=False);raise
        self.job_kind='connect';self.job=self.s.pool.submit(work)
        def cleanup(job):
            if a.closed and not job.cancelled():
                try:job.result().close(disconnect=False)
                except Exception:pass
        self.job.add_done_callback(cleanup)
        self.status.set('제어권 요청·장치 설정 확인 중');self.connect_btn.state(['disabled'])
    def install(self,link):
        from .remote_client import RemoteDetector,RemoteMotionSession,RemoteLeaderSession
        a=self.app
        if link.error:raise ValueError(link.error)
        a.remote=link;a.camera=getattr(link,'restored_camera',None);a.session=None;a.leader_session=getattr(link,'restored_leader',None);a.latest=None
        a.detector=RemoteDetector(a,link);a.pose_latch=a.detector.latches[a.active_jig]
        state=link.latest_state or {};follower=state.get('follower')
        if follower and follower['running'] and not a.camera_only:
            a.session=RemoteMotionSession(link,a.profile['port'],a.calibration);a.session.running=True;link.listeners['follower']=a.session
            a.session.consume(follower,time.monotonic()-state['server_now'],time.monotonic())
            if a.leader_session is not None:a.bind_leader_connection()
        a.device_target.set('장치: Pi · '+link.config['host']);self.s.refresh_device_ports()
        if not hasattr(link,'devices_prepared'):
            if a.camera_needed():a.start_camera()
            else:link.rpc('camera_stop')
        elif getattr(a,'workspace_manager',None) and a.camera_needed():a.guard(a.start_camera)
        devices=link.health['devices'];ports=devices['serial'];cameras=devices['cameras']
        self.status.set('Pi 연결됨 · '+link.config['host']+'\n로봇 포트: '+(', '.join(ports) or '아직 없음')+'\n카메라: '+(', '.join(cameras) or '아직 없음'))
        a.notice('Pi 제어 연결 완료 · 중단된 동작은 자동 재개하지 않습니다.')
        a.startup_devices_pending=False
        self.recovery_pending=False;self.retry_count=0;self.retry_at=0.

    def use_local(self):
        if self.app.camera_only:raise ValueError('카메라 전용 실행은 Pi 카메라를 사용합니다. 장치 대상을 바꾸려면 일반 실행으로 다시 시작하세요.')
        if self.job:raise ValueError('Pi 연결 확인이 끝난 뒤 전환하세요.')
        self.s.require_idle();a=self.app;self.auto_enabled=False;self.recovery_pending=False;self.retry_count=0;self.retry_at=0.
        if a.camera:a.camera.close();a.camera.join(2)
        if a.camera and a.camera.running:raise ValueError('Pi 카메라 연결 정리 중입니다. 잠시 후 다시 전환하세요.')
        if a.remote:a.remote.close(disconnect=False)
        a.remote=None;a.remote_mode=False;a.stop_linear_state();a.camera=None;a.session=None;a.leader_session=None;a.latest=None
        from .vision_service import MultiDetector
        a.detector=MultiDetector(a.catalog,a.profile,a.active_jig,a.pose_latch);a.detector.seconds=a.pose_latch.seconds;a.detector.clear()
        self.s.refresh_device_ports();a.preferences['device_host']='local';a.save_preferences();a.device_target.set('장치: 이 PC')
        if a.camera_needed():a.start_camera()
        self.status.set('이 PC의 USB·카메라를 사용합니다.')
    def poll(self):
        a=self.app
        if a.closed:return
        if self.job and self.job.done():
            job=self.job;kind=self.job_kind;self.job=None;self.probe_btn.state(['!disabled']);self.connect_btn.state(['!disabled'])
            result=None
            try:
                result=job.result()
                if kind in ('connect','reconnect'):self.install(result)
                else:self.status.set(f"SSH 접속 확인됨 · {result['target']} · {result['system']}\n로봇팔 제어는 상단 전체 연결로 별도 요청합니다.")
            except Exception as exc:
                if kind in ('connect','reconnect'):
                    if result:result.close(disconnect=False)
                    self.auto_enabled=False;self.recovery_pending=True;self.restore_devices=False
                    self.retry_count=0;self.retry_at=0.;a.startup_devices_pending=False
                    self.status.set(f'제어 연결 실패 · 자동 재시도 없음\n{exc}\n필요할 때 전체 연결을 다시 누르세요.')
                    a.device_target.set('장치: Pi · 조회 전용');a.notice('제어 연결 실패 · 조회 전용 유지 · '+str(exc),True)
                else:self.status.set(str(exc));a.notice(str(exc),True)
        if self.job or not a.remote_mode or getattr(a,'work_observation_mode',False):return
        if a.remote and a.remote.error and not self.recovery_pending:self.schedule_recovery()
