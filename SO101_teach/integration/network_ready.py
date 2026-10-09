"""Read-only IPv4 readiness gate before DDS creates its network transports."""
import ipaddress
import json
import math
import re
import subprocess
import time


def network_state(interface):
    """Require a usable link, IPv4 address and its connected subnet route."""
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}',interface):
        raise ValueError('올바른 네트워크 인터페이스 이름을 지정하세요.')
    def read(*args):
        result=subprocess.run(['ip','-j','-4',*args],capture_output=True,text=True,check=True,timeout=2)
        return json.loads(result.stdout)
    try:
        links=read('address','show','dev',interface)
        if len(links)!=1:return None,'인터페이스 확인 대기'
        link=links[0];flags=set(link.get('flags',[]))
        if link.get('operstate')!='UP' or not {'UP','LOWER_UP','MULTICAST'}<=flags:
            return None,'링크 연결·멀티캐스트 준비 대기'
        addresses=[]
        for value in link.get('addr_info',[]):
            if value.get('family')!='inet' or value.get('scope')!='global':continue
            address=ipaddress.IPv4Address(value['local'])
            if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:continue
            if value.get('valid_life_time')==0 or {'tentative','dadfailed'}&set(value.get('flags',[])):continue
            addresses.append(str(address))
        if not addresses:return None,'사용 가능한 IPv4 주소 대기'
        routes=read('route','show','dev',interface)
        usable=[]
        for route in routes:
            if route.get('scope')!='link' or route.get('type','unicast')!='unicast':continue
            if set(route.get('flags',[]))&{'linkdown','dead'}:continue
            if route.get('dev',interface)!=interface:continue
            subnet=ipaddress.IPv4Network(route['dst'],strict=False)
            if any(ipaddress.IPv4Address(address) in subnet and route.get('prefsrc',address)==address for address in addresses):
                usable.append((str(subnet),route.get('prefsrc','')))
        if not usable:return None,'IPv4 서브넷 통신 경로 대기'
        return {'interface':interface,'ifindex':link['ifindex'],'addresses':sorted(addresses),'routes':sorted(usable)},'준비됨'
    except (OSError,subprocess.SubprocessError,ValueError,TypeError,KeyError):
        return None,'인터페이스·주소·경로 조회 대기'


def wait_for_network(interface='wlan0',timeout=60.,stable_seconds=2.,*,probe=network_state,clock=time.monotonic,sleep=time.sleep,emit=print):
    if not all(math.isfinite(v) and v>0 for v in (timeout,stable_seconds)) or timeout<stable_seconds:
        raise ValueError('네트워크 제한시간은 안정화 시간 이상인 양수여야 합니다.')
    deadline=clock()+timeout;previous=None;stable_at=None;last_reason=None
    emit(f'NETWORK_WAIT: {interface} · IPv4 주소·경로 안정화 확인',flush=True)
    while clock()<deadline:
        state,reason=probe(interface);now=clock()
        if now>=deadline:break
        if state is None:previous=None;stable_at=None
        elif state!=previous:previous=state;stable_at=now
        elif now-stable_at>=stable_seconds:
            emit(f'NETWORK_READY: {interface} · {", ".join(state["addresses"])} · {stable_seconds:g}초 안정',flush=True)
            return state
        if reason!=last_reason:
            emit('NETWORK_STATUS: '+reason,flush=True);last_reason=reason
        sleep(min(.5,max(0.,deadline-clock())))
    raise RuntimeError(f'NETWORK_TIMEOUT: {interface} · {timeout:g}초 안에 IPv4 주소·경로가 안정되지 않았습니다.')
