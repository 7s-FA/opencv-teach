"""Current-frame carrier half-turn evidence from its exposed central junction."""
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from .height_reference import detection_plane_z

# Reviewed saved-image reference: B middle above, A middle below, model +X right.
# This is fixed fixture evidence, not an adopted pose or previous frame result.
REFERENCE = Path(__file__).resolve().parents[1]/'assets/reference/carrier-junction.png'
PIXELS_PER_MM = 4
MIN_SCORE = .55
MIN_MARGIN = .08


def edge_evidence(image):
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY)
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    product=((hsv[:,:,1]>65)&(hsv[:,:,2]>25)).astype(np.uint8)
    product=cv2.dilate(product,np.ones((17,17),np.uint8))
    valid=product==0
    edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),15,45)>0
    return edges & valid,valid


@lru_cache(maxsize=1)
def reference_evidence():
    image=cv2.imread(str(REFERENCE))
    if image is None or image.shape!=(241,281,3):
        raise ValueError('운반용 지그 접합부 기준 이미지 오류')
    edges,valid=edge_evidence(image)
    yy,xx=np.indices(edges.shape)
    region=(abs(yy-120)<=100)&(abs(xx-140)<=112)
    return edges,valid,region


def compare(image):
    reference,reference_valid,region=reference_evidence()
    observed,observed_valid=edge_evidence(image)
    best={'score':0.,'shift_mm':None,'edge_pixels':0}
    # Every possible seed is inside this rectangle. Removing its empty
    # exterior preserves the distance at all sampled pixels. Integer shifts
    # are exact slices rather than two full-image affine warps per candidate.
    reference=reference[20:221,28:253];reference_valid=reference_valid[20:221,28:253]
    for dy in range(-12,13,4):
        for dx in range(-12,13,4):
            edges=observed[20-dy:221-dy,28-dx:253-dx]
            valid=observed_valid[20-dy:221-dy,28-dx:253-dx]
            common=reference_valid & valid
            a=reference & common;b=edges & common
            if a.sum()<25 or b.sum()<25:continue
            da=cv2.distanceTransform((~a).astype(np.uint8),cv2.DIST_L2,3)
            db=cv2.distanceTransform((~b).astype(np.uint8),cv2.DIST_L2,3)
            precision=float(np.exp(-da[b]/4).mean());recall=float(np.exp(-db[a]/4).mean())
            score=2*precision*recall/max(.0001,precision+recall)
            if score>best['score']:
                best={'score':float(score),'shift_mm':[dx/4,dy/4],'edge_pixels':int(min(a.sum(),b.sum()))}
    return best


def evaluate(frame,mesh,allowed,profile,candidate,*,outline_profile=None):
    """Only choose a half-turn; never change the measured center or boundary."""
    from .vision import project_plane
    xs=np.linspace(-35,35,281);ys=np.linspace(30,-30,241)
    xx,yy=np.meshgrid(xs,ys);local=np.c_[xx.ravel(),yy.ravel()]
    yaw=float(candidate['metric']['yaw_deg'])%360
    angle=np.deg2rad(yaw);rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    world=local@rotation.T*float(candidate.get('boundary_scale',1.))+candidate['metric']['center_xy_mm']
    uv=project_plane(world,profile,detection_plane_z(profile,mesh)+5).reshape(241,281,2)
    visible=cv2.remap(allowed,uv[:,:,0],uv[:,:,1],cv2.INTER_NEAREST,borderMode=cv2.BORDER_CONSTANT)
    region=reference_evidence()[2]
    if np.mean(visible[region]>0)<.98:
        return {'verified':False,'reason':'접합부 비교 영역이 영상·ROI 밖입니다.','scores':[]}
    image=cv2.remap(frame,uv[:,:,0],uv[:,:,1],cv2.INTER_LINEAR)
    first=compare(image);second=compare(cv2.rotate(image,cv2.ROTATE_180))
    rows=[{'yaw_deg':yaw,**first},{'yaw_deg':(yaw+180)%360,**second}]
    rows.sort(key=lambda r:r['score'],reverse=True)
    best,other=rows;margin=best['score']-other['score']
    # Score directly from the original frame, avoiding a second interpolation.
    # The overlay still enters the normal rectified-to-raw restoration path.
    outline_world=world.reshape(241,281,2)[[20,20,220,220],[28,252,252,28]]
    outline=project_plane(outline_world,outline_profile or profile,detection_plane_z(profile,mesh)+5)
    return {'verified':best['score']>=MIN_SCORE and margin>=MIN_MARGIN,
            'yaw_deg':best['yaw_deg'],'score':best['score'],'margin':margin,'scores':rows,
            'outline_px':outline.tolist(),
            'reason':'접합부 두 방향의 형상 일치도 또는 차이가 부족합니다.'}
