"""Approved room geometry for the desktop preview; no control-frame changes."""
from copy import deepcopy
from pathlib import Path
from functools import lru_cache
import json
import math
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from .domain import ROOT

ASSET_DIR=ROOT/'assets/full_map'


def map_from_preview(placement):
    """Rigid display transform only. Saved teaching coordinates remain arm-local."""
    board=placement['board']
    rotation=Rotation.from_euler('z',-90-float(board['yaw_deg']),degrees=True)
    size=np.asarray(board['size_mm'],float)/1000
    target=np.array([1.5+size[1]/2,size[0]/2,.195])
    source=np.r_[np.asarray(board['center_xy_mm'],float)/1000,float(board['top_z_mm'])/1000]
    return rotation,target-rotation.apply(source)


def overview_view(placement):
    rotation,shift=map_from_preview(placement)
    center=rotation.inv().apply(np.array([1.8,.9,.14])-shift)
    yaw=rotation.as_euler('xyz',degrees=True)[2]
    return (90.-yaw,-63.,4.2,*center)


@lru_cache(maxsize=2)
def _source(path,mtime):
    source=ET.parse(path).getroot()
    for node in source.iter():
        filename=node.get('file')
        if filename:node.set('file',str((Path(path).parent/filename).resolve()))
    return source


def append_full_map(scene,placement,*,mujoco_display=True):
    if not placement or not placement.get('board'):return
    path=ASSET_DIR/'full-map-preview.xml'
    source=_source(str(path),path.stat().st_mtime_ns)
    world=scene.find('worldbody');assets=scene.find('asset')
    rotation,shift=map_from_preview(placement);inverse=rotation.inv()
    root=ET.SubElement(world,'body',name='full_map_environment',
        pos=' '.join(map(str,inverse.apply(-shift))),
        quat=' '.join(map(str,inverse.as_quat()[[3,0,1,2]])))
    # Do not re-import the frozen assembly arms: the app already owns live
    # active/other-arm joints, carriage state, mocap jigs and their exact frames.
    # The registered pallet also owns its saved/detected pose (or invisibility).
    for child in source.find('worldbody'):
        if child.get('name') in ('assembly_workcell','map_finished_pallet') or child.tag=='light':continue
        node=deepcopy(child)
        for geom in node.iter('geom'):
            if geom.get('name','').startswith('wall_'):geom.set('group','1')
            elif geom.get('group')=='2':geom.set('group','3')
        root.append(node)
    for item in source.find('asset'):
        name=item.get('name')
        if not name:continue
        old=assets.find(f"{item.tag}[@name='{name}']")
        if old is None:assets.append(deepcopy(item))
        elif item.tag=='material':old.attrib.update(item.attrib)
    # Replace the small old floor/backdrop only in the display build. Keeping
    # the legacy preview_floor body lets the worker retain its tabletop update.
    for geom in scene.iter('geom'):
        name=geom.get('name','');mesh=geom.get('mesh','')
        if name in ('preview_floor','workcell_physical_floor','workcell_backdrop'):
            geom.set('group','3')
        if mesh.startswith('sts3215'):
            geom.attrib.pop('rgba',None);geom.set('material','photo_servo')
        elif 'xlerobot' in mesh and 'finger' in mesh:
            geom.attrib.pop('rgba',None);geom.set('material','photo_blue_tpu')
    # Reuse the new assembly-camera fixing screws without its static arm tree.
    original_camera=source.find(".//body[@name='overhead_camera_pcb']")
    active_camera=scene.find(".//body[@name='overhead_camera_pcb']")
    if original_camera is not None and active_camera is not None:
        for child in original_camera.findall('body'):
            if child.get('name','').startswith('photo_'):active_camera.append(deepcopy(child))
    for light in list(world.findall('light')):world.remove(light)
    for original in source.find('worldbody').findall('light'):
        light=deepcopy(original)
        light.set('pos',' '.join(map(str,inverse.apply(np.fromstring(light.get('pos'),sep=' ')-shift))))
        light.set('dir',' '.join(map(str,inverse.apply(np.fromstring(light.get('dir'),sep=' ')))))
        world.append(light)
    scene.find('./visual/headlight').attrib.update(source.find('./visual/headlight').attrib)
    scene.find('./visual/quality').attrib.update(source.find('./visual/quality').attrib)
    existing=scene.find('./visual/map')
    if existing is None:existing=ET.SubElement(scene.find('visual'),'map')
    existing.set('shadowscale','.7')

    from .preview_materials import apply_colors, append_robot_details
    apply_colors(scene)
    append_robot_details(scene, active_arm=placement.get('active_arm_id','arm2'))
    if mujoco_display:
        from .mujoco_finish import append_mujoco_finish
        append_mujoco_finish(scene,root)
