"""Read an unfused carrier plus six fixed fixture shells from a single STL."""
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


def top_annulus(tri):
    """Find the unique full circular opening at a fixture's highest surface."""
    high=tri.max((0,1));flat=np.ptp(tri[:,:,2],axis=1)<.001
    faces=tri[flat & (abs(tri[:,0,2]-high[2])<.001)]
    if not len(faces):return None
    points=np.unique(np.round(faces.reshape(-1,3)[:,:2],5),axis=0)
    if len(points)<64:return None
    center=points.mean(0);radii=np.linalg.norm(points-center,axis=1)
    groups=[]
    for radius in sorted(radii):
        if not groups or radius-np.mean(groups[-1])>.08:groups.append([radius])
        else:groups[-1].append(radius)
    if len(groups)!=2 or min(map(len,groups))<32:return None
    inner,outer=map(np.mean,groups)
    if not 4<=inner<=17 or not .8<=outer-inner<=4:return None
    for radius in (inner,outer):
        ring=points[abs(radii-radius)<.08]-center;angles=np.sort(np.arctan2(ring[:,1],ring[:,0]))
        if np.max(np.diff(np.r_[angles,angles[0]+2*np.pi]))>np.deg2rad(15):return None
    return {'center_mm':center.tolist(),'z_mm':float(high[2]),'inner_radius_mm':float(inner),'outer_radius_mm':float(outer)}


def carrier_components(tri):
    """Recognize only one enclosing base and six equal, regularly placed plates.

    This is geometric metadata, not proof that the six fixtures are visible in
    a camera image. The complete mesh and its original coordinates are kept.
    """
    low=tri.min((0,1));high=tri.max((0,1));extent=high-low
    if extent[2]<min(extent[:2])*.08:return None
    vertices,index=np.unique(np.round(tri.reshape(-1,3),5),axis=0,return_inverse=True)
    faces=index.reshape(-1,3);a=faces.ravel();b=np.roll(faces,1,axis=1).ravel()
    graph=coo_matrix((np.ones(len(a),np.uint8),(a,b)),shape=(len(vertices),len(vertices)))
    count,labels=connected_components(graph,directed=False)
    if count!=7:return None
    face_labels=labels[faces[:,0]]
    components=[tri[face_labels==i] for i in range(count)]
    bounds=[(t.min((0,1)),t.max((0,1))) for t in components]
    bases=[i for i,(lo,hi) in enumerate(bounds) if np.allclose(lo[:2],low[:2],atol=.01) and np.allclose(hi[:2],high[:2],atol=.01) and abs(lo[2]-low[2])<.01 and hi[2]-lo[2]<extent[2]*.5]
    if len(bases)!=1:return None
    base=bases[0];fixtures=[]
    for i,(lo,hi) in enumerate(bounds):
        if i==base:continue
        if np.any(lo[:2]<=low[:2]) or np.any(hi[:2]>=high[:2]) or lo[2]<bounds[base][0][2] or lo[2]>bounds[base][1][2]+.1:return None
        fixtures.append({'center_mm':((lo[:2]+hi[:2])/2).tolist(),'size_mm':(hi-lo).tolist(),'bottom_z_mm':float(lo[2])})
    sizes=np.array([p['size_mm'][:2] for p in fixtures])
    if np.max(abs(sizes/np.median(sizes,axis=0)-1))>.15:return None
    # Reuse the same complete-grid/spacing/long-axis validation as detection.
    from .grid_model import grid_layout
    layout={'kind':'fixed_grid','fixtures':fixtures}
    if grid_layout({'shape':'rectangle','size_mm':extent.tolist(),'low_mm':low.tolist(),'assembly':layout}) is None:return None
    openings=[feature for i,t in enumerate(components) if i!=base for feature in [top_annulus(t)] if feature is not None]
    if len(openings)==1:layout['orientation_hole']=openings[0]
    return components[base],layout
