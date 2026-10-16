"""Selected C layout: nearby A-left/B-right badges, outside all part polygons."""
import cv2
import numpy as np
from .inspection_overlay import label_image


class PartLabels:
    def __init__(self):
        self.key=None;self.rectangles=[];self.protected=None

    def draw(self,image,labels):
        # Cache only exact projected geometry, never camera pixels or judgments.
        h,w=image.shape[:2]
        key=(h,w,tuple((r['id'],r['text'],r['product'],np.asarray(r['polygon'],float).tobytes(),np.asarray(r['top'],float).tobytes()) for r in labels))
        if key!=self.key:
            mask=np.zeros((h,w),np.uint8)
            for row in labels:
                for points in (row['polygon'],row['top']):
                    p=np.asarray(points,float)
                    if len(p)>=3 and np.isfinite(p).all():cv2.fillConvexPoly(mask,np.rint(p).astype(np.int32),1)
            self.protected=cv2.dilate(mask,np.ones((9,9),np.uint8))
            occupied=self.protected.copy();rectangles=[]
            for row in sorted(labels,key=lambda r:float(np.asarray(r['polygon'])[:,1].mean())):
                badge=label_image(row['text']);bh,bw=badge.shape[:2]
                if bh+8>h or bw+8>w:continue
                cx,cy=np.asarray(row['polygon'],float).mean(0)
                if not np.isfinite([cx,cy]).all():continue
                yy,xx=np.ogrid[4:h-bh-3:2,4:w-bw-3:2]
                integral=cv2.integral(occupied)
                clear=(integral[yy+bh,xx+bw]-integral[yy,xx+bw]-integral[yy+bh,xx]+integral[yy,xx])==0
                dx=xx+bw/2-cx;dy=yy+bh/2-cy
                sign=-1 if row['product']=='A' else 1
                cost=dx*dx+dy*dy+np.maximum(-sign*dx,0)**2*2+dy*dy*.35
                cost=np.where(clear,cost,np.inf)
                iy,ix=np.unravel_index(np.argmin(cost),cost.shape)
                if not np.isfinite(cost[iy,ix]):continue
                x,y=int(xx[0,ix]),int(yy[iy,0])
                rectangles.append({'id':row['id'],'text':row['text'],'rect':[x,y,bw,bh]})
                occupied[max(0,y-6):min(h,y+bh+6),max(0,x-6):min(w,x+bw+6)]=1
            self.key=key;self.rectangles=rectangles
        for row in self.rectangles:
            x,y,bw,bh=row['rect'];image[y:y+bh,x:x+bw]=label_image(row['text'])
        return self.rectangles
