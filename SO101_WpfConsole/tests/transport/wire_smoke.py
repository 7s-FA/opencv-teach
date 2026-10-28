"""Authenticated TLS transport smoke against the isolated fake ROS peer only."""
import asyncio,json,ssl,uuid,time
from pathlib import Path
import httpx
import jsonschema
import websockets

root=Path(__file__).resolve().parents[2];config=Path.home()/'.config/so101-bridge'
context=ssl.create_default_context(cafile=str(config/'server.crt'))
token=(config/'token').read_text().strip();headers={'Authorization':'Bearer '+token}
checks=[];validated=set()
schema=json.loads((root/'contracts/protocol.schema.json').read_text())
def validate(value,name):
    jsonschema.Draft202012Validator({**schema,'$ref':'#/$defs/'+name},format_checker=jsonschema.FormatChecker()).validate(value)
    validated.add(name)
def check(value,label):
    assert value,label
    checks.append(label)
async def main():
    async with httpx.AsyncClient(base_url='https://127.0.0.1:8443',verify=context,headers=headers,timeout=5) as client:
        check((await client.get('/v1/snapshot',headers={'Authorization':'Bearer invalid'})).status_code==401,'bad token rejected')
        response=await client.post('/v1/session',json={'protocol_version':1});check(response.status_code==201,'session created')
        session=response.json();validate(session,'SessionInfo');generation=session['session_generation'];client.headers['X-Session-Generation']=generation
        check((await client.post('/v1/session',json={'protocol_version':1})).status_code==409,'second operator rejected')
        snapshots=[]
        async with websockets.connect('wss://127.0.0.1:8443/v1/events',ssl=context,additional_headers={**headers,'X-Session-Generation':generation}) as ws:
            async def receive():
                async for raw in ws:
                    value=json.loads(raw)
                    if value.get('type')=='lease':
                        validate(value,'UiLeaseChallenge');await ws.send(json.dumps(dict(type='lease_ack',protocol_version=1,bridge_instance_id=session['bridge_instance_id'],nonce=value['nonce'],session_generation=generation)))
                    elif value.get('type')=='supervision_challenge':
                        validate(value,'SupervisionFrame');value['payload'].pop('expires_in_ms');value['type']='supervision_ack';await ws.send(json.dumps(value))
                    else:validate(value,'GatewayEvent');snapshots.append(value['snapshot'])
            reader=asyncio.create_task(receive())
            async def until(predicate,label,seconds=12):
                end=time.monotonic()+seconds
                while time.monotonic()<end:
                    if reader.done():reader.result()
                    if predicate():return
                    await asyncio.sleep(.05)
                raise AssertionError('Timeout: '+label+' '+str(snapshots[-1:] if snapshots else []))
            await until(lambda:snapshots and all(a['connection']=='ONLINE' for a in snapshots[-1]['arms']),'both online')
            check(True,'fresh UI-Pi challenge/ack roundtrip')
            key=uuid.uuid4().hex;request=dict(protocol_version=1,session_generation=generation,request_id=key,command='BUILD_LOAD_A')
            submitted=await client.post('/v1/commands',json=request);check(submitted.status_code==202,'job admitted');validate(submitted.json(),'CommandReply')
            duplicate=await client.post('/v1/commands',json=request);check(duplicate.json()['code']=='EXISTING_REQUEST','duplicate returns receipt');validate(duplicate.json(),'CommandReply')
            await until(lambda:snapshots[-1]['arms'][0]['job'] and snapshots[-1]['arms'][0]['job']['state']=='BUILD_DONE','chain milestone')
            check(snapshots[-1]['arms'][0]['job']['state']=='BUILD_DONE','BUILD_DONE is not final success')
            control=dict(request,request_id=uuid.uuid4().hex,command='EMER_STOP')
            check((await client.post('/v1/commands',json=control)).status_code==202,'urgent stop accepted')
            await until(lambda:snapshots[-1]['arms'][0]['job']['state']=='PAUSED','job paused')
            await until(lambda:snapshots[-1]['arms'][0]['control']['state']=='SUCCEEDED','stop result')
            check(snapshots[-1]['arms'][0]['job']['request_id']==key,'control result does not overwrite job')
            await asyncio.sleep(.4)
            restart=dict(request,request_id=uuid.uuid4().hex,command='RESTART')
            check((await client.post('/v1/commands',json=restart)).status_code==202,'explicit restart accepted')
            await until(lambda:snapshots[-1]['arms'][0]['control']['command']=='RESTART' and snapshots[-1]['arms'][0]['control']['state']=='SUCCEEDED','restart result')
            (root/'verification/live/lab-peer/finish.json').write_text('{"success":true}')
            await until(lambda:snapshots[-1]['arms'][0]['job']['state']=='SUCCEEDED','final result')
            check(True,'actual Action Result drives success')
            missing=await client.get('/v1/requests/'+uuid.uuid4().hex,params={'command':'BUILD_A','arm':'arm2'});check(missing.json()['result_code']=='NOT_FOUND','missing receipt reconciled by Pi query without execute');validate(missing.json(),'RequestRecord')
            query=await client.get('/v1/requests/'+key);check(query.json()['state']=='SUCCEEDED','request query preserves ID')
            reader.cancel();await asyncio.gather(reader,return_exceptions=True)
        await client.delete('/v1/session')
        stale=await client.post('/v1/commands',json=restart);check(stale.status_code==409,'closed-session commands rejected')
    events=[json.loads(line) for line in (root/'verification/live/lab-peer/events.jsonl').read_text().splitlines()]
    check(sum(e.get('request_id')==key and e['kind']=='execute' for e in events)==1,'exactly one ROS execution for duplicate HTTP intent')
    (root/'verification/live/wire-smoke.json').write_text(json.dumps(dict(passed=len(checks),checks=checks,validated_types=sorted(validated)),ensure_ascii=False,indent=2))
    print(json.dumps(dict(passed=len(checks),checks=checks,validated_types=sorted(validated)),ensure_ascii=False))
asyncio.run(main())
