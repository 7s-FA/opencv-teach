"""White board and a static, articulated-by-hierarchy linear stage for display only."""
import math
import xml.etree.ElementTree as ET
from .domain import ROOT

LINEAR_FIXTURE_X_MM=(-40.,40.)
LINEAR_FIXTURE_BASE_Z_MM=9.4
# B housing pallet CAD bounds: (-34,-34,0) .. (34,34,8.5) mm.
LINEAR_FIXTURE_CENTER_Z_MM=4.25


def numbers(values):return ' '.join(str(float(v)) for v in values)


def yaw_quat(degrees):
    a=math.radians(degrees)/2
    return numbers([math.cos(a),0,0,math.sin(a)])


def carriage_position_mm(stage):
    """Map command values to the camera-confirmed rigid carriage endpoints."""
    stroke=float(stage['stroke_mm']);endpoints=stage.get('endpoint_reference',{})
    forward=endpoints.get('forward',{});retracted=endpoints.get('retracted',{})
    if 'carriage_x_mm' in forward and 'carriage_x_mm' in retracted:
        fraction=(stroke-retracted['commanded_mm'])/(forward['commanded_mm']-retracted['commanded_mm'])
        return retracted['carriage_x_mm']+fraction*(forward['carriage_x_mm']-retracted['carriage_x_mm'])
    return stroke+stage.get('carriage_home_x_mm',-50.)


def linear_endpoint_centers_mm(stage):
    """Stage-local simulation references at the same far-from-motor pallet center."""
    result={}
    for name in ('forward','retracted'):
        endpoint=stage.get('endpoint_reference',{}).get(name)
        if not endpoint:continue
        x=carriage_position_mm({**stage,'stroke_mm':endpoint['commanded_mm']})
        result[name]=(x+max(LINEAR_FIXTURE_X_MM),0.,LINEAR_FIXTURE_BASE_Z_MM+LINEAR_FIXTURE_CENTER_Z_MM)
    return result


def append_environment(scene,placement):
    if not placement:return
    world=scene.find('worldbody');assets=scene.find('asset')
    board=placement.get('board')
    if board:
        floor=scene.find(".//geom[@name='preview_floor']");floor.attrib.pop('material',None);floor.set('rgba','0 0 0 0');floor.set('group','3')
        thickness=board['visual_thickness_mm']
        ET.SubElement(assets,'material',name='workcell_white_board',rgba='.94 .94 .93 1',emission='.04',specular='0',shininess='0')
        body=ET.SubElement(world,'body',name='workcell_board',pos=numbers([v/1000 for v in [*board['center_xy_mm'],board['top_z_mm']-thickness/2]]),quat=yaw_quat(board['yaw_deg']))
        ET.SubElement(body,'geom',name='workcell_board_surface',type='box',size=numbers([v/2000 for v in [*board['size_mm'],thickness]]),material='workcell_white_board',contype='0',conaffinity='0',mass='0')
        legs=board.get('legs')
        if legs:
            ET.SubElement(assets,'mesh',name='workcell_leg_assembly',file=str(ROOT/'assets/table_legs/table_leg_185mm_assembly.stl'),scale='.001 .001 .001')
            bottom_z=-thickness/2-legs['height_mm']
            for index,xy in enumerate(legs['centers_xy_mm']):
                leg=ET.SubElement(body,'body',name=f'workcell_leg_{index}',pos=numbers([xy[0]/1000,xy[1]/1000,bottom_z/1000]))
                ET.SubElement(leg,'geom',name=f'workcell_leg_{index}_visual',type='mesh',mesh='workcell_leg_assembly',rgba='.62 .66 .68 1',contype='0',conaffinity='0',mass='0')
            ET.SubElement(assets,'texture',name='workcell_stone_texture',type='2d',file=str(ROOT/'assets/textures/grey-stone-tiles.png'))
            ET.SubElement(assets,'material',name='workcell_stone',texture='workcell_stone_texture',texrepeat='2 2',texuniform='true',reflectance='0',specular='0',shininess='0')
            # Shares the board hierarchy so a changed tabletop datum moves all parts together.
            ET.SubElement(body,'geom',name='workcell_physical_floor',type='plane',pos=numbers([0,0,bottom_z/1000]),size='2 2 .001',material='workcell_stone',contype='0',conaffinity='0')
            enclosure=board.get('enclosure')
            if enclosure:
                panel=enclosure['panel_thickness_mm'];height=legs['height_mm'];width,depth=board['size_mm']
                z=-thickness/2-height/2
                for index,(x,y,sx,sy) in enumerate([(0,-(depth-panel)/2,width,panel),(0,(depth-panel)/2,width,panel),(-(width-panel)/2,0,panel,depth-2*panel),((width-panel)/2,0,panel,depth-2*panel)]):
                    ET.SubElement(body,'geom',name=f'workcell_side_panel_{index}',type='box',pos=numbers([x/1000,y/1000,z/1000]),size=numbers([sx/2000,sy/2000,height/2000]),material='workcell_white_board',contype='0',conaffinity='0',mass='0')
            backdrop=board.get('backdrop')
            if backdrop:
                panel=backdrop['thickness_mm'];height=backdrop['height_mm'];width=backdrop['width_mm']
                x=board['size_mm'][0]/2+backdrop['gap_mm']+panel/2
                ET.SubElement(body,'geom',name='workcell_backdrop',type='box',pos=numbers([x/1000,0,(bottom_z+height/2)/1000]),size=numbers([panel/2000,width/2000,height/2000]),material='workcell_white_board',contype='0',conaffinity='0',mass='0')
        for index,base_name in enumerate(('base_link','arm3_preview_base_link')):
            base=scene.find(f".//body[@name='{base_name}']")
            if base is None:continue
            active_three=placement.get('active_arm_id')=='arm3'
            base_z=placement['base_xyz_mm'][2] if (index==0 and active_three or index==1 and not active_three) else 0
            # Shaft-circle fits at Z=12.8mm in the supplied base mesh.
            for hole,(x,y) in enumerate(((55.9104,27.7765),(55.9104,-27.7765),(-13.8646,31.75),(-13.8646,-31.75))):
                mount_screw(base,assets,f'arm{index+2}_base_mount_{hole}',x,y,15.1,board['top_z_mm']-thickness-base_z,radius=2.3,style='countersunk')
    stage=placement.get('linear_stage')
    if not stage:return
    root=ET.SubElement(world,'body',name='linear_stage',pos=numbers([v/1000 for v in stage['position_mm']]),quat=yaw_quat(stage['yaw_deg']))
    files={
        'rail':'assets/linear_stage/source/02_rail_segment_150mm_PRINT_4.stl',
        'rail_mirror':'assets/linear_stage/rail_mirrored.stl',
        'carriage':'assets/linear_stage/source/01_carriage_2jigs.stl',
        'ear':'assets/linear_stage/ear_assembled.stl',
        'ear_mirror':'assets/linear_stage/ear_assembled_mirrored.stl',
        'stop':'assets/linear_stage/source/05_end_stop_PRINT_2.stl',
        'a_jig':'assets/carrier_assembly/source/current/b_housing_jig.stl',
        **{key:'assets/linear_stage/'+key+'.stl' for key in ('l12_tube','l12_motor_housing','l12_screws','l12_rod')}}
    latest_connector=stage.get('front_connector_model')=='latest_top_mount'
    observed_connector=stage.get('front_connector_model')=='photo_observed'
    if latest_connector:files['top_mount']='assets/linear_stage/top_mount_assembled.stl'
    if observed_connector:
        files['carriage']='assets/linear_stage/observed_carriage.stl'
        files['observed_clevis']='assets/linear_stage/observed_front_clevis.stl'
    for name,path in files.items():ET.SubElement(assets,'mesh',name='linear_'+name,file=str(ROOT/path),scale='.001 .001 .001')
    def part(parent,name,mesh,pos=(0,0,0),color='.51 .55 .54 1'):
        body=ET.SubElement(parent,'body',name=name,pos=numbers([v/1000 for v in pos]))
        ET.SubElement(body,'geom',name=name+'_visual',type='mesh',mesh='linear_'+mesh,rgba=color,contype='0',conaffinity='0',mass='0')
        return body
    for side in (-1,1):
        for index,x in enumerate((-75,75)):
            part(root,f'linear_rail_{side}_{index}','rail' if side==1 else 'rail_mirror',(x,side*48.6,0))
    for side in (-1,1):
        for index,x in enumerate((-135,-75,-15,15,75,135)):
            mount_screw(root,assets,f'linear_rail_screw_{side}_{index}',x,side*55.6,4,-placement.get('board',{}).get('visual_thickness_mm',10),radius=2.)
    # The reference photo has a far stop and an open motor-side end.
    part(root,'linear_far_stop','stop',stage.get('end_stop_position_mm',[144,0,0]))
    stop=stage.get('end_stop_position_mm',[144,0,0])
    for side in (-1,1):mount_screw(root,assets,f'linear_stop_screw_{side}',stop[0],stop[1]+side*55.6,stop[2]+10,-placement.get('board',{}).get('visual_thickness_mm',10),radius=2.)
    carriage_x=carriage_position_mm(stage)
    carriage=ET.SubElement(root,'body',name='linear_carriage',pos=numbers([carriage_x/1000,0,0]))
    part(carriage,'linear_carriage_plate','carriage',(0,0,4.4))
    if latest_connector or observed_connector:
        part(carriage,'linear_front_clevis','top_mount' if latest_connector else 'observed_clevis')
        pin_x=stage.get('front_pin_x_mm',-118.71)/1000;pin_z=stage.get('actuator_axis_z_mm',7.6)/1000
        # Visible fork pin is transverse; the rear mounting screw is vertical.
        for name,y,radius,half_length in [('shaft',0,.002,.01),('head',.0095,.0035,.0012),('nut',-.0095,.0035,.0012)]:
            ET.SubElement(carriage,'geom',name='linear_front_pin_'+name,type='cylinder',size=numbers([radius,half_length]),pos=numbers([pin_x,y,pin_z]),quat='.7071067812 .7071067812 0 0',material=silver_metal_material(assets),contype='0',conaffinity='0',mass='0')
    else:
        part(carriage,'linear_ear_positive','ear',(-92.5,4,9.4))
        part(carriage,'linear_ear_negative','ear_mirror',(-92.5,-4,9.4))
    for i,x in enumerate(LINEAR_FIXTURE_X_MM):
        fixture=part(carriage,f'linear_a_fixture_{i}','a_jig',(x,0,LINEAR_FIXTURE_BASE_Z_MM))
        geom=fixture.find('geom');geom.attrib.pop('rgba');geom.set('material',white_pla_material(assets))
        ET.SubElement(fixture,'site',name=f'linear_pallet_center_{i+1}',type='sphere',
                      pos=numbers([0,0,LINEAR_FIXTURE_CENTER_Z_MM/1000]),size='.004',rgba='0 0 0 0')
    for name,center in linear_endpoint_centers_mm(stage).items():
        # Fixed to the rail frame, so both endpoint references survive a state change.
        ET.SubElement(root,'site',name='linear_reference_'+name,type='sphere',
                      pos=numbers([v/1000 for v in center]),size='.004',rgba='0 0 0 0')
    axis_z=stage.get('actuator_axis_z_mm',7.)
    rear_z=stage.get('actuator_rear_axis_z_mm',axis_z)
    rear_x=stage.get('rear_eye_x_mm',-295.)
    # Rear mount stays fixed; the actuator pivots slightly as the front pin follows the rail.
    distance_x=carriage_x+stage.get('front_pin_x_mm',-92.5)-rear_x
    pitch=math.atan2(rear_z-axis_z,distance_x)
    actuator=ET.SubElement(root,'body',name='linear_actuator',pos=numbers([rear_x/1000,0,rear_z/1000]),quat=numbers([math.cos(pitch/2),0,math.sin(pitch/2),0]))
    roll=math.radians(stage.get('actuator_body_roll_deg',0.))/2
    housing=ET.SubElement(actuator,'body',name='linear_housing_orientation',quat=numbers([math.cos(roll),math.sin(roll),0,0]))
    part(housing,'linear_l12_tube','l12_tube',color='.79 .80 .78 1')
    part(housing,'linear_l12_housing','l12_motor_housing',color='.09 .10 .11 1')
    screws=part(housing,'linear_l12_screws','l12_screws',color='.52 .55 .57 1')
    geom=screws.find('geom');geom.attrib.pop('rgba');geom.set('material',silver_metal_material(assets))
    rod_extension=math.hypot(distance_x,rear_z-axis_z)-152.5
    part(actuator,'linear_l12_rod','l12_rod',(rod_extension,0,0),'.66 .70 .72 1')
    if latest_connector or observed_connector:
        for name,z,radius,half_length in [('shaft',(4+rear_z)/2000,.002,(4+rear_z)/2000),('washer',(rear_z+4.4)/1000,.0043,.0004),('head',(rear_z+5.5)/1000,.0035,.0008)]:
            ET.SubElement(root,'geom',name='linear_rear_mount_'+name,type='cylinder',size=numbers([radius,half_length]),pos=numbers([rear_x/1000,0,z]),material=silver_metal_material(assets),contype='0',conaffinity='0',mass='0')
    ET.SubElement(actuator,'site',name='linear_rear_eye',size='.002',rgba='.15 .55 .7 1')


def append_carrier_platform(assets,parent,jig,index):
    """Display-only Burger follows its carrier's mocap pose; adds no control joints."""
    if jig.get('platform')!='TurtleBot3 Burger':return
    height=float(jig.get('platform_height_mm') or 195.)
    if not math.isfinite(height) or abs(height-195)>1e-6:raise ValueError('현재 버거 지지대는 운반판 높이 195mm용입니다.')
    platform=ET.SubElement(parent,'body',name=f'carrier_burger_{index}',pos=numbers([0,0,-height/1000]),quat=yaw_quat(jig.get('platform_yaw_deg',-90.)))
    for part,color in [('base_link','.22 .25 .27 1'),('wheel_left_link','.08 .09 .10 1'),('wheel_right_link','.08 .09 .10 1'),('base_scan','.10 .12 .13 1')]:
        name=f'burger_{index}_{part}'
        ET.SubElement(assets,'mesh',name=name,file=str(ROOT/'assets/turtlebot3_burger'/(part+'.stl')),scale='.001 .001 .001')
        ET.SubElement(platform,'geom',name=name,type='mesh',mesh=name,rgba=color,contype='0',conaffinity='0',mass='0')
    # Original Burger hex profile, fitted between the deck and carrier underside ribs.
    name=f'burger_{index}_carrier_standoffs'
    ET.SubElement(assets,'mesh',name=name,file=str(ROOT/'assets/turtlebot3_burger/carrier_mount_standoffs.stl'),scale='.001 .001 .001')
    ET.SubElement(platform,'geom',name=name,type='mesh',mesh=name,material=silver_metal_material(assets),contype='0',conaffinity='0',mass='0')
    for hole,(x,y) in enumerate(((-64,-11.8),(-64,24.4),(64,-24.4),(64,11.8))):
        mount_screw(parent,assets,f'burger_{index}_carrier_screw_{hole}',x,y,1.6,-6.,radius=1.5,washer=False)


def white_pla_material(assets):
    name='workcell_white_pla'
    if assets.find("material[@name='"+name+"']") is None:
        ET.SubElement(assets,'material',name=name,rgba='.975 .975 .965 1',emission='.025',specular='0',shininess='0',reflectance='0')
    return name


def silver_metal_material(assets):
    name='workcell_silver_metal'
    if assets.find("material[@name='"+name+"']") is None:
        ET.SubElement(assets,'material',name=name,rgba='.65 .68 .71 1',specular='.6',shininess='.35',reflectance='.05')
    return name


def mount_screw(parent,assets,name,x,y,top,bottom,*,radius,style='pan',washer=True):
    """Visual screw on a measured hole axis; countersunk cap remains below the face."""
    material=silver_metal_material(assets)
    body=ET.SubElement(parent,'body',name=name,pos=numbers([x/1000,y/1000,0]))
    if style=='countersunk':
        head_radius=4.65;head_height=2.3;head_top=top-.03;head_bottom=head_top-head_height;washer=False
    else:
        head_radius=radius+1.2;head_height=2.1;head_bottom=top+(.4 if washer else 0);head_top=head_bottom+head_height
    shaft_top=head_bottom
    ET.SubElement(body,'geom',name=name+'_shaft',type='cylinder',size=numbers([radius/1000,(shaft_top-bottom)/2000]),pos=numbers([0,0,(shaft_top+bottom)/2000]),material=material,contype='0',conaffinity='0',mass='0')
    if washer:
        ET.SubElement(body,'geom',name=name+'_washer',type='cylinder',size=numbers([(radius+2)/1000,.0002]),pos=numbers([0,0,(top+.2)/1000]),material=material,contype='0',conaffinity='0',mass='0')
    mesh=f'fastener_{style}_{head_radius}_{head_height}'
    if assets.find("mesh[@name='"+mesh+"']") is None:
        ET.SubElement(assets,'mesh',name=mesh,file=str(ROOT/'assets/fasteners'/(style+'_head.stl')),scale=numbers([head_radius/1000,head_radius/1000,head_height/1000]))
    ET.SubElement(body,'geom',name=name+'_head',type='mesh',mesh=mesh,pos=numbers([0,0,head_bottom/1000]),material=material,contype='0',conaffinity='0',mass='0')
    for index,size in enumerate(((1.25,.24,.01),(.24,1.25,.01))):
        ET.SubElement(body,'geom',name=name+f'_slot_{index}',type='box',size=numbers([v/1000 for v in size]),pos=numbers([0,0,(head_top-.005)/1000]),rgba='.10 .11 .12 1',contype='0',conaffinity='0',mass='0')
