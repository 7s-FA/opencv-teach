"""Build exposed top surfaces/height discontinuities; runtime has no mesh dependency."""
import json
import numpy as np
import cv2
import build_roi_reference as cad

cad.SCALE=.4
ROOT=cad.ROOT
OUT=cad.OUT

def features(items):
    labels,_,depth=cad.render_top(items,with_depth=True)
    finite=np.isfinite(depth)
    safe=np.where(finite,depth,-20).astype(np.float32)
    edge=np.zeros(labels.shape,bool)
    for axis in (0,1):
        delta=np.abs(np.diff(safe,axis=axis))>.65
        if axis==0:edge[:-1]|=delta;edge[1:]|=delta
        else:edge[:,:-1]|=delta;edge[:,1:]|=delta
    # Material boundaries are useful for the visible insert inside the cap.
    edge|=cv2.morphologyEx(labels,cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))>0
    def points(mask):
        yy,xx=np.where(mask & finite)
        return np.c_[-35+(xx+.5)*cad.SCALE,-35+(yy+.5)*cad.SCALE,depth[yy,xx]].astype(np.float32)
    result={'edge':points(edge),'color':points(labels>1)}
    for kind in ('housing','insert','cap'):
        index=next((i for i,(name,_) in enumerate(items,1) if name==kind),-1)
        result[kind]=points(labels==index)
    return result

def main():
    ref=cad.load_json(OUT/'roi_reference.json')
    carrier=cad.load_json(ROOT/'SO101_teach/assets/carrier_assembly/assembly.json')
    products={g:{k:cad.read_mesh(ROOT/f'모델링/{g} 제품/{g} {label}.stl') for k,label in cad.LABELS.items()} for g in ('A','B')}
    arrays={};metadata={}
    def save(key,items,info):
        if len(items)>1:
            bounds=np.array([m.bounds for _,m in items[1:]])
            info['bounds_mm']=[bounds[:,0].min(0).tolist(),bounds[:,1].max(0).tolist()]
        for feature,points in features(items).items():arrays[key+'__'+feature]=points
        metadata[key]=info
    for row in ref['stations']['carrier']['rois']:
        fixture=next(f for f in carrier['parts'] if f['name']==row['id'])
        origin=np.asarray(row['fixture_center_xyz_mm'])
        # Local crop axes stay parallel to carrier axes; each mesh pose includes fixture yaw.
        jig=cad.read_mesh(ROOT/'SO101_teach/assets/carrier_assembly'/fixture['source'])
        jp=cad.transform([0,0,0],fixture['yaw_deg']);jig=cad.shown(jig,jp)
        pose=np.asarray(row['part_transform_local_mm']);pose[:3,3]-=origin
        part=cad.shown(products[row['product']][row['part']],pose)
        prefix='carrier_'+row['id']
        save(prefix+'_empty',[('fixture',jig)],{'state':'empty','product':row['product'],'part':row['part']})
        save(prefix+'_present',[('fixture',jig),(row['part'],part)],{'state':'present','product':row['product'],'part':row['part']})
        if row['part']=='insert':arrays[prefix+'_present__body']=np.asarray(part.vertices,np.float32)
        for other_group,parts in products.items():
            for other_kind,mesh in parts.items():
                if (other_group,other_kind)==(row['product'],row['part']):continue
                orient=cad.transform([0,0,0],fixture['yaw_deg'])@cad.product_orientation(other_kind,mesh)
                seat=cad.seating_height(cad.shown(mesh,orient),jig)
                wrong=cad.shown(mesh,cad.transform([0,0,seat])@orient)
                save(prefix+'_wrong_'+other_group+'_'+other_kind,[('fixture',jig),(other_kind,wrong)],{'state':'wrong_part','product':other_group,'part':other_kind})
    for station in ('linear','finished'):
        fixture=cad.read_mesh(ROOT/('SO101_teach/assets/carrier_assembly/source/current/b_housing_jig.stl' if station=='linear' else '모델링/완제품 팔레트/완제품 팔레트.stl'))
        for group,profile in ref['assembly_profiles'].items():
            shift=0 if station=='linear' else cad.seating_height(products[group]['housing'],fixture)-profile['housing_seat_z_mm']
            for stage in profile['stages']:
                items=[('fixture',fixture)]
                for kind in stage['expected_parts']:
                    pose=np.array(profile['part_poses'][kind]['part_transform_local_mm']);pose[2,3]+=shift
                    items.append((kind,cad.shown(products[group][kind],pose)))
                save(station+'_'+group+'_'+stage['id'],items,{'state':stage['id'],'product':group,'visible':stage['expected_visible_parts'],'hidden':stage['occluded_parts_requiring_history']})
            # A lone cap sits much lower than the completed assembly.
            # Compare it explicitly rather than forcing it into a housing state.
            orient=np.array(profile['part_poses']['cap']['part_transform_local_mm']);orient[:3,3]=0
            cap=cad.shown(products[group]['cap'],orient)
            cap=cad.shown(cap,cad.transform([0,0,cad.seating_height(cap,fixture)]))
            save(station+'_'+group+'_cap_only',[('fixture',fixture),('cap',cap)],{'state':'cap_only','product':group,'visible':['cap'],'hidden':[]})
    np.savez_compressed(OUT/'shape_templates.npz',**arrays)
    (OUT/'shape_templates.json').write_text(json.dumps({'schema':1,'source':'CAD exposed surfaces, insert body vertices and height edges','mm_per_pixel':cad.SCALE,'sources_sha256':cad.SOURCE_ROWS,'templates':metadata},ensure_ascii=False,indent=2)+'\n')
    print(f'{len(metadata)} shape templates saved')

if __name__=='__main__':main()
