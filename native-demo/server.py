"""One native desktop per Codespace. Bind only loopback; forward port privately."""
import asyncio,json,os,secrets,fcntl,shutil,socket,subprocess,sys,tempfile,time,signal
from pathlib import Path
from aiohttp import web,WSMsgType
HERE=Path(__file__).resolve().parent
REPO=HERE.parent
RUN=Path(os.environ.get('SO101_DEMO_STATE',str(Path.home()/'.cache/so101-native-demo/session')))
SOURCE=Path(os.environ.get('SO101_DEMO_SOURCE',str(REPO/'SO101_teach'))).resolve()
PYTHON=os.environ.get('SO101_DEMO_PYTHON',sys.executable)
PROCESSES=[]


def prepare_app():
    RUN.mkdir(parents=True,exist_ok=True)
    (RUN/'ui-state.json').unlink(missing_ok=True)
    (RUN/'camera.json').write_text('{"case":"faults"}')
    target=RUN/'app'
    if target.exists():shutil.rmtree(target)
    target.mkdir()
    for name in ('so101_teach','assets','inspection','integration','tests','calibration'):
        shutil.copytree(SOURCE/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc','build','install','log'))
    original=SOURCE/'examples/data' if (SOURCE/'examples/data').exists() else SOURCE/'data'
    data=target/'data';data.mkdir()
    for name in ('calibration','episodes','jig_assets','inspection_reference'):
        if (original/name).exists():shutil.copytree(original/name,data/name)
    for name in ('profile.json','robot_profiles.json','preferences.json','jigs.json','model-adjustments.json','workcell-preview.json'):
        if (original/name).exists():shutil.copy2(original/name,data/name)
    for file in data.rglob('*.json'):
        value=file.read_text()
        for prefix in (str(SOURCE),'/path/to/Final_Arm/SO101_teach','/home/fresh/Documents/Final_Arm/SO101_teach'):
            value=value.replace(prefix,str(target))
        file.write_text(value)
    def offline(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k=='mode':value[k]='demo'
                elif k in ('token','password','identity_file'):value[k]=''
                elif k=='port' and isinstance(v,str):value[k]='DEMO'
                else:offline(v)
        elif isinstance(value,list):
            for v in value:offline(v)
    for name in ('profile.json','robot_profiles.json'):
        file=data/name;value=json.loads(file.read_text());offline(value);file.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    pref=data/'preferences.json';value=json.loads(pref.read_text()) if pref.exists() else {};value['device_host']='local';pref.write_text(json.dumps(value,ensure_ascii=False))
    return target


def launch(command,env):
    log=open(RUN/'runtime.log','ab',buffering=0)
    p=subprocess.Popen(command,env=env,stdout=log,stderr=log,start_new_session=True);log.close();PROCESSES.append(p);return p


def stop_processes():
    for p in reversed(PROCESSES):
        if p.poll() is None:
            try:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    for p in PROCESSES:
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass


async def start_desktop(app):
    target=await asyncio.to_thread(prepare_app)
    number=next(n for n in range(110,180) if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    display=f':{number}';auth=RUN/'xauth';auth.touch(mode=0o600);subprocess.run(['xauth','-f',str(auth),'add',display,'.',secrets.token_hex(16)],check=True,stdout=subprocess.DEVNULL)
    env={**os.environ,'DISPLAY':display,'XAUTHORITY':str(auth),'LIBGL_ALWAYS_SOFTWARE':'1','MUJOCO_GL':'glfw','SO101_DEMO_APP':str(target),'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'}
    env.pop('WAYLAND_DISPLAY',None);env['XDG_SESSION_TYPE']='x11'
    xvfb=launch([os.environ.get('XVFB_BIN','Xvfb'),display,'-screen','0','1920x1080x24','-noreset','-nolisten','tcp','-auth',str(auth)],env)
    for _ in range(400):
        if xvfb.poll() is not None:raise RuntimeError('Xvfb failed; inspect runtime.log')
        if Path(f'/tmp/.X11-unix/X{number}').exists():break
        await asyncio.sleep(.05)
    else:raise RuntimeError('X display startup timed out')
    command=[PYTHON,str(HERE/'app.py')]
    if os.environ.get('SO101_LOCAL_SANDBOX')=='1':
        command=['bwrap','--die-with-parent','--unshare-net','--unshare-pid','--unshare-ipc','--unshare-uts','--ro-bind','/usr','/usr','--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64','--symlink','usr/bin','/bin','--proc','/proc','--dev','/dev','--dir','/dev/shm','--tmpfs','/dev/shm','--tmpfs','/tmp','--dir','/tmp/.X11-unix','--bind',f'/tmp/.X11-unix/X{number}',f'/tmp/.X11-unix/X{number}','--ro-bind','/etc/fonts','/etc/fonts','--ro-bind','/etc/ld.so.cache','/etc/ld.so.cache','--ro-bind',str(Path(PYTHON).parent.parent),str(Path(PYTHON).parent.parent),'--ro-bind',str(HERE),str(HERE),'--bind',str(RUN),str(RUN),'--setenv','HOME',str(RUN/'home'),'--chdir',str(target),'--',*command]
    app['desktop']=launch(command,env)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    app['vnc_port']=port
    launch([os.environ.get('X11VNC_BIN','x11vnc'),'-display',display,'-auth',str(auth),'-rfbport',str(port),'-localhost','-forever','-shared','-nopw','-noxdamage','-noshm','-listen','127.0.0.1'],env)
    for _ in range(400):
        try:r,w=await asyncio.open_connection('127.0.0.1',port);w.close();await w.wait_closed();break
        except OSError:await asyncio.sleep(.05)
    else:raise RuntimeError('VNC startup timed out')
    yield
    stop_processes()


async def index(request):return web.FileResponse(HERE/'index.html')
async def health(request):
    file=RUN/'ui-state.json';state=json.loads(file.read_text()) if file.exists() else {'ready':False}
    state['ready']=state['ready'] and request.app['desktop'].poll() is None
    return web.json_response({**state,'kind':'native-python-tk','camera':'saved-faults-photo','hardware':'demo'})
def check_origin(request):
    origin=request.headers.get('Origin','')
    from urllib.parse import urlparse
    expected={request.host.split(':')[0]}
    codespace=os.environ.get('CODESPACE_NAME');domain=os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN','app.github.dev')
    if codespace:expected.add(f'{codespace}-{os.environ.get("PORT","6080")}.{domain}')
    if urlparse(origin).hostname not in expected:raise web.HTTPForbidden(text='Unexpected origin')

async def set_camera(request):
    check_origin(request)
    value=await request.json()
    if value.get('case') not in ('faults','complete'):raise web.HTTPBadRequest()
    (RUN/'camera.json').write_text(json.dumps({'case':value['case']}))
    return web.json_response({'ok':True})

async def websocket(request):
    check_origin(request)
    ws=web.WebSocketResponse(protocols=['binary'],max_msg_size=2*1024*1024,heartbeat=30);await ws.prepare(request)
    reader,writer=await asyncio.open_connection('127.0.0.1',request.app['vnc_port'])
    async def desktop_to_web():
        while block:=await reader.read(65536):await ws.send_bytes(block)
        await ws.close()
    task=asyncio.create_task(desktop_to_web())
    try:
        async for message in ws:
            if message.type==WSMsgType.BINARY:writer.write(message.data);await writer.drain()
            elif message.type==WSMsgType.ERROR:break
    finally:
        task.cancel();writer.close();await writer.wait_closed()
        try:await task
        except (asyncio.CancelledError,ConnectionError):pass
    return ws


def main():
    RUN.mkdir(parents=True,exist_ok=True)
    lock=open(RUN/'server.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    app=web.Application();app.cleanup_ctx.append(start_desktop)
    app.router.add_get('/',index);app.router.add_get('/health',health);app.router.add_post('/camera',set_camera);app.router.add_get('/websockify',websocket)
    app.router.add_static('/novnc/',HERE/'node_modules/@novnc/novnc',show_index=False)
    try:web.run_app(app,host='127.0.0.1',port=int(os.environ.get('PORT','6080')),access_log=None,shutdown_timeout=2)
    finally:stop_processes()
if __name__=='__main__':main()
