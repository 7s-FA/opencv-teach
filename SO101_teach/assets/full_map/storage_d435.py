"""Photograph-based D435 assembly, confined to the independent map draft."""
from pathlib import Path
import hashlib
import json
import numpy as np
from scipy.spatial.transform import Rotation
import xml.etree.ElementTree as ET


def nums(v):
    return ' '.join(str(float(x)) for x in v)


def install(scene, mount, layout, out, root):
    assets=scene.find('asset')
    out=Path(out); folder=out/'assets'; folder.mkdir(exist_ok=True)
    top=mount.find("geom[@name='storage_overhead_cam_mount_top']")
    p=np.fromstring(top.get('pos'),sep=' ')
    q=np.fromstring(top.get('quat'),sep=' ')
    original=Rotation.from_quat(q[[1,2,3,0]])
    # Turn only the upper stock part around its vertical socket axis. The old
    # UVC plate now projects rearward; the lower and middle column stay fixed.
    reverse=Rotation.from_euler('z',180,degrees=True)
    top.set('quat',nums((reverse*original).as_quat()[[3,0,1,2]]))
    mount.remove(mount.find("body[@name='storage_overhead_camera_pcb']"))
    # Horizontal end face measured directly from the original STL, Y=357.600006.
    cap_origin=p+np.array([0,0,.3576000061035156])
    cap=ET.SubElement(mount,'body',name='storage_d435_cap_frame',pos=nums(cap_origin))
    paths={}; hashes={}; bounds={}
    for label,filename in [('base','D435_마운트 베이스.stl'),('tilt','D435_틸팅 마운트.stl')]:
        source=out/'sources'/('d435_mount_base_source.stl' if label=='base' else 'd435_tilt_source.stl')
        raw=source.read_bytes()
        vertices=np.array([list(map(float,line.split()[1:])) for line in raw.decode().splitlines()
                           if line.strip().startswith('vertex')],dtype=np.float64).reshape(-1,3,3)
        # MuJoCo requires binary STL; keep every source triangle, with no scaling,
        # remeshing, reflection or edited dimensions. Source is in millimetres.
        rows=np.zeros(len(vertices),dtype=[('normal','<f4',3),('vertices','<f4',(3,3)),('attr','<u2')])
        rows['vertices']=vertices
        normals=np.cross(vertices[:,1]-vertices[:,0],vertices[:,2]-vertices[:,0])
        normals/=np.maximum(np.linalg.norm(normals,axis=1)[:,None],1e-30)
        rows['normal']=normals
        dest=folder/('d435_'+label+'.stl')
        dest.write_bytes(b'Original downloaded STL; binary encoding only'.ljust(80,b' ')+np.uint32(len(rows)).tobytes()+rows.tobytes())
        ET.SubElement(assets,'mesh',name='storage_d435_'+label,file=str(dest),scale='.001 .001 .001')
        paths[label]=str(source);hashes[label]=hashlib.sha256(raw).hexdigest()
        bounds[label]={'source_mm':[vertices.min((0,1)).tolist(),vertices.max((0,1)).tolist()],
                       'encoding_max_error_mm':float(np.max(np.abs(rows['vertices']-vertices)))}
    ET.SubElement(cap,'geom',name='storage_d435_mount_base',type='mesh',mesh='storage_d435_base',
                  material='robot_and_camera_mount_plastic',mass='0',contype='0',conaffinity='0')
    # The two downloaded parts already share hinge coordinates (10.7, 0, 23) mm.
    # Photo 1000015418 shows the camera plate rising toward the camera. Estimate
    # 20 degrees above horizontal (= optical axis 20 degrees from down).
    angle=20.; hinge=np.array([.0107,0,.023])
    R=Rotation.from_euler('y',-angle,degrees=True)
    head=ET.SubElement(cap,'body',name='storage_d435_tilt_frame',
                      pos=nums(hinge-R.apply(hinge)),quat=nums(R.as_quat()[[3,0,1,2]]))
    ET.SubElement(head,'geom',name='storage_d435_tilting_mount',type='mesh',mesh='storage_d435_tilt',
                  material='robot_and_camera_mount_plastic',mass='0',contype='0',conaffinity='0')
    camera_path=root.parent/'Test_IL/assets/camera_mounts/d435_body_mm.stl'
    # The unchanged camera mesh has 231186 triangles, above MuJoCo's per-STL
    # limit. Split its original triangle records into two render meshes.
    camera_raw=camera_path.read_bytes(); count=int.from_bytes(camera_raw[80:84],'little')
    camera_meshes=[]
    for index,start in enumerate(range(0,count,150000)):
        stop=min(start+150000,count); dest=folder/f'd435_body_{index}.stl'
        dest.write_bytes(camera_raw[:80]+np.uint32(stop-start).tobytes()+camera_raw[84+start*50:84+stop*50])
        name=f'storage_d435_body_mesh_{index}'
        ET.SubElement(assets,'mesh',name=name,file=str(dest),scale='.001 .001 .001')
        camera_meshes.append(name)
    # D435 source axes: X=optical forward, Y=long dimension, Z=body up.
    # Rear mounting plane seats below the tilting plate's z=20.75 mm face.
    Rc=Rotation.from_matrix(np.array([[0,0,1],[0,1,0],[-1,0,0]]))
    rear=np.array([-.0101547,0,.0125]); seat=np.array([.0377,0,.02075])
    pc=seat-Rc.apply(rear)
    camera=ET.SubElement(head,'body',name='storage_d435_camera',pos=nums(pc),quat=nums(Rc.as_quat()[[3,0,1,2]]))
    for index,name in enumerate(camera_meshes):
        ET.SubElement(camera,'geom',name=f'storage_d435_body_{index}',type='mesh',mesh=name,
                      rgba='.72 .74 .75 1',mass='0',contype='0',conaffinity='0')
    # Nominal optical center only; no calibrated camera/extrinsics are introduced.
    ET.SubElement(camera,'site',name='storage_d435_rgb_nominal',pos='.0106 .0325 .0125',size='.001',rgba='0 0 0 0')
    paths['camera']=str(camera_path);hashes['camera']=hashlib.sha256(camera_path.read_bytes()).hexdigest()
    info={'module':'RealSense D435','stock_upper_part_reversed_deg':180,
          'mount':'Downloaded mount base + tilting mount; original geometry',
          'tilt_from_vertical_down_deg':angle,'tilt_source':'Photo 1000015418 side view; approximate, not measured',
          'estimated_tilt_uncertainty_deg':5,'mount_material':'robot_and_camera_mount_plastic',
          'cap_seating_height_stock_stl_mm':357.6000061035156,
          'cap_origin_in_mast_frame_m':cap_origin.tolist(),'hinge_in_cap_frame_mm':(hinge*1000).tolist(),
          'camera_rear_seat_in_untilted_cap_frame_mm':(seat*1000).tolist(),
          'camera_rear_point_source_mm':(rear*1000).tolist(),
          'source_files':paths,'source_sha256':hashes,'source_bounds':bounds,
          'camera_mesh_encoding':'Original triangles split into two STL files; no mesh simplification',
          'photographs':['1000015417.jpg','1000015418.jpg'],
          'assembly_status':'CAD mating-plane placement with photo-estimated tilt; physical fit and optical calibration not verified'}
    layout['camera'].pop('module_size_mm',None)
    layout['camera'].update(info)
    (out/'d435-assembly.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
