"""Display-only procedural materials and shared photo-matched scene appearance."""
from functools import lru_cache
import numpy as np

@lru_cache(maxsize=2)
def oak_texture(width,height):
    # Seamless, matte light oak boards. Grain varies along each board, not per frame.
    u=np.arange(width,dtype=float)[None,:]/width;v=np.arange(height,dtype=float)[:,None]/height
    board=np.minimum((u*6).astype(int),5);across=u*6-board
    phases=np.array([.2,1.6,4.1,2.7,5.2,.9])[board]
    wave=across+.045*np.sin(2*np.pi*v+phases)+.018*np.sin(6*np.pi*v+phases)
    grain=np.sin(2*np.pi*(18*wave+.23*np.sin(2*np.pi*v+phases)))
    fine=np.sin(2*np.pi*(49*wave+.4*np.sin(4*np.pi*v+phases)))
    variation=np.array([2,-4,5,-1,3,-3])[board]
    shade=variation+3.5*grain+1.2*fine-5*np.maximum(0,grain)**10
    # Subtle plank edges; avoid dark stripes that compete with white jigs.
    shade-=np.where((across<.012)|(across>.988),10.,0.)
    rgb=np.array([215.,191.,157.])[None,None,:]+shade[:,:,None]*np.array([1.,1.05,.95])
    return np.clip(rgb,0,255).astype(np.uint8)

def apply_preview_materials(model):
    # The full room uses a separate source texture. Soften its contrast only in
    # the MuJoCo display model; the original file remains the Isaac reference.
    try:
        model.material('mujoco_ceiling_panel')
        floor=model.material('workcell_stone').id
    except KeyError:pass
    else:
        import mujoco
        index=int(model.mat_texid[floor,int(mujoco.mjtTextureRole.mjTEXROLE_RGB)])
        if index>=0:
            start=int(model.tex_adr[index]);w=int(model.tex_width[index]);h=int(model.tex_height[index]);c=int(model.tex_nchannel[index])
            pixels=model.tex_data[start:start+w*h*c].reshape(h,w,c)
            rgb=pixels[...,:3].astype(float)
            contrast=profile()['mujoco']['floor_texture_contrast']
            pixels[...,:3]=np.clip(rgb*contrast+rgb.mean(axis=(0,1))*(1-contrast),0,255).astype(np.uint8)
    try:index=model.texture('floor').id
    except KeyError:return
    w,h=int(model.tex_width[index]),int(model.tex_height[index]);channels=int(model.tex_nchannel[index]);start=int(model.tex_adr[index])
    if channels!=3:raise ValueError('미리보기 바닥 텍스처는 RGB여야 합니다.')
    model.tex_data[start:start+w*h*channels]=oak_texture(w,h).reshape(-1)


from copy import deepcopy
from fnmatch import fnmatchcase
import json
import xml.etree.ElementTree as ET
from .domain import ROOT

ASSETS = ROOT / 'assets/full_map'


@lru_cache(maxsize=2)
def _read_profile(path, modified):
    with open(path,encoding='utf-8') as source:
        return json.load(source)


def profile():
    path = ASSETS / 'photo-match.json'
    return deepcopy(_read_profile(str(path), path.stat().st_mtime_ns))


def numbers(values):
    return ' '.join(str(float(v)) for v in values)


def material(assets, name, spec):
    node = assets.find(f"material[@name='{name}']")
    if node is None:
        node = ET.SubElement(assets, 'material', name=name)
    node.set('rgba', numbers([*spec.get('color_srgb', [.5]*3), 1]))
    node.set('shininess', str(max(.02, (1-spec.get('roughness', .8))*.45)))
    node.set('specular', str(spec.get('specular', .08)))
    node.set('reflectance', '0')
    if spec.get('texture'):
        texname = 'matched_texture_'+name
        if assets.find(f"texture[@name='{texname}']") is None:
            ET.SubElement(assets, 'texture', name=texname, type='2d',
                          file=str((ASSETS/spec['texture']).resolve()))
        node.set('texture', texname)
        node.set('texrepeat', numbers([spec.get('texture_scale', 1)]*2))
        node.set('texuniform', 'true' if name == 'workcell_stone' else 'false')
    return node


def apply_colors(scene, appearance=None):
    """Update optical attributes only; no joint, body, or collision attributes."""
    spec = profile()
    assets = scene.find('asset')
    for name, override in spec['surfaces'].items():
        existing = (appearance or {}).get('surfaces', {}).get(name, {})
        merged = {**existing, **override}
        material(assets, name, merged)
        if appearance is not None:
            appearance['surfaces'][name] = merged
    for geom in scene.iter('geom'):
        name = geom.get('name', '')
        for index, rule in enumerate(spec['static_appearance_rules']):
            if not fnmatchcase(name, rule['pattern']):
                continue
            current = assets.find(f"material[@name='{geom.get('material')}']")
            color = np.fromstring(geom.get('rgba', current.get('rgba', '.5 .5 .5 1')
                if current is not None else '.5 .5 .5 1'), sep=' ')[:3].tolist()
            surface = {'color_srgb': rule.get('color', color),
                       **rule.get('surface', {})}
            key = f'photo_rule_{index}_{name}'
            material(assets, key, surface)
            geom.attrib.pop('rgba', None)
            geom.set('material', key)
            if appearance is not None:
                appearance['surfaces'][key] = surface
    if appearance is not None:
        appearance['lighting'].update(spec['lighting'])
        appearance['photo_match_revision'] = spec['material_revision']
        appearance['printed_layer_height_mm'] = spec['printed_layer_height_mm']


def append_robot_details(scene, active_arm='arm2', appearance=None):
    """Link-local wiring follows the active and other arms, with zero mass."""
    from scipy.interpolate import make_interp_spline
    spec = profile()
    assets = scene.find('asset')
    for color, rgb in spec['wire_colors'].items():
        surface = {'color_srgb': rgb, 'roughness': .52}
        material(assets, 'matched_wire_'+color, surface)
        if appearance is not None:
            appearance['surfaces']['matched_wire_'+color] = surface
    surface = {'color_srgb': [.62, .64, .62], 'metallic': .85, 'roughness': .32}
    material(assets, 'matched_joint_head', surface)
    if appearance is not None:
        appearance['surfaces']['matched_joint_head'] = surface
    robots = [('', active_arm), ('arm3_preview_', 'arm2' if active_arm == 'arm3' else 'arm3'),
              ('arm1_', 'arm1')]
    for prefix, robot in robots:
        for link, points in spec['robot_wires'].items():
            body = scene.find(f".//body[@name='{prefix+link}']")
            if body is None or body.find("geom[@name='"+prefix+link+"_matched_wire_red_0']") is not None:
                continue
            route = np.asarray(points, float)
            if robot == 'arm3' and link in ('upper_arm_link', 'lower_arm_link'):
                route[:, 2] = -.0147
            for index, color in enumerate(spec['wire_colors']):
                adjusted = route.copy()
                adjusted[:, 2] += (index-1)*.00125
                smooth = make_interp_spline(np.arange(len(route)), adjusted, k=2)(
                    np.linspace(0, len(route)-1, 32))
                for segment, (a, b) in enumerate(zip(smooth[:-1], smooth[1:])):
                    ET.SubElement(body, 'geom', name=f'{prefix+link}_matched_wire_{color}_{segment}',
                        type='capsule', fromto=numbers([*a, *b]), size='.00053',
                        material='matched_wire_'+color, contype='0', conaffinity='0', mass='0')
        for link, z in [('upper_arm_link', -.0138), ('lower_arm_link', -.0123),
                        ('wrist_link', -.0093)]:
            body = scene.find(f".//body[@name='{prefix+link}']")
            name = prefix+link+'_matched_joint_head'
            if body is not None and body.find(f"geom[@name='{name}']") is None:
                ET.SubElement(body, 'geom', name=name, type='cylinder',
                    pos=numbers([0, 0, z]), size='.0023 .0003',
                    material='matched_joint_head', contype='0', conaffinity='0', mass='0')


def append_background(scene, placement, appearance):
    """Promote the verified wall-side accessories without adding collisions."""
    from scipy.spatial import ConvexHull
    from .full_map import map_from_preview
    spec = profile()
    world = scene.find('worldbody')
    if world.find("body[@name='photo_matched_background']") is not None:
        return
    rotation, shift = map_from_preview(placement)
    parent = ET.SubElement(world, 'body', name='photo_matched_background',
        pos=numbers(shift), quat=numbers(rotation.as_quat()[[3, 0, 1, 2]]))
    assets = scene.find('asset')
    for row in spec['background_details']:
        name = 'shared_'+row['name']
        surface = {**row['surface'], 'color_srgb': row['color']}
        material(assets, name, surface)
        appearance['surfaces'][name] = surface
        attrs = dict(name=name+'_mesh', file=str((ASSETS/row['mesh']).resolve()))
        if surface.get('texture'):
            # A ten-micron visual prism gives MuJoCo a valid 3-D hull.
            raw = (ASSETS/row['mesh']).read_bytes()
            count = int.from_bytes(raw[80:84], 'little')
            if len(raw) != 84+count*50:
                raise ValueError('Expected a preserved binary label STL')
            triangles = np.frombuffer(raw, offset=84, count=count,
                dtype=[('normal','<f4',(3,)),('vertices','<f4',(3,3)),('attribute','<u2')])
            xyz = np.unique(triangles['vertices'].reshape(-1,3),axis=0)
            vertices = np.r_[xyz, xyz-[0, 0, .00001]]
            faces = ConvexHull(vertices).simplices.copy()
            for face in faces:
                a,b,c=vertices[face]
                if np.dot(np.cross(b-a,c-a),(a+b+c)/3-vertices.mean(0))<0:
                    face[1],face[2]=face[2],face[1]
            frame = np.asarray(surface['world_from_texture'])
            uv = ((vertices-frame[:3, 3])@frame[:3, :3])[:, :2]*surface['texrepeat']
            attrs = dict(name=name+'_mesh', vertex=numbers(vertices.ravel()),
                         face=' '.join(map(str, faces.ravel())),
                         texcoord=numbers(uv.ravel()))
        ET.SubElement(assets, 'mesh', inertia='shell', **attrs)
        ET.SubElement(parent, 'geom', name=name, type='mesh', mesh=name+'_mesh',
            material=name, contype='0', conaffinity='0', mass='0')
