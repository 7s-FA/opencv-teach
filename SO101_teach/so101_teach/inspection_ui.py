"""Small in-window model viewer. Shares one lazy renderer between settings tabs."""
import io,tkinter as tk
from tkinter import ttk
from PIL import Image,ImageTk
from .preview import pan_view

class ModelView(ttk.Frame):
    def __init__(self,parent,kind):
        super().__init__(parent,padding=(12,12),style='Card.TFrame',width=430)
        self.kind=kind;self.spec=None;self.version=0;self.view='whole';self.actual_view=None;self.request=None;self.image=None;self.meta=None;self.photo=None;self.drag=None
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        ttk.Label(self,text='집게 모델과 기준점' if kind=='tcp' else '선택한 지그 모델',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.caption=tk.StringVar(value='모델을 준비합니다.');ttk.Label(self,textvariable=self.caption,wraplength=400,style='Small.TLabel').grid(row=1,column=0,sticky='w',pady=8)
        self.canvas=tk.Canvas(self,bg='#e6ecf2',highlightthickness=0,width=400,height=300);self.canvas.grid(row=2,column=0,sticky='nsew')
        self.canvas.bind('<Configure>',lambda e:self.paint())
        bar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=3,column=0,sticky='ew',pady=8)
        for text,preset in [('집게 전체' if kind=='tcp' else '입체로 보기','whole'),('끝부분 확대' if kind=='tcp' else '위에서 보기','detail')]:
            ttk.Button(bar,text=text,command=lambda p=preset:self.preset(p)).pack(side='left',padx=(0,5))
        ttk.Label(self,text='왼쪽: 회전 · 오른쪽: 이동 · 휠: 확대/축소',style='Small.TLabel').grid(row=4,column=0,sticky='w')
        self.details=tk.StringVar(value='');ttk.Label(self,textvariable=self.details,wraplength=400,style='Muted.TLabel').grid(row=5,column=0,sticky='w',pady=(10,0))
        for button in (1,2,3):
            self.canvas.bind(f'<ButtonPress-{button}>',lambda e:self.begin_drag(e))
        self.canvas.bind('<B1-Motion>',self.rotate);self.canvas.bind('<B2-Motion>',self.pan);self.canvas.bind('<B3-Motion>',self.pan)
        self.canvas.bind('<Button-4>',lambda e:self.zoom(.88));self.canvas.bind('<Button-5>',lambda e:self.zoom(1.12))
    def set_spec(self,spec,error=None):
        if spec==self.spec and not error:return
        if self.kind=='jig':self.view='whole';self.actual_view=None
        self.spec=spec;self.version+=1;self.request=None;self.image=None;self.meta=None
        self.caption.set(error or '모델 읽는 중…');self.details.set('');self.paint()
    def preset(self,p):self.view=p;self.request=None
    def begin_drag(self,e):
        if self.actual_view:self.drag=(e.x,e.y,tuple(self.actual_view))
    def rotate(self,e):
        if self.drag:
            x,y,v=self.drag;self.view=(v[0]-(e.x-x)*.5,max(-89,min(89,v[1]-(e.y-y)*.3)),*v[2:]);self.request=None
        return 'break'
    def pan(self,e):
        if self.drag:
            x,y,v=self.drag;self.view=pan_view(v,e.x-x,e.y-y,min(self.canvas.winfo_height(),self.canvas.winfo_width()*.75));self.request=None
        return 'break'
    def zoom(self,factor):
        if self.actual_view:self.view=(*self.actual_view[:2],max(.008,min(20.,self.actual_view[2]*factor)),*self.actual_view[3:]);self.request=None
        return 'break'
    def accept(self,item):
        if item[0]=='inspection_error':
            self.image=None;self.meta=None;self.caption.set('모델을 표시할 수 없습니다.');self.details.set(item[2]);self.paint();return
        self.image=Image.open(io.BytesIO(item[2])).copy();self.meta=item[5];self.actual_view=self.meta['view']
        if self.kind=='tcp':
            x,y,z=self.meta['xyz_mm'];distance=self.meta['offset_mm']
            self.caption.set('A·B 같은 위치: 모델 원점 = 고정 집게 중앙' if self.meta.get('tcp_label')=='고정 집게 중앙' else 'A 파랑: 모델 기준점   B 주황: 사용 TCP')
            self.details.set(f'A → B: X {x:.3f} / Y {y:.3f} / Z {z:.3f} mm\n두 점의 직선거리 {distance:.3f} mm\n선택 기준: {self.meta.get("tcp_label","직접 설정")}\n이 XYZ는 집게와 함께 회전하는 축 기준입니다.\n설명용 집게 자세이며 실물은 움직이지 않습니다.')
        else:
            x,y,z=self.meta['size_mm'];rim=self.meta['rim_mm']
            self.caption.set('선택한 STL 형상 · 실물 감지 위치가 아닙니다.' if self.meta['has_stl'] else 'STL 없음 · 입력 치수로 만든 외곽선입니다.')
            if self.meta.get('main_scene_assembly'):self.caption.set('메인 시뮬과 같은 완성 조립본 · 티칭 방향 기준')
            self.details.set(f'가로 X {x:.2f} · 세로 Y {y:.2f} · 전체 높이 {z:.2f} mm\n'+(f'청록색 테두리: 검출면 · 바닥에서 {rim:.2f} mm\n' if rim is not None else '이 STL에서 넓은 검출면을 찾지 못했습니다.\n')+'빨강 X · 초록 Y · 중심은 외곽의 가운데입니다.\n실제 도킹 위치는 카메라 감지 후 티칭 3D에 표시합니다.')
        self.paint()
    def paint(self):
        c=self.canvas
        for item in c.find_all():
            if 'model-image' not in c.gettags(item):c.delete(item)
        if self.image is None:
            c.delete('model-image')
            c.create_text(18,25,text=self.caption.get(),anchor='nw',fill='#526477',width=max(80,c.winfo_width()-36));return
        w,h=max(1,c.winfo_width()),max(1,c.winfo_height());im=self.image.copy();im.thumbnail((w,h),Image.Resampling.LANCZOS);iw,ih=im.size;ox,oy=(w-iw)/2,(h-ih)/2
        photo=ImageTk.PhotoImage(im,master=c);existing=c.find_withtag('model-image')
        if existing:c.itemconfigure(existing[0],image=photo);c.coords(existing[0],ox,oy)
        else:c.create_image(ox,oy,image=photo,anchor='nw',tags='model-image')
        self.photo=photo;c.tag_lower('model-image')
        pts={n:(ox+uv[0]*iw,oy+uv[1]*ih) for n,uv in self.meta['markers'].items()}
        origin=pts.get('origin' if self.kind=='tcp' else 'center')
        if origin:
            for n,color in [('X','#be2734'),('Y','#237a37'),('Z','#365ab5')]:
                if n in pts:
                    x,y=pts[n];c.create_line(*origin,x,y,fill=color,width=2,arrow='last');c.create_text(x+9,y,text=n,fill=color,font=('Noto Sans CJK KR',11,'bold'))
        if self.kind=='tcp':
            if 'origin' in pts and 'tcp' in pts:c.create_line(*pts['origin'],*pts['tcp'],fill='#a74408',dash=(3,2),width=2)
            for n,tag,label,color,tx,ty in [('origin','A','모델 기준점','#165bb1',14,18),('tcp','B','사용 TCP','#a74408',max(170,w-150),50)]:
                if n not in pts:continue
                x,y=pts[n]
                if not 0<=x<=w or not 0<=y<=h:continue
                c.create_line(tx+55,ty+16,x,y,fill=color,width=1.5)
                c.create_oval(x-4,y-4,x+4,y+4,fill=color,outline='white',width=1.5)
                text=c.create_text(tx+6,ty+3,text=f'{tag}  {label}',anchor='nw',fill=color,font=('Noto Sans CJK KR',11,'bold'));bounds=c.bbox(text)
                bg=c.create_rectangle(bounds[0]-4,bounds[1]-2,bounds[2]+4,bounds[3]+2,fill='white',outline=color);c.tag_lower(bg,text)
        elif origin:c.create_oval(origin[0]-3,origin[1]-3,origin[0]+3,origin[1]+3,fill='#11697a',outline='white')
