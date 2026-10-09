"""Normalized simple polygons, including legacy rectangles and four-point ROIs."""
from functools import lru_cache
import numpy as np

@lru_cache(maxsize=128)
def _validate_polygon(vertices):
    def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def on_segment(a,b,p):return abs(cross(a,b,p))<1e-10 and min(a[0],b[0])-1e-10<=p[0]<=max(a[0],b[0])+1e-10 and min(a[1],b[1])-1e-10<=p[1]<=max(a[1],b[1])+1e-10
    n=len(vertices)
    for i,a in enumerate(vertices):
        b=vertices[(i+1)%n]
        if abs(a[0]-b[0])+abs(a[1]-b[1])<1e-9:raise ValueError('감지 범위의 점이 겹칩니다. 다시 그려 주세요.')
        for j in range(i+1,n):
            if j==i+1 or i==0 and j==n-1:continue
            c,d=vertices[j],vertices[(j+1)%n]
            ab1,ab2,cd1,cd2=cross(a,b,c),cross(a,b,d),cross(c,d,a),cross(c,d,b)
            if ab1*ab2<0 and cd1*cd2<0 or on_segment(a,b,c) or on_segment(a,b,d) or on_segment(c,d,a) or on_segment(c,d,b):
                raise ValueError('선이 서로 교차하거나 겹칩니다. 영역을 한 바퀴 둘러 그려 주세요.')
    area=abs(sum(a[0]*vertices[(i+1)%n][1]-a[1]*vertices[(i+1)%n][0] for i,a in enumerate(vertices)))/2
    if area<1e-8:raise ValueError('감지 범위의 면적이 없습니다.')
    return area

def roi_vertices(roi):
    if roi is None:return [[0.,0.],[1.,0.],[1.,1.],[0.,1.]]
    try:points=np.asarray(roi,dtype=float)
    except (TypeError,ValueError):raise ValueError('감지 범위 형식을 확인하세요.') from None
    if not np.isfinite(points).all() or np.any(points<0) or np.any(points>1):raise ValueError('감지 범위는 영상 안에 지정하세요.')
    if points.shape==(4,):
        left,top,right,bottom=points
        if not left<right or not top<bottom:raise ValueError('감지 범위의 너비와 높이를 확인하세요.')
        return [[left,top],[right,top],[right,bottom],[left,bottom]]
    if points.ndim!=2 or points.shape[1]!=2 or not 3<=len(points)<=128:raise ValueError('감지 범위는 3~128개의 경계점으로 구성해야 합니다.')
    if len(points)>3 and np.linalg.norm(points[0]-points[-1])<1e-9:points=points[:-1]
    _validate_polygon(tuple(map(tuple,points)))
    return points.tolist()

def ordered_roi(points):
    """Retain compatibility for four-corner callers; the UI now draws a stroke."""
    points=np.asarray(points,float);center=points.mean(0)
    points=points[np.argsort(np.arctan2(points[:,1]-center[1],points[:,0]-center[0]))]
    points=np.roll(points,-int(np.argmin(points.sum(1))),axis=0)
    vertices=roi_vertices(points)
    if _validate_polygon(tuple(map(tuple,vertices)))<.0004:raise ValueError('감지 범위가 너무 작습니다.')
    return vertices

def freehand_roi(points,width,height):
    import cv2
    points=np.asarray(points,float)
    if points.ndim!=2 or points.shape[1]!=2 or len(points)<3:raise ValueError('마우스로 영역을 둘러 그린 뒤 놓으세요.')
    # Simplify in source-image pixels so wide and tall strokes have equal precision.
    pixels=np.float32(np.clip(points,0,1)*[width,height]);epsilon=1.5
    while True:
        contour=cv2.approxPolyDP(pixels.reshape(-1,1,2),epsilon,True).reshape(-1,2)
        if len(contour)<=128:break
        epsilon*=1.5
    vertices=roi_vertices(contour/[width,height])
    if _validate_polygon(tuple(map(tuple,vertices)))<.0004:raise ValueError('감지 범위가 너무 작습니다. 조금 더 넓게 그려 주세요.')
    return vertices

def roi_mask(roi,width,height):
    import cv2
    vertices=roi_vertices(roi);mask=np.zeros((height,width),np.uint8)
    if roi is not None and np.asarray(roi).shape==(4,):
        x0,y0,x1,y1=np.rint(np.asarray(roi)*[width,height,width,height]).astype(int)
        mask[y0:y1,x0:x1]=255
    else:cv2.fillPoly(mask,[np.rint(np.asarray(vertices)*[width,height]).astype(np.int32)],255)
    return mask
