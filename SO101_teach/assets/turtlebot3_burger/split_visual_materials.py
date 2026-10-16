"""Separate original, disconnected CAD parts for color only; preserve every triangle."""
from pathlib import Path
import hashlib,json,struct
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

HERE=Path(__file__).resolve().parent
def main():
    source=HERE/'base_link.stl';raw=source.read_bytes()
    faces=np.frombuffer(raw[84:],dtype=[('normal','<f4',3),('vertices','<f4',(3,3)),('attribute','<u2')])
    vertices,inv=np.unique(np.round(faces['vertices'].reshape(-1,3),4),axis=0,return_inverse=True)
    f=inv.reshape(-1,3);a=f[:,[0,1,2]].ravel();b=f[:,[1,2,0]].ravel()
    _,labels=connected_components(coo_matrix((np.ones(len(a)),(a,b)),shape=(len(vertices),len(vertices))),directed=False)
    ids=labels[f[:,0]];groups={'frame':[],'pcb':[],'posts':[]};parts=[]
    for k in np.unique(ids):
        index=np.flatnonzero(ids==k);p=faces['vertices'][index].reshape(-1,3)
        lo=p.min(0);hi=p.max(0);extent=hi-lo
        # Two actual thin PCB substrates: 56x85mm SBC and 105x75mm OpenCR.
        board=(np.allclose(extent[:2],[56,85],atol=.05) and extent[2]<3) or (np.allclose(extent[:2],[105,75],atol=.05) and extent[2]<3)
        post=extent[2]>20 and max(extent[:2])<9
        group='pcb' if board else 'posts' if post else 'frame'
        groups[group].extend(index.tolist())
        if group!='frame':parts.append({'component':int(k),'group':group,'min_mm':lo.tolist(),'max_mm':hi.tolist(),'triangles':len(index)})
    indices=np.concatenate([np.asarray(i) for i in groups.values()])
    assert np.array_equal(np.sort(indices),np.arange(len(faces)))
    assert sum(p['group']=='pcb' for p in parts)==2
    report={'original_sha256':hashlib.sha256(raw).hexdigest(),'original_triangles':len(faces),
      'geometry_changed':False,'classification':'Disconnected original PCB substrates and narrow vertical support columns; other triangles remain in the frame group.',
      'original_source':'provenance.json','parts':parts,'files':{}}
    for group,index in groups.items():
        data=faces[np.sort(index)];path=HERE/f'base_link_{group}.stl'
        path.write_bytes(('Original Burger geometry / '+group).encode().ljust(80,b'\0')+struct.pack('<I',len(data))+data.tobytes())
        report['files'][path.name]={'triangles':len(data),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    (HERE/'visual-material-parts.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'triangles':len(faces),'files':report['files'],'pcb_parts':2,'support_columns':sum(p['group']=='posts' for p in parts)},indent=2))

if __name__=='__main__':main()
