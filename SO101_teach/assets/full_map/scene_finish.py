"""Photo-referenced display materials and explicit, non-physical fasteners."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from PIL import Image


def numbers(values):return ' '.join(str(float(v)) for v in values)


def apply_finish(scene, layout, out, root):
    from so101_teach.workcell_scene import mount_screw, silver_metal_material, BURGER_FINISH
    assets=scene.find('asset');world=scene.find('worldbody');out=Path(out)
    texture_dir=out/'textures';texture_dir.mkdir(exist_ok=True)
    # Subtle, deterministic printing striations. This is a procedural surface,
    # not a texture extracted from a photographed object's baked illumination.
    yy,xx=np.mgrid[:512,:512];rng=np.random.default_rng(101)
    filament=np.clip(250+2*np.sin(2*np.pi*yy/8)+rng.normal(0,.65,(512,512)),0,255).astype(np.uint8)
    Image.fromarray(np.repeat(filament[...,None],3,axis=2)).save(texture_dir/'filament-microcolor.png')
    nx=np.zeros((512,512));ny=.055*np.cos(2*np.pi*yy/8);nz=np.sqrt(1-ny*ny)
    normal=np.stack([nx,ny,nz],axis=-1)
    Image.fromarray(np.clip((normal*.5+.5)*255,0,255).astype(np.uint8)).save(texture_dir/'filament-normal.png')
    ET.SubElement(assets,'texture',name='photo_filament_texture',type='2d',file=str(texture_dir/'filament-microcolor.png'))
    surfaces={}
    def material(name,rgb,roughness=.75,metallic=0,specular=.15,texture=None):
        m=assets.find(f"material[@name='{name}']")
        if m is None:m=ET.SubElement(assets,'material',name=name)
        m.set('rgba',numbers([*rgb,1]));m.set('specular',str(specular))
        m.set('shininess',str(max(.02,(1-roughness)*.45)));m.set('reflectance','0');m.set('emission','0')
        if texture:
            m.set('texture',texture);m.set('texuniform','true');m.set('texrepeat','6 6')
        surfaces[name]={'color_srgb':rgb,'roughness':roughness,'metallic':metallic,'specular':specular}
        if texture:surfaces[name].update(normal_texture='textures/filament-normal.png',normal_strength=.055,layer_appearance='fine printed plastic')
        return name
    material('robot_and_camera_mount_plastic',[.18,.20,.215],.94,specular=.035,texture='photo_filament_texture')
    material('arm1_gray_filament',[.68,.70,.71],.94,specular=.035,texture='photo_filament_texture')
    material('workcell_white_board',[.88,.885,.875],.97,specular=.02)
    material('workcell_white_pla',[.91,.915,.90],.94,specular=.035,texture='photo_filament_texture')
    material('gripper_rubber',[.025,.025,.025],.90,specular=.05)
    material('photo_servo',[.095,.105,.115],.90,specular=.04)
    material('photo_blue_tpu',[.10,.57,.84],.95,specular=.03,texture='photo_filament_texture')
    material('photo_purple_pla',[.58,.40,.76],.94,specular=.035,texture='photo_filament_texture')
    material('photo_orange_pla',[1.0,.52,.075],.94,specular=.035,texture='photo_filament_texture')
    material('photo_green_ball',[.28,.34,.15],.90,specular=.04)
    material('photo_d435_aluminum',[.68,.70,.70],.58,metallic=.65,specular=.3)
    silver=silver_metal_material(assets);material(silver,[.64,.67,.69],.42,metallic=.8,specular=.4)
    floor=assets.find("material[@name='workcell_stone']")
    floor.set('rgba','.72 .72 .72 1');floor.set('specular','.48');floor.set('shininess','.16')
    surfaces['workcell_stone']={'color_srgb':[.72,.72,.72],'roughness':.42,'metallic':0,'specular':.48,
        'texture':str(root/'assets/textures/grey-stone-tiles.png'),'texture_scale':2,
        'source':'Existing real-stone reference texture; visible broad sheen without mirror reflection. Updated 2026-10-12.'}
    for name,spec in BURGER_FINISH.items():
        surfaces[name]={**spec,'source':'https://www.robotis.com/shop/item.php?it_id=901-0118-201',
            'basis':'User selected ARM2/3 frame color, with separate dark rubber, green PCB and metallic supports; manufacturer photographs are the component reference, not measured material values.'}
    for g in scene.iter('geom'):
        name=g.get('name','');mesh=g.get('mesh','');m=None
        if mesh.startswith('sts3215'):m='photo_servo'
        elif 'xlerobot' in mesh and 'finger' in mesh:m='photo_blue_tpu'
        elif '_fixture_' in name or name.startswith('storage_carrier_'):m='workcell_white_pla'
        elif name.startswith('A_') and '_product_' in name:m='photo_green_ball' if '_ball_' in name else 'photo_purple_pla'
        elif name.startswith('B_') and '_product_' in name:m='photo_blue_tpu' if '_tpu_' in name else 'photo_orange_pla'
        elif name.startswith('storage_d435_body_'):m='photo_d435_aluminum'
        if m:g.attrib.pop('rgba',None);g.set('material',m)
    records=[]
    # The downloaded D435 parts have explicit rear screw and hinge holes.
    head=scene.find(".//body[@name='storage_d435_tilt_frame']")
    for i,y in enumerate((-22.5,22.5)):
        name=f'photo_d435_rear_screw_{i}'
        mount_screw(head,assets,name,37.7,y,25.25,18.25,radius=1.5)
        records.append({'name':name,'basis':'Original downloaded tilting-mount rear holes','nominal':'M3','axis_local':'Z'})
    cap=scene.find(".//body[@name='storage_d435_cap_frame']")
    hinge=ET.SubElement(cap,'body',name='photo_d435_hinge_fastener',pos='.0107 0 .023',quat='.7071067811865476 -.7071067811865476 0 0')
    mount_screw(hinge,assets,'photo_d435_hinge_bolt',0,0,17.4,-20.4,radius=2.,washer=True)
    # Six-sided nut, generated at nominal M4 proportions, on the opposite ear.
    theta=np.arange(6)*np.pi/3;rad=.00404
    vertices=np.concatenate([np.c_[rad*np.cos(theta),rad*np.sin(theta),np.full(6,z)] for z in (-.0206,-.0174)])
    faces=[]
    for i in range(6):
        j=(i+1)%6;faces.extend([[i,j,j+6],[i,j+6,i+6]])
    for i in range(1,5):faces.extend([[0,i+1,i],[6,6+i,6+i+1]])
    ET.SubElement(assets,'mesh',name='photo_m4_nut_mesh',vertex=numbers(vertices.ravel()),face=' '.join(map(str,np.array(faces).ravel())))
    ET.SubElement(hinge,'geom',name='photo_d435_hinge_nut',type='mesh',mesh='photo_m4_nut_mesh',material=silver,contype='0',conaffinity='0',mass='0')
    records.append({'name':'photo_d435_hinge_bolt','basis':'Coincident 4.4/4.5 mm hinge holes in downloaded parts','nominal':'M4 with washer and hex nut','axis_local':'Y'})
    # Upper UVC modules on the assembly mast: four PCB corner fixing screws.
    for body in scene.iter('body'):
        if body.get('name')=='overhead_camera_pcb':
            for i,(x,y) in enumerate(((-13.5,-13.5),(-13.5,13.5),(13.5,-13.5),(13.5,13.5))):
                mount_screw(body,assets,f'photo_assembly_pcb_screw_{i}',x,y,.8,-3.2,radius=1.,washer=False)
                records.append({'name':f'photo_assembly_pcb_screw_{i}','basis':'27 mm corner pitch from the existing camera-mount CAD','nominal':'M2'})
    # Existing base/rail/carrier screws are retained and re-shaded as metal.
    existing=[g.get('name') for g in scene.iter('geom') if any(k in g.get('name','') for k in ('_mount_','_screw_')) and g.get('name','').endswith('_head')]
    # The photograph identifies panel shape, not a three-fixture limit.
    # This grid is a representative layout; actual fixture count is not measured.
    panels=[{'name':f'Ceiling_{row}_{col}','center_m':[x,y,2.4],'size_xy_m':[.30,1.20]}
            for row,y in enumerate((-1.3,.9,3.1)) for col,x in enumerate((-.9,.9,2.7,4.5))]
    layout['appearance']={'reference':'User photographs 1000015414–5419 for colors and 1000015420 for ceiling panels; not a calibrated photometric reconstruction',
        'user_finish_confirmation':'Matte parts and ARM1 lightened to match photos. User clarified many evenly distributed ceiling panels and very faint shadows, not only three lights. Continuous room floor and all four map walls retained. 2026-10-12.',
        'surfaces':surfaces,'lighting':{'type':'evenly distributed rectangular diffuse ceiling panel grid','color_temperature_k':5000,'ceiling_height_m':2.4,'measured':False,
          'reference_photo':'1000015420.jpg','panels':panels,'actual_fixture_count':None,'mujoco_proxy_lights':6,
          'headlight_ambient':.48,'headlight_diffuse':.04,'per_light_ambient':0,
          'isaac_panel_intensity':900.,'isaac_room_bounce_intensity':500.,
          'geometry_basis':'Panel shape and even distribution follow the photograph and user clarification. The 4x3 grid, sizes, spacing and height are illustrative, not measured.',
          'reason':'Strong distributed fill and a weak shadow component produce only faint shadows while retaining white surface detail.'},
        'fasteners':records,'retained_fastener_heads':len(existing),
        'scope':'Only supported mounting axes are populated. Intentional carrier openings, gripper clearance and unused mounting slots remain open.'}
    layout['arm1']['appearance']['rgba']=[.68,.70,.71,1]
    for light in list(world.findall('light')):world.remove(light)
    # Six broad proxies approximate the grid within the raster light budget.
    # Only a weak component casts a shadow; the other panels fill it in.
    for i,pos in enumerate(([x,y,2.4] for y in (-.2,2.) for x in (0.,1.8,3.6))):
        shadow=i==1;level=.045 if shadow else .105
        ET.SubElement(world,'light',name=f'photo_ceiling_fill_{i}',pos=numbers(pos),dir='0 0 -1',
            ambient='0 0 0',diffuse=numbers([level,level,level*.985]),specular='.12 .12 .12',
            attenuation='1 0 0',cutoff='80',exponent='0',directional='false',bulbradius='.45',castshadow='true' if shadow else 'false')
    h=scene.find('./visual/headlight');h.set('ambient','.48 .48 .48');h.set('diffuse','.04 .04 .04');h.set('specular','.08 .08 .08')
    scene.find('./visual/quality').set('shadowsize','4096')
    visual_map=scene.find('./visual/map')
    if visual_map is None:visual_map=ET.SubElement(scene.find('visual'),'map')
    visual_map.set('shadowscale','0.7')
    (out/'appearance.json').write_text(json.dumps(layout['appearance'],ensure_ascii=False,indent=2)+'\n')
