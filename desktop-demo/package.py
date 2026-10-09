"""Package a verified standalone build, including third-party notices."""
import hashlib,importlib.metadata,json,os,shutil,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1];dist=root/'dist/SO101-Teach-Demo'
licenses=dist/'third-party-licenses';shutil.copytree(root/'third-party-licenses',licenses,dirs_exist_ok=True)
for candidate in (Path(sys.base_prefix)/'LICENSE.txt',Path(sys.base_prefix)/f'lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt'):
    if candidate.exists():shutil.copy2(candidate,licenses/'Python-LICENSE.txt');break
else:raise RuntimeError('Python license was not found in the build runtime')
records=[]
for package in importlib.metadata.distributions():
    name=package.metadata['Name'];records.append({'name':name,'version':package.version,'license':package.metadata.get('License-Expression') or package.metadata.get('License')})
    for file in package.files or []:
        if '.dist-info/' in str(file) and any(word in str(file).lower() for word in ('license','copying','notice')):
            target=licenses/name/Path(str(file).split('.dist-info/',1)[1]);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(package.locate_file(file),target)
(licenses/'packages.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
shutil.copy2(root/'desktop-demo/README.md',dist/'README.md')
if sys.platform=='win32':
    name='SO101-Teach-Demo-Windows-x64';archive=shutil.make_archive(str(root/'dist'/name),'zip',root/'dist','SO101-Teach-Demo')
else:
    name='SO101-Teach-Demo-Linux-x64';archive=shutil.make_archive(str(root/'dist'/name),'gztar',root/'dist','SO101-Teach-Demo')
file=Path(archive);file.with_name(file.name+'.sha256').write_text(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+file.name+'\n')
print(file)
