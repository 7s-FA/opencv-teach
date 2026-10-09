"""STL-derived internal divider intersections as independent carrier evidence."""
import math
import cv2
import numpy as np
from .height_reference import detection_plane_z


def grid_layout(mesh):
    """Infer a complete regular 3-by-2 array of fixtures or equal openings."""
    if mesh.get('shape')!='rectangle':return None
    assembly=mesh.get('assembly',{})
    if assembly.get('kind')=='fixed_grid':
        size=np.asarray(mesh['size_mm'][:2],float);low=np.asarray(mesh['low_mm'][:2],float)
        holes=[{'center':((np.asarray(f['center_mm'])-low)/size).tolist(),'size':(np.asarray(f['size_mm'][:2])/size).tolist()} for f in assembly['fixtures']]
    else:holes=mesh.get('holes',[])
    if len(holes)<6:return None
    areas=np.array([np.prod(h['size']) for h in holes],float)
    large=[h for h,a in zip(holes,areas) if a>=areas.max()*.7]
    if len(large)!=6:return None
    centers=np.array([h['center'] for h in large],float);sizes=np.array([h['size'] for h in large],float)
    if np.any(sizes<=0) or np.max(abs(sizes/np.median(sizes,axis=0)-1))>.15:return None
    groups=[]
    for axis in (0,1):
        clusters=[]
        for value in sorted(centers[:,axis]):
            if not clusters or abs(value-np.mean(clusters[-1]))>.025:clusters.append([value])
            else:clusters[-1].append(value)
        groups.append(np.array([np.mean(c) for c in clusters]))
    if sorted(map(len,groups))!=[2,3]:return None
    bins={(int(np.argmin(abs(groups[0]-x))),int(np.argmin(abs(groups[1]-y)))) for x,y in centers}
    if len(bins)!=6:return None
    axis=0 if len(groups[0])==3 else 1
    spacing=np.diff(groups[axis])
    if min(spacing)<=0 or max(spacing)/min(spacing)>1.15:return None
    size=np.asarray(mesh['size_mm'][:2],float)
    if size[axis]<size[1-axis]*1.2:return None
    dividers=(groups[axis][:-1]+groups[axis][1:])/2
    other=float(np.mean(groups[1-axis]))
    points=np.zeros((2,2));points[:,axis]=(dividers-.5)*size[axis];points[:,1-axis]=(other-.5)*size[1-axis]
    return {'axis':axis,'points':points,'spacing':float(np.linalg.norm(points[1]-points[0])),'size':size}


def grid_hypotheses(frame,mesh,allowed,profile,*,evidence=None,visible=None,refine_lines=False):
    """Find two parallel dividers crossed by a third, with visible arm support.

    Intersections seed the known outer rectangle; they never by themselves
    establish an object detection or a motion-ready pose.
    """
    from .vision import world_xy,project_plane
    layout=grid_layout(mesh)
    if layout is None or not profile or not profile.get('intrinsics') or not profile.get('extrinsics'):return []
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    gray=evidence.source.blurred()
    edges=evidence.edges()
    # ROI clipping changes Hough's segment sampling, even for lines wholly
    # inside the region. Seed from the full image, then constrain the evidence
    # to the ROI below; final outer-rim containment remains strict.
    hough_edges=edges if visible is None else cv2.bitwise_and(evidence.source.edges(),visible)
    left=top=0
    if visible is not None and not np.array_equal(allowed,visible):
        x,y,width,height=cv2.boundingRect(allowed)
        if not width or not height:return []
        left=max(0,x-16);top=max(0,y-16)
        right=min(frame.shape[1],x+width+16);bottom=min(frame.shape[0],y+height+16)
        # A padded rectangle preserves uninterrupted local lines without
        # spending the Pi's frame budget on unrelated floor texture.
        hough_edges=hough_edges[top:bottom,left:right]
    segments=cv2.HoughLinesP(hough_edges,1,np.pi/720,threshold=25,minLineLength=25,maxLineGap=8)
    if segments is None:return []
    z=detection_plane_z(profile,mesh);lines=[]
    segments=segments[:,0]+np.array([left,top,left,top])
    # Discard unrelated full-image segments before the expensive grid search.
    # Hough itself still sees the same camera-valid image for every ROI.
    midpoint=np.rint((segments[:,:2]+segments[:,2:])/2).astype(int)
    segments=segments[allowed[midpoint[:,1],midpoint[:,0]]>0]
    order=np.argsort(-np.sum((segments[:,2:]-segments[:,:2])**2,axis=1))[:240]
    for segment in segments[order]:
        xy=world_xy(segment.reshape(2,2),profile,z);vector=xy[1]-xy[0];length=float(np.linalg.norm(vector))
        if not 30<length<max(layout['size'])*1.3:continue
        direction=vector/length
        if direction[0]<0:direction=-direction
        normal=np.array([-direction[1],direction[0]])
        lines.append({'xy':xy,'direction':direction,'normal':normal,'offset':float(normal@xy.mean(0)),'length':length})
    # A crossing can split a Hough segment into two halves. Rejoin only nearby
    # collinear fragments; later arm tests still use the unmodified edge image.
    merged=[]
    for line in sorted(lines,key=lambda line:-line['length']):
        for existing in merged:
            if abs(line['direction']@existing['direction'])<math.cos(math.radians(2)):continue
            if abs(existing['normal']@line['xy'].mean(0)-existing['offset'])>2.5:continue
            a=np.sort(existing['xy']@existing['direction']);b=np.sort(line['xy']@existing['direction'])
            if max(a[0],b[0])-min(a[1],b[1])>12:continue
            if refine_lines:
                points=np.concatenate([existing.get('fragments',existing['xy']),line['xy']])
                # Refit all observed fragments; keeping the longest fragment's
                # initial angle amplifies its pixel noise at distant crossings.
                vx,vy,cx,cy=cv2.fitLine(np.float32(points),cv2.DIST_L2,0,.01,.01).reshape(-1)
                direction=np.array([vx,vy],float)
                if direction[0]<0:direction=-direction
                normal=np.array([-direction[1],direction[0]]);center=np.array([cx,cy])
                projection=(points-center)@direction
                xy=center+np.array([projection.min(),projection.max()])[:,None]*direction
                existing.update(xy=xy,direction=direction,normal=normal,offset=float(normal@center),length=float(np.ptp(projection)),fragments=points)
            else:
                points=np.concatenate([existing['xy'],line['xy']]);direction=existing['direction']
                center=(existing['xy'].mean(0)*existing['length']+line['xy'].mean(0)*line['length'])/(existing['length']+line['length'])
                projection=(points-center)@direction
                xy=center+np.array([projection.min(),projection.max()])[:,None]*direction
                existing.update(xy=xy,offset=float(existing['normal']@center),length=float(np.ptp(projection)))
            break
        else:merged.append(line)
    lines=sorted(merged,key=lambda line:-line['length'])[:80]
    distance=evidence.distance()
    h,w=gray.shape;hypotheses=[];pairs=[];gap=layout['spacing'];axis=layout['axis']
    for i,a in enumerate(lines):
        if a['length']<layout['size'][1-axis]*.4:continue
        for b in lines[i+1:]:
            if b['length']<layout['size'][1-axis]*.4:continue
            if abs(a['direction']@b['direction'])<math.cos(math.radians(3)):continue
            separation=abs(float(a['normal']@(b['xy'].mean(0)-a['xy'].mean(0))))
            if abs(separation/gap-1)>.12:continue
            pairs.append((a['length']+b['length'],a,b))
    for _,a,b in sorted(pairs,key=lambda row:-row[0])[:30]:
        for cross in lines:
            if cross['length']<gap*.6 or abs(a['direction']@cross['direction'])>math.sin(math.radians(5)):continue
            intersections=[]
            for parallel in (a,b):
                matrix=np.array([parallel['normal'],cross['normal']])
                point=np.linalg.solve(matrix,[parallel['offset'],cross['offset']])
                # A small gap at the junction is expected, not a long invented
                # extension of an unrelated part or floor line.
                if any(np.min((line['xy']-point)@line['direction'])>10 or np.max((line['xy']-point)@line['direction'])< -10 for line in (parallel,cross)):break
                intersections.append(point)
            if len(intersections)!=2:continue
            observed=np.array(intersections)
            direction=a['normal']
            observed=observed[np.argsort(observed@direction)]
            delta=observed[1]-observed[0];length=float(np.linalg.norm(delta));scale=length/gap
            if not .88<=scale<=1.12:continue
            along=delta/length
            u=along if axis==0 else np.array([along[1],-along[0]])
            v=np.array([-u[1],u[0]]);rotation=np.column_stack([u,v])
            center=observed.mean(0)-layout['points'].mean(0)@rotation.T*scale
            angle=float(math.atan2(u[1],u[0]));pose=np.r_[center,angle]
            # Three of four supported arms at each junction, including both
            # directions of at least one divider, reject mere corner pairs.
            arms=np.array([u,-u,v,-v]);samples=observed[:,None,None,:]+arms[None,:,None,:]*np.array([6.,10.,14.,18.])[None,None,:,None]
            uv=project_plane(samples.reshape(-1,2),profile,z).reshape(2,4,4,2)
            pixel=np.rint(uv).astype(int);inside=(pixel[...,0]>=0)&(pixel[...,0]<w)&(pixel[...,1]>=0)&(pixel[...,1]<h)
            px=np.clip(pixel[...,0],0,w-1);py=np.clip(pixel[...,1],0,h-1);inside &=allowed[py,px]>0
            support=((distance[py,px]<3)&inside).mean(2)
            if np.any(np.count_nonzero(support>=.5,axis=1)<3):continue
            score=float(support.mean())
            item={'pose':pose,'points_world':observed,'points_px':project_plane(observed,profile,z),'arm_support':support.tolist(),'score':score}
            duplicate=next((i for i,p in enumerate(hypotheses) if np.linalg.norm(p['pose'][:2]-center)<6 and abs((p['pose'][2]-angle+np.pi/2)%np.pi-np.pi/2)<np.deg2rad(4)),None)
            if duplicate is None:hypotheses.append(item)
            elif score>hypotheses[duplicate]['score']:hypotheses[duplicate]=item
    return sorted(hypotheses,key=lambda p:-p['score'])[:8]


def matching_grid(candidate,hypotheses,mesh):
    layout=grid_layout(mesh);metric=candidate.get('metric')
    if layout is None or not metric:return None
    yaw=math.radians(metric['yaw_deg']);rotation=np.array([[math.cos(yaw),-math.sin(yaw)],[math.sin(yaw),math.cos(yaw)]])
    scale=float(candidate.get('boundary_scale',candidate.get('outer_shape',{}).get('fit_scale',1.)))
    expected=layout['points']@rotation.T*scale+metric['center_xy_mm'];matches=[]
    for item in hypotheses:
        error=min(float(np.max(np.linalg.norm(expected-item['points_world'],axis=1))),float(np.max(np.linalg.norm(expected-item['points_world'][::-1],axis=1))))
        angle=abs((yaw-item['pose'][2]+np.pi/2)%np.pi-np.pi/2)
        if error<=6 and angle<=np.deg2rad(5):matches.append((error,item))
    return min(matches,key=lambda pair:pair[0]) if matches else None


def attach_grid(result,hypotheses,mesh):
    for candidate in result.get('candidates',[]):
        match=matching_grid(candidate,hypotheses,mesh)
        if match:
            error,item=match;candidate.update(grid_verified=True,grid_crossings_px=item['points_px'].tolist(),grid_arm_support=item['arm_support'],grid_alignment_error_mm=error)
    result['grid_hypotheses']=len(hypotheses)
    return result
