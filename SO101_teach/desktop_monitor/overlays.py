"""Display recorded geometry; never infer a pass or re-run image inspection."""
import math
import time
from copy import deepcopy
from .model import summary,run_events,parts,EXPECTED
from so101_teach.episode_inspection import matches_expected

COLORS={'adopted':'#48d6d3','pending':'#ffd36c','pass':'#76e5a6','error':'#ff958a',
        'observed':'#d1e8ff','back':'#142d38'}
TARGETS={'housing':'하단','insert':'중단','cap':'상단','1':'A 팔레트','2':'B 팔레트','pallet':'완성품 팔레트'}


def polygon(value):
    if not isinstance(value,(list,tuple)) or not 3<=len(value)<=4096:return None
    if not all(isinstance(p,(list,tuple)) and len(p)==2 and
               all(type(v) in (int,float) and math.isfinite(v) and abs(v)<100000 for v in p) for p in value):return None
    return value


def visual_for(snapshot,run):
    value=snapshot.get('visual') or {}
    if not run or any(value.get(k)!=run.get(k) for k in ('run_id','job_id','arm')):return {}
    size=value.get('source_size')
    if not isinstance(size,(list,tuple)) or len(size)!=2 or not all(type(v) is int and 0<v<=8192 for v in size):return {}
    return value


def inspection_marks(snapshot,run,*,evidence=False,now=None):
    visual=visual_for(snapshot,run)
    if not visual:return []
    info=summary(snapshot,run)
    if not evidence and (not info['live'] or snapshot.get('camera',{}).get('arm')!=run.get('arm')):return []
    now=time.monotonic() if now is None else now
    elapsed=max(0,now-snapshot.get('received',now))+snapshot.get('round_trip_s',0)
    states={row['id']:row for row in info['checks']}
    rows=visual.get('checks',{});marks=[]
    moving=max((r['at'] for r in run_events(snapshot,run) if parts(r)[0]=='LINEAR_STARTED'),default=0)
    for key,item in rows.items():
        if evidence and (key not in visual.get('frame_checks',[]) or item.get('captured_at')!=visual.get('captured_at')):continue
        age=item.get('age_s',float('inf'))+elapsed
        if not evidence and not 0<=age<3:continue
        state=states.get(key,{})
        if not evidence and (state.get('state') in (None,'대기','재검사')
                             or item.get('captured_wall_time',0)<state.get('_started_at',0)):continue
        check=item.get('check') or {};row=item.get('row') or {}
        if not evidence and check.get('station')=='linear' and moving>item.get('captured_wall_time',0):continue
        region=polygon(row.get('roi_display_polygon')) or polygon(row.get('roi_polygon'))
        contours=[p for name in ('observed_polygon','part_top_polygon') if (p:=polygon(row.get(name))) is not None] if evidence else []
        label=TARGETS.get(check.get('target'),check.get('target','검사'))
        if check.get('station')=='carrier':label=(item.get('product') or '')+' '+label
        progress=state.get('state','판정 대기')
        try:
            matches=bool(check.get('station') and check.get('target') and check.get('expected')
                         and matches_expected(row,check,item.get('product') or state.get('product')))
        except (KeyError,TypeError,ValueError):matches=False
        if not evidence and progress=='통과' and not matches:
            continue  # The accepted observation may still be in the writer queue.
        # Frame classification is an observation; only recorded execution events
        # can say that the temporal acceptance criteria passed.
        tone='error' if progress=='실패' else 'pass' if progress=='통과' and matches else 'pending'
        if row.get('quality') in ('abnormal','outside') and not matches:tone='error'
        count=state.get('reason','')
        if progress=='검사 중' and count.replace('/','').isdigit():progress+=' '+count
        title=label+' · '+progress
        detail=row.get('label') or row.get('state') or item.get('reason') or '관측 대기'
        reason=row.get('reason') or item.get('reason') or ''
        if evidence:title=label+' · '+detail
        marks.append({'id':key,'title':title,'region':region,'contours':contours,'tone':tone,
            'reason':reason,'observed':detail,'expected':EXPECTED.get(check.get('expected'),check.get('expected','')),
            'age_s':age,'state':progress})
    return marks


def crop_evidence(image,source_size,marks):
    """Crop a displayed copy, keeping every contour in the original coordinate frame."""
    sw,sh=source_size
    points=[p for mark in marks for poly in ([mark['region']] if mark['region'] else [])+mark['contours'] for p in poly]
    if not points:return image,source_size,marks
    left=max(0,min(p[0] for p in points)-45);right=min(sw,max(p[0] for p in points)+45)
    top=max(0,min(p[1] for p in points)-45);bottom=min(sh,max(p[1] for p in points)+45)
    if right<=left or bottom<=top:return image,source_size,marks
    width=min(sw,max(480,right-left));height=min(sh,max(270,bottom-top))
    x=max(0,min(sw-width,(left+right-width)/2));y=max(0,min(sh-height,(top+bottom-height)/2))
    box=(round(x*image.width/sw),round(y*image.height/sh),round((x+width)*image.width/sw),round((y+height)*image.height/sh))
    # Rounding the JPEG crop changes the exact origin; use those rounded bounds
    # for the overlay transform too, rather than shifting the evidence by a pixel.
    x,y=box[0]*sw/image.width,box[1]*sh/image.height
    size=((box[2]-box[0])*sw/image.width,(box[3]-box[1])*sh/image.height)
    shifted=deepcopy(marks)
    for mark in shifted:
        if mark['region']:mark['region']=[[p[0]-x,p[1]-y] for p in mark['region']]
        mark['contours']=[[[p[0]-x,p[1]-y] for p in poly] for poly in mark['contours']]
    return image.crop(box),size,shifted


def paint_annotations(canvas,rect,source_size,adoptions,checks,*,heading='',font=None):
    """Use display-pixel text so Korean labels stay readable after letterboxing."""
    from so101_teach.label_layout import LabelLayout
    x,y,width,height=rect;sw,sh=source_size
    if font is None:
        from tkinter import font as tkfont
        font=tkfont.Font(root=canvas,family='Noto Sans CJK KR',size=10,weight='bold')
    def points(poly):return [(max(0,min(width,p[0]*width/sw)),max(0,min(height,p[1]*height/sh))) for p in poly]
    groups=[]
    for item in adoptions:
        if not item.get('image_size') or list(item['image_size'])==list(source_size):groups.append(points(item['quad']))
    for item in checks:
        region=points(item['region']) if item['region'] else None
        if region:groups.append(region)
    layout=LabelLayout((height,width),groups)
    if heading:layout.protect([[0,0],[width,0],[width,31],[0,31]])
    def line(poly,color,dashed=False):
        if not poly:return
        coords=[coordinate for px,py in [*poly,poly[0]] for coordinate in (x+px,y+py)]
        canvas.create_line(*coords,fill='#142d38',width=5,tags='annotation')
        canvas.create_line(*coords,fill=color,width=2,dash=(6,4) if dashed else (),tags='annotation')
    def badge(text,point,color):
        limit=max(40,min(280,width-16));shown=text
        while len(shown)>2 and font.measure(shown)>limit-14:shown=shown[:-2]+'…'
        bw=font.measure(shown)+14;bh=25
        position=layout.place(bw,bh,point)
        if position is None:return
        bx,by=position
        if abs(bx-point[0])+abs(by-point[1])>35:
            canvas.create_line(x+point[0],y+point[1],x+bx+bw/2,y+by+bh/2,fill=color,width=1,tags='annotation')
        canvas.create_rectangle(x+bx,y+by,x+bx+bw,y+by+bh,fill=COLORS['back'],outline=color,tags='annotation')
        canvas.create_text(x+bx+7,y+by+bh/2,text=shown,anchor='w',fill=color,font=font,tags='annotation')
    for item in adoptions:
        if item.get('image_size') and list(item['image_size'])!=list(source_size):continue
        poly=points(item['quad']);line(poly,COLORS['adopted'])
        center=item.get('center_px')
        if not isinstance(center,(list,tuple)) or len(center)!=2 or not all(type(v) in (int,float) and math.isfinite(v) for v in center):center=None
        if not center:center=[sum(p[0] for p in item['quad'])/len(poly),sum(p[1] for p in item['quad'])/len(poly)]
        cx,cy=center[0]*width/sw,center[1]*height/sh
        if 0<=cx<=width and 0<=cy<=height:
            canvas.create_line(x+cx-7,y+cy,x+cx+7,y+cy,fill=COLORS['adopted'],width=2,tags='annotation')
            canvas.create_line(x+cx,y+cy-7,x+cx,y+cy+7,fill=COLORS['adopted'],width=2,tags='annotation')
        badge(item['name']+' · 채택 '+f"{item['yaw_deg']:.1f}°",(min(p[0] for p in poly),min(p[1] for p in poly)),COLORS['adopted'])
    for index,item in enumerate(checks):
        color=COLORS[item['tone']];poly=points(item['region']) if item['region'] else None
        line(poly,color,dashed=True)
        for contour in item['contours']:line(points(contour),COLORS['observed'])
        point=(max(p[0] for p in poly),max(p[1] for p in poly)) if poly else (8,45+index*32)
        badge(item['title'],point,color)
    if heading:
        canvas.create_rectangle(x,y,x+min(width,font.measure(heading)+20),y+29,fill=COLORS['back'],outline='',tags='annotation')
        canvas.create_text(x+10,y+14,text=heading,anchor='w',fill='#ffffff',font=font,tags='annotation')
