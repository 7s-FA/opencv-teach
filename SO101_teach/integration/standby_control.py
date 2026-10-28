"""Control existing idle preparation without acquiring or starting devices."""
import json
from urllib.request import Request,urlopen


def cancel(root):
    cancelled=False;errors=[]
    for folder in (root/'data',root/'data/arm3-runtime'):
        meta=folder/'remote-server.json'
        if not meta.exists():continue
        try:
            info=json.loads(meta.read_text());token=(folder/'remote-token').read_text().strip()
            request=Request('http://127.0.0.1:'+str(info['port'])+'/standby',
                data=b'{"operation":"cancel"}',headers={'Authorization':'Bearer '+token})
            with urlopen(request,timeout=1) as response:value=json.load(response)
            if not value.get('ok'):raise RuntimeError(value.get('error','준비 대기 취소 확인 실패'))
            if value['value'].get('cancelled') and not value['value'].get('stopped'):
                raise RuntimeError('준비 대기는 취소했지만 최신 정지 상태를 확인하지 못했습니다.')
            cancelled=bool(value['value'].get('cancelled')) or cancelled
        except Exception as exc:
            errors.append(('arm3' if folder.name=='arm3-runtime' else 'arm2')+': '+str(exc))
    if errors:raise RuntimeError('; '.join(errors))
    return cancelled
