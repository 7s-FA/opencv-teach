"""Load desktop MJCF assets without native Windows filename encoding limits."""
import hashlib,os
from pathlib import Path
import xml.etree.ElementTree as ET


def virtual_assets(xml,base,assets=None):
    """Read our scene's includes, meshes and textures using Python Unicode paths."""
    blobs=dict(assets or {})
    def pack(text,directory):
        root=ET.fromstring(text)
        compiler=root.find('compiler')
        dirs={kind:compiler.get(kind+'dir',compiler.get('assetdir','')) if compiler is not None else '' for kind in ('mesh','texture')}
        if compiler is not None:
            for key in ('meshdir','texturedir','assetdir'):compiler.attrib.pop(key,None)
        for node in root.iter():
            filename=node.get('file')
            if not filename or filename in blobs:continue
            path=(directory/dirs.get(node.tag,'')/filename).resolve()
            name='file_'+hashlib.sha256(str(path).encode('utf-8')).hexdigest()+path.suffix
            if name not in blobs:
                blobs[name]=pack(path.read_text(encoding='utf-8'),path.parent).encode('utf-8') if node.tag=='include' else path.read_bytes()
            node.set('file',name)
        return ET.tostring(root,encoding='unicode')
    return pack(xml,Path(base)),blobs


def load_model(path=None,*,xml=None,assets=None):
    import mujoco
    if os.name!='nt':
        return mujoco.MjModel.from_xml_path(str(path)) if path is not None else mujoco.MjModel.from_xml_string(xml,assets=assets)
    base=Path(path).parent if path is not None else Path.cwd()
    text=Path(path).read_text(encoding='utf-8') if path is not None else xml
    text,blobs=virtual_assets(text,base,assets)
    return mujoco.MjModel.from_xml_string(text,assets=blobs)
