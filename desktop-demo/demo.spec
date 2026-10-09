from pathlib import Path
from PyInstaller.utils.hooks import collect_all,collect_submodules
repo=Path(SPECPATH).parent
source=repo/'SO101_teach'
datas=[(str(source/name),name) for name in ('assets','inspection','calibration','examples/data')]
datas += [(str(source/'tests/fixtures/inspection-all'/name),'tests/fixtures/inspection-all') for name in ('complete.jpg','faults.jpg')]
datas += [(str(repo/'desktop-demo/photos'),'demo-photos'),(str(repo/'third-party-licenses'),'third-party-licenses')]
binaries=[];hidden=collect_submodules('so101_teach')
for module in ('mujoco','glfw','OpenGL','etils'):
    d,b,h=collect_all(module);datas+=d;binaries+=b;hidden+=h
a=Analysis([str(repo/'desktop-demo/app.py')],pathex=[str(source),str(repo/'desktop-demo')],binaries=binaries,datas=datas,hiddenimports=hidden,excludes=['torch','lerobot','pytest','IPython','matplotlib','pandas','scipy.tests','numpy.tests'],noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[('X utf8=1',None,'OPTION')],exclude_binaries=True,name='SO101-Teach-Demo',debug=False,strip=False,upx=False,console=False)
coll=COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='SO101-Teach-Demo')
