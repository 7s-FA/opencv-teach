"""Place display labels outside protected image regions; never alter detections."""
import numpy as np

class LabelLayout:
    def __init__(self,shape,polygons=()):
        self.height,self.width=shape[:2];self.blocked=[];self.labels=[]
        for polygon in polygons:self.protect(polygon)
    def protect(self,polygon):
        p=np.asarray(polygon,float).reshape(-1,2)
        if len(p) and np.isfinite(p).all():
            lo=p.min(0)-6;hi=p.max(0)+6
            self.blocked.append((lo[0],lo[1],hi[0],hi[1]))
    def place(self,width,height,point):
        if width+8>self.width or height+8>self.height:return None
        px,py=map(float,point);regions=self.blocked+self.labels
        xs={4,self.width-width-4,int(px),int(px-width/2)}
        ys={4,self.height-height-4,int(py-height-8),int(py+8)}
        for x0,y0,x1,y1 in regions:
            xs.update((int(x0-width-6),int(x1+6)))
            ys.update((int(y0-height-6),int(y1+6)))
        choices=[]
        for x in xs:
            if not 4<=x<=self.width-width-4:continue
            for y in ys:
                if not 4<=y<=self.height-height-4:continue
                if any(x<x1 and x+width>x0 and y<y1 and y+height>y0 for x0,y0,x1,y1 in regions):continue
                distance=(x+width/2-px)**2+(y+height/2-py)**2
                choices.append((distance,y,x))
        if not choices:return None
        _,y,x=min(choices);self.labels.append((x-3,y-3,x+width+3,y+height+3));return x,y


def draw_linear_centers(image,profile,reference,placement,endpoint='명령 위치',*,labels=True,layout=None):
    import cv2
    from .inspection_geometry import anchors_for,project
    from .inspection_overlay import draw_label
    anchors,_=anchors_for(image,{},{},{},profile,reference,'리니어 조립','전체',placement,endpoint)
    if not any(a['inspection_allowed'] for a in anchors):return []
    if layout is None:layout=LabelLayout(image.shape)
    for row in anchors:
        try:layout.protect(project([[-35,-35,25],[35,-35,25],[35,35,25],[-35,35,25]],row,profile))
        except (ValueError,cv2.error):pass
    marks=[]
    for row in anchors:
        try:
            uv=project([[0,0,4.25]],row,profile)[0];x,y=np.rint(uv).astype(int)
            if not (0<=x<image.shape[1] and 0<=y<image.shape[0]):continue
            cv2.circle(image,(x,y),7,(20,25,30),-1,cv2.LINE_AA)
            cv2.circle(image,(x,y),4,(255,230,30),-1,cv2.LINE_AA)
            label=row['label']+' 중심'
            if len(anchors)>2:label+=' · '+row['source'].split(' · ')[-1]
            if labels:draw_label(image,label,(x,y),layout=layout)
            marks.append({'id':row['id'],'point':[float(uv[0]),float(uv[1])],'source':row['source']})
        except (ValueError,KeyError,cv2.error):continue
    return marks
