"""Bounded, content-checked static preparation. Never caches observations."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from .domain import ROOT

_catalogs=OrderedDict()
_lock=threading.Lock()


def execution_catalog(data,profile):
    from .configuration import JigCatalog
    root=Path(data).resolve()
    geometry_profile=deepcopy(profile)
    # configuration_bundle rewrites only calibration file locations. Catalog
    # construction uses the camera/height geometry, never either motor file.
    geometry_profile.pop('calibration_file',None)
    if isinstance(geometry_profile.get('leader'),dict):geometry_profile['leader'].pop('calibration_file',None)
    def identity():
        path=root/'jigs.json'
        raw=path.read_bytes() if path.exists() else b''
        items=json.loads(raw) if raw else {'pallet':{'stl':str(ROOT/'assets/jigs/pallet.stl')}}
        meshes=tuple(sorted((str(Path(v['stl']).resolve()),hashlib.sha256(Path(v['stl']).read_bytes()).hexdigest()) for v in items.values()))
        return (JigCatalog,str(root),json.dumps(geometry_profile,sort_keys=True,allow_nan=False),hashlib.sha256(raw).hexdigest(),meshes)
    key=identity()
    with _lock:
        if key not in _catalogs:
            value=JigCatalog(root,profile=profile)
            if identity()!=key:raise ValueError('준비 중 지그 자료가 변경되었습니다. 다시 준비하세요.')
            _catalogs[key]=value
            while len(_catalogs)>4:_catalogs.popitem(last=False)
        _catalogs.move_to_end(key)
        # Callers may edit their own catalog; the cached geometry stays private.
        result=deepcopy(_catalogs[key])
        result.floor_profile=deepcopy(profile)
        return result
