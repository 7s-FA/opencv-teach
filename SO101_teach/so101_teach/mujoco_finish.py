"""MuJoCo-only room finish; shared geometry and Isaac materials stay untouched."""
import xml.etree.ElementTree as ET
from .domain import ROOT


def append_mujoco_finish(scene, room=None):
    """Apply to a display scene, with panels in the room's local map frame.

    Classic OpenGL provides one planar reflection, not rough PBR reflections.
    A Gaussian-blurred rectangular emitter approximates their soft outline; it is reflected by the
    renderer at each viewpoint, never painted into the floor's color texture.
    Downward single-sided meshes keep the overhead overview unobstructed.
    """
    assets=scene.find('asset')
    if assets.find("material[@name='mujoco_ceiling_panel']") is not None:return
    if room is None:room=scene.find('worldbody')
    floor=assets.find("material[@name='workcell_stone']")
    if floor is None:return
    floor.attrib.update(rgba='.62 .62 .62 1',specular='.25',shininess='.22',reflectance='.035')
    ET.SubElement(assets,'texture',name='mujoco_panel_texture',type='2d',
                  file=str(ROOT/'assets/textures/mujoco-ceiling-panel.png'))
    ET.SubElement(assets,'material',name='mujoco_ceiling_panel',texture='mujoco_panel_texture',
                  rgba='1 1 1 1',emission='6',specular='0',shininess='0',reflectance='0')
    ET.SubElement(assets,'mesh',name='mujoco_panel_face',inertia='shell',
                  vertex='-.55 -1.10 0 -.55 1.10 0 .55 1.10 0 .55 -1.10 0',
                  face='0 1 2 0 2 3',texcoord='0 0 0 1 1 1 1 0')
    import json
    panels=json.loads((ROOT/'assets/full_map/appearance.json').read_text())['lighting']['panels']
    for panel in panels:
        ET.SubElement(room,'geom',name='mujoco_'+panel['name'],type='mesh',mesh='mujoco_panel_face',
                      material='mujoco_ceiling_panel',pos=' '.join(map(str,panel['center_m'])),
                      contype='0',conaffinity='0',mass='0',group='1')
    for light in scene.find('worldbody').findall('light'):
        level=.045 if light.get('castshadow')=='true' else .14
        light.set('diffuse',f'{level} {level*1.02} {level*1.045}')
        light.set('specular','.14 .14 .14')
    head=scene.find('./visual/headlight')
    head.attrib.update(ambient='.32 .33 .34',diffuse='.06 .06 .06',specular='.035 .035 .035')
    quality=scene.find('./visual/quality')
    quality.attrib.update(numquads='32',offsamples='4',shadowsize='4096')


def prepare_finish_scene(scene, model):
    """Exclude visual emitters from shadow casting; retain native reflections."""
    import mujoco
    try:panel=model.material('mujoco_ceiling_panel').id
    except KeyError:return
    scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION]=True
    for geom in scene.geoms[:scene.ngeom]:
        if geom.matid==panel:geom.category=mujoco.mjtCatBit.mjCAT_DECOR


def render_finished_frame(renderer, model):
    """Reflect ceiling emitters only, on pixels belonging to the visible floor.

    Classic MuJoCo cannot exclude individual objects from its mirror pass. A
    separate emitter pass avoids sharp wall reflections/occlusion, while the
    normal scene preserves walls, objects and their shadows. Segmentation keeps
    the added radiance off objects, tape and the background. All temporary
    changes affect MjvScene only and are restored before overlays or the next
    frame; the physical model, camera and original texture remain untouched.
    """
    import mujoco
    import cv2
    import numpy as np
    try:
        panel_id=model.material('mujoco_ceiling_panel').id
        floor_id=model.geom('map_floor').id
    except KeyError:return renderer.render()
    scene=renderer.scene
    prepare_finish_scene(scene,model)
    geoms=list(scene.geoms[:scene.ngeom])
    floor=next((g for g in geoms if g.objtype==mujoco.mjtObj.mjOBJ_GEOM and g.objid==floor_id),None)
    if floor is None or floor.rgba[3]<=0:return renderer.render()
    flags=scene.flags.copy()
    saved=[(g,g.rgba.copy(),g.reflectance,g.specular,g.emission,g.segid) for g in geoms]
    try:
        # The base image contains the real scene, without any mirror silhouettes.
        for g in geoms:g.reflectance=0
        scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION]=False
        base=renderer.render()
        # Only floor versus occluder is needed. Small binary IDs also avoid
        # multi-byte ID-color edge artifacts in multisampled render buffers.
        # Keep labels in one byte and far apart: mixed edge pixels must not
        # become a floor label and add a bright fringe to an object's outline.
        occluder_id=min(len(geoms)-1,254)
        for g in geoms:g.segid=-1 if g.rgba[3]<=0 else 0 if g is floor else occluder_id
        # We only need the binary floor label, not an object-ID/type image.
        # Read the same ID-color pass directly and avoid remapping every pixel
        # through the renderer's full int32 segmentation table on every frame.
        scene.flags[mujoco.mjtRndFlag.mjRND_SEGMENT]=True
        scene.flags[mujoco.mjtRndFlag.mjRND_IDCOLOR]=True
        try:ids=renderer.render()
        finally:
            for flag in (mujoco.mjtRndFlag.mjRND_SEGMENT,mujoco.mjtRndFlag.mjRND_IDCOLOR):
                scene.flags[flag]=flags[flag]
        visible_floor=cv2.inRange(ids,(1,0,0),(1,0,0))  # segid 0 is encoded as RGB (1, 0, 0)
        if not visible_floor.any():return base
        # A black floor contributes no base color in this pass. Its native
        # reflection projects the feathered ceiling emitters for this camera.
        for g in geoms:
            if g is not floor and g.matid!=panel_id:g.rgba[3]=0
        floor.rgba[:3]=0;floor.specular=0;floor.emission=0
        floor.reflectance=next(row[2] for row in saved if row[0] is floor)
        scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION]=True
        for flag in (mujoco.mjtRndFlag.mjRND_SHADOW,mujoco.mjtRndFlag.mjRND_SKYBOX,mujoco.mjtRndFlag.mjRND_FOG):
            scene.flags[flag]=False
        reflected=renderer.render()
        cv2.add(base,reflected,dst=base,mask=visible_floor)
        return base
    finally:
        for g,rgba,reflectance,specular,emission,segid in saved:
            g.rgba[:]=rgba;g.reflectance=reflectance;g.specular=specular;g.emission=emission;g.segid=segid
        scene.flags[:]=flags
