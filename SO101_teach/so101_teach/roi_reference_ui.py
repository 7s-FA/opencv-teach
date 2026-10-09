"""Read-only CAD inspection reference; never connects devices or enables inspection."""
import json
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk
from .domain import ROOT

PARTS = {'housing': '하단', 'insert': '중단', 'cap': '상단'}
STAGES = {'empty': '빈 팔레트', 'housing_seated': '하단 안착',
          'insert_added': '중단 추가', 'cap_added': '상단 결합'}
VIEWS = ('전체 배치 · 운반 / 리니어 / 완성품', 'A 조립 순서', 'B 조립 순서')


def numbers(values):
    return ' / '.join(f'{value:.2f}'.rstrip('0').rstrip('.') for value in values)


def pose_text(label, roi):
    yaw = roi.get('expected_yaw_deg')
    heading = f'{yaw:g}°' if yaw is not None else '구형 · 각도 판정 제외'
    return (f'{label}  |  중심 XY {numbers(roi["search_roi_center_xy_mm"])} mm  |  방향 {heading}\n'
            f'    탐색 크기 {numbers(roi["search_roi_size_mm"])} mm · 높이 Z {numbers(roi["part_z_range_mm"])} mm')


def reference_text(data, product=None):
    if product:
        profile = data['assembly_profiles'][product]
        lines = [f'{product} 제품 · 조립 팔레트 중심 / 바닥 기준',
                 '빈 팔레트 → 하단 안착 → 중단 추가 → 상단 결합',
                 'A는 CAD로 산출한 후보 기준입니다.' if product == 'A' else 'B는 강화학습에 사용한 CAD와 일치하는 기준입니다.']
        for stage in profile['stages']:
            visible = ', '.join(PARTS[p] for p in stage['expected_visible_parts']) or '부품 없음'
            hidden = ', '.join(PARTS[p] for p in stage['occluded_parts_requiring_history']) or '없음'
            previous = stage['requires_previous_pass']
            lines.extend(['', STAGES[stage['id']], f'영상에서 확인: {visible} · 가려져 이전 PASS가 필요한 부품: {hidden}',
                          f'선행 단계: {STAGES[previous] + " PASS 필요" if previous else "시작 단계"}'])
            if stage['new_part']:
                part = stage['new_part']
                lines.append(pose_text('추가 부품 ' + PARTS[part], profile['part_poses'][part]))
        lines.extend(['', '이전 PASS는 같은 실행·제품·팔레트·제품 종류에 속해야 합니다.',
                      '단계 시작 이후의 안정된 새 영상이 필요합니다. 가림·흔들림은 판단 불가입니다.',
                      '위쪽 영상만으로 내부 안착 깊이와 결합 강도를 판정할 수 없습니다.'])
    else:
        stations = data['stations']
        lines = ['운반용 지그 · 판 중심 XY / 밑면 Z=0 기준',
                 '양의 각도는 위에서 보았을 때 반시계 방향입니다.']
        lines.extend(pose_text(f'{r["product"]} {PARTS[r["part"]]}', r) for r in stations['carrier']['rois'])
        lines.extend(['', '리니어 위 조립 팔레트 · 캐리지 중심 기준',
                      '팔레트 중심 XYZ: ' + ' ; '.join(numbers(p) + ' mm' for p in stations['linear_assembly']['fixture_centers_carriage_mm']),
                      '영상 검사에서는 두 팔레트를 모두 표시하며 수신한 조립·하차 위치 명령의 고정 좌표를 사용합니다.',
                      '', '완성품 팔레트 · 팔레트 중심 XY / 밑면 Z=0 기준'])
        lines.extend(pose_text(product + ' 완성품', roi) for product, roi in stations['finished_pallet']['rois'].items())
        lines.extend(['이전 조립 PASS를 유지하고, 그리퍼가 빠진 뒤 외곽과 위치를 확인합니다.',
                      '', '각도 해석: 사각 외곽만으로는 90° 회전을 구분할 수 없습니다.',
                      '운반 지그의 앞뒤 방향을 확정하고 감지 방향 보정은 한 번만 적용해야 합니다.'])
    return '\n\n'.join(lines)


class ROIReferencePanel(ttk.Frame):
    def __init__(self, parent, directory=None):
        super().__init__(parent, padding=12, style='Card.TFrame')
        self.directory = directory or ROOT / 'inspection'
        self.data = None
        self.image = self.photo = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(3, weight=2)
        self.status = tk.StringVar(value='CAD 원본 · 공정 합격 기준 미확정')
        label = ttk.Label(self, textvariable=self.status, style='Small.TLabel')
        label.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        label.bind('<Configure>', lambda e: label.configure(wraplength=e.width))
        bar = ttk.Frame(self, style='Card.TFrame');bar.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        self.choice = ttk.Combobox(bar, values=VIEWS, state='readonly', width=34)
        self.choice.current(0);self.choice.pack(side='left')
        self.choice.bind('<<ComboboxSelected>>', lambda e: self.select())
        self.zoom = tk.StringVar(value='화면 맞춤')
        zoom = ttk.Combobox(bar, values=('화면 맞춤', '100%', '150%'), textvariable=self.zoom, state='readonly', width=10)
        zoom.pack(side='left', padx=8);zoom.bind('<<ComboboxSelected>>', lambda e: self.paint())
        ttk.Label(bar, text='확대 후 스크롤로 이동', style='Small.TLabel').pack(side='left')
        area = ttk.Frame(self);area.grid(row=2, column=0, sticky='nsew');area.columnconfigure(0, weight=1);area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, bg='#eef2f6', highlightthickness=0, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        for orient, row, col, sticky in [('horizontal',1,0,'ew'), ('vertical',0,1,'ns')]:
            scroll = ttk.Scrollbar(area, orient=orient, command=self.canvas.xview if orient=='horizontal' else self.canvas.yview)
            scroll.grid(row=row, column=col, sticky=sticky)
            self.canvas.configure(**{'xscrollcommand' if orient=='horizontal' else 'yscrollcommand': scroll.set})
        self.canvas.bind('<Configure>', lambda e: self.paint())
        detail = ttk.Frame(self);detail.grid(row=3, column=0, sticky='nsew', pady=(10, 0));detail.rowconfigure(0, weight=1);detail.columnconfigure(0, weight=1)
        self.details = tk.Text(detail, wrap='word', height=7, width=1, font=('Noto Sans CJK KR',11), bg='white', fg='#142333', relief='flat', padx=10, pady=8)
        self.details.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(detail, command=self.details.yview);scroll.grid(row=0, column=1, sticky='ns');self.details.configure(yscrollcommand=scroll.set)
        try:
            self.data = json.loads((self.directory / 'roi_reference.json').read_text(encoding='utf-8'))
            self.select()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.set('ROI 기준을 읽지 못했습니다. inspection 폴더를 확인하세요.')
            self.set_text(str(exc));self.choice.configure(state='disabled')

    def set_text(self, text):
        self.details.configure(state='normal');self.details.delete('1.0','end');self.details.insert('1.0',text);self.details.configure(state='disabled');self.details.yview_moveto(0)

    def select(self):
        index = self.choice.current()
        product = (None, 'A', 'B')[index]
        self.set_text(reference_text(self.data, product))
        self.status.set('CAD 원본 · 공정 합격 기준 미확정' + f' · 탐색 여유 {self.data["search_margin_mm"]:g} mm는 합격 공차가 아닙니다.')
        filename = f'assembly_stages_{product}.png' if product else 'roi_overview.png'
        try:
            with Image.open(self.directory / 'previews' / filename) as source:
                self.image = source.convert('RGB')
        except OSError:
            self.image = None
            self.status.set('기준 그림을 읽지 못했습니다. 아래 수치 기준을 확인하세요.')
        self.canvas.xview_moveto(0);self.canvas.yview_moveto(0);self.paint()

    def paint(self):
        self.canvas.delete('all')
        if self.image is None:
            self.canvas.create_text(20,20,anchor='nw',text='기준 그림 없음',fill='#526477');return
        w,h = max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())
        scale = min(w/self.image.width,h/self.image.height) if self.zoom.get()=='화면 맞춤' else float(self.zoom.get().strip('%'))/100
        im = self.image.resize((max(1,round(self.image.width*scale)), max(1,round(self.image.height*scale))),Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(im, master=self.canvas)
        self.canvas.create_image(max(0,(w-im.width)//2),max(0,(h-im.height)//2),image=self.photo,anchor='nw')
        self.canvas.configure(scrollregion=(0,0,max(w,im.width),max(h,im.height)))
