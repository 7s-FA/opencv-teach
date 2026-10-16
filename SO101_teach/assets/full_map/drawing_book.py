"""Readable A3 vector drawing set generated from the approved layout data."""
from pathlib import Path
import json,math
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1];OUT=ROOT/'output/pdf';OUT.mkdir(parents=True,exist_ok=True)
D=json.loads((HERE/'layout.json').read_text())
M=json.loads((HERE/'isaac/scene-manifest.json').read_text())
FONT='/usr/share/fonts/truetype/nanum/'
pdfmetrics.registerFont(TTFont('NG',FONT+'NanumGothic.ttf'));pdfmetrics.registerFont(TTFont('NGB',FONT+'NanumGothicBold.ttf'))
C=canvas.Canvas(str(OUT/'so101-workcell-layout.pdf'),pagesize=(420*mm,297*mm))
C.setTitle('SO-101 작업 셀 배치도 · 전체 맵 / 자재창고 / 바닥 표식')
C.setAuthor('SO-101 Workcell')
INK='#203544';MUTED='#61727C';BLUE='#1475A5';ORANGE='#AD6425';PURPLE='#7856A6';GRID='#DDE3E7'


def color(h):return tuple(int(h[i:i+2],16)/255 for i in (1,3,5))
def stroke(h,w=.25):C.setStrokeColorRGB(*color(h));C.setLineWidth(w)
def fill(h):C.setFillColorRGB(*color(h))
def text(x,y,s,size=9,bold=False,col=INK,align='left'):
    fill(col);C.setFont('NGB' if bold else 'NG',size/mm)
    getattr(C,{'left':'drawString','center':'drawCentredString','right':'drawRightString'}[align])(x,y,s)
def lines(x,y,rows,size=9,leading=5,col=INK):
    for s in rows:text(x,y,s,size,col=col);y-=leading
    return y

def line(a,b,col=INK,w=.25,dash=None):
    stroke(col,w);C.setDash(dash or []);C.line(*a,*b);C.setDash([])
def rect(x,y,w,h,fc=None,ec=INK,lw=.25):
    if fc:fill(fc)
    stroke(ec,lw);C.rect(x,y,w,h,fill=bool(fc),stroke=1)
def path(points,col=INK,w=.25,close=False,fc=None):
    p=C.beginPath();p.moveTo(*points[0])
    for q in points[1:]:p.lineTo(*q)
    if close:p.close()
    stroke(col,w)
    if fc:fill(fc)
    C.drawPath(p,stroke=1,fill=bool(fc))
def arrow(p,angle,col=BLUE):
    length=1.8;spread=.55
    a=(p[0]+length*math.cos(angle)+spread*math.sin(angle),p[1]+length*math.sin(angle)-spread*math.cos(angle))
    b=(p[0]+length*math.cos(angle)-spread*math.sin(angle),p[1]+length*math.sin(angle)+spread*math.cos(angle))
    path([a,p,b],col,.2)
def dim(a,b,label,offset=6,col=BLUE,sz=8):
    dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy);ux,uy=dx/length,dy/length;nx,ny=-uy,ux
    aa=(a[0]+nx*offset,a[1]+ny*offset);bb=(b[0]+nx*offset,b[1]+ny*offset)
    line(a,(aa[0]+nx*1.5,aa[1]+ny*1.5),col,.12);line(b,(bb[0]+nx*1.5,bb[1]+ny*1.5),col,.12)
    line(aa,bb,col,.18);ang=math.atan2(dy,dx);arrow(aa,ang,col);arrow(bb,ang+math.pi,col)
    C.saveState();C.translate((aa[0]+bb[0])/2,(aa[1]+bb[1])/2);C.rotate(math.degrees(ang))
    width=pdfmetrics.stringWidth(label,'NG',sz/mm);fill('#FFFFFF');C.rect(-width/2-1,-1,width+2,4.5,fill=1,stroke=0)
    text(0,.4,label,sz,col=col,align='center');C.restoreState()

def header(page,title,subtitle):
    C.saveState();C.scale(mm,mm)
    text(18,278,'SO-101 작업 셀',11,True,BLUE)
    text(18,264,title,23,True)
    text(402,278,f'FM-0{page}',11,True,align='right')
    text(402,271,'REV 1.0 · 2026-10-12',8,col=MUTED,align='right')
    text(18,255,subtitle,9,col=MUTED)
    line((18,249),(402,249),GRID,.3)

def footer(page):
    line((18,19),(402,19),GRID,.3)
    text(18,12,'배치 검토용  |  실측 치수와 사진 추정치를 구분하여 사용',8,col=MUTED)
    text(402,12,f'A3 가로 · 단위 mm · {page} / 3',8,col=MUTED,align='right')
    C.restoreState();C.showPage()

def map_frame(x,y,s,objects=True):
    P=lambda xx,yy:(x+xx*s,y+yy*s)
    rect(x,y,3600*s,1800*s,'#C4CBCF',INK,.65)
    for xx in range(300,3600,300):line(P(xx,0),P(xx,1800),'#E7EBED',.13)
    for yy in range(300,1800,300):line(P(0,yy),P(3600,yy),'#E7EBED',.13)
    for col,key in [('#FFFFFF','white'),('#141B20','black')]:
        for row in D['floor_tape'][key]:path([P(a*1000,b*1000) for a,b in row['xy']],col,20*s)
    for xx,yy,ww,hh in [(1500,0,600,D['assembly_size_mm'][1]),(3150,910,450,450)]:
        rect(*P(xx,yy),ww*s,hh*s,'#FFFFFF',INK,.4)
    for group,spec in D['jigs'].items():
        u,v=spec['near_corner_uv_mm'];du,dv=spec['extent_uv_mm']
        xx,yy=3600-u-du,1360-v-dv
        rect(*P(xx,yy),du*s,dv*s,'#EFE8F5' if group=='A' else '#F8E9D9',PURPLE if group=='A' else ORANGE,.2)
        text(*P(xx+du/2,yy+dv/2-14),group,8,True,PURPLE if group=='A' else ORANGE,'center')
    for row in D['parking_robots']:
        xx,yy=[v*1000 for v in row['center']]
        rect(*P(xx-110,yy-74),220*s,148*s,'#F2F4F5',MUTED,.25)
        text(*P(xx,yy-16),row['robot'],8,True,align='center')
    if objects:
        for label,prefix in [('R1','arm1_'),('R2',''),('R3','arm3_preview_')]:
            body=next(b for b in M['bodies'] if b['name']==prefix+'base_link')
            xx,yy=body['position'][:2];a,b=P(xx*1000,yy*1000)
            fill('#376B86');stroke('#FFFFFF',.3);C.circle(a,b,2.5,fill=1,stroke=1)
            text(a+3.5,b-1,label,8,True,BLUE)
        stage=next(b for b in M['bodies'] if b['name']=='linear_stage');a,b=P(*[v*1000 for v in stage['position'][:2]])
        line((a,b-9),(a,b+9),MUTED,1.5)
    for board in D['aruco']['boards']:
        xx,yy=[v*1000 for v in board['center'][:2]];a,b=P(xx,yy)
        if board['name'] in ('P1','P2'):rect(a-3,b-1,6,2,'#FFFFFF',BLUE,.3);text(a,b+2.6,board['name'],7,True,BLUE,'center')
        elif board['name']=='MAT':rect(a-1,b-3,2,6,'#FFFFFF',BLUE,.3)
        else:rect(a-3,b-1,6,2,'#FFFFFF',BLUE,.3)
    return P

# 01 - Overall placement, identical orientation to the accepted whole-map view.
header(1,'전체 배치도','조립대가 아래, 자재창고가 오른쪽인 위쪽 시점 · A3 100% 인쇄 시 1:12')
x,y,s=24,81,1/12
P=map_frame(x,y,s)
dim(P(0,0),P(3600,0),'3600',offset=-8,sz=10)
dim(P(0,0),P(0,1800),'1800',offset=9,sz=10)
dim(P(2100,0),P(3600,0),'1500 · O에서 조립대 가장자리',offset=-16,sz=8)
dim(P(3600,0),P(3600,910),'910',offset=-5,sz=8)
dim(P(2100,1800),P(3600,1800),'1500 · P1 중심',offset=5,sz=8)
dim(P(1740,1800),P(3600,1800),'1860 · P2 중심',offset=11,sz=8)
text(*P(1800,615),'조립대',11,True,align='center')
text(*P(1800,555),'600 × 514.3',8,col=MUTED,align='center')
text(*P(3375,1440),'자재창고',10,True,align='center')
text(*P(1370,1110),'REST',8,col=MUTED,align='right')
text(*P(3600,-48),'O',9,True,BLUE)
text(343,230,'장치 안내',11,True)
lines(343,221,['R1  자재창고 로봇팔','R2·R3  조립대 로봇팔','M1·M2  이동 로봇','MAT  자재창고 도킹','ASM  조립대 도킹','P1·P2  주차 위치','REST  대기 구역'],8.5,6)
text(343,167,'도면 읽는 법',11,True)
lines(343,157,['파랑 치수: 사용자 실측','회색 배치: 사진 추정','O: 맵 우하단 모서리','','벽 안쪽 3600 × 1800','양 작업대 상판 높이 195','로봇 자세는 표시용 예시'],8.5,5.5)
text(24,53,'확정한 마커 묶음 중심',10,True,BLUE)
lines(24,45,['주차 P1: 우측 모서리에서 1500  /  P2: 1860','조립대 ASM: 부착면 정면의 왼쪽 끝에서 205','자재창고 MAT: 부착면 정면의 왼쪽 끝에서 300'],9,6)
text(223,53,'실측과 추정 구분',10,True)
lines(223,45,['마커 거리는 4개 마커 전체 묶음의 중심 기준입니다.','바닥 표식·마커 높이·카메라 기울기는 사진 기반입니다.','세부 지그 배치는 FM-02, 바닥 표식은 FM-03을 확인하세요.'],9,6)
footer(1)

# 02 - Storage detail in the same orientation, removing the old rotated inset.
header(2,'자재창고 상세 배치','FM-01과 같은 위쪽 시점 · A3 100% 인쇄 시 1:2.5 · Q는 자재창고 우하단 모서리')
x,y,s=30,49,.4;P=lambda a,b:(x+a*s,y+b*s)
rect(x,y,450*s,450*s,'#FAFBFC',INK,.65)
line(P(450,0),P(450,450),INK,1)
text(215,226,'벽',9,True)
for group,spec in D['jigs'].items():
    u,v=spec['near_corner_uv_mm'];du,dv=spec['extent_uv_mm'];xx,yy=450-u-du,450-v-dv
    rect(*P(xx,yy),du*s,dv*s,'#F3EDF8' if group=='A' else '#FCF1E6',PURPLE if group=='A' else ORANGE,.5)
    angle=math.radians(180+spec['yaw_deg']);cx0,cy0=450-spec['center_uv_mm'][0],450-spec['center_uv_mm'][1]
    for index,cx in enumerate((-72,0,72)):
        for row,cy in enumerate((-36,36)):
            px=cx0+cx*math.cos(angle)-cy*math.sin(angle);py=cy0+cx*math.sin(angle)+cy*math.cos(angle)
            rect(*P(px-34,py-34),68*s,68*s,'#FFFFFF','#BAC5CC',.2)
            kind=('상단','중단','하단')[index]
            if kind=='하단':
                model=D['storage_lower_fixture_layout']['fixture_keys_by_row'][group][row][0].upper()
                label=model+' 하단';note='이전형 · 구멍' if model=='B' else '이전형 · 평판'
            else:label=group+' '+kind;note='이전형' if group=='B' and kind=='상단' else ''
            a,b=P(px,py);text(a,b+1,label,9,True,PURPLE if group=='A' else ORANGE,'center')
            if note:text(a,b-4,note,7,col=MUTED,align='center')
    text(*P(xx+du/2,yy+dv+8),group+' 구역',10,True,PURPLE if group=='A' else ORANGE,'center')
# Base outlines from the actual layout bounds.
for key,col,label in [('arm1',BLUE,'R1'),('camera',MUTED,'D435 기둥')]:
    lo,hi=D[key]['base_outline_storage_mm' if key=='arm1' else 'mast_outline_storage_mm']
    xx,yy=450-hi[0],450-hi[1];ww,hh=hi[0]-lo[0],hi[1]-lo[1]
    rect(*P(xx,yy),ww*s,hh*s,'#D4DEE4',col,.4);text(*P(xx+ww/2,yy+hh/2),label,8,True,col,'center')
dim(P(0,0),P(450,0),'450',offset=-11,sz=10)
dim(P(0,0),P(0,450),'450',offset=10,sz=10)
dim(P(220,0),P(450,0),'230 · A 운반판 가장자리',offset=-5,sz=8)
dim(P(370,0),P(450,0),'80',offset=5,sz=8)
dim(P(450,0),P(450,40),'40',offset=-6,sz=8)
dim(P(450,0),P(450,160),'160',offset=-16,sz=8)
text(*P(458,-12),'Q',10,True,BLUE)
text(242,230,'하단 지그 혼합 배치',13,True)
lines(242,220,['A 구역: A형 하단 1개 + B형 하단 1개','B 구역: A형 하단 1개 + B형 하단 1개','','하단 4개 모두 이전 버전입니다.','B 상단 2개도 10월 11일 교체 직전 버전입니다.','그림은 제품을 뺀 지그 자리만 표시합니다.'],10,7)
text(242,169,'설치 기준',12,True)
lines(242,159,['운반판: 각 220 × 148','상판 높이: 바닥에서 195','R1 베이스 외곽 - 앞쪽 판 가장자리: 80','카메라 기둥 외곽 - 앞쪽 판 가장자리: 30','두 거리는 사진에서 지정한 외곽 기준입니다.'],9,6.5)
text(242,117,'카메라 구성',12,True)
lines(242,107,['상단: RealSense D435','기존 기둥 최상단을 반대로 결합','마운트 베이스 + 틸팅 마운트 + D435','수직 아래에서 약 20° 기울임 (사진 추정)','','R1 집게: 32 × 32 UVC + XLeRobot 마운트'],9,6.5)
text(242,56,'표면 마감',12,True)
lines(242,46,['PLA 출력물·작업대·벽: 무광','바닥: 약한 유광 / 금속: 은은한 반사'],9,6.5)
footer(2)

# 03 - Floor tape/marker explanation; estimated routing cannot be misread as survey.
header(3,'바닥 표식과 ArUco 배치','바닥 선: 현장 사진 기반 정렬안 · 실측으로 확정한 치수는 FM-01과 FM-02에 구분하여 표기')
x,y,s=24,81,1/12;P=map_frame(x,y,s,objects=False)
text(*P(1800,575),'ASM',10,True,BLUE,'center');text(*P(3375,1435),'MAT',10,True,BLUE,'center')
text(*P(1370,1100),'REST',9,True,MUTED,'right')
text(343,230,'REST 표식 상세',11,True)
# Exact tape dimensions, enlarged independently and explicitly not to the main scale.
x0,y0,k=349,153,.22
rect(343,147,59,70,'#B6BEC4','#B6BEC4',.1)
rect(x0,y0,160*k,240*k,'#B6BEC4','#FFFFFF',20*k)
line((x0+130*k,y0+20*k),(x0+130*k,y0+220*k),'#151B20',20*k)
line((x0+160*k,y0+120*k),(399,y0+120*k),'#FFFFFF',20*k)
lines(343,142,['흰 사각형 네 면 유지','검은선은 오른쪽 안쪽','테두리와 겹치지 않음','위·아래에도 간격 유지'],8.5,5.5)
text(24,62,'표식 규격과 구분',11,True)
lines(24,52,['흰선: 경로·구역 테두리  /  검은선: 정지선·표식','선 폭: 도면에서 20으로 통일 (사진 추정)','마커: DICT_4X4_50 · 한 변 30 · 중심 간격 40 · 보드 90 × 90'],9,6)
text(232,62,'마커 ID',11,True)
lines(232,52,['MAT·ASM: 0-3  /  P1(M1): 4-7  /  P2(M2): 8-11','REST에는 ArUco를 배치하지 않습니다.','마커 가로 위치는 실측값, 높이·바닥선은 사진 추정값입니다.'],9,6)
footer(3)
C.save();print(OUT/'so101-workcell-layout.pdf')
