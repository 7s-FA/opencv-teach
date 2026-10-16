"""Display the same two rubber pads as the existing ARM2 physics model."""
import xml.etree.ElementTree as ET
from scipy.spatial.transform import Rotation
from .domain import ROOT

PAD_SOURCE=ROOT/'assets/so101/gripper_pad_reference.urdf'
PAD_MATERIAL='gripper_rubber'


def append_gripper_pads(scene):
    """Attach before copying the arm so fixed/moving pads follow every jaw.

    Meshes are already in metres; the URDF's original scale and local placement
    are authoritative. Display geoms add neither control joints nor inferred
    mass/contact parameters to the teaching model. ARM2's existing PhysX pad
    collisions and material properties remain owned by its physics URDF.
    """
    assets=scene.find('asset')
    if assets.find(f"material[@name='{PAD_MATERIAL}']") is None:
        ET.SubElement(assets,'material',name=PAD_MATERIAL,rgba='.025 .025 .025 1',
                      specular='.05',shininess='.045',reflectance='0')
    source=ET.parse(PAD_SOURCE).getroot()
    for link in source.findall('link'):
        for visual in link.findall('visual'):
            name=visual.get('name','')
            if name not in ('razer_fixed_pad','razer_moving_pad'):continue
            parent=scene.find(f".//body[@name='{link.get('name')}']")
            if parent is None:raise ValueError('집게 패드의 장착 링크가 없습니다: '+link.get('name'))
            if parent.find(f"geom[@name='{name}']") is not None:continue
            mesh=visual.find('geometry/mesh');origin=visual.find('origin')
            if assets.find(f"mesh[@name='{name}']") is None:
                ET.SubElement(assets,'mesh',name=name,
                    file=str((PAD_SOURCE.parent/mesh.get('filename')).resolve()),scale=mesh.get('scale','1 1 1'))
            rotation=Rotation.from_euler('xyz',[float(v) for v in origin.get('rpy','0 0 0').split()])
            ET.SubElement(parent,'geom',name=name,type='mesh',mesh=name,material=PAD_MATERIAL,
                pos=origin.get('xyz','0 0 0'),quat=' '.join(map(str,rotation.as_quat()[[3,0,1,2]])),
                contype='0',conaffinity='0',mass='0',group='1')
