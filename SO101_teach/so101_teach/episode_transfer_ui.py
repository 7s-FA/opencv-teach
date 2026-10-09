"""Episode library delivery inside episode adjustment; never starts a robot."""
import tkinter as tk
from tkinter import ttk
from . import episode_transfer, pi_connection
from .arm_workspace import arm_id, ARM_NAMES


class EpisodeTransferPanel:
    def __init__(self, settings):
        self.settings=settings;self.app=settings.app;self.job=None;self.job_kind=None
        self.target=None;self.config=None;self.sent_request=None
        self.tabs=self.app.episode_adjust_panel.tabs
        self.page=page=ttk.Frame(self.tabs,style='Card.TFrame',padding=12)
        self.tabs.add(page,text='에피소드 전송');page.columnconfigure(0,weight=1)
        ttk.Label(page,text='선택 에피소드를 Pi에 저장',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.source=tk.StringVar()
        ttk.Label(page,textvariable=self.source,justify='left').grid(row=1,column=0,sticky='ew',pady=(12,8))
        ttk.Label(page,text='왼쪽에서 전송할 에피소드를 선택하세요.\n같은 에피소드는 갱신하고, 새 에피소드는 목록에 추가합니다.',style='Small.TLabel',justify='left').grid(row=2,column=0,sticky='ew',pady=6)
        self.destination=tk.StringVar(value='설정·안내의 Pi 연결 주소를 사용합니다.')
        ttk.Label(page,textvariable=self.destination,style='Small.TLabel',justify='left').grid(row=3,column=0,sticky='ew',pady=6)
        actions=ttk.Frame(page,style='Card.TFrame');actions.grid(row=4,column=0,sticky='w',pady=8)
        self.query_button=self.app.button(actions,'Pi 저장 상태 조회',self.query)
        self.send_button=self.app.button(actions,'선택 에피소드 전송',self.send,True)
        self.query_button.pack(side='left',padx=(0,8));self.send_button.pack(side='left');self.send_button.state(['disabled'])
        self.apply_settings=tk.BooleanVar(value=False);self.source_id=None
        self.settings_check=ttk.Checkbutton(page,text='저장한 실행 설정도 함께 적용',variable=self.apply_settings)
        self.settings_check.grid(row=5,column=0,sticky='w')
        self.current=tk.StringVar(value='조회하면 선택 에피소드의 Pi 저장 상태를 표시합니다.')
        self.current_label=ttk.Label(page,textvariable=self.current,justify='left');self.current_label.grid(row=6,column=0,sticky='ew',pady=8)
        ttk.Label(page,text='저장된 스텝·완료 알림·완제품 종류·안착 검사를 전송합니다.\n기존 A/B 실행 연결은 유지합니다. 실행 설정은 위 항목을 선택해야 적용합니다.\n속도·측정값은 이 팔의 공통 설정, 제한시간은 연결된 실행에 적용됩니다.\n전송 전에 Pi의 이전 파일을 백업하며, 전송만으로 로봇이 움직이지 않습니다.',style='Small.TLabel',justify='left').grid(row=7,column=0,sticky='ew',pady=8)
        self.status=tk.StringVar(value='Pi 저장 상태를 조회한 뒤 전송할 수 있습니다.')
        self.status_label=ttk.Label(page,textvariable=self.status,style='Muted.TLabel',justify='left');self.status_label.grid(row=8,column=0,sticky='ew',pady=8)
        def fit_text(event):
            for widget in page.winfo_children():
                if isinstance(widget,ttk.Label):widget.configure(wraplength=max(120,event.width-24))
        page.bind('<Configure>',fit_text);self.tabs.bind('<<NotebookTabChanged>>',self.tab_changed,add='+')
        self.refresh_source()

    def refresh_source(self):
        a=self.app;episode=a.episode
        if self.source_id!=episode['id']:self.apply_settings.set(False);self.source_id=episode['id']
        self.settings_check.state(['!disabled'] if episode.get('pi_execution_settings') else ['disabled'])
        name=a.episode_name.get().strip() or '이름 없음'
        self.source.set(f'{ARM_NAMES[arm_id(a.profile)]} · {name}\n{len(episode["steps"])}개 스텝 · 완제품 {episode.get("product_type","미지정")} · 시작 전 검사 {len(episode.get("startup_inspections",[]))}개 · 스텝 검사 {sum("inspection" in s for s in episode["steps"])}개')
        if self.target:
            saved=self.target.get('episodes',{}).get(episode['id'])
            linked=[slot for slot,value in self.target['recipes'].items() if value.get('episode_id')==episode['id']]
            self.current.set((f'Pi 저장본: {saved["name"]} · {saved["step_count"]}개 스텝\n전송하면 이 에피소드를 갱신합니다.' if saved else 'Pi에 없는 에피소드입니다. 전송하면 새로 저장합니다.')+'\n'+('기존 실행 연결: '+', '.join(linked)+' · 유지' if linked else '실행 연결 없음 · 저장만 진행'))

    def tab_changed(self,event=None):
        if self.tabs.select()==str(self.page):
            if self.target and self.target['arm']!=arm_id(self.app.profile):
                self.target=None;self.send_button.state(['disabled'])
            self.refresh_source()

    def open(self):
        self.app.open_episode_adjust();self.tabs.select(self.page);self.refresh_source()
        if not self.job:self.query()

    def start_job(self,kind,config,request):
        self.job_kind=kind;self.query_button.state(['disabled']);self.send_button.state(['disabled'])
        self.job=self.settings.pool.submit(episode_transfer.remote_request,config,request)

    def query(self):
        if self.job:return
        self.target=None;self.send_button.state(['disabled']);self.refresh_source()
        self.config=pi_connection.validate(pi_connection.load(self.app.data_dir),True)
        self.destination.set(f'Pi {self.config["user"]}@{self.config["host"]} · {self.config["app_dir"]}')
        self.status.set('Pi 에피소드 목록 조회 중…')
        self.start_job('inspect',self.config,{'operation':'inspect','arm':arm_id(self.app.profile)})

    def send(self):
        if self.job:return
        if not self.target:raise ValueError('Pi 저장 상태를 먼저 조회하세요.')
        if self.app.episode_adjust_panel.dirty:raise ValueError('변경한 에피소드 설정·안착 검사를 저장한 뒤 전송하세요.')
        self.settings.require_calculation_idle()
        if self.settings.pi_panel.job:raise ValueError('Pi 연결 처리가 끝난 뒤 전송하세요.')
        config=pi_connection.validate(pi_connection.load(self.app.data_dir),True)
        if config!=self.config:
            self.target=None;self.send_button.state(['disabled']);raise ValueError('Pi 주소 설정이 바뀌었습니다. 다시 조회하세요.')
        request=episode_transfer.episode_delivery_request(self.app,self.target,apply_settings=self.apply_settings.get())
        self.sent_request=request;self.refresh_source()
        self.status.set(f'{request["episode"]["name"]} 전송 중…')
        self.start_job('store_episode',config,request)

    def poll(self):
        if self.tabs.select()==str(self.page):self.refresh_source()
        if not self.job or not self.job.done():return
        job,kind=self.job,self.job_kind;self.job=None;self.query_button.state(['!disabled'])
        try:
            value=job.result()
            if kind=='inspect':
                if value['arm']!=arm_id(self.app.profile) or value['calibration_sha256']!=self.app.calibration.sha256:
                    raise ValueError('PC와 Pi의 로봇팔 또는 영점이 다릅니다. 같은 설정인지 확인한 뒤 다시 조회하세요.')
                if 'episodes' not in value:raise ValueError('Pi 에피소드 목록을 확인할 수 없습니다. 다시 조회하세요.')
                self.target=value;self.refresh_source();self.send_button.state(['!disabled'])
                self.status.set('조회 완료 · 선택 에피소드만 저장·갱신합니다.')
            else:
                self.target=None
                self.current.set(f'{value["name"]} · {value["step_count"]}개 스텝 저장 확인\n기존 실행 연결 유지 · '+('실행 설정 적용' if value.get('settings_applied') else '팔 공통 설정 유지'))
                self.status.set('전송 완료 · 이전 파일 백업: '+value['backup_directory'])
                self.app.notice('Pi 에피소드 전송 완료 · '+value['name'])
        except Exception as exc:
            self.target=None;self.status.set(str(exc));self.app.notice(str(exc),True)
