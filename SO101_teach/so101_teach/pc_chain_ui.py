"""Separate PC chain controls and read-only saved-frame comparison status."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from .pc_compute_broker import configuration


def summary(config):
    path=Path(config.get('report',''))
    if not path.is_file():return '저장 영상 비교 진행 중 · 판정 일치율과 시간을 확인한 뒤 실물 비교를 진행합니다.'
    try:
        report=json.loads(path.read_text());totals=report['summary']
        return (f"저장 영상 {totals['frames']}장 · 판정 일치 {totals['matching_operations']}/{totals['operations']}건\n"
                f"처리 시간 중앙값: Pi {totals['pi_median_seconds']:.3f}초 → PC 협력 {totals['pc_median_seconds']:.3f}초\n"
                'Pi→PC 왕복 포함 · 실제 조립·적재 총시간은 실물 비교에서 측정')
    except (OSError,ValueError,KeyError,TypeError):return '비교 결과를 읽지 못했습니다. 저장된 검증 보고서를 확인하세요.'


def show(app):
    previous=getattr(app,'pc_chain_window',None)
    if previous is not None and previous.winfo_exists():previous.lift();return previous
    window=tk.Toplevel(app.root);app.pc_chain_window=window;window.title('PC 협력 · 조립·적재');window.geometry('660x470');window.minsize(620,450)
    frame=ttk.Frame(window,padding=22);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='PC 협력 A/B 조립·적재',style='Section.TLabel').pack(anchor='w')
    ttk.Label(frame,text='PC: GPU 인식·CAD/OpenCV 판정   ·   Pi: 카메라·팔·리니어·작업 순서',wraplength=610).pack(anchor='w',pady=(8,4))
    ttk.Label(frame,text='Pi에 저장된 A/B 레시피와 속도·판정 기준을 사용합니다.\n현재 편집 중인 단일 에피소드를 실행하는 버튼과 구분됩니다.',wraplength=610).pack(anchor='w',pady=(0,12))
    try:config=configuration(require_enabled=False);error=None
    except Exception as exc:config={};error=str(exc)
    ttk.Label(frame,text=error or summary(config),wraplength=610,justify='left').pack(anchor='w',pady=8)
    enabled=config.get('physical_execution_enabled') is True and not app.camera_only
    status='실물 비교 활성화됨 · 두 팔 연결과 최신 정지 상태가 필요합니다.' if enabled else '저장 영상 검증 단계 · 실물 실행 비활성'
    ttk.Label(frame,text=status,wraplength=610).pack(anchor='w',pady=8)
    controls=ttk.Frame(frame);controls.pack(fill='x',pady=(8,0))
    def start(command):
        from .pc_chain_execution import PcChainExecution
        app.guard(lambda:PcChainExecution(app,command))
    window.pc_buttons=[]
    for letter in ('A','B'):
        button=ttk.Button(controls,text=f'PC · {letter} 조립 → 적재',command=lambda letter=letter:start('PC_BUILD_LOAD_'+letter))
        button.pack(side='left',fill='x',expand=True,padx=(0,8));window.pc_buttons.append(button)
        if not enabled:button.state(['disabled'])
    window.close_button=ttk.Button(frame,text='닫기',command=window.destroy);window.close_button.pack(anchor='e',pady=(14,0))
    return window
