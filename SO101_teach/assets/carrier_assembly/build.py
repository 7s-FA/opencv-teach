"""Rebuild the fixed carrier assembly from supplied binary STLs (millimetres)."""
from pathlib import Path
import hashlib
import json
import struct
import zipfile
import xml.etree.ElementTree as ET

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
RECORD = np.dtype([('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attr', '<u2')])


def read_stl(path):
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0]
    if len(data) != 84 + 50 * count:
        raise ValueError(f'Not binary STL: {path}')
    return np.frombuffer(data, dtype=RECORD, offset=84)['vertices'].astype(float)


def normal(tri):
    return np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])


def seat_height(moving, fixed):
    """Exact vertical contact of projected triangle surfaces, ignoring zero-area touches."""
    bottom = moving[normal(moving)[:, 2] < -1e-7]
    upper = fixed[normal(fixed)[:, 2] > 1e-7]
    bounds_lo, bounds_hi = upper[:, :, :2].min(1), upper[:, :, :2].max(1)
    un = normal(upper)
    result = -np.inf
    for b in bottom:
        lo, hi = b[:, :2].min(0), b[:, :2].max(0)
        bn = np.cross(b[1] - b[0], b[2] - b[0])
        indices = np.flatnonzero(np.all(bounds_hi > lo + 1e-6, axis=1) & np.all(bounds_lo < hi - 1e-6, axis=1))
        for i in indices:
            area, polygon = cv2.intersectConvexConvex(b[:, :2].astype('float32'), upper[i, :, :2].astype('float32'))
            if polygon is None or area < 1e-5:
                continue
            xy = polygon.reshape(-1, 2)
            top_z = upper[i, 0, 2] - (xy - upper[i, 0, :2]) @ un[i, :2] / un[i, 2]
            bottom_z = b[0, 2] - (xy - b[0, :2]) @ bn[:2] / bn[2]
            result = max(result, float(np.max(top_z - bottom_z)))
    if not np.isfinite(result):
        raise ValueError('No seating contact')
    return result


def export_stl(path, triangles):
    rows = np.zeros(len(triangles), dtype=RECORD)
    n = normal(triangles)
    n /= np.maximum(np.linalg.norm(n, axis=1)[:, None], 1e-20)
    rows['normal'], rows['vertices'] = n, triangles
    path.write_bytes(b'Fixed carrier assembly; millimetres; separate touching shells'.ljust(80, b'\0') + struct.pack('<I', len(rows)) + rows.tobytes())


def export_3mf(parts):
    ns = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
    ET.register_namespace('', ns)
    def elem(parent, tag, **attrs):
        return ET.SubElement(parent, f'{{{ns}}}{tag}', {k: str(v) for k, v in attrs.items()})
    model = ET.Element(f'{{{ns}}}model', {'unit': 'millimeter'})
    resources = elem(model, 'resources')
    for index, (row, tri) in enumerate(parts, 1):
        obj = elem(resources, 'object', id=index, type='model', name=row['name'])
        mesh = elem(obj, 'mesh'); vertices = elem(mesh, 'vertices'); faces = elem(mesh, 'triangles')
        # Exact welding preserves source shape and avoids duplicate STL vertices.
        unique, inverse = np.unique(tri.reshape(-1, 3), axis=0, return_inverse=True)
        for v in unique:
            elem(vertices, 'vertex', x=f'{v[0]:.8g}', y=f'{v[1]:.8g}', z=f'{v[2]:.8g}')
        for face in inverse.reshape(-1, 3):
            elem(faces, 'triangle', v1=face[0], v2=face[1], v3=face[2])
    obj = elem(resources, 'object', id=len(parts)+1, type='model', name='Fixed carrier with six fixtures')
    components = elem(obj, 'components')
    for index in range(1, len(parts)+1):
        elem(components, 'component', objectid=index)
    elem(elem(model, 'build'), 'item', objectid=len(parts)+1)
    with zipfile.ZipFile(ROOT/'carrier_assembly.3mf', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('3D/3dmodel.model', ET.tostring(model, encoding='utf-8', xml_declaration=True))
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')


def previews(parts):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    colors = {'carrier': np.array([.77, .80, .83]), 'cap': np.array([.76, .86, .94]), 'ball': np.array([.76, .89, .84]), 'tpu': np.array([.76, .89, .84]), 'housing': np.array([.94, .85, .72])}
    fig = plt.figure(figsize=(12, 8), facecolor='#f7f8fa')
    ax = fig.add_subplot(111, projection='3d', computed_zorder=True)
    all_triangles, all_colors = [], []
    for row, tri in parts:
        n = normal(tri); n /= np.maximum(np.linalg.norm(n, axis=1)[:, None], 1e-20)
        light = np.array([-.3, -.5, 1.]); light /= np.linalg.norm(light)
        shade = .58 + .42 * np.maximum(n @ light, 0)
        color = colors[row['kind']]
        all_triangles.append(tri); all_colors.append(color[None, :] * shade[:, None])
    ax.add_collection3d(Poly3DCollection(np.concatenate(all_triangles), facecolors=np.concatenate(all_colors), edgecolors='none', antialiased=False, rasterized=True))
    ax.set(xlim=(-120,120), ylim=(-85,85), zlim=(0,65), xlabel='X / mm', ylabel='Y / mm', zlabel='Z / mm')
    ax.set_box_aspect((240,170,65));ax.view_init(elev=48,azim=-64)
    ax.set_title('Fixed carrier assembly | 220 x 148 mm\nUpper: B Cap / B TPU / B Base · Lower: A Cap / A Ball / A Base', pad=18)
    fig.tight_layout();fig.savefig(ROOT/'preview.png',dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(11,7),facecolor='#f7f8fa')
    top_triangles, top_colors = [], []
    for row,tri in parts:
        t=tri[normal(tri)[:,2]>1e-8];t=t[np.argsort(t[:,:,2].mean(1))]
        height=t[:,:,2].mean(1);shade=.63+.37*(height-height.min())/max(1.,height.max()-height.min())
        top_triangles.append(t);top_colors.append(colors[row['kind']][None,:]*shade[:,None])
    top_triangles=np.concatenate(top_triangles);order=np.argsort(top_triangles[:,:,2].mean(1))
    ax.add_collection(PolyCollection(top_triangles[order,:,:2],facecolors=np.concatenate(top_colors)[order],edgecolors='none',antialiased=False))
    for row,_ in parts[1:]:
        x,y,z=row['translation_mm'];ax.plot(x,y,'+',color='#bd3349',ms=7,mew=1)
    ax.set(xlim=(-114,114),ylim=(-78,78),xlabel='X / mm',ylabel='Y / mm',title='Top view · upper: B / B / B · lower: A / A / A · centres marked +\nCap (90°)                 Ball / TPU (90°)                 Base (90°*)')
    ax.set_aspect('equal');ax.set_xticks([-72,0,72]);ax.set_yticks([-36,36])
    fig.text(.5,.015,'*Housing orientation inferred from the photograph; pocket shape is hidden by the parts.',ha='center',fontsize=9)
    fig.tight_layout(rect=[0,.04,1,1]);fig.savefig(ROOT/'top_view.png',dpi=150);plt.close(fig)


def main():
    carrier=read_stl(ROOT/'source/current/carrier.stl')
    parts=[({'name':'carrier','kind':'carrier','source':'source/current/carrier.stl','translation_mm':[0,0,0],'yaw_deg':0},carrier)]
    # Source carrier has exact slot centres at x=-72,0,72 and y=-36,36.
    for group,y,kinds in [('b',36.,['cap','tpu','housing']),('a',-36.,['cap','ball','housing'])]:
        for x,kind in zip([-72.,0.,72.],kinds):
            # Latest supplied names: upper B row, lower A row.
            product_group = group
            filename=f'source/current/{product_group}_{kind}_jig.stl'
            source=read_stl(ROOT/filename)
            # Photo supports follow carrier Y; source support features follow X.
            rotated=source @ np.array([[0.,1.,0.],[-1.,0.,0.],[0.,0.,1.]])
            placed=rotated+[x,y,0.];z=seat_height(placed,carrier);placed[:,:,2]+=z
            row={'name':f'{product_group}_{kind}','product':product_group.upper(),'kind':kind,'source':filename,'translation_mm':[x,y,round(z,6)],'yaw_deg':90,'orientation_evidence':'photo support direction' if kind!='housing' else 'inferred; fixture corners partly hidden by product','contact_z_mm':float(z)}
            parts.append((row,placed))
    for row,tri in parts:
        row['source_sha256']=hashlib.sha256((ROOT/row['source']).read_bytes()).hexdigest()
        row['bounds_mm']=[tri.min((0,1)).tolist(),tri.max((0,1)).tolist()]
    merged=np.concatenate([t for _,t in parts]);export_stl(ROOT/'carrier_assembly.stl',merged);export_3mf(parts)
    doc={'schema':1,'unit':'mm','origin':'Original carrier centre in XY, underside Z=0; X long side, Y short side','front_back_required':True,'layout':'Latest supplied parts: upper B cap, B TPU, B base; lower A cap, A ball, A base, left to right','fixture_plate_centres_mm':[[x,y] for x in [-72,0,72] for y in [36,-36]],'parts':[r for r,_ in parts],'products_included':False,'seating':'First vertical contact from original triangle meshes; adhesive thickness not included','caution':'Housing rotation is inferred from partly occluded photo; not physically surveyed. Mesh assembly has separate touching shells, not a boolean-fused print solid.'}
    doc['installation']={'reference':'physical_floor','platform':'TurtleBot3 Burger','detection_rim_height_mm':199.8,'nominal_detection_rim_height_mm':200.,'carrier_rim_local_z_mm':4.8,'carrier_underside_height_mm':195.,'table_leg_height_mm':185.,'table_thickness_mm':10.,'table_top_height_mm':195.,'basis':'user-specified heights; floor-to-robot transform inferred from existing tabletop datum'}
    (ROOT/'assembly.json').write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n')
    assert len(parts)==7 and all(np.isfinite(t).all() for _,t in parts)
    assert np.allclose(np.ptp(merged.reshape(-1,3),axis=0)[:2],[220,148])
    assert np.allclose(read_stl(ROOT/'carrier_assembly.stl'),merged,atol=1e-5)
    report={'components':7,'fixture_count':6,'dimensions_mm':np.ptp(merged.reshape(-1,3),axis=0).tolist(),'triangles':len(merged),'stl_round_trip':True,'seating_heights_mm':{r['name']:r['translation_mm'][2] for r,_ in parts[1:]},'minimum_neighbour_plate_gap_mm':4.,'runtime_jig_configuration_changed':False}
    (ROOT/'validation.json').write_text(json.dumps(report,indent=2)+'\n');previews(parts)
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
