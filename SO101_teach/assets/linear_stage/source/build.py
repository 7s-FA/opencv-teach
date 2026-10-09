from pathlib import Path
import cadquery as c,trimesh,json
O=Path(__file__).resolve().parent

def box(x,y,z,at=(0,0,0)):
 return c.Workplane('XY').box(x,y,z,centered=(True,True,False)).translate(at)
def hole(x,y,r,z,h):return c.Workplane('XY').workplane(offset=z).center(x,y).circle(r).extrude(h)
# Coordinates: motion X, bed z0; carrier floor z4.4 on rail lower ledges.
plate=box(160,80,5)
# low socket walls sit clear of rail lips; pockets two68.4 squares.
for x in [-40,40]:
 rim=box(72,72,2,(x,0,5)).cut(box(68.4,68.4,2.2,(x,0,5)))
 plate=plate.union(rim)
# end mounting tongue for adjustable bracket
plate=plate.union(box(24,52,5,(-91,0,0)))
plate=plate.cut(box(25,21,6,(-92,0,-.5)))
# Four adjustable transverse slots for bracket feet, X rows.
for x in [-98,-87]:
 for y in [-18,18]:
  cut=c.Workplane('XY').center(x,y).slot2D(10,4.5,90).extrude(5.2)
  plate=plate.cut(cut)
# rail one segment150 long; bottom ledge z4, carrier z4.4..9.4, lip underside10.0
# positive side rail: inner wall y40.6, outer50.6; top lip reaches36.6
rail=box(150,14,4,(0,43.6,0)).union(box(150,10,6,(0,45.6,4))).union(box(150,14,3,(0,43.6,10)))
# mounting flange accessible outside running channel
rail=rail.union(box(150,10,4,(0,55.6,0)))
for x in [-60,0,60]:rail=rail.cut(hole(x,55.6,2.25,0,4.2))
# Ear lowered to pin center7mm above common mounting surface.
# Coordinates relative to plate top9.4; pin z=-2.4, ear bottom global2.9.
foot=box(22,20,4,(0,12,0))
for x in [-5.5,5.5]:foot=foot.cut(hole(x,14,2.25,0,4.2))
ear=box(22,4,10.5,(0,2,-6.5))
pin=c.Solid.makeCylinder(2.25,6,c.Vector(0,-1,-2.4),c.Vector(0,1,0))
bracket=foot.union(ear).cut(c.Workplane('XY').newObject([pin]))
# Print sideways on ear broad face; y=0 becomes bed z=0.
bracket_print=bracket.rotate((0,0,0),(1,0,0),90)
# Optional end stop bars fixed to bench, set outside100mm working travel.
stop=box(12,120,10)
for y in [-55.6,55.6]:stop=stop.cut(hole(0,y,2.25,0,10.2))
parts={'01_carriage_2jigs':plate,'02_rail_segment_150mm_PRINT_4':rail.translate((0,-48.6,0)),'03_low_pin_ear_PRINT_2':bracket_print,'05_end_stop_PRINT_2':stop}
report={}
for n,p in parts.items():
 assert p.val().isValid()
 c.exporters.export(p,str(O/(n+'.stl')))
 m=trimesh.load_mesh(O/(n+'.stl'));assert m.is_watertight and len(m.split())==1
 report[n]={'size_mm':m.extents.tolist(),'watertight':True}
# Layout: 2x150 rail segments each side, total300. Carrier travels center x -50 to+50.
a=c.Assembly();a.add(plate.translate((0,0,4.4)),name='carriage')
for side in [-1,1]:
 for x in [-75,75]:
  r=rail if side==1 else rail.mirror('XZ')
  a.add(r.translate((x,0,0)),name=f'rail_{side}_{x}')
for side in [-1,1]:
 e=bracket if side==1 else bracket.mirror('XZ')
 a.add(e.translate((-92.5,side*4,9.4)),name=f'ear_{side}')
a.save(str(O/'layout.step'))
# check rail/carriage full travel including tongue clearance
for x in [-50,-25,0,25,50]:
 moving=plate.translate((x,0,4.4))
 for side in [-1,1]:
  for dx in [-75,75]:
   r=(rail if side==1 else rail.mirror('XZ')).translate((dx,0,0))
   assert sum(s.Volume() for s in moving.intersect(r).solids().vals())<1e-5
# Validate ears across adjustable gap4..12mm against carriage.
for offset in [2,4,6]:
 for sign in [-1,1]:
  e=(bracket if sign==1 else bracket.mirror('XZ')).translate((-92.5,sign*offset,9.4))
  assert sum(v.Volume() for v in e.intersect(plate.translate((0,0,4.4))).solids().vals())<1e-5
report['pin_center_from_bed_mm']=7.0
report['ear_gap_adjustment_mm']=[4,12]
report['travel_mm']=100;report['rail_total_length_mm']=300
(O/'validation.json').write_text(json.dumps(report,indent=2))
print(report)
