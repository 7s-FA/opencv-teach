"""Display proxy for the photographed revised window and forward fork.
Not a manufacturing drawing: main plate/rims reuse source dimensions, connector is image-derived.
"""
from pathlib import Path
import cadquery as cq,json
out=Path(__file__).resolve().parent.parent
pin_x=-118.71;pin_z=7.6

def box(x,y,z,at):return cq.Workplane('XY').box(x,y,z,centered=(True,True,False)).translate(at)
plate=box(160,80,5,(0,0,0))
for x in [-40,40]:plate=plate.union(box(72,72,2,(x,0,5)).cut(box(68.4,68.4,2.2,(x,0,5))))
plate=plate.union(box(34,52,5,(-97,0,0))).cut(box(22,21,5.2,(-93,0,-.1)))
cq.exporters.export(plate,str(out/'observed_carriage.stl'),tolerance=.1,angularTolerance=.2)
parts=[]
for y in [-5.5,5.5]:
 ear=box(17.5,4,10.5,(pin_x+.75,y,2.9))
 bore=cq.Solid.makeCylinder(2.25,30,cq.Vector(pin_x,-15,pin_z),cq.Vector(0,1,0))
 parts.append(ear.cut(cq.Workplane('XY').newObject([bore])).val())
cq.exporters.export(cq.Compound.makeCompound(parts),str(out/'observed_front_clevis.stl'),tolerance=.1,angularTolerance=.2)
(out/'observed-connector-provenance.json').write_text(json.dumps({'status':'photo-derived display approximation, not a verified replacement print model','basis':'front-fixation.jpg and overhead camera; old source bracket pin at -92.5 mm does not match photo','main_plate_mm':[160,80,5],'fixture_pocket_centers_x_mm':[-40,40],'tongue_mm':[34,52,5],'window_mm':[22,21],'front_pin_local_mm':[pin_x,0,pin_z],'pin_bore_mm':4.5,'clevis_gap_mm':7,'part_min_z_mm':2.9,'body_roll_deg':-90,'physical_clearance_verified':False},indent=2))
print('Photo-derived display connector exported')
