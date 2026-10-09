"""Tessellate manufacturer STEP in mm; preserve telescoping bodies separately."""
from pathlib import Path
import cadquery as cq,json,hashlib
p=Path(__file__).resolve().parent;out=p.parent
shape=cq.importers.importStep(str(p/'l12_100mm_in.stp'));solids=shape.solids().vals()
parts={'l12_tube':[0,7],'l12_motor_housing':[1,2,11],'l12_screws':[3,4,5,6],'l12_rod':[8,9,10]}
report={}
for name,indices in parts.items():
 grouped=cq.Compound.makeCompound([solids[i] for i in indices]).rotate((0,0,0),(0,0,1),90).translate((35.5,0,0))
 cq.exporters.export(grouped,str(out/(name+'.stl')),tolerance=.05,angularTolerance=.15)
 b=grouped.BoundingBox();report[name]={'step_solid_indices':indices,'bounds_mm':[b.xmin,b.ymin,b.zmin,b.xmax,b.ymax,b.zmax]}
(out/'actuator-provenance.json').write_text(json.dumps({'model':'L12-100-100-6-R','source':'https://www.actuonix.com/assets/images/datasheets/L12_STP.zip','product':'https://www.actuonix.com/l12-100-100-6-r','unit':'mm','local_axes':'rear eye center X=0, rod extends along +X; rod axis Z=0','closed_eye_distance_cad_mm':152.5,'closed_eye_distance_datasheet_mm':152,'stroke_mm':100,'parts':report},indent=2))
print('Exported',list(parts))
