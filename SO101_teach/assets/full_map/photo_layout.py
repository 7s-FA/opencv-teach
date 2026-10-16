"""Photo-traced floor tape and documented ArUco boards for the layout preview.

No motion or ROS configuration. Dimensions from Confluence are distinguished
from approximate photograph registration and manually identified floor anchors.
"""
from pathlib import Path
import json
import math
import xml.etree.ElementTree as ET
import cv2
import numpy as np
from scipy.spatial.transform import Rotation

SOURCE_PHOTO='/tmp/codex-remote-attachments/01a0c1bd-1c8b-7a10-a9e0-b8c99a9433cb/75b32ce9-150a-411d-9cc6-7663f4a352f0/2-1000015416.jpg'
ASSEMBLY_PHOTO=SOURCE_PHOTO.replace('2-1000015416','1-1000015415')
SOURCES=[
    {'title':'ArUco 마커 + IR 센서를 활용한 정밀 도킹',
     'url':'https://rorobot.atlassian.net/wiki/spaces/robot3/pages/18776065/ArUco+IR',
     'used':'DICT_4X4_50; 30 mm marker, 40 mm center pitch, 90 mm board; IDs 0,1 / 2,3'},
    {'title':'AMR(버거1·2) 진행 정리 및 1차 목표 기준 완료 사항',
     'url':'https://rorobot.atlassian.net/wiki/spaces/robot3/pages/30900268/AMR+1+2+1',
     'used':'MAT/ASM IDs 0–3; M1 PARK 4–7; M2 PARK 8–11; REST has no ArUco'},
    {'title':'9.30 Solo Waypoint + Docking 주행 테스트',
     'url':'https://rorobot.atlassian.net/wiki/spaces/robot3/pages/24346649/9.30+Solo+Waypoint+Docking',
     'used':'Independent confirmation of normal and parking ID groups; Nav2 coordinates not imported without a frame transform'}]

# New measured parking centers constrain the image-to-floor registration.
# Heights and floor point selection remain photograph estimates, not surveyed.
FIT=json.loads((Path(__file__).parent/'photo-reference/measured-ground-fit.json').read_text())
IMAGE_TO_FLOOR=np.array(FIT['ground_homography'])
ANCHORS=[{'pixel':px,'world_xy_m':xy} for px,xy in zip(FIT['ground_anchors_px'][:3],FIT['ground_anchors_world_m'][:3])]
ALIGNMENT={'basis': 'User requested approximate alignment on 2026-10-12; local straightening of photo-derived floor '
          'tape, not surveyed coordinates.',
 'rules': ['All segments horizontal or vertical; right-angle bends.',
           'Parking guides retain measured marker-center X=1.74/2.10 m; right parking box centered on P1.',
           'Right parking box and both material-approach vertical runs share X=2.22 m.',
           'Material upper/lower routes end at X=3.12 m; stops/tabs paired about MAT marker Y=1.06 m.',
           'REST white rectangle stays closed; its black stop is inset inside the right border with end gaps. The branch meets the outer white border.',
           'Assembly black stop centers on the white approach.'],
 'width_mm': 20,
 'unchanged': 'Table/robot/jig positions, measured ArUco centers, route topology and intentional gaps.'}
WHITE=[{'name': 'parking_left_to_assembly',
  'px': [[354, 284], [307, 498]],
  'xy': [[1.74, 1.5], [1.74, 0.85]],
  'photo_estimated_xy_before_alignment': [[1.74, 1.5], [1.74, 0.85]]},
 {'name': 'parking_right_to_rest',
  'px': [[497, 286], [489, 413], [235, 411]],
  'xy': [[2.1, 1.5], [2.1, 1.1], [1.56, 1.1]],
  'photo_estimated_xy_before_alignment': [[2.1, 1.5], [2.1, 1.1], [1.56, 1.1]]},
 {'name': 'parking_right_outline',
  'px': [[438, 283], [447, 221], [538, 245], [532, 286]],
  'xy': [[1.98, 1.5], [1.98, 1.66], [2.22, 1.66], [2.22, 1.5]],
  'photo_estimated_xy_before_alignment': [[1.98, 1.5], [1.98, 1.66], [2.23, 1.66], [2.23, 1.5]]},
 {'name': 'mat_upper_approach',
  'px': [[538, 245], [528, 378], [868, 385]],
  'xy': [[2.22, 1.66], [2.22, 1.18], [3.12, 1.18]],
  'photo_estimated_xy_before_alignment': [[2.23, 1.66], [2.23, 1.19], [3.06, 1.19]]},
 {'name': 'mat_lower_approach',
  'px': [[496, 644], [514, 466], [903, 481]],
  'xy': [[2.22, 0.52], [2.22, 0.94], [3.12, 0.94]],
  'photo_estimated_xy_before_alignment': [[2.2, 0.52], [2.2, 0.95], [3.12, 0.95]]},
 {'name': 'assembly_inner_approach',
  'px': [[405, 413], [379, 569]],
  'xy': [[1.93, 1.1], [1.93, 0.7]],
  'photo_estimated_xy_before_alignment': [[1.93, 1.1], [1.93, 0.69]]},
 {'name': 'rest_outline',
  'px': [[192, 366], [258, 370], [231, 451], [162, 449], [192, 366]],
  'xy': [[1.4, 1.22], [1.56, 1.22], [1.56, 0.98], [1.4, 0.98], [1.4, 1.22]],
  'photo_estimated_xy_before_alignment': [[1.4, 1.23], [1.56, 1.23], [1.56, 0.99], [1.4, 0.99], [1.4, 1.23]]},
 {'name': 'mat_stop_upper_tab',
  'px': [[844, 395], [879, 396]],
  'xy': [[2.97, 1.14], [3.12, 1.14]],
  'photo_estimated_xy_before_alignment': [[3.005, 1.15], [3.12, 1.15]]},
 {'name': 'mat_stop_lower_tab',
  'px': [[839, 465], [898, 466]],
  'xy': [[2.97, 0.98], [3.12, 0.98]],
  'photo_estimated_xy_before_alignment': [[2.97, 0.985], [3.13, 0.985]]}]
BLACK=[{'name': 'park_stop',
  'px': [[312, 283], [534, 287]],
  'xy': [[1.62, 1.5], [2.22, 1.5]],
  'photo_estimated_xy_before_alignment': [[1.64, 1.5], [2.23, 1.5]]},
 {'name': 'rest_stop',
  'px': [[246, 372], [224, 446]],
  'xy': [[1.53, 1.00], [1.53, 1.20]],
  'photo_estimated_xy_before_alignment': [[1.535, 1.215], [1.535, 1.005]]},
 {'name': 'assembly_stop',
  'px': [[309, 572], [430, 575]],
  'xy': [[1.79, 0.7], [2.07, 0.7]],
  'photo_estimated_xy_before_alignment': [[1.77, 0.69], [2.05, 0.69]]},
 {'name': 'mat_stop',
  'px': [[827, 384], [842, 459]],
  'xy': [[2.97, 0.94], [2.97, 1.18]],
  'photo_estimated_xy_before_alignment': [[2.96, 1.19], [2.97, 0.985]]},
 {'name': 'mat_upper_registration',
  'px': [[857, 394], [871, 395]],
  'xy': [[3.06, 1.14], [3.09, 1.14]],
  'photo_estimated_xy_before_alignment': [[3.06, 1.15], [3.086, 1.15]]},
 {'name': 'mat_lower_registration',
  'px': [[874, 463], [885, 464]],
  'xy': [[3.06, 0.98], [3.09, 0.98]],
  'photo_estimated_xy_before_alignment': [[3.085, 0.985], [3.11, 0.985]]}]
PARKING=[{'name': 'P2', 'robot': 'M2', 'ids': [8, 9, 10, 11], 'center': [1.74, 1.6]}, {'name': 'P1', 'robot': 'M1', 'ids': [4, 5, 6, 7], 'center': [2.1, 1.6]}]
BOARDS=[{'name': 'MAT', 'label': '자재창고', 'ids': [0, 1, 2, 3], 'center': [3.1485, 1.06, 0.115], 'yaw': -90, 'evidence': 'Measured horizontal offset; height estimated from photographs; ID group retained from team documentation', 'distance_from_face_left_mm': 300, 'horizontal_position_source': 'User measured 2026-10-12; refers to center of four-marker group'}, {'name': 'ASM', 'label': '조립대', 'ids': [0, 1, 2, 3], 'center': [1.895, 0.5158, 0.115], 'yaw': 180, 'evidence': 'Measured horizontal offset; height estimated from photographs; ID group retained from team documentation', 'distance_from_face_left_mm': 205, 'horizontal_position_source': 'User measured 2026-10-12; refers to center of four-marker group'}, {'name': 'P2', 'label': 'M2 주차', 'ids': [8, 9, 10, 11], 'center': [1.74, 1.798, 0.1105], 'yaw': 0, 'evidence': 'Measured horizontal offset; height estimated from photographs; ID group retained from team documentation', 'distance_from_right_corner_mm': 1860, 'horizontal_position_source': 'User measured 2026-10-12; refers to center of four-marker group'}, {'name': 'P1', 'label': 'M1 주차', 'ids': [4, 5, 6, 7], 'center': [2.1, 1.798, 0.1105], 'yaw': 0, 'evidence': 'Measured horizontal offset; height estimated from photographs; ID group retained from team documentation', 'distance_from_right_corner_mm': 1500, 'horizontal_position_source': 'User measured 2026-10-12; refers to center of four-marker group'}]


def numbers(values):return ' '.join(str(float(v)) for v in values)


def board_pixels(ids,cell=40):
    """90x90 mm board, with exact 30mm markers and 10mm white gaps/margin."""
    dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    image=np.full((18*cell,18*cell),255,np.uint8)
    for index,ident in enumerate(ids):
        marker=cv2.aruco.generateImageMarker(dictionary,ident,6*cell)
        x=(2+(index%2)*8)*cell;y=(2+(index//2)*8)*cell
        image[y:y+6*cell,x:x+6*cell]=marker
    return image


def append_scene(scene,layout,out):
    world=scene.find('worldbody')
    for color,rows in [('white',WHITE),('black',BLACK)]:
        for row in rows:
            for index,(a,b) in enumerate(zip(row['xy'],row['xy'][1:])):
                a,b=np.array(a),np.array(b);delta=b-a;center=(a+b)/2
                angle=math.atan2(delta[1],delta[0])/2
                ET.SubElement(world,'geom',name='tape_'+row['name']+'_'+str(index),type='box',
                    pos=numbers([*center,.0007 if color=='white' else .0015]),
                    quat=numbers([math.cos(angle),0,0,math.sin(angle)]),
                    size=numbers([np.linalg.norm(delta)/2,.010,.0006]),
                    rgba='.95 .95 .92 1' if color=='white' else '.018 .022 .025 1',
                    contype='0',conaffinity='0',mass='0',group='4')
    dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for spec in BOARDS:
        orientation=Rotation.from_euler('z',spec['yaw'],degrees=True)*Rotation.from_euler('x',90,degrees=True)
        parent=ET.SubElement(world,'body',name='aruco_board_'+spec['name'],pos=numbers(spec['center']),
                              quat=numbers(orientation.as_quat()[[3,0,1,2]]))
        ET.SubElement(parent,'geom',name='aruco_board_'+spec['name']+'_paper',type='box',
            size='.045 .045 .0005',rgba='.99 .99 .99 1',contype='0',conaffinity='0',mass='0')
        for index,ident in enumerate(spec['ids']):
            marker=cv2.aruco.generateImageMarker(dictionary,ident,6)
            cx=-.020+(index%2)*.040;cy=.020-(index//2)*.040
            for row,col in zip(*np.where(marker==0)):
                ET.SubElement(parent,'geom',name=f'aruco_{spec["name"]}_{ident}_{row}_{col}',type='box',
                    pos=numbers([cx+(col-2.5)*.005,cy-(row-2.5)*.005,.0007]),
                    size='.0025 .0025 .0002',rgba='.004 .004 .004 1',contype='0',conaffinity='0',mass='0')
    layout['floor_tape']={'white':WHITE,'black':BLACK,'width_mm':20,
        'position_source':'Photo-derived topology with user-requested alignment of bends, endpoints and stops',
        'alignment':ALIGNMENT}
    layout['aruco']={'dictionary':'DICT_4X4_50','marker_side_mm':30,'center_pitch_mm':40,'paper_size_mm':90,
                     'boards':BOARDS,'rest_has_marker':False,'sources':SOURCES,'pose_verified_for_navigation':False}
    layout['route_source']='Photo floor tracing with local manual alignment; not surveyed coordinates'
    layout['illustrative_tape_polylines_m']=[r['xy'] for r in WHITE]
    layout['waiting_robot_centers_mm']=[[int(v*1000) for v in r['center']] for r in PARKING]
    layout['parking_robots']=PARKING
    out=Path(out);ref=out/'photo-reference';ref.mkdir(exist_ok=True)
    checks={}
    for row in BOARDS:
        image=board_pixels(row['ids']);cv2.imwrite(str(ref/(row['name']+'-board.png')),image)
        _,ids,_=cv2.aruco.ArucoDetector(dictionary).detectMarkers(image)
        found=sorted(ids.ravel().tolist()) if ids is not None else []
        assert found==row['ids'],(row['name'],found)
        checks[row['name']]=found
    report={'source_photo':SOURCE_PHOTO,'assembly_photo':ASSEMBLY_PHOTO,'marker_horizontal_measurements':'P1 from right1500mm, P2 from right1860mm; ASM face-left205mm, MAT face-left300mm','image_to_floor_m':IMAGE_TO_FLOOR.tolist(),
        'floor_anchor_points':ANCHORS,'floor_registration':'Measured parking X centers, wall-plane marker correspondences and parallel floor-tape direction; floor depth/height remain estimates',
        'white':WHITE,'black':BLACK,'floor_alignment':ALIGNMENT,'boards':BOARDS,'decoded_generated_board_ids':checks,
        'source_docs':SOURCES,'limitations':['Absolute line coordinates and panel heights are photo estimates.',
        'All four horizontal marker-group positions are user measured. Heights remain photo estimates; ASM front is visible in 1000015415.jpg.',
        'The older 300x200 whiteboard draft is superseded by user-confirmed 360x180 cm.',
        'No Nav2 frame transform was available, so team waypoint coordinates were not treated as this world frame.']}
    (ref/'photo-layout-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def plot_floor(ax):
    for color,rows in [('white',WHITE),('black',BLACK)]:
        for row in rows:
            xy=np.array(row['xy'])*100
            ax.plot(xy[:,0],xy[:,1],color='white' if color=='white' else '#171e24',lw=2.4,solid_capstyle='butt',zorder=2)


def plot_markers(ax):
    from matplotlib.patches import Rectangle
    for row in BOARDS:
        x,y,_=np.array(row['center'])*100
        if row['name']=='MAT':
            ax.add_patch(Rectangle((x-1,y-4.5),2,9,fc='white',ec='#183f5f',lw=1,zorder=7))
            xytext=(283,115);ha='center'
        elif row['name']=='ASM':
            ax.add_patch(Rectangle((x-4.5,y-1),9,2,fc='white',ec='#183f5f',lw=1,zorder=7))
            xytext=(237,59);ha='center'
        else:
            ax.add_patch(Rectangle((x-4.5,y-1.5),9,2,fc='white',ec='#183f5f',lw=1,zorder=7))
            xytext=(x,185);ha='center'
        ax.annotate(f'{row["name"]} · ID {row["ids"][0]}–{row["ids"][-1]}',xy=(x,y),xytext=xytext,
                    color='#183f5f',fontsize=8,ha=ha,arrowprops=dict(arrowstyle='-',lw=.65,color='#183f5f'),zorder=8)
    ax.text(137,111,'REST',ha='right',va='center',fontsize=8,color='#394e5d')
    ax.text(137,104,'마커 없음',ha='right',va='center',fontsize=7,color='#394e5d')


def draw_board_sheet(out):
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(1,3,figsize=(15,6),facecolor='#f7f9fb')
    for ax,ids,label in zip(axes,([0,1,2,3],[4,5,6,7],[8,9,10,11]),
                            ('MAT / ASM · 작업대 공통','P1 · M1 주차','P2 · M2 주차')):
        ax.imshow(board_pixels(ids),cmap='gray',vmin=0,vmax=255,extent=(0,90,0,90),interpolation='nearest')
        ax.set_title(label+'\n'+str(ids[:2])+' / '+str(ids[2:]),fontsize=13,pad=14)
        ax.set_xlabel('mm');ax.set_ylabel('mm');ax.set_xticks([0,10,40,50,80,90]);ax.set_yticks([0,10,40,50,80,90])
    fig.suptitle('ArUco 도킹 보드 · 팀 문서 규격과 사진 검출 ID 반영',fontsize=20,y=.98)
    fig.text(.5,.055,'DICT_4X4_50 · 마커 30mm · 가장자리 간격 10mm · 중심 간격 40mm · 흰 여백 포함 90×90mm',ha='center',fontsize=12)
    fig.tight_layout(rect=(0,.10,1,.88));fig.savefig(Path(out)/'aruco-boards.png',dpi=150);plt.close(fig)
