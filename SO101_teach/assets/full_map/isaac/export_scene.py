"""Build and render a portable Isaac Sim USD from the approved shared scene."""
from pathlib import Path
import argparse,json,sys,shutil,math,os,hashlib
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
DATA=json.loads((HERE/'scene-manifest.json').read_text())
if not DATA.get('sources'):raise RuntimeError('prepare_isaac.py로 최신 원본을 먼저 준비하세요.')
for source,expected in DATA['sources'].items():
    if not Path(source).is_file() or hashlib.sha256(Path(source).read_bytes()).hexdigest()!=expected:
        raise RuntimeError('원본 변경 후 prepare_isaac.py를 다시 실행해야 합니다: '+source)
parser=argparse.ArgumentParser()
parser.add_argument('--output',default=str(ROOT/'exports/full_map_isaac'))
parser.add_argument('--skip-render',action='store_true')
from isaaclab.app import AppLauncher
AppLauncher.add_app_launcher_args(parser)
parser.set_defaults(headless=True,device='cpu',enable_cameras=True)
args=parser.parse_args();args.kit_args='--/log/outputStreamLevel=error --/log/fileLogLevel=info'
launcher=AppLauncher(args);app=launcher.app
import numpy as np
from scipy.spatial.transform import Rotation
from pxr import Usd,UsdGeom,UsdShade,UsdLux,UsdPhysics,PhysxSchema,Sdf,Gf,Vt,Tf
from PIL import Image
import isaaclab.sim as sim_utils
from isaacsim.core.utils.extensions import enable_extension
enable_extension('isaacsim.sensors.camera')
from isaacsim.sensors.camera import Camera
OUT=Path(args.output).resolve();OUT.mkdir(parents=True,exist_ok=True)
ARR=np.load(HERE/'geometry.npz')
BODIES={r['id']:r for r in DATA['bodies']}


def gfquat(q):return Gf.Quatd(float(q[0]),Gf.Vec3d(*map(float,q[1:])))
def rot(q):return Rotation.from_quat(np.asarray(q)[[1,2,3,0]])
def rgba_linear(rgb):
    c=np.asarray(rgb,float);return tuple(np.where(c<=.04045,c/12.92,((c+.055)/1.055)**2.4).tolist())
def transform(prim,pos,q,scale=None):
    x=UsdGeom.Xformable(prim);x.AddTranslateOp().Set(Gf.Vec3d(*map(float,pos)))
    x.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(gfquat(q))
    if scale is not None:x.AddScaleOp().Set(Gf.Vec3f(*map(float,scale)))
def bind(prim,material):UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


def build_material(stage,name,rgb,surface):
    path='/World/Materials/'+name
    mat=UsdShade.Material.Define(stage,path);shader=UsdShade.Shader.Define(stage,path+'/Surface')
    shader.CreateIdAttr('UsdPreviewSurface')
    shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(rgba_linear(rgb))
    shader.CreateInput('roughness',Sdf.ValueTypeNames.Float).Set(float(surface.get('roughness',.94)))
    shader.CreateInput('metallic',Sdf.ValueTypeNames.Float).Set(float(surface.get('metallic',0)))
    shader.CreateInput('ior',Sdf.ValueTypeNames.Float).Set(float(surface.get('ior',1.46)))
    shader.CreateInput('clearcoat',Sdf.ValueTypeNames.Float).Set(float(surface.get('clearcoat',0)))
    shader.CreateInput('clearcoatRoughness',Sdf.ValueTypeNames.Float).Set(float(surface.get('clearcoat_roughness',.2)))
    mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(),'surface')
    if surface.get('texture'):
        dest=OUT/'textures'/Path(surface['texture']).name;dest.parent.mkdir(exist_ok=True);shutil.copy2(surface['texture'],dest)
        tex=UsdShade.Shader.Define(stage,path+'/ColorTexture');tex.CreateIdAttr('UsdUVTexture')
        tex.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(dest)))
        tex.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('sRGB')
        tex.CreateInput('wrapS',Sdf.ValueTypeNames.Token).Set('repeat');tex.CreateInput('wrapT',Sdf.ValueTypeNames.Token).Set('repeat')
        tex.CreateInput('scale',Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*rgba_linear(rgb),1))
        reader=UsdShade.Shader.Define(stage,path+'/UV');reader.CreateIdAttr('UsdPrimvarReader_float2')
        reader.CreateInput('varname',Sdf.ValueTypeNames.Token).Set('st')
        tex.CreateInput('st',Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(),'result')
        shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(),'rgb')
    if surface.get('normal_texture'):
        src=HERE.parent/surface['normal_texture'];dest=OUT/'textures'/src.name;dest.parent.mkdir(exist_ok=True);shutil.copy2(src,dest)
        tex=UsdShade.Shader.Define(stage,path+'/PrintedLayers');tex.CreateIdAttr('UsdUVTexture')
        tex.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath(str(dest)))
        tex.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('raw')
        tex.CreateInput('wrapS',Sdf.ValueTypeNames.Token).Set('repeat');tex.CreateInput('wrapT',Sdf.ValueTypeNames.Token).Set('repeat')
        tex.CreateInput('scale',Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(2,2,2,1));tex.CreateInput('bias',Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(-1,-1,-1,0))
        reader=UsdShade.Shader.Define(stage,path+'/LayerUV');reader.CreateIdAttr('UsdPrimvarReader_float2')
        reader.CreateInput('varname',Sdf.ValueTypeNames.Token).Set('st')
        tex.CreateInput('st',Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(),'result')
        shader.CreateInput('normal',Sdf.ValueTypeNames.Normal3f).ConnectToSource(tex.ConnectableAPI(),'rgb')
    return mat


def camera_at(stage,name,eye,target):
    camera=Camera('/World/Views/'+name,resolution=(1920,1080))
    camera.initialize()
    eye=np.asarray(eye,float);target=np.asarray(target,float)
    back=eye-target;back/=np.linalg.norm(back);up=np.array([0.,0.,1.])
    right=np.cross(up,back);right/=np.linalg.norm(right)
    q=Rotation.from_matrix(np.column_stack([right,np.cross(back,right),back])).as_quat()
    camera.set_world_pose(eye,q[[3,0,1,2]],camera_axes='usd');camera.set_clipping_range(.001,30)
    c=UsdGeom.Camera(camera.prim);c.CreateHorizontalApertureAttr(27.18);c.CreateVerticalApertureAttr(15.29);c.CreateFocalLengthAttr(18.45)
    return camera


def run():
    sim=sim_utils.SimulationContext(sim_utils.SimulationCfg(dt=1/120,device='cpu',render=sim_utils.RenderCfg(**DATA['rendering']['renderer'])))
    sim._disable_app_control_on_stop_handle=True;stage=sim.stage
    UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.SetStageMetersPerUnit(stage,1.)
    world=UsdGeom.Xform.Define(stage,'/World');stage.SetDefaultPrim(world.GetPrim())
    stage.SetStartTimeCode(0);stage.SetEndTimeCode(240);stage.SetTimeCodesPerSecond(60)
    materials={};surfaces=DATA['appearance']['surfaces']
    for g in DATA['geoms']:
        name=g['material'] or 'rgba_'+('_'.join(str(round(v*255)) for v in g['rgba'][:3]))
        if name not in materials:materials[name]=build_material(stage,name,g['rgba'][:3],surfaces.get(name,{}))
    rigid_paths={}
    for label in ('ARM1','ARM2','ARM3'):
        root=UsdGeom.Xform.Define(stage,'/World/Robots/'+label);root.GetPrim().SetDisplayName(label+' · SO-101')
        UsdPhysics.ArticulationRootAPI.Apply(root.GetPrim())
        settings=PhysxSchema.PhysxArticulationAPI.Apply(root.GetPrim())
        settings.CreateEnabledSelfCollisionsAttr(False);settings.CreateSolverPositionIterationCountAttr(16)
    for b in DATA['bodies']:
        if not b['robot'] or b['rigid']!=b['id']:continue
        path='/World/Robots/'+b['robot']+'/Links/'+b['name'];rigid_paths[b['id']]=path
        link=UsdGeom.Xform.Define(stage,path);transform(link.GetPrim(),b['position'],b['quaternion'])
        UsdPhysics.RigidBodyAPI.Apply(link.GetPrim())
        mass=UsdPhysics.MassAPI.Apply(link.GetPrim());mass.CreateMassAttr(max(.001,b['mass']))
        mass.CreateCenterOfMassAttr(Gf.Vec3f(*b['center_of_mass']));mass.CreateDiagonalInertiaAttr(Gf.Vec3f(*[max(1e-8,v) for v in b['inertia']]))
        q=b['principal_axes'];mass.CreatePrincipalAxesAttr(Gf.Quatf(q[0],Gf.Vec3f(*q[1:])))
        PhysxSchema.PhysxRigidBodyAPI.Apply(link.GetPrim()).CreateDisableGravityAttr(True)
        if b['is_root']:
            joint=UsdPhysics.FixedJoint.Define(stage,'/World/Robots/'+b['robot']+'/Joints/fixed_base')
            joint.CreateBody1Rel().SetTargets([path]);joint.CreateLocalPos0Attr(Gf.Vec3f(*b['position']))
            q=b['quaternion'];joint.CreateLocalRot0Attr(Gf.Quatf(q[0],Gf.Vec3f(*q[1:])))
    for j in DATA['joints']:
        path='/World/Robots/'+j['robot']+'/Joints/'+j['name'];joint=UsdPhysics.RevoluteJoint.Define(stage,path)
        joint.CreateBody0Rel().SetTargets([rigid_paths[j['body0']]]);joint.CreateBody1Rel().SetTargets([rigid_paths[j['body1']]])
        joint.CreateAxisAttr('Z');joint.CreateLowerLimitAttr(math.degrees(j['range_rad'][0]));joint.CreateUpperLimitAttr(math.degrees(j['range_rad'][1]))
        b0,b1=BODIES[j['body0']],BODIES[j['body1']];r0,r1=rot(b0['quaternion']),rot(b1['quaternion'])
        align=Rotation.align_vectors([j['axis_local']],[[0,0,1]])[0]
        child_frame=r1*align;parent_frame=Rotation.from_rotvec(-j['position_rad']*np.asarray(j['axis_world']))*child_frame
        for index,b,r,f in [(0,b0,r0,parent_frame),(1,b1,r1,child_frame)]:
            p=r.inv().apply(np.asarray(j['anchor_world'])-b['position']);q=(r.inv()*f).as_quat()
            getattr(joint,f'CreateLocalPos{index}Attr')(Gf.Vec3f(*map(float,p)))
            getattr(joint,f'CreateLocalRot{index}Attr')(Gf.Quatf(float(q[3]),Gf.Vec3f(*map(float,q[:3]))))
        drive=UsdPhysics.DriveAPI.Apply(joint.GetPrim(),'angular');drive.CreateTypeAttr('force')
        drive.CreateTargetPositionAttr(math.degrees(j['position_rad']));drive.CreateStiffnessAttr(60.);drive.CreateDampingAttr(4.);drive.CreateMaxForceAttr(3.)
        state=PhysxSchema.JointStateAPI.Apply(joint.GetPrim(),'angular')
        state.CreatePositionAttr(math.degrees(j['position_rad']));state.CreateVelocityAttr(0.)
    exported=0;collision_count=0
    for g in DATA['geoms']:
        parent=rigid_paths[g['rigid']] if g['rigid'] is not None else '/World/Environment'
        path=parent+'/Visuals/'+Tf.MakeValidIdentifier(g['name']);r=rot(g['quaternion']);p=np.asarray(g['position'])
        if g['rigid'] is not None:
            b=BODIES[g['rigid']];rb=rot(b['quaternion']);p=rb.inv().apply(p-b['position']);r=rb.inv()*r
        q=r.as_quat()[[3,0,1,2]];kind=g['type'];scale=None
        if kind==7:
            geom=UsdGeom.Mesh.Define(stage,path);mid=g['mesh'];points=ARR[f'vertices_{mid}'];faces=ARR[f'faces_{mid}']
            geom.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points.astype(np.float32)))
            geom.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(faces),3,dtype=np.int32)))
            geom.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(faces.astype(np.int32).ravel()))
            geom.CreateSubdivisionSchemeAttr('none')
            if f'normals_{mid}' in ARR:
                normals=ARR[f'normals_{mid}'][ARR[f'normal_indices_{mid}'].ravel()]
                geom.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals.astype(np.float32)));geom.SetNormalsInterpolation('faceVarying')
            # Low-contrast layer normals follow each source mesh's local section.
            uv=np.c_[points[:,0]*6,points[:,2]*6]
            UsdGeom.PrimvarsAPI(geom).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.vertex).Set(Vt.Vec2fArray.FromNumpy(uv.astype(np.float32)))
        elif kind==6:
            # A mesh floor gives deterministic world-scale UVs across engines.
            if g['name']=='map_floor':
                sx,sy,sz=g['size'];points=np.array([[-sx,-sy,sz],[sx,-sy,sz],[sx,sy,sz],[-sx,sy,sz]],np.float32)
                geom=UsdGeom.Mesh.Define(stage,path);geom.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points));geom.CreateFaceVertexCountsAttr([4]);geom.CreateFaceVertexIndicesAttr([0,1,2,3]);geom.CreateSubdivisionSchemeAttr('none')
                # World-anchored UVs preserve tile scale/phase when the room
                # floor extends beyond the operational map's four walls.
                uv=(points[:,:2]+np.asarray(g['position'][:2]))*2
                UsdGeom.PrimvarsAPI(geom).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.vertex).Set(Vt.Vec2fArray.FromNumpy(uv.astype(np.float32)))
            else:geom=UsdGeom.Cube.Define(stage,path);geom.CreateSizeAttr(2.);scale=g['size']
        elif kind in (2,4):
            geom=UsdGeom.Sphere.Define(stage,path);geom.CreateRadiusAttr(g['size'][0]);scale=None if kind==2 else [1,g['size'][1]/g['size'][0],g['size'][2]/g['size'][0]]
        elif kind in (3,5):
            geom=(UsdGeom.Capsule if kind==3 else UsdGeom.Cylinder).Define(stage,path)
            geom.CreateRadiusAttr(g['size'][0]);geom.CreateHeightAttr(g['size'][1]*2);geom.CreateAxisAttr('Z')
        else:continue
        transform(geom.GetPrim(),p,q,scale)
        material=g['material'] or 'rgba_'+('_'.join(str(round(v*255)) for v in g['rgba'][:3]));bind(geom.GetPrim(),materials[material])
        geom.GetPrim().SetCustomDataByKey('sourceGeom',g['name'])
        if g['cutaway_wall']:UsdGeom.Imageable(geom).MakeInvisible()
        if g['name']=='map_floor' or g['name'].startswith(('wall_','storage_top','storage_side','workcell_board_surface')):
            UsdPhysics.CollisionAPI.Apply(geom.GetPrim());collision_count+=1
        exported+=1
    lighting=DATA['appearance']['lighting']
    dome=UsdLux.DomeLight.Define(stage,'/World/Lighting/RoomBounce');dome.CreateIntensityAttr(lighting['isaac_room_bounce_intensity']);dome.CreateColorAttr(Gf.Vec3f(*lighting.get('ambient_color',[.96,.98,1.])))
    for panel in lighting['panels']:
        light=UsdLux.RectLight.Define(stage,'/World/Lighting/'+panel['name'])
        light.CreateWidthAttr(panel['size_xy_m'][0]);light.CreateHeightAttr(panel['size_xy_m'][1]);light.CreateIntensityAttr(lighting['isaac_panel_intensity'])
        light.CreateEnableColorTemperatureAttr(True);light.CreateColorTemperatureAttr(lighting['color_temperature_k'])
        light.AddTranslateOp().Set(Gf.Vec3d(*panel['center_m']))
    cameras=[('Overview',camera_at(stage,'Overview',[1.8,-1.00,3.88],[1.8,.9,.14])),
             ('Storage',camera_at(stage,'Storage',[2.72,.39,1.11],[3.375,1.13,.40]))]
    stage.GetRootLayer().customLayerData={'title':'SO-101 full workcell - approved layout','revision':'2026-10-12 R1.0','finish':'Matte PLA, tables and walls; mild floor sheen','scope':'3 articulated arms; geometry and drive smoke test, no grasp/contact success claim'}
    stage.GetRootLayer().Export(str(OUT/'so101_full_map.usdc'))
    from isaaclab.assets import Articulation,ArticulationCfg
    from isaaclab.actuators import ImplicitActuatorCfg
    robots={label:Articulation(ArticulationCfg(prim_path='/World/Robots/'+label,spawn=None,
        actuators={'joints':ImplicitActuatorCfg(joint_names_expr=['.*'],stiffness=60.,damping=4.,effort_limit_sim=3.)})) for label in ('ARM1','ARM2','ARM3')}
    sim.reset()
    import torch
    initial_checks={}
    for label,robot in robots.items():
        expected={j['name']:j['position_rad'] for j in DATA['joints'] if j['robot']==label}
        target=torch.tensor([[expected[n] for n in robot.joint_names]],dtype=torch.float32,device=robot.device)
        initial_checks[label]={'before_target':robot.data.joint_pos[0].cpu().tolist(),'target':target[0].cpu().tolist()}
        robot.set_joint_position_target(target);robot.write_data_to_sim()
    (OUT/'joint-initialization.json').write_text(json.dumps(initial_checks,indent=2)+'\n')
    for _ in range(12):
        sim.step(render=True)
        for robot in robots.values():robot.update(sim.get_physics_dt())
    joint_checks={}
    for label,robot in robots.items():
        expected={j['name']:j['position_rad'] for j in DATA['joints'] if j['robot']==label}
        observed=robot.data.joint_pos[0].cpu().numpy();target=np.array([expected[n] for n in robot.joint_names])
        error=float(np.max(np.abs(observed-target)))
        if len(observed)!=6 or not np.isfinite(observed).all() or error>.08:
            raise RuntimeError(f'{label}: initial joint state mismatch {error}; observed={observed.tolist()}, expected={target.tolist()}')
        joint_checks[label]={'joints':robot.joint_names,'maximum_pose_error_rad':error}
    report={'usd':'so101_full_map.usdc','articulations':3,'revolute_joints':18,'visual_geometries':exported,
            'static_collision_surfaces':collision_count,'physics_steps':12,'headless':True,'rendered_views':[],
            'articulation_validation':joint_checks,'rendering':DATA['rendering'],
            'gripper_pads':sum('razer_' in g['name'] for g in DATA['geoms']),
            'limitations':['Contact/grasp dynamics and actuator parameters are not physically calibrated.','Marker heights, D435 tilt and floor-tape positions contain photo estimates.']}
    if not args.skip_render:
        for _ in range(22):sim.render()
        for name,camera in cameras:
            pixels=np.asarray(camera.get_rgba(),np.uint8)
            if pixels.shape!=(1080,1920,4) or pixels[...,:3].std()<3:raise RuntimeError(name+' render failed')
            Image.fromarray(pixels[...,:3]).save(OUT/(name.lower()+'.png'));report['rendered_views'].append(name)
    # Reopen the disk asset to ensure the deliverable itself is complete.
    saved=Usd.Stage.Open(str(OUT/'so101_full_map.usdc'))
    for prim in saved.Traverse():
        attr=prim.GetAttribute('inputs:file')
        if attr and attr.Get():
            path=Path(attr.Get().path)
            if path.is_absolute() and path.is_relative_to(OUT):attr.Set(Sdf.AssetPath(str(path.relative_to(OUT))))
    saved.GetRootLayer().Save()
    revolute=[p for p in saved.Traverse() if p.IsA(UsdPhysics.RevoluteJoint)]
    assert len(revolute)==18 and saved.GetDefaultPrim().GetName()=='World'
    assert report['gripper_pads']==6
    for prim in saved.Traverse():
        attr=prim.GetAttribute('inputs:file')
        if attr and attr.Get():
            asset=attr.Get()
            assert not Path(asset.path).is_absolute() and (OUT/asset.path).is_file(),asset.path
    report['relative_textures_verified']=True
    assert UsdGeom.GetStageMetersPerUnit(saved)==1.0
    (OUT/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (OUT/'source_manifest.json').write_text(json.dumps(DATA['sources'],ensure_ascii=False,indent=2)+'\n')
    (OUT/'layout.json').write_text(json.dumps(DATA['layout'],ensure_ascii=False,indent=2)+'\n')
    (OUT/'rendering.json').write_text(json.dumps(DATA['rendering'],ensure_ascii=False,indent=2)+'\n')
    (OUT/'appearance.json').write_text(json.dumps(DATA['appearance'],ensure_ascii=False,indent=2)+'\n')
    (OUT/'error.log').unlink(missing_ok=True)
    print('FULL_MAP_ISAAC_COMPLETE',json.dumps(report),flush=True)

try:run()
except BaseException:
    import traceback
    (OUT/'error.log').write_text(traceback.format_exc());raise
finally:
    import threading
    error=1 if (OUT/'error.log').exists() else 0
    timer=threading.Timer(8.,lambda:os._exit(error));timer.daemon=True;timer.start()
    app.close(wait_for_replicator=False)
