"""Build nominal inspection geometry from SO-101 CAD and existing RL seating helpers.
No camera, robot, ROS, user calibration or teaching data is modified.
"""
from pathlib import Path
import hashlib
import json
import sys

import cv2
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon
from matplotlib import font_manager
import numpy as np
from scipy.spatial.transform import Rotation
import trimesh

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'SO101_rl'))
from scene_assets import seating_height, product_orientation, transform

SCALE = .2
SEARCH_MARGIN = 3.
LABELS = {'housing': '하단', 'insert': '중단', 'cap': '상단'}
COLORS = {'fixture': '#d9dde3', 'housing': '#e19a32', 'insert': '#367bd2', 'cap': '#46a778'}
SOURCE_ROWS = {}


def read_mesh(path):
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    SOURCE_ROWS[str(path.relative_to(ROOT))] = digest
    return trimesh.load(path, force='mesh')


def load_json(path):
    path = Path(path)
    SOURCE_ROWS[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return json.loads(path.read_text())


def normalized(data):
    if isinstance(data, (bool, np.bool_)): return bool(data)
    if isinstance(data, np.ndarray): return normalized(data.tolist())
    if isinstance(data, (float, np.floating)): return round(float(data), 5)
    if isinstance(data, (int, np.integer)): return int(data)
    if isinstance(data, dict): return {k: normalized(v) for k, v in data.items()}
    if isinstance(data, (tuple, list)): return [normalized(v) for v in data]
    return data


def geometry(part, pose, yaw, *, yaw_observable=True):
    placed = part.copy(); placed.apply_transform(pose)
    low, high = placed.bounds
    center = (low[:2] + high[:2]) / 2
    return {
        'part_origin_xyz_mm': pose[:3, 3], 'part_transform_local_mm': pose,
        'expected_yaw_deg': yaw if yaw_observable else None,
        'yaw_observable': yaw_observable,
        'footprint_bbox_xy_mm': [*low[:2], *high[:2]],
        'search_roi_xy_mm': [*(low[:2]-SEARCH_MARGIN), *(high[:2]+SEARCH_MARGIN)],
        'search_margin_mm': SEARCH_MARGIN, 'search_margin_measured': False,
        'search_roi_center_xy_mm': center,
        'search_roi_size_mm': high[:2]-low[:2]+2*SEARCH_MARGIN,
        'part_z_range_mm': [low[2], high[2]],
        'camera_projection_requires_part_height': True,
    }


def render_top(items, low=-35., high=35., *, with_depth=False):
    """Orthographic triangle z-buffer. Labels represent actually exposed CAD surfaces."""
    count = int(round((high-low)/SCALE))
    depth = np.full((count, count), -np.inf)
    labels = np.zeros((count, count), np.uint8)
    for label, (_, mesh) in enumerate(items, 1):
        for t in mesh.triangles:
            xmin, ymin = np.maximum(0, np.floor((t[:, :2].min(0)-low)/SCALE).astype(int))
            xmax, ymax = np.minimum(count-1, np.ceil((t[:, :2].max(0)-low)/SCALE).astype(int))
            if xmin>xmax or ymin>ymax: continue
            a,b,c=t
            den=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
            if abs(den)<1e-10: continue
            x=low+(np.arange(xmin,xmax+1)+.5)*SCALE
            y=low+(np.arange(ymin,ymax+1)+.5)*SCALE
            xx,yy=np.meshgrid(x,y)
            u=((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/den
            v=((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/den
            w=1-u-v
            z=u*a[2]+v*b[2]+w*c[2]
            region=depth[ymin:ymax+1,xmin:xmax+1]
            use=(u>=-1e-8)&(v>=-1e-8)&(w>=-1e-8)&(z>region+1e-7)
            region[use]=z[use]
            labels[ymin:ymax+1,xmin:xmax+1][use]=label
    result={}
    for label,(kind,_) in enumerate(items,1):
        ys,xs=np.where(labels==label)
        result[kind]={'visible_area_mm2':len(xs)*SCALE*SCALE,
                      'visible_bbox_xy_mm':None if not len(xs) else
                      [low+xs.min()*SCALE,low+ys.min()*SCALE,low+(xs.max()+1)*SCALE,low+(ys.max()+1)*SCALE]}
    return (labels,result,depth) if with_depth else (labels,result)


def shown(mesh, pose):
    mesh=mesh.copy();mesh.apply_transform(pose);return mesh


def main():
    font=Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if font.exists(): plt.rcParams['font.family']=font_manager.FontProperties(fname=font).get_name()
    plt.rcParams['axes.unicode_minus']=False
    carrier_path=ROOT/'SO101_teach/assets/carrier_assembly/assembly.json'
    carrier=load_json(carrier_path)
    stage=load_json(ROOT/'SO101_teach/assets/linear_stage/assembly-provenance.json')
    rl=load_json(ROOT/'SO101_rl/outputs/assembly_current_12/scene.json')
    rl_cfg=load_json(ROOT/'SO101_rl/config/assembly.json')
    alignment=load_json(ROOT/'SO101_rl/config/cad_alignment.json')
    products={group:{kind:read_mesh(ROOT/f'모델링/{group} 제품/{group} {label}.stl') for kind,label in LABELS.items()} for group in ('A','B')}
    for kind, row in rl['parts'].items():
        ours={'tpu':'insert'}.get(kind,kind)
        source=ROOT/f'모델링/B 제품/B {LABELS[ours]}.stl'
        assert hashlib.sha256(source.read_bytes()).hexdigest()==row['source_sha256'], 'B geometry differs from RL source'
    final_pallet=read_mesh(ROOT/'모델링/완제품 팔레트/완제품 팔레트.stl')
    linear_fixture=read_mesh(ROOT/'SO101_teach/assets/carrier_assembly/source/current/b_housing_jig.stl')
    result={'schema':1,'units':{'length':'mm','angle':'deg'},'inspection_enabled':False,
            'status':'CAD_REFERENCE_REQUIRES_IMAGE_VALIDATION',
            'coordinates':{'xy':'right-handed local jig frame; +Z upward; positive yaw CCW viewed from +Z',
                           'carrier_origin':'plate centre XY / underside Z=0',
                           'linear_origin':'carriage centre XY; fixture origins at (-40,0,9.4), (40,0,9.4)',
                           'finished_origin':'70x70 pallet centre XY / underside Z=0'},
            'search_margin_mm':SEARCH_MARGIN,'search_margin_is_acceptance_tolerance':False,
            'live_pixel_rois':None,'requires_live_camera_registration':True,
            'simulation_offsets_applied_to_real_roi':False,
            'stations':{},'assembly_profiles':{}}
    transport=[]
    for fixture in carrier['parts']:
        if fixture['name']=='carrier': continue
        group=fixture['product'];kind={'ball':'insert','tpu':'insert'}.get(fixture['kind'],fixture['kind'])
        part=products[group][kind]
        # B cap inversion is directly supported by the RL source. A uses the same
        # nominal mounting candidate and must be confirmed from a real reference image.
        orient=product_orientation('cap' if kind=='cap' else kind,part)
        jig=read_mesh(carrier_path.parent/fixture['source'])
        seat=seating_height(shown(part,orient),jig)
        pose=transform(fixture['translation_mm'],fixture['yaw_deg'])@transform([0,0,seat])@orient
        row={'id':fixture['name'],'product':group,'part':kind,
             'fixture_center_xyz_mm':fixture['translation_mm'],'fixture_yaw_deg':fixture['yaw_deg'],
             'pose_basis':'nominal CAD fixture + first-contact seating; simulation tuning excluded',
             'orientation_basis':'RL-confirmed cap inversion' if group=='B' and kind=='cap' else 'CAD nominal; real sample required',
             'roll_deg':180 if kind=='cap' else 0,
             'rotation_equivalence_deg':None if group=='A' and kind=='insert' else 90,
             **geometry(part,pose,fixture['yaw_deg'],yaw_observable=not(group=='A' and kind=='insert'))}
        transport.append(row)
    result['stations']['carrier']={'anchor':'detected_jig_pose','jig_id':rl['carrier']['jig_id'],
        'requires_full_heading_resolution':True,
        'detector_to_mesh_yaw':'apply mesh_yaw_offset_deg from the confirmed detection; never omit or apply twice',
        'reference_detection_mesh_yaw_offset_deg':rl['carrier']['mesh_yaw_offset_deg'],
        'size_xy_mm':[220,148],'rois':transport}
    stage_profiles={}
    for group,parts in products.items():
        seat=seating_height(parts['housing'],linear_fixture)
        insert_z=seating_height(parts['insert'],parts['housing'])
        nested=shown(parts['insert'],transform([0,0,insert_z]))
        cap_orientation=product_orientation('cap',parts['cap'])
        cap_bottom=seating_height(shown(parts['cap'],cap_orientation),trimesh.util.concatenate([parts['housing'],nested]))
        poses={'housing':transform([0,0,seat]),'insert':transform([0,0,seat+insert_z]),
               'cap':transform([0,0,seat+cap_bottom])@cap_orientation}
        if group=='B':
            assert abs(seat-rl['assembly']['housing_seating_z_mm'])<.001
            assert abs(insert_z-rl['assembly']['tpu_bottom_in_housing_mm'])<.001
            assert abs(cap_bottom-rl['assembly']['cap_bottom_in_housing_mm'])<.001
        profile={'basis':'B: matching RL CAD; A: CAD-derived candidate, not an RL-validated assembly',
                 'part_poses':{kind:geometry(parts[kind],pose,0,yaw_observable=not(group=='A' and kind=='insert')) for kind,pose in poses.items()},
                 'housing_seat_z_mm':seat,'insert_bottom_relative_to_housing_mm':insert_z,
                 'cap_bottom_relative_to_housing_mm':cap_bottom,
                 'cap_bottom_minus_housing_rim_mm':cap_bottom-parts['housing'].bounds[1,2],
                 'sim_only_candidate_tolerances':{'xy_mm':{'housing':2.,'insert':1.5,'cap':1.5},
                    'housing_yaw_deg':5.,'measured':False,'apply_to_real_pass_fail':False},'stages':[]}
        fig,axes=plt.subplots(1,4,figsize=(16,4.6),constrained_layout=True)
        for i,kinds in enumerate([[],['housing'],['housing','insert'],['housing','insert','cap']]):
            key=['empty','housing_seated','insert_added','cap_added'][i]
            items=[('fixture',linear_fixture)]+[(kind,shown(parts[kind],poses[kind])) for kind in kinds]
            mask,visibility=render_top(items)
            mask_path=OUT/'previews'/f'{group}_{key}_labels.png';cv2.imwrite(str(mask_path),np.flipud(mask))
            canvas=np.full((*mask.shape,3),255,np.uint8)
            for label,(kind,_) in enumerate(items,1):
                rgb=tuple(int(COLORS[kind][j:j+2],16) for j in (1,3,5));canvas[mask==label]=rgb
            axes[i].imshow(canvas,origin='lower',extent=[-35,35,-35,35])
            axes[i].axhline(0,color='#78828f',lw=.5);axes[i].axvline(0,color='#78828f',lw=.5)
            axes[i].set_title([f'{group} · 빈 팔레트',f'{group} · 하단 안착',f'{group} · 중단 삽입',f'{group} · 상단 결합'][i])
            axes[i].set_xlabel('지그 원점 기준 X (mm)');axes[i].set_ylabel('Y (mm)')
            axes[i].set_aspect('equal')
            if kinds:
                row=profile['part_poses'][kinds[-1]];x0,y0,x1,y1=row['search_roi_xy_mm']
                axes[i].add_patch(Rectangle((x0,y0),x1-x0,y1-y0,fill=False,ec='#d62f5c',ls='--',lw=1.5))
            stage_row={'id':key,'expected_parts':kinds,
                       'new_part':kinds[-1] if kinds else None,
                       'requires_previous_pass':None if i==0 else ['empty','housing_seated','insert_added'][i-1],
                       'history_binding_keys':['run_id','product_id','fixture_id','product_type'],
                       'expected_visible_parts':[kind for kind in kinds if visibility[kind]['visible_area_mm2']>0],
                       'occluded_parts_requiring_history':[kind for kind in kinds if visibility[kind]['visible_area_mm2']==0],
                       'new_part_search_roi_xy_mm':profile['part_poses'][kinds[-1]]['search_roi_xy_mm'] if kinds else [-28,-28,28,28],
                       'requires_frame_after_stage_start':True,
                       'visible_geometry':visibility,
                       'top_view_label_mask':str(mask_path.relative_to(OUT)),
                       'label_mapping':{str(n):kind for n,(kind,_) in enumerate(items,1)},
                       'mask_grid':{'mm_per_pixel':SCALE,'top_left_xy_mm':[-35,35],'y_direction':'down'},
                       'hidden_parts_policy':'require previous-stage PASS; never classify occluded pixels as missing',
                       'runtime_checks':['correct stage order','fresh stable unobstructed image','new part presence + relative pose','remaining visible earlier parts'],
                       'cannot_prove_from_top_view':['hidden internal seating','physical contact depth','magnet engagement strength']}
            profile['stages'].append(stage_row)
        fig.suptitle('단계별 누적 CAD 가시 영역 · 점선은 새 부품 검색 ROI · 실물 PASS/FAIL 기준 미확정',fontsize=12)
        fig.savefig(OUT/'previews'/f'assembly_stages_{group}.png',dpi=160);plt.close(fig)
        result['assembly_profiles'][group]=profile
        final_seat=seating_height(parts['housing'],final_pallet)
        pose=transform([0,0,final_seat-seat])
        combined=trimesh.util.concatenate([shown(parts[kind],poses[kind]) for kind in parts])
        stage_profiles[group]={'expected_product':group,'nominal_yaw_deg':0,
            'yaw_policy':'90-degree-equivalent outer square; use a distinguishing feature for unique direction',
            **geometry(combined,pose,0),
            'prior_assembly_pass_required':True,'inspection_after_gripper_clear':True,
            'hidden_internal_parts_not_reinspected':True}
    result['stations']['linear_assembly']={'anchor':'fixed_image_registration_at_confirmed_endpoint',
        'fixture_centers_carriage_mm':stage['fixture_centers_local_mm'],
        'fixture_yaw_deg':[0,0],'active_fixture_index':None,'rl_example_fixture_index':1,
        'active_fixture_basis':'Select after real image/teaching registration; RL B reference uses motor-far fixture (index 1)',
        'endpoint_pixel_rois':{'build':None,'load':None},
        'fixed_pixel_roi_must_not_follow_a_moving_carriage':True,
        'profile_refs':{'A':'assembly_profiles.A','B':'assembly_profiles.B'},
        'readiness':['linear initial command completed','fixture reference edges in registered ROI','gripper outside inspection region']}
    result['stations']['finished_pallet']={'anchor':'detected_jig_pose','jig_id':'pallet','size_xy_mm':[70,70],
        'slot_count':1,'slot_center_xy_mm':[0,0],'symmetry_deg':90,'rois':stage_profiles}
    result['simulation_reference_only']={'source':'SO101_rl/outputs/assembly_current_12/scene.json',
        'carrier_global_offsets':{k:rl['carrier'][k] for k in ('cad_offset_mm','cad_yaw_deg','cad_tilt_deg')},
        'nest_offsets_mm':alignment['nest_offsets_mm'],'receiver_offset_mm':rl_cfg['receiver_offset_mm'],
        'reason_not_applied':'These are simulation registration candidates, not measured real jig offsets.'}
    result['sources_sha256']=SOURCE_ROWS
    (OUT/'roi_reference.json').write_text(json.dumps(normalized(result),ensure_ascii=False,indent=2)+'\n')
    fig,axes=plt.subplots(1,3,figsize=(16,5.5),constrained_layout=True)
    ax=axes[0];ax.add_patch(Rectangle((-110,-74),220,148,fc='#f0f3f6',ec='#5e6874'))
    for row in transport:
        cx,cy,_=row['fixture_center_xyz_mm'];ax.add_patch(Rectangle((cx-34,cy-34),68,68,fill=False,ec='#adb5bf'))
        x0,y0,x1,y1=row['search_roi_xy_mm'];ax.add_patch(Rectangle((x0,y0),x1-x0,y1-y0,fill=False,ec=COLORS[row['part']],lw=2))
        ax.text(cx,cy+7,row['product']+' '+LABELS[row['part']],ha='center',fontsize=9)
        angle='각도 제외' if not row['yaw_observable'] else '90°'
        ax.text(cx,cy-9,f'({cx:g}, {cy:g})\n{angle}',ha='center',va='center',fontsize=8)
    ax.set_xlim(-120,120);ax.set_ylim(-90,90);ax.set_title('운반용 지그 · 원점/각도 추종')
    ax=axes[1]
    for i,cx in enumerate((-40,40)):
        ax.add_patch(Rectangle((cx-34,-34),68,68,fc='#f0f3f6',ec='#5e6874'))
        ax.add_patch(Rectangle((cx-28,-28),56,56,fill=False,ec='#d62f5c',lw=2))
        ax.text(cx,5,f'팔레트 {i+1}\n({cx:g}, 0)',ha='center',fontsize=10)
        ax.text(cx,-15,'단계별 ROI',ha='center',fontsize=9)
    ax.set_xlim(-80,80);ax.set_ylim(-50,50);ax.set_title('리니어 · 정지 위치별 고정 ROI 등록')
    ax=axes[2];ax.add_patch(Rectangle((-35,-35),70,70,fc='#f0f3f6',ec='#5e6874'))
    ax.add_patch(Rectangle((-28,-28),56,56,fill=False,ec='#46a778',lw=2))
    ax.text(0,8,'완성품 중심\n(0, 0)',ha='center',fontsize=11)
    ax.text(0,-15,'0° 기준 / 90° 대칭',ha='center',fontsize=9)
    ax.set_xlim(-42,42);ax.set_ylim(-45,45);ax.set_title('완제품 팔레트 · 원점/각도 추종')
    for ax in axes:
        ax.plot(0,0,'k+',ms=10);ax.axhline(0,c='#9ba4b0',lw=.5);ax.axvline(0,c='#9ba4b0',lw=.5)
        ax.set_aspect('equal');ax.set_xlabel('로컬 X (mm)');ax.set_ylabel('로컬 Y (mm)')
    fig.suptitle('검사 ROI 기준안 · CAD 기준 / 실제 카메라 등록 전 · 검색 여유 3mm는 합격 공차가 아님',fontsize=13)
    fig.savefig(OUT/'previews/roi_overview.png',dpi=160);plt.close(fig)
    print(json.dumps({g:{'seat':v['housing_seat_z_mm'],'insert_z':v['insert_bottom_relative_to_housing_mm'],
                         'cap_bottom_z':v['cap_bottom_relative_to_housing_mm'],
                         'final_visible':v['stages'][-1]['visible_geometry']} for g,v in result['assembly_profiles'].items()},ensure_ascii=False,default=lambda x:normalized(x)))


if __name__=='__main__':main()
