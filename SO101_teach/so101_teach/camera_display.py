"""Short visual interpolation, isolated from measured coordinates and captures."""
from copy import deepcopy
import numpy as np

def display_results(poses,results,now,*,raw=False):
    if raw:return deepcopy(results)
    output={}
    for key in set(poses.states)-set(results):poses.states.pop(key,None)
    for key,result in results.items():
        item=result.get('selected') or {}
        if not item or not all(k in item for k in ('center_px','image_angle_deg','quad')):
            poses.states.pop(key,None);output[key]=deepcopy(result);continue
        held=any(result.get(k) for k in ('pose_held','pose_frozen','teaching_held','adopted_view'))
        prior=poses.states.get(key)
        if prior and np.linalg.norm(np.asarray(item['center_px'])-prior[1]['center_px'])>30:
            poses.states.pop(key,None)
        shown=poses.update(key,item,now,frozen=held)
        # Numeric labels always show the detector's actual measurements.
        if 'metric' in item:shown['metric']=deepcopy(item['metric'])
        output[key]={**deepcopy(result),'selected':shown}
        output[key]['candidates']=[shown if c.get('quad')==item.get('quad') else deepcopy(c) for c in result.get('candidates',[])]
    return output
