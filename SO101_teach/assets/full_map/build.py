"""Full-map layout draft, independent of live teaching/control configuration.

Run with /home/fresh/venv/il/bin/python. All units in the scene are metres;
the drawing is in centimetres. No device connections or simulation steps.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

os.environ.setdefault('MUJOCO_GL', 'egl')
import numpy as np
from scipy.spatial.transform import Rotation
from scipy.spatial import ConvexHull
import mujoco
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Circle
from matplotlib.font_manager import FontProperties
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
sys.path.insert(0, str(ROOT))
from so101_teach.preview import configured_scene
from so101_teach.workcell_preview import load_placement
from so101_teach.workcell_scene import append_carrier_platform, numbers, yaw_quat
from so101_teach.domain import JOINTS
from photo_layout import PARKING, BOARDS, append_scene as append_photo_layout, plot_floor, plot_markers, draw_board_sheet
from storage_d435 import install as install_storage_d435
from scene_finish import apply_finish

FONT = '/usr/share/fonts/truetype/nanum/NanumGothic.ttf'
plt.rcParams.update({'font.family': FontProperties(fname=FONT).get_name(),
                     'axes.unicode_minus': False, 'svg.fonttype': 'path'})
INK, MUTED, BLUE, PURPLE, ORANGE = '#173042', '#607583', '#1777ad', '#7953b5', '#bc691c'
# Rows are carrier-local cy=-36,+36 mm. In photo 1000015419, A row 1
# is the upper lower-part slot; B row 0 is the left lower-part slot.
STORAGE_HOUSING_TYPES={'A':('a_housing','b_housing'),'B':('b_housing','a_housing')}
LAYOUT = {
    'revision': '2026-10-12 release 1: approved full map for teaching app and Isaac Sim', 'display_only': True, 'unit': 'mm',
    'origin': 'Simulation world lower left; drawing measurement datum O is lower right at (3600,0,0)',
    'measurement_datum_O_world_mm': [3600,0,0],
    'axes': {'X': 'World right; assembly offset is measured leftward from O', 'Y': 'Up the right/storage wall from O', 'Z': 'Up from floor'},
    'map_inner_size_mm': [3600, 1800],
    'assembly_near_corner_mm': [2100, 0],
    'storage_near_corner_mm': [3150, 910], 'storage_size_mm': [450, 450],
    'storage_local_axes': 'u = -world X away from right wall; v = -world Y toward assembly; distance w from Q is 450-v',
    'storage_from_local': {'origin_world_mm': [3600,1360,0], 'yaw_deg':180},
    'storage_jig_reference': 'Q=(u=0,v=450), wall-side corner on the ARM1-right side; offsets are (u,w)',
    'storage_photo_reference': '1000015414.jpg and codex-clipboard-727594a1-0fbd-4f7e-a015-2fe254d9ebf6.png; assembly bottom, storage right, waiting robots top; photos not mirrored',
    'jigs': {
        'B': {'offset_from_Q_uw_mm': [80, 40], 'near_corner_uv_mm': [80, 190], 'extent_uv_mm': [148, 220], 'center_uv_mm': [154, 300], 'yaw_deg': 90},
        'A': {'offset_from_Q_uw_mm': [230, 160], 'near_corner_uv_mm': [230, 142], 'extent_uv_mm': [220, 148], 'center_uv_mm': [340, 216], 'yaw_deg': 180}},
    'arm1': {'edge': 'v=450', 'front_corner_u_mm': 450, 'nearest_base_edge_u_mm': 370,
             'corner_gap_mm': 80, 'yaw_deg': -90, 'pose_source': 'Illustrative SO-101 posture'},
    'camera': {'edge': 'v=0', 'front_corner_u_mm': 450, 'nearest_mast_edge_u_mm': 420,
               'mast_corner_gap_mm': 30, 'module_size_mm': [32, 32], 'mount': 'Existing left arm mount and overhead mount CAD'},
    'confirmed': ['3600x1800 inner map', 'Distances to nearest table edges', 'Jig offsets to nearest carrier corners',
                  '450x450 storage table', 'Tables against walls', 'Same table height and material',
                  'Storage overhead is D435; stock 32x32-mount top reversed, downloaded base and tilt mount installed',
                  'ARM1 also has the 32x32 UVC wrist camera and XLeRobot bracket used in Test_IL',
                  'Storage mast left and ARM1 right in a front view, without mirroring the photograph',
                  'Entire map: assembly at bottom middle, storage at right middle, waiting robots along top wall'],
    'draft_assumptions': ['8 cm measured to robot base outline, 3 cm to vertical mast outline',
                          'ARM1 posture and gray filament tone are photo estimates; plastic material properties match other arms',
                          'Q is the wall-side corner on ARM1 right, now corrected to u=0,v=450',
                          'Floor tape coordinates, parking poses and marker mounting heights are photograph estimates; marker pattern/size/ID groups follow team documentation',
                          'All four perimeter panels use existing 400 mm height and remain visible'],
    'rl_ready': False,
    'limitations': 'Visual geometry draft only. No dynamics, collision, grasp, camera extrinsic or RL validation.'}


def rot(deg):
    return Rotation.from_euler('z', deg, degrees=True)


def xyz(node):
    return np.fromstring(node.get('pos', '0 0 0'), sep=' ')


def qrot(node):
    q = np.fromstring(node.get('quat', '1 0 0 0'), sep=' ')
    return Rotation.from_quat(q[[1, 2, 3, 0]])


def stl_vertices(path):
    raw = Path(path).read_bytes()
    rows = np.frombuffer(raw[84:], dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    return rows['v'].reshape(-1, 3).astype(float)


def source_geom_vertices(scene, geom):
    asset = scene.find("./asset/mesh[@name='" + geom.get('mesh') + "']")
    v = stl_vertices(asset.get('file')) * np.fromstring(asset.get('scale', '1 1 1'), sep=' ')
    return qrot(geom).apply(v) + xyz(geom)


def rename_tree(node, prefix):
    for item in node.iter():
        if item.get('name'):
            item.set('name', prefix + item.get('name'))


def freeze_arm(body, angles):
    for node in body.iter('body'):
        for joint in list(node.findall('joint')):
            name = joint.get('name').removeprefix('arm1_')
            angle = angles.get(name, 0.)
            axis = np.fromstring(joint.get('axis', '0 0 1'), sep=' ')
            turn = Rotation.from_rotvec(axis / np.linalg.norm(axis) * angle)
            R = qrot(node)
            anchor = np.fromstring(joint.get('pos', '0 0 0'), sep=' ')
            node.set('pos', numbers(xyz(node) + R.apply(anchor - turn.apply(anchor))))
            node.set('quat', numbers((R * turn).as_quat()[[3, 0, 1, 2]]))
            node.remove(joint)
        for inertial in list(node.findall('inertial')):
            node.remove(inertial)


def box(parent, name, center, size, material='workcell_white_board', **extra):
    return ET.SubElement(parent, 'geom', name=name, type='box', pos=numbers(center),
                         size=numbers(np.asarray(size) / 2), material=material,
                         contype='0', conaffinity='0', mass='0', **extra)


def add_mesh(assets, name, file, scale=.001):
    if assets.find("mesh[@name='"+name+"']") is None:
        ET.SubElement(assets, 'mesh', name=name, file=str(file), scale=numbers([scale]*3))


def mesh_geom(parent, name, mesh, pos=(0, 0, 0), yaw=0, color='.96 .96 .94 1', quat=None):
    return ET.SubElement(parent, 'geom', name=name, type='mesh', mesh=mesh, pos=numbers(pos),
                         quat=quat or yaw_quat(yaw), rgba=color, contype='0', conaffinity='0', mass='0')


def append_arm1_wrist_camera(assets, arm):
    """Reuse the two Test_IL attachments in the shared SO-101 gripper frame."""
    source=ROOT.parent/'Test_IL/assets/camera_mounts/assembly.json'
    record=json.loads(source.read_text())
    wrist=arm.find(".//body[@name='gripper_link']")
    names={'wrist_xlerobot_mount','wrist_32x32_camera'}
    attachments=[row for row in record['attachments'] if row['name'] in names]
    assert len(attachments)==2 and all(row['link']=='gripper_link' for row in attachments)
    hashes={str(source.relative_to(ROOT.parent)):hashlib.sha256(source.read_bytes()).hexdigest()}
    for row in attachments:
        path=Path(row['file']);T=np.array(row['transform_mm'])
        name='arm1_camera_mesh_'+row['name']
        add_mesh(assets,name,path)
        color='.24 .28 .31 1' if row['name']=='wrist_32x32_camera' else '.10 .11 .12 1'
        mesh_geom(wrist,row['name'],name,T[:3,3]/1000,color=color,
                  quat=numbers(Rotation.from_matrix(T[:3,:3]).as_quat()[[3,0,1,2]]))
        hashes[str(path.relative_to(ROOT.parent))]=hashlib.sha256(path.read_bytes()).hexdigest()
    optical=np.array(record['wrist_camera_transform_mm'])
    ET.SubElement(wrist,'body',name='gripper_rgb_optical_frame',pos=numbers(optical[:3,3]/1000),
                  quat=numbers(Rotation.from_matrix(optical[:3,:3]).as_quat()[[3,0,1,2]]))
    LAYOUT['arm1']['wrist_camera']={
        'module':'32x32 UVC', 'mount':'Original XLeRobot wrist bracket from Test_IL',
        'parent_body':'arm1_gripper_link', 'source_assembly':str(source),
        'source_sha256':hashes, 'attachments':attachments,
        'optical_transform_in_gripper_mm':optical.tolist(),
        'optical_status':'Existing CAD nominal frame, not measured hand-eye calibration',
        'mesh_note':'Original visual mesh retained; its simplified PCB outline is approximately 34 mm.'}


def make_scene(preserve_joints=False):
    placement = load_placement(ROOT/'data')
    if preserve_joints:placement['other_arm_dynamic']=True
    scene = ET.fromstring(configured_scene([], workcell=placement))
    world, assets = scene.find('worldbody'), scene.find('asset')
    original_arm = deepcopy(world.find("body[@name='base_link']"))
    mounts = [deepcopy(n) for n in world if n.get('name', '').startswith('overhead_')]
    for tag in ('actuator', 'sensor', 'contact', 'equality'):
        for node in list(scene.findall(tag)):
            scene.remove(node)
    if not preserve_joints:freeze_arm(world, dict(zip(JOINTS, [0, -.10, -1., .5, .05, .10])))
    for parent in scene.iter():
        for node in list(parent):
            if node.get('name') in ('preview_floor', 'observed_pallet', 'workcell_physical_floor', 'workcell_backdrop'):
                parent.remove(node)
    board = placement['board']
    assembly_size = np.array([board['size_mm'][1], board['size_mm'][0]]) / 1000
    center = np.array([1.5+assembly_size[0]/2, assembly_size[1]/2, .2024])
    rotation = rot(-90-board['yaw_deg'])
    shift = center - rotation.apply([*(np.array(board['center_xy_mm'])/1000), 0])
    assembly = ET.Element('body', name='assembly_workcell', pos=numbers(shift),
                          quat=numbers(rotation.as_quat()[[3, 0, 1, 2]]))
    for node in list(world):
        if node.tag != 'light':
            world.remove(node)
            assembly.append(node)
    world.append(assembly)
    # A single rigid transform preserves every established assembly-table placement.
    LAYOUT['assembly_size_mm'] = (assembly_size*1000).tolist()
    LAYOUT['floor_from_arm2'] = {'xyz_mm': (shift*1000).tolist(), 'yaw_deg': -90-board['yaw_deg']}
    LAYOUT['table_top_height_mm'] = 195
    # Current finished pallet, approximate pose from the supplied front view.
    add_mesh(assets, 'map_finished_pallet', ROOT/'assets/jigs/pallet.stl')
    mesh_geom(world, 'map_finished_pallet', 'map_finished_pallet', (1.61, .38, .195))

    # One continuous room slab passes beneath every map wall. The operational
    # map stays 360x180 cm; the surrounding floor is visual context only.
    box(world, 'map_floor', [1.8, .9, -.007], [12., 10., .014], 'workcell_stone')
    LAYOUT['room_floor']={'size_mm':[12000,10000],'center_mm':[1800,900,-7],
        'top_z_mm':0,'same_surface_inside_and_outside':True,
        'basis':'Illustrative room extension requested by user; map working bounds remain 3600x1800 mm.'}
    # Exact 360x180 cm clear inner dimensions, with all four perimeter walls visible.
    for name, pos, size, group in [
        ('wall_assembly', [1.8, -.0025, .2], [3.61, .005, .4], '1'),
        ('wall_storage', [3.6025, .9, .2], [.005, 1.8, .4], '1'),
        ('wall_opposite_assembly', [1.8, 1.8025, .2], [3.61, .005, .4], '1'),
        ('wall_opposite_storage', [-.0025, .9, .2], [.005, 1.8, .4], '1')]:
        box(world, name, pos, size, group=group)
    # Whole storage cell is on the RIGHT wall. A proper 180-degree rigid rotation
    # keeps its local front-view arrangement and all component handedness intact.
    storage = ET.SubElement(world, 'body', name='storage_table', pos='3.6 1.36 0',quat=yaw_quat(180))
    box(storage, 'storage_top', [.225, .225, .190], [.45, .45, .010])
    for i, (x, y, sx, sy) in enumerate(((.225,.0025,.45,.005),(.225,.4475,.45,.005),(.0025,.225,.005,.44),(.4475,.225,.005,.44))):
        box(storage, 'storage_side_'+str(i), [x,y,.0925], [sx,sy,.185])
    for i, (x,y) in enumerate(((.05,.05),(.05,.4),(.4,.05),(.4,.4))):
        mesh_geom(storage, 'storage_leg_'+str(i), 'workcell_leg_assembly', (x,y,0), color='.62 .66 .68 1')

    # Photo from assembly: ARM1 is on the far/right edge v=450, facing inward -v.
    base_geoms = original_arm.findall('geom')[:2]
    base_vertices = np.concatenate([source_geom_vertices(scene, n) for n in base_geoms])
    rv = rot(LAYOUT['arm1']['yaw_deg']).apply(base_vertices)
    arm_pos = np.array([.370-rv[:,0].max(), .450-rv[:,1].max(), .1974])
    arm1 = ET.SubElement(storage, 'body', name='arm1_frame', pos=numbers(arm_pos), quat=yaw_quat(LAYOUT['arm1']['yaw_deg']))
    append_arm1_wrist_camera(assets,original_arm)
    # Same plastic shading as other arms, with gray filament only on ARM1 prints.
    material=deepcopy(assets.find("material[@name='robot_and_camera_mount_plastic']"))
    material.set('name','arm1_gray_filament');material.set('rgba','.62 .65 .67 1');assets.append(material)
    printed={'base_motor_holder_so101_v1','base_so101_v2','motor_holder_so101_base_v1',
             'rotation_pitch_so101_v1','upper_arm_so101_v1','under_arm_so101_v1',
             'motor_holder_so101_wrist_v1','wrist_roll_pitch_so101_v2','waveshare_mounting_plate_so101_v2',
             'xlerobot_fixed_holder','xlerobot_moving_holder'}
    for geom in original_arm.iter('geom'):
        if geom.get('mesh') in printed:
            geom.attrib.pop('rgba',None);geom.set('material','arm1_gray_filament')
        elif geom.get('name')=='wrist_xlerobot_mount':
            geom.attrib.pop('rgba',None);geom.set('material','robot_and_camera_mount_plastic')
    LAYOUT['arm1']['appearance']={'printed_parts':'gray filament','rgba':[.62,.65,.67,1],
        'material_properties':'copied from robot_and_camera_mount_plastic',
        'camera_mounts':'same dark material/color as ARM2/3 mounts'}
    rename_tree(original_arm, 'arm1_')
    if not preserve_joints:freeze_arm(original_arm, dict(zip(JOINTS, [0, -.10, -1., .5, .05, .10])))
    arm1.append(original_arm)
    LAYOUT['arm1']['base_origin_storage_mm'] = (arm_pos*1000).tolist()
    LAYOUT['arm1']['base_outline_storage_mm'] = [(rv.min(0)+arm_pos)[:2].tolist(), (rv.max(0)+arm_pos)[:2].tolist()]
    LAYOUT['arm1']['base_outline_storage_mm'] = (np.array(LAYOUT['arm1']['base_outline_storage_mm'])*1000).tolist()

    # Mast is on the assembly-side/left edge v=0 and points inward +v.
    # Move the stock arm-base piece to the bottom mount's opposite socket, using
    # the existing CAD pitch. All parts retain proper rotations and original meshes.
    nominal=json.loads((ROOT/'calibration/mount_geometry/left_nominal_pose.json').read_text())
    socket_pitch=nominal['opposite_socket_pitch_mm']
    arm_base=next(n for n in mounts if n.get('name')=='overhead_arm_base')
    p=xyz(arm_base);p[1]+=socket_pitch/1000;arm_base.set('pos',numbers(p))
    LAYOUT['camera']['arm_base_opposite_socket_shift_mm']=socket_pitch
    mount_rot = rot(90)
    bottom = next(n for n in mounts if n.get('name') == 'overhead_cam_mount_bottom')
    middle = next(n for n in mounts if n.get('name') == 'overhead_cam_mount_middle')
    bottom_v = mount_rot.apply(source_geom_vertices(scene, bottom))
    mast_v = mount_rot.apply(source_geom_vertices(scene, middle))
    mount_pos = np.array([.420-mast_v[:,0].max(), -bottom_v[:,1].min(), .2024])
    mount = ET.SubElement(storage, 'body', name='storage_camera_mount', pos=numbers(mount_pos), quat=yaw_quat(90))
    for node in mounts:
        rename_tree(node, 'storage_')
        mount.append(node)
    LAYOUT['camera']['mount_origin_storage_mm'] = (mount_pos*1000).tolist()
    LAYOUT['camera']['mast_outline_storage_mm'] = ((np.array([mast_v.min(0),mast_v.max(0)])+mount_pos)[:,:2]*1000).tolist()
    install_storage_d435(scene,mount,LAYOUT,OUT,ROOT)

    reference = json.loads((ROOT/'inspection/roi_reference.json').read_text())
    fixture_spec = json.loads((ROOT/'assets/carrier_assembly/assembly.json').read_text())
    fixture_rows = {row['name']: row for row in fixture_spec['parts']}
    previous_dir=ROOT/'verification/b-jigs-20261011/before/SO101_teach/assets/carrier_assembly'
    previous_rows={row['name']:row for row in json.loads((OUT/'sources/previous-carrier-assembly.json').read_text())['parts']}
    previous_sources={}
    for key in ('b_cap','b_housing','a_housing'):
        previous=previous_rows[key]
        path=OUT/'assets'/('storage_previous_'+key+'_jig.stl')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest==previous['source_sha256']
        previous_sources[key]={'source':str(path),'source_sha256':digest,
            'version':'Immediately before the 2026-10-11 B fixture replacement',
            'backup_source':str(previous_dir/previous['source']),
            'yaw_deg':previous['yaw_deg'],'height_mm':float(np.ptp(stl_vertices(path)[:,2])),
            'count':2}
    LAYOUT['storage_previous_fixture_versions']=previous_sources
    LAYOUT['storage_lower_fixture_layout']={
        'photo':'1000015419.jpg','carrier_local_row_cy_mm':[-36,36],
        'fixture_keys_by_row':STORAGE_HOUSING_TYPES,
        'photo_positions':{'A_upper':'b_housing','A_lower':'a_housing',
                           'B_left':'b_housing','B_right':'a_housing'},
        'all_lower_fixtures_previous_version':True,
        'note':'Storage groups contain mixed lower fixtures, not exclusively A or B fixtures.'}
    LAYOUT['storage_fixture_instances']={}
    part_rows = {row['id']: row for row in reference['stations']['carrier']['rois']}
    carrier_dir = ROOT/'assets/carrier_assembly'
    add_mesh(assets, 'storage_carrier', carrier_dir/'source/current/carrier.stl')
    product_color = {'A': {'cap': '.58 .37 .78 1', 'housing': '.58 .37 .78 1', 'ball': '.28 .38 .14 1'},
                     'B': {'cap': '1 .42 .035 1', 'housing': '1 .42 .035 1', 'tpu': '.02 .49 .83 1'}}
    for group, spec in LAYOUT['jigs'].items():
        c = ET.SubElement(storage, 'body', name='storage_jig_'+group,
                          pos=numbers([*(np.array(spec['center_uv_mm'])/1000),.195]), quat=yaw_quat(spec['yaw_deg']))
        mesh_geom(c, 'storage_carrier_'+group, 'storage_carrier')
        for row, cy in enumerate((-36,36)):
            for cx, kind in zip((-72,0,72), ('cap','ball' if group=='A' else 'tpu','housing')):
                key = group.lower()+'_'+kind
                fixture_key=STORAGE_HOUSING_TYPES[group][row] if kind=='housing' else key
                fs, ps = (previous_rows[fixture_key] if fixture_key in previous_sources else fixture_rows[fixture_key]), part_rows[key]
                fixture_path=Path(previous_sources[fixture_key]['source']) if fixture_key in previous_sources else carrier_dir/fs['source']
                add_mesh(assets, 'storage_fixture_'+fixture_key, fixture_path)
                name=f'{group}_{kind}_fixture_{row}'
                mesh_geom(c, name, 'storage_fixture_'+fixture_key,
                          (cx/1000,cy/1000,.0048), yaw=fs['yaw_deg'])
                LAYOUT['storage_fixture_instances'][name]={'fixture_key':fixture_key,'group':group,
                    'row':row,'previous_version':fixture_key in previous_sources}
                label = {'cap':'상단','ball':'중단','tpu':'중단','housing':'하단'}[kind]
                add_mesh(assets, 'storage_product_'+key, ROOT.parent/f'모델링/{group} 제품/{group} {label}.stl')
                T=np.array(ps['part_transform_local_mm']);T[:3,3]+=np.array([cx,cy,4.8])-ps['fixture_center_xyz_mm']
                mesh_geom(c, f'{group}_{kind}_product_{row}', 'storage_product_'+key, T[:3,3]/1000,
                          color=product_color[group][kind], quat=numbers(Rotation.from_matrix(T[:3,:3]).as_quat()[[3,0,1,2]]))
    # Photos establish two waiting Burger robots; their precise positions are not measured.
    add_mesh(assets, 'map_mixed_carrier', carrier_dir/'carrier_assembly.stl')
    for i,park in enumerate(PARKING):
        x,y=park['center']
        carrier = ET.SubElement(world, 'body', name=f'waiting_carrier_{i}', pos=numbers([x,y,.195]),quat=yaw_quat(180))
        mesh_geom(carrier, f'waiting_carrier_mesh_{i}', 'map_mixed_carrier')
        append_carrier_platform(assets,carrier,{'platform':'TurtleBot3 Burger','platform_height_mm':195,'platform_yaw_deg':-90},i+10)
    append_photo_layout(scene,LAYOUT,OUT)
    for light in world.findall('light'):
        world.remove(light)
    ET.SubElement(world,'light',pos='1.5 .7 3.5',dir='0 0 -1',diffuse='.65 .65 .65',specular='.05 .05 .05',castshadow='true')
    scene.find('statistic').set('center','1.8 .9 .1');scene.find('statistic').set('extent','4')
    scene.find('./visual/global').set('offwidth','1920');scene.find('./visual/global').set('offheight','1080')
    scene.find('./visual/headlight').set('ambient','.55 .55 .55')
    sky=scene.find("./asset/texture[@type='skybox']")
    sky.set('rgb1','.89 .93 .95');sky.set('rgb2','.89 .93 .95')
    for g in scene.iter('geom'):
        g.set('contype','0');g.set('conaffinity','0')
    for s in scene.iter('site'):
        s.set('rgba','0 0 0 0')
    apply_finish(scene,LAYOUT,OUT,ROOT)
    ET.indent(scene,space='  ')
    ET.ElementTree(scene).write(OUT/('full-map-articulated.xml' if preserve_joints else 'full-map-preview.xml'),encoding='unicode')
    if not preserve_joints:(OUT/'layout.json').write_text(json.dumps(LAYOUT,ensure_ascii=False,indent=2)+'\n')
    return scene


def compiled_vertices(model, data, index):
    typ=model.geom_type[index]
    if typ==mujoco.mjtGeom.mjGEOM_MESH:
        m=model.geom_dataid[index];start=model.mesh_vertadr[m];count=model.mesh_vertnum[m]
        v=model.mesh_vert[start:start+count]
    elif typ==mujoco.mjtGeom.mjGEOM_BOX:
        v=np.array([[x,y,z] for x in (-1,1) for y in (-1,1) for z in (-1,1)])*model.geom_size[index]
    else:
        return None
    return v@data.geom_xmat[index].reshape(3,3).T+data.geom_xpos[index]


def geom_hull(ax,model,data,index, *,offset=(0,0),color='#d9e1e6',alpha=1,storage_front=False):
    v=compiled_vertices(model,data,index)
    if v is None:return
    xy=v[:,:2]*100-np.asarray(offset)
    if storage_front:
        uv=np.column_stack([360-v[:,0]*100,136-v[:,1]*100])
        xy=np.column_stack([uv[:,1],45-uv[:,0]])
    try:hull=ConvexHull(xy)
    except Exception:return
    ax.add_patch(Polygon(xy[hull.vertices],fc=color,ec='#556874',lw=.65,alpha=alpha))


def dimension(ax,a,b,text,off=0,color=BLUE,fontsize=10):
    a=np.array(a,float);b=np.array(b,float)
    unit=(b-a)/np.linalg.norm(b-a);normal=np.array([-unit[1],unit[0]])
    aa=a+off*normal;bb=b+off*normal
    ax.plot([a[0],aa[0]],[a[1],aa[1]],lw=.7,color=color,alpha=.5)
    ax.plot([b[0],bb[0]],[b[1],bb[1]],lw=.7,color=color,alpha=.5)
    ax.annotate('',xy=aa,xytext=bb,arrowprops=dict(arrowstyle='<->',color=color,lw=1,shrinkA=0,shrinkB=0))
    c=(aa+bb)/2
    angle=math.degrees(math.atan2(unit[1],unit[0]))
    ax.text(*c,text,ha='center',va='center',rotation=angle,fontsize=fontsize,color=color,
            bbox=dict(fc='white',ec='none',pad=1.7))


def setup_ax(ax,xlim,ylim):
    ax.set_aspect('equal');ax.set_xlim(*xlim);ax.set_ylim(*ylim);ax.axis('off')


def plan(model,data):
    fig=plt.figure(figsize=(19.2,8.8),facecolor='#f7f9fb')
    fig.text(.035,.951,'전체 맵 배치',fontsize=26,color=INK,weight='bold')
    fig.text(.035,.916,'SO-101  ·  ARM1 자재 적재 / ARM2·3 조립  ·  단위 cm',fontsize=12,color=MUTED)
    ax=fig.add_axes([.035,.09,.58,.76]);setup_ax(ax,(-30,400),(-32,204))
    ax.add_patch(Rectangle((0,0),360,180,fc='#c4cbd0',ec=INK,lw=2))
    for x in range(30,360,30):ax.plot([x,x],[0,180],color='white',lw=.8)
    for y in range(30,180,30):ax.plot([0,360],[y,y],color='white',lw=.8)
    plot_floor(ax)
    for x,y in LAYOUT['waiting_robot_centers_mm']:
        ax.add_patch(Rectangle((x/10-11,y/10-7.4),22,14.8,fc='#dce3e8',ec=MUTED,ls='--',lw=1))
    ax.text(192,143,'주차 M2 / M1',ha='center',fontsize=9,color=MUTED)
    w,h=np.array(LAYOUT['assembly_size_mm'])/10
    ax.add_patch(Rectangle((150,0),w,h,fc='white',ec=INK,lw=1.7))
    ax.add_patch(Rectangle((315,91),45,45,fc='white',ec=INK,lw=1.7))
    # Actual compiled model outlines, with no independent geometry redraw.
    for i in range(model.ngeom):
        name=model.geom(i).name
        if name.startswith(('linear_','overhead_','storage_','A_','B_')) and 'product' not in name and not name.startswith(('storage_top','storage_side','storage_leg')):
            geom_hull(ax,model,data,i,color='#cbd5dc',alpha=.95)
        if model.body(model.geom_bodyid[i]).name in ('base_link','arm3_preview_base_link','arm1_base_link') or name=='map_finished_pallet':
            geom_hull(ax,model,data,i,color='#b9cad5')
    for group,spec in LAYOUT['jigs'].items():
        x,y=np.array(spec['near_corner_uv_mm'])/10;sx,sy=np.array(spec['extent_uv_mm'])/10
        x,y=360-x-sx,136-y-sy
        ax.add_patch(Rectangle((x,y),sx,sy,fc=PURPLE if group=='A' else ORANGE,ec='white',alpha=.75,lw=.6))
        ax.text(x+sx/2,y+sy/2,group,color='white',ha='center',va='center',fontsize=10,weight='bold')
    markers=[('ARM1','arm1_base_link',BLUE),('ARM2','base_link',BLUE),('ARM3','arm3_preview_base_link',ORANGE)]
    for name,body,color in markers:
        p=data.body(body).xpos[:2]*100
        ax.add_patch(Circle(p,2.2,fc=color,ec='white',lw=.8))
        if name=='ARM1':textxy=(276,85)
        elif name=='ARM2':textxy=(244,28)
        else:textxy=(131,17)
        label='ARM1\n집게 카메라 부착' if name=='ARM1' else name
        ax.annotate(label,xy=p,xytext=textxy,fontsize=10,color=color,ha='center',arrowprops=dict(arrowstyle='-',color=color,lw=.7))
    plot_markers(ax)
    ax.text(337.5,144,'자재창고 45 × 45',ha='center',fontsize=11,weight='bold',color=INK)
    ax.text(180,62,'조립대 60 × 51.43',ha='center',fontsize=11,weight='bold',color=INK)
    dimension(ax,(0,180),(360,180),'360',off=16,fontsize=13)
    dimension(ax,(0,0),(0,180),'180',off=16,fontsize=13)
    dimension(ax,(210,0),(360,0),'150',off=-16,fontsize=11)
    dimension(ax,(360,0),(360,91),'91',off=-17,fontsize=11)
    ax.plot(360,0,'o',color=BLUE,ms=6);ax.text(362,-5,'O',va='top',ha='left',color=BLUE,fontsize=12,weight='bold')
    ax.plot([30,80],[33,33],color=INK,lw=3);ax.text(55,38,'50 cm',color=INK,fontsize=9,ha='center')
    for x in (30,80):ax.plot([x,x],[30,36],color=INK,lw=1)

    # Same world XY axes as the overall map; clockwise 90 degrees from the former inset.
    ax=fig.add_axes([.65,.12,.32,.70]);setup_ax(ax,(-13,60),(-13,58))
    ax.add_patch(Rectangle((0,0),45,45,fc='white',ec=INK,lw=2))
    ax.plot([45,45],[0,45],color=INK,lw=5)
    for i in range(model.ngeom):
        name=model.geom(i).name;body=model.body(model.geom_bodyid[i]).name
        if name.startswith(('storage_overhead_','storage_d435_')):
            geom_hull(ax,model,data,i,offset=(315,91),color='#454f5b')
        if body=='arm1_base_link':
            geom_hull(ax,model,data,i,offset=(315,91),color='#a5b6c2')
    for group,spec in LAYOUT['jigs'].items():
        u,v=np.array(spec['near_corner_uv_mm'])/10;du,dv=np.array(spec['extent_uv_mm'])/10
        x,y,sx,sy=45-u-du,45-v-dv,du,dv
        color=PURPLE if group=='A' else ORANGE
        ax.add_patch(Rectangle((x,y),sx,sy,fc='#f0e9f8' if group=='A' else '#fff0de',ec=color,lw=1.5))
        R=rot(spec['yaw_deg']);c=np.array(spec['center_uv_mm'])/10
        for cx,kind in zip((-7.2,0,7.2),('상단','중단','하단')):
            for row,cy in enumerate((-3.6,3.6)):
                uv=R.apply([cx,cy,0])[:2]+c;xy=np.array([45-uv[0],45-uv[1]])
                ax.add_patch(Rectangle(xy-3.4,6.8,6.8,fc='white',ec=color,lw=.65))
                slot_label=STORAGE_HOUSING_TYPES[group][row][0].upper()+' 하단' if kind=='하단' else kind
                ax.text(*xy,slot_label,ha='center',va='center',color=color,fontsize=8)
        ax.text(x+sx/2,y+sy+1.4,f'{group} 구역',ha='center',va='bottom',color=color,fontsize=11,weight='bold')
        qx,qy=x+sx,y
        ax.plot([45,qx],[qy,qy],color=color,ls=':',lw=.8)
        ax.plot([qx,qx],[0,qy],color=color,ls=':',lw=.8)
        ax.plot(qx,qy,'o',ms=4,color=color)
    dimension(ax,(37,0),(45,0),'8',off=-4.5,color=ORANGE,fontsize=10)
    dimension(ax,(22,0),(45,0),'23',off=-10,color=PURPLE,fontsize=10)
    dimension(ax,(45,0),(45,4),'4',off=-3.5,color=ORANGE,fontsize=9)
    dimension(ax,(45,0),(45,16),'16',off=-8.5,color=PURPLE,fontsize=10)
    dimension(ax,(0,45),(45,45),'45',off=9,fontsize=12)
    dimension(ax,(0,0),(0,45),'45',off=10,fontsize=12)
    dimension(ax,(0,0),(8,0),'8',off=-4.5,fontsize=10)
    dimension(ax,(0,42),(0,45),'3',off=5,fontsize=10)
    ax.text(10,10,'ARM1',color=BLUE,fontsize=10,ha='center',weight='bold')
    ax.annotate('상단 D435',xy=(9,41),xytext=(33,39),color=INK,fontsize=10,ha='center',
                arrowprops=dict(arrowstyle='-',color=MUTED,lw=.8))
    ax.plot(45,0,'o',color=INK,ms=5);ax.text(46,-1,'Q',ha='left',va='top',fontsize=12,color=INK,weight='bold')
    fig.text(.65,.865,'자재창고 확대',fontsize=16,color=INK,weight='bold')

    fig.savefig(OUT/'full-map-plan.png',dpi=150,facecolor=fig.get_facecolor())
    fig.savefig(OUT/'full-map-plan.svg',facecolor=fig.get_facecolor())
    plt.close(fig)


def render(model,data,filename,view,title,subtitle,hide_walls=False):
    model.vis.global_.offwidth=1920;model.vis.global_.offheight=1080
    model.vis.map.znear=.001
    camera=mujoco.MjvCamera();camera.azimuth,camera.elevation,camera.distance=view[:3];camera.lookat[:]=view[3:]
    options=mujoco.MjvOption();options.geomgroup[3]=0
    options.geomgroup[4]=1
    if hide_walls:options.geomgroup[2]=0
    with mujoco.Renderer(model,width=1920,height=1080) as renderer:
        renderer.update_scene(data,camera=camera,scene_option=options)
        from so101_teach.mujoco_finish import render_finished_frame
        image=Image.fromarray(render_finished_frame(renderer,model))
    draw=ImageDraw.Draw(image)
    draw.rectangle((0,0,1920,109),fill='#f7f9fb')
    draw.text((42,22),title,font=ImageFont.truetype(FONT,30),fill=INK)
    draw.text((42,67),subtitle,font=ImageFont.truetype(FONT,18),fill=MUTED)
    draw.rectangle((0,1032,1920,1080),fill='#f7f9fb')
    draw.text((42,1047),'배치 검토용  ·  자재창고 상단 D435 / ARM1 집게 32×32 UVC  ·  D435 틸트는 사진 추정값',font=ImageFont.truetype(FONT,17),fill=MUTED)
    image.save(OUT/filename)


def verify(model,data):
    boxgeom=model.geom('storage_top');p=data.geom('storage_top').xpos;s=boxgeom.size
    assert np.allclose(p[:2]-s[:2],[3.15,.91])
    assert np.allclose(s[:2]*2,[.45,.45])
    assert abs(p[0]+s[0]-3.6)<1e-9
    assert abs(p[2]+s[2]-.195)<1e-9
    assembly=compiled_vertices(model,data,model.geom('workcell_board_surface').id)
    assert np.allclose(assembly.min(0)[:2],[1.5,0],atol=1e-9)
    assert np.allclose(assembly.max(0)[:2],[2.1,.5142877335531805],atol=1e-9)
    assert abs(assembly[:,2].max()-.195)<1e-9
    for group,spec in LAYOUT['jigs'].items():
        pts=compiled_vertices(model,data,model.geom('storage_carrier_'+group).id)
        low=pts.min(0);high=pts.max(0)
        uv=np.array(spec['near_corner_uv_mm']);extent=np.array(spec['extent_uv_mm'])
        assert np.allclose(low[:2],np.array([3.6,1.36])-(uv+extent)/1000,atol=1e-6)
        assert np.allclose((high-low)[:2],np.array(spec['extent_uv_mm'])/1000,atol=1e-6)
        assert low[0]>=3.149999 and high[0]<=3.600001 and low[1]>=.909999 and high[1]<=1.360001
        from_Q=np.array([3.6-high[0],low[1]-.91])*1000
        assert np.allclose(from_Q,spec['offset_from_Q_uw_mm'],atol=.001)
    outline=np.array(LAYOUT['arm1']['base_outline_storage_mm'])
    assert abs(outline[1,0]-370)<1e-6 and abs(outline[1,1]-450)<1e-6
    assert abs(np.array(LAYOUT['camera']['mast_outline_storage_mm'])[1,0]-420)<1e-6
    camera_base=compiled_vertices(model,data,model.geom('storage_overhead_cam_mount_bottom').id)
    assert abs(camera_base[:,1].max()-1.36)<1e-6
    # Assembly-side photo fixes the two opposing edges; do not mirror any mesh.
    assert data.body('arm1_base_link').xpos[1]<camera_base[:,1].min()
    assert data.body('storage_jig_A').xpos[0]<data.body('storage_jig_B').xpos[0]
    for body in ('arm1_frame','storage_camera_mount','storage_jig_A','storage_jig_B'):
        assert np.isclose(np.linalg.det(data.body(body).xmat.reshape(3,3)),1)
    mount_plate=compiled_vertices(model,data,model.geom('storage_overhead_arm_base').id)
    assert mount_plate[:,0].min()>=3.149999 and mount_plate[:,0].max()<=3.600001
    # Regression for the actual user correction: assembly bottom-center,
    # storage right-center and the two measured offsets from lower-right O.
    assert p[0]>3 and .9<p[1]<1.4 and assembly[:,1].min()<1e-8
    assert np.isclose(3.6-assembly[:,0].max(),1.5)
    assert np.isclose(p[1]-s[1],.91)
    fixture_count=sum('_fixture_' in model.geom(i).name and model.geom(i).name[:2] in ('A_','B_') for i in range(model.ngeom))
    product_count=sum('_product_' in model.geom(i).name and model.geom(i).name[:2] in ('A_','B_') for i in range(model.ngeom))
    assert fixture_count==12 and product_count==12
    previous_fixture_checks={}
    for key,spec in LAYOUT['storage_previous_fixture_versions'].items():
        expected=spec['height_mm']/1000
        instances=[name for name,row in LAYOUT['storage_fixture_instances'].items() if row['fixture_key']==key]
        assert len(instances)==2
        for name in instances:
            geom=model.geom(name)
            assert model.geom_dataid[geom.id]==model.mesh('storage_fixture_'+key).id
            points=compiled_vertices(model,data,geom.id)
            assert np.isclose(np.ptp(points[:,2]),expected,atol=1e-7)
            assert np.isclose(points[:,2].min(),.1998,atol=1e-7)
        previous_fixture_checks[key]={'instances':instances,'height_mm':spec['height_mm'],
                                     'matches_previous_source_sha256':spec['source_sha256']}
    assert model.njnt==0 and model.nu==0
    # Confirm imported meshes stay attached to ARM1's gripper with source transforms.
    wrist=model.body('arm1_gripper_link').id
    R=data.xmat[wrist].reshape(3,3);origin=data.xpos[wrist]
    wrist_checks={}
    for row in LAYOUT['arm1']['wrist_camera']['attachments']:
        g=model.geom('arm1_'+row['name']).id
        assert model.geom_bodyid[g]==wrist
        T=np.array(row['transform_mm'])
        expected=stl_vertices(row['file'])@T[:3,:3].T+T[:3,3]
        actual=(compiled_vertices(model,data,g)-origin)@R*1000
        delta=float(np.max(np.abs(np.array([actual.min(0),actual.max(0)])-np.array([expected.min(0),expected.max(0)]))))
        assert delta<.001
        wrist_checks[row['name']]={'parent':'arm1_gripper_link','source_bounds_error_mm':delta}
    optical=LAYOUT['arm1']['wrist_camera']['optical_transform_in_gripper_mm']
    expected=R@np.array(optical)[:3,3]/1000+origin
    assert np.allclose(data.body('arm1_gripper_rgb_optical_frame').xpos,expected,atol=1e-9)
    aruco_checks={}
    for board in BOARDS:
        body=model.body('aruco_board_'+board['name'])
        assert np.allclose(data.body(body.id).xpos,board['center'])
        paper=model.geom('aruco_board_'+board['name']+'_paper')
        assert np.allclose(paper.size[:2]*2,[.09,.09])
        aruco_checks[board['name']]={'ids':board['ids'],'paper_mm':90,'position':board['center']}
    for color in ('white','black'):
        for line in LAYOUT['floor_tape'][color]:
            points=np.asarray(line['xy'])
            assert np.all(points>=0) and np.all(points<=np.array([3.6,1.8]))
            deltas=np.diff(points,axis=0)
            assert np.all(np.sum(np.abs(deltas)>1e-9,axis=1)==1)
    tape={r['name']:np.asarray(r['xy']) for color in ('white','black') for r in LAYOUT['floor_tape'][color]}
    assert np.isclose(tape['parking_right_outline'][:,0].mean(),2.10)
    assert np.isclose(tape['parking_right_outline'][:,0].max(),tape['mat_upper_approach'][0,0])
    assert np.isclose(tape['mat_upper_approach'][0,0],tape['mat_lower_approach'][0,0])
    assert np.isclose(tape['rest_stop'].mean(0)[1],tape['parking_right_to_rest'][-1,1])
    # Photo 1000015416: the black strip is separate from, and entirely inside,
    # all four sides of the white rectangle. Include the actual 20 mm width.
    rest_box=tape['rest_outline'];rest_black=tape['rest_stop'];half_width=.010
    assert np.allclose(rest_box[0],rest_box[-1])
    assert rest_black[0,0]+half_width<rest_box[:,0].max()-half_width
    assert rest_black[0,0]-half_width>rest_box[:,0].min()+half_width
    assert rest_black[:,1].min()>rest_box[:,1].min()+half_width
    assert rest_black[:,1].max()<rest_box[:,1].max()-half_width
    assert np.isclose(tape['parking_right_to_rest'][-1,0],rest_box[:,0].max())
    assert np.allclose(tape['assembly_stop'].mean(0),tape['assembly_inner_approach'][-1])
    assert np.isclose(tape['mat_stop'][:,1].mean(),data.body('aruco_board_MAT').xpos[1])
    assert np.allclose(tape['mat_stop_upper_tab'][:,0],tape['mat_stop_lower_tab'][:,0])
    assert np.allclose(tape['mat_upper_registration'][:,0],tape['mat_lower_registration'][:,0])
    assert np.isclose(tape['mat_upper_approach'][-1,0],tape['mat_lower_approach'][-1,0])
    assert not any(model.body(i).name=='aruco_board_REST' for i in range(model.nbody))
    assert np.isclose(3.6-data.body('aruco_board_P1').xpos[0],1.50)
    assert np.isclose(3.6-data.body('aruco_board_P2').xpos[0],1.86)
    assert np.isclose(2.10-data.body('aruco_board_ASM').xpos[0],.205)
    assert np.isclose(1.36-data.body('aruco_board_MAT').xpos[1],.300)
    gray=model.material('arm1_gray_filament').id;dark=model.material('robot_and_camera_mount_plastic').id
    assert np.allclose(model.mat_rgba[gray],LAYOUT['arm1']['appearance']['rgba'])
    for prop in ('mat_specular','mat_shininess','mat_reflectance'):
        assert np.allclose(getattr(model,prop)[gray],getattr(model,prop)[dark])
    assert model.geom('arm1_wrist_xlerobot_mount').matid[0]==dark
    assert model.geom('storage_overhead_cam_mount_bottom').matid[0]==dark
    for name in ('storage_d435_mount_base','storage_d435_tilting_mount'):
        assert model.geom(name).matid[0]==dark
    assert not any(model.geom(i).name=='storage_overhead_pcb' for i in range(model.ngeom))
    # Check both downloaded parts in their own rigid frames after compilation,
    # so mesh recentering cannot conceal scale or assembly errors.
    for part,geom,body in [('base','storage_d435_mount_base','storage_d435_cap_frame'),
                           ('tilt','storage_d435_tilting_mount','storage_d435_tilt_frame')]:
        frame=data.body(body);R=frame.xmat.reshape(3,3)
        local=(compiled_vertices(model,data,model.geom(geom).id)-frame.xpos)@R*1000
        assert np.allclose([local.min(0),local.max(0)],LAYOUT['camera']['source_bounds'][part]['source_mm'],atol=.001)
    cap=data.body('storage_d435_cap_frame');head=data.body('storage_d435_tilt_frame')
    hinge=np.array(LAYOUT['camera']['hinge_in_cap_frame_mm'])/1000
    assert np.allclose(cap.xpos+cap.xmat.reshape(3,3)@hinge,head.xpos+head.xmat.reshape(3,3)@hinge)
    camera=data.body('storage_d435_camera');axis=camera.xmat.reshape(3,3)[:,0]
    assert np.isclose(np.degrees(np.arccos(-axis[2])),20.)
    assert axis[1]<0 # From the mast toward the work area / ARM1 side.
    rear=np.array(LAYOUT['camera']['camera_rear_point_source_mm'])/1000
    seat=np.array(LAYOUT['camera']['camera_rear_seat_in_untilted_cap_frame_mm'])/1000
    assert np.allclose(camera.xpos+camera.xmat.reshape(3,3)@rear,head.xpos+head.xmat.reshape(3,3)@seat)
    stock_R=data.body('storage_camera_mount').xmat.reshape(3,3)
    # Original top's +X extension must now face away from the camera.
    top=model.geom('storage_overhead_cam_mount_top').id
    top_vertices=(compiled_vertices(model,data,top)-data.body('storage_camera_mount').xpos)@stock_R
    top_center=np.array([-.002275058471081998,.1160326636358576,.1807])
    assert top_vertices[:,0].min()<top_center[0]-.045
    d435_checks={'downloaded_dimensions_preserved':True,'tilt_pivot_coincident':True,
                  'camera_rear_seated_on_tilting_plate':True,'stock_top_reversed':True,
                  'optical_axis_from_down_deg':float(np.degrees(np.arccos(-axis[2]))),
                  'optical_axis_world':axis.tolist(),'angle_is_photo_estimate':True}
    report={'passed':True,'table_and_jig_dimension_checks':True,'table_wall_contact':True,
            'arm1_edge_gap_mm':80,'mast_edge_gap_mm':30,'storage_fixture_count':fixture_count,
            'storage_product_count':product_count,'static_scene_no_actuators':True,
            'physics_steps_executed':0,'geoms':model.ngeom,'arm1_wrist_camera':wrist_checks,
            'floor_tape':{'white_polylines':len(LAYOUT['floor_tape']['white']),
                          'black_polylines':len(LAYOUT['floor_tape']['black']),'within_map':True,
                          'all_segments_axis_aligned':True,'paired_endpoints_and_stop_centers_aligned':True},
            'rest_stop_inside_closed_white_box':True,
            'aruco_boards':aruco_checks,'rest_has_no_aruco':True,
            'user_measured_marker_centers_verified':True,'arm1_gray_same_plastic_properties':True,
            'storage_d435':d435_checks,
            'storage_previous_fixtures':previous_fixture_checks,
            'storage_mixed_lower_fixture_positions':LAYOUT['storage_lower_fixture_layout']['photo_positions'],
            'storage_orientation':{'photo':'1000015414.jpg + user clipboard photo',
                'whole_map':{'assembly':'bottom center','storage':'right middle','waiting_robots':'top wall'},
                'datum_O_world_mm':[3600,0,0],'assembly_distance_from_O_mm':1500,'storage_distance_from_O_mm':910,
                'mast':'front-view left / map upper side','arm1':'front-view right / map lower side',
                'A':'front / map left','B':'rear / map right','photo_or_mesh_mirrored':False,
                'jig_offsets_from_corrected_Q_verified':True},
            'live_calibration_and_teaching_changed':False,
            'sources_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [ROOT/'data/workcell-preview.json',ROOT/'assets/so101/inspection_scene.xml',ROOT/'assets/carrier_assembly/assembly.json']}}
    (OUT/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    old=os.getcwd();os.chdir(OUT)
    log=OUT/'MUJOCO_LOG.TXT';had_log=log.exists()
    try:
        make_scene()
        from so101_teach.mujoco_finish import append_mujoco_finish
        from so101_teach.preview_materials import apply_preview_materials
        display_scene=ET.parse(OUT/'full-map-preview.xml').getroot()
        append_mujoco_finish(display_scene)
        model=mujoco.MjModel.from_xml_string(ET.tostring(display_scene,encoding='unicode'));data=mujoco.MjData(model)
        apply_preview_materials(model)
        mujoco.mj_forward(model,data)
        report=verify(model,data)
        plan(model,data)
        draw_board_sheet(OUT)
        render(model,data,'full-map-3d.png',(90,-63,4.2,1.8,.9,.14),'전체 맵 · 이어진 바닥과 천장 패널 조명',
               '사방 벽 유지 · 고르게 분산된 패널 조명과 옅은 그림자 · 부품과 ARM1 색상 완화')
        render(model,data,'rest-floor-detail.png',(90,-90,.60,1.58,1.10,.005),'왼쪽 바닥 표식 · 위에서 본 모습',
               '흰 사각 테두리 네 면 유지 · 검은선은 안쪽 배치 · 오른쪽 흰 경로는 박스 테두리에 연결')
        render(model,data,'storage-detail-3d.png',(25,-42,1.33,3.375,1.13,.40),'자재창고 · 사진에 맞춘 밝은 부품과 ARM1',
               '고르게 분산된 패널 조명 · 아주 옅은 그림자 · 출력물의 무광 질감 유지')
        render(model,data,'turtlebot-detail-3d.png',(105,-20,.58,2.10,1.60,.12),'TurtleBot3 Burger · 조립용 로봇팔과 같은 프레임 색상',
               '차체: ARM2·3과 같은 회색 · 타이어: 검정 고무 · 보드: 어두운 회녹색 · 지지대: 은색')
        product_groups=model.geom_group.copy()
        for i in range(model.ngeom):
            name=model.geom(i).name;body=model.body(model.geom_bodyid[i]).name
            if '_product_' in name or body.startswith('arm1_') or name.startswith(('storage_overhead_','storage_d435_')):
                model.geom_group[i]=3
        render(model,data,'storage-jigs-detail-3d.png',(25,-52,.85,3.375,1.13,.23),'자재창고 지그 · 제품·로봇팔·기둥을 숨긴 확인 화면',
               'A/B 구역의 하단 지그는 A형·B형 혼합  |  하단 지그 4개 모두 이전 버전 · 높이 8.5 mm')
        render(model,data,'storage-jigs-photo-view.png',(90,-90,.75,3.375,1.135,.195),'자재창고 지그 · 첨부 사진과 같은 방향',
               'A 구역: 위 B형 / 아래 A형  |  B 구역: 왼쪽 B형 / 오른쪽 A형  |  제품·로봇팔·기둥은 확인을 위해 숨김')
        model.geom_group[:]=product_groups
        focus=data.body('storage_d435_cap_frame').xpos+np.array([0,-.012,.008])
        saved_groups=model.geom_group.copy()
        for i in range(model.ngeom):
            name=model.geom(i).name
            if not name.startswith(('storage_d435_','photo_d435_')) and name not in ('storage_overhead_cam_mount_top','storage_overhead_cam_mount_middle'):
                model.geom_group[i]=3
        render(model,data,'d435-mount-side-3d.png',(180,-5,.24,*focus),'D435 장착부 · 측면',
               '기존 최상단 부품 180° 회전 → 마운트 베이스 → 틸팅 마운트 → D435  |  수직 아래 기준 약 20°')
        render(model,data,'d435-mount-detail-3d.png',(245,-35,.24,*focus),'D435 장착부 · 결합 상태',
               '다운로드 STL 원본 치수 유지  |  마운트 재질·색상은 다른 로봇팔과 동일  |  각도는 사진 기반 추정')
        model.geom_group[:]=saved_groups
        render(model,data,'assembly-view-3d.png',(45,-40,3.7,2.05,.95,.15),'조립대에서 자재창고를 바라본 방향',
               '첨부 사진 1000015414.jpg 기준  |  자재창고 왼쪽 카메라 기둥 / 오른쪽 ARM1')
        focus=(data.body('arm1_gripper_link').xpos+data.body('arm1_gripper_rgb_optical_frame').xpos)/2
        saved_colors=model.geom_rgba.copy()
        bodies={model.body('arm1_base_link').id}
        for i in range(model.nbody):
            if model.body_parentid[i] in bodies:bodies.add(i)
        for i in range(model.ngeom):
            if model.geom_bodyid[i] not in bodies:model.geom_rgba[i,3]=0
        render(model,data,'arm1-wrist-camera-3d.png',(298,-25,.35,*focus),'ARM1 집게 카메라 · 장착부 확대',
               'Test_IL의 32×32 UVC 모듈·XLeRobot 마운트와 동일한 형상 및 조립 좌표')
        model.geom_rgba[:]=saved_colors
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        if log.exists() and not had_log:
            if log.stat().st_size:print(log.read_text()[-2500:])
            log.unlink()
        os.chdir(old)


if __name__=='__main__':main()
