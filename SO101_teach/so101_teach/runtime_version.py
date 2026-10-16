"""Path-independent Pi runtime identity; desktop rendering is not a wire contract.

New Python modules are shared by default. Excluding another desktop module is
a reviewed change to this file, so both installations must receive that change.
This module uses only the standard library and never opens a device.
"""
import ast
import hashlib
import json
from pathlib import Path

SCHEMA=1
DESKTOP_MODULES=frozenset('''
__main__ ui arm_workspaces settings_ui calibration_ui camera_inspection_ui
episode_adjust_ui episode_inspection_ui episode_transfer_ui failure_history_ui
inspection_ui leader_assist_ui pi_settings_ui roi_reference_ui startup_inspection_ui
arm_selection_ui ui_scroll ui_status ui_theme step_editor dual_monitor camera_display
help_content demo_workspace preview preview_materials mujoco_finish full_map
jig_comparison gripper_pads workcell_preview workcell_scene inspection
'''.split())
REQUIRED_ASSETS=(
    'requirements-pi.txt',
    'assets/fonts/NanumGothic.ttf','assets/fonts/NanumGothic-LICENSE.txt',
    'assets/joint_alignment/joint_alignment.json','assets/joint_alignment/arm3_joint_alignment.json',
    'assets/so101/so101_modified.urdf',
    'assets/reference/camera.json','assets/reference/carrier-junction.png',
    'inspection/roi_reference.json','inspection/shape_templates.json',
    'inspection/shape_templates.npz','inspection/appearance_reference.json',
)


def runtime_paths(root):
    root=Path(root)
    files=[p for p in (root/'so101_teach').rglob('*.py')
           if not (p.parent==root/'so101_teach' and p.stem in DESKTOP_MODULES)]
    # Old *.before_*.py snapshots are not importable module names or entry points.
    files+=[p for p in (root/'integration').glob('*.py') if p.stem.isidentifier()]
    files+=[root/name for name in REQUIRED_ASSETS]
    return sorted(set(files))


def validate_boundaries(root):
    """Do not silently let runtime logic depend on excluded desktop modules."""
    root=Path(root);violations=[]
    missing=[name for name in REQUIRED_ASSETS if not (root/name).is_file()]
    if missing:raise ValueError('필수 실행 자산 누락: '+', '.join(missing))
    for path in runtime_paths(root):
        if path.suffix!='.py':continue
        for node in ast.walk(ast.parse(path.read_bytes(),filename=str(path))):
            names=[]
            if isinstance(node,ast.Import):names=[a.name for a in node.names]
            elif isinstance(node,ast.ImportFrom):
                module=node.module or ''
                if node.level and path.parent==root/'so101_teach':module='so101_teach.'+module
                names=[module] if module.rstrip('.')!='so101_teach' else [module.rstrip('.')+'.'+a.name for a in node.names]
            for name in names:
                if name.startswith('so101_teach.') and name.split('.')[1] in DESKTOP_MODULES:
                    violations.append(f'{path.relative_to(root)}:{node.lineno} → {name}')
    if violations:raise ValueError('Pi 실행 코드가 화면 전용 모듈을 참조합니다: '+'; '.join(violations))


def manifest(root,*,allow_missing=False):
    root=Path(root)
    files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime_paths(root) if not allow_missing or p.is_file()}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'schema':SCHEMA,'sha256':digest,'files':files}


def difference(local,remote):
    """An invalid or unknown descriptor must never fall back to a weaker check."""
    if not isinstance(remote,dict) or remote.get('schema')!=SCHEMA:
        return ['실행부 버전 형식 미지원']
    files=remote.get('files')
    if not isinstance(files,dict) or not files or not all(isinstance(k,str) and isinstance(v,str) and len(v)==64 for k,v in files.items()):
        return ['실행부 파일 목록 오류']
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if remote.get('sha256')!=digest:return ['실행부 파일 해시 오류']
    return sorted(name for name in local['files'].keys()|files.keys() if local['files'].get(name)!=files.get(name))
