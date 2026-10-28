"""TLS/bearer API; all ROS and service mutations run on the owning executor."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from bridge_service import BridgeError


def create_app(runtime,token):
    @asynccontextmanager
    async def lifespan(app):
        runtime.start()
        try:yield
        finally:await asyncio.to_thread(runtime.stop)
    app=FastAPI(title='SO-101 control bridge',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    def authenticate(headers):
        if not hmac.compare_digest(headers.get('authorization','').encode('utf-8'), ('Bearer '+token).encode('utf-8')):
            raise BridgeError('UNAUTHORIZED','인증 정보가 일치하지 않습니다.',401)
    def generation(headers):return headers.get('x-session-generation','')
    async def call(method,*args,priority=10):
        return await asyncio.wrap_future(runtime.call(method,*args,priority=priority))
    @app.exception_handler(BridgeError)
    async def error(request,exc):return JSONResponse(status_code=exc.status,content=dict(protocol_version=1,code=exc.code,detail=exc.detail))
    @app.post('/v1/session',status_code=201)
    async def session(request:Request):
        authenticate(request.headers)
        try:value=await request.json()
        except ValueError:raise BridgeError('INVALID_REQUEST','세션 요청 형식 오류',400)
        if value!={'protocol_version':1} or type(value.get('protocol_version')) is not int:raise BridgeError('VERSION_MISMATCH','규약 버전 불일치',422)
        return await call('session')
    @app.delete('/v1/session')
    async def close(request:Request):
        authenticate(request.headers);key=generation(request.headers);await call('close',key,priority=-1)
        return dict(protocol_version=1,session_generation=key,closed=True)
    @app.get('/v1/snapshot')
    async def snapshot(request:Request):
        authenticate(request.headers);return await call('snapshot',generation(request.headers))
    @app.post('/v1/commands')
    async def command(request:Request):
        authenticate(request.headers)
        raw=bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw)>4096:raise BridgeError('INVALID_REQUEST','요청 크기 제한 초과',413)
        try:value=json.loads(raw)
        except (ValueError,TypeError):raise BridgeError('INVALID_REQUEST','JSON 형식 오류',400)
        if not isinstance(value,dict):raise BridgeError('INVALID_REQUEST','명령 형식 오류',400)
        if value.get('session_generation')!=generation(request.headers):raise BridgeError('SESSION_EXPIRED','세션 세대 불일치')
        result=await call('submit',value,priority=0 if value.get('command')=='EMER_STOP' else 10)
        status=202 if result['accepted'] and result['code']=='ACCEPTED' else 200 if result['accepted'] else 503 if result['code']=='STORAGE_ERROR' else 409
        return JSONResponse(status_code=status,content=result)
    @app.get('/v1/requests/{key}')
    async def query(key:str,request:Request):
        authenticate(request.headers);value=await call('query',generation(request.headers),key,request.query_params.get('command'),request.query_params.get('arm'))
        if value is None:raise BridgeError('NOT_FOUND','중계에 접수 기록이 없습니다.',404)
        # Queries for archived IDs are not represented in the two current slots.
        # Briefly await their ROS result without blocking the ROS or WS loop.
        if value['state'] not in ('SUCCEEDED','FAILED','CANCELLED','CLEARED'):
            observed=value['updated_at']
            for _ in range(20):
                await asyncio.sleep(.05)
                value=await call('request_record',generation(request.headers),key)
                if value['updated_at']!=observed:break
        return value
    @app.websocket('/v1/events')
    async def events(ws:WebSocket):
        key=generation(ws.headers);attached=False
        try:
            authenticate(ws.headers);await call('attach',key,priority=-1);attached=True
            await ws.accept()
            async def sender():
                sent=set();counter=0
                while True:
                    # Bounded batches; no replay queue on a restored/suspended VM.
                    messages=await call('pending_challenges',key,priority=-1)
                    for value in messages:
                        identity=(value['arm'],value['epoch'],value['nonce'])
                        if identity not in sent:
                            await asyncio.wait_for(ws.send_json(dict(type='supervision_challenge',payload=value)),.5);sent.add(identity)
                    sent.intersection_update((v['arm'],v['epoch'],v['nonce']) for v in messages)
                    if counter%5==0:
                        await asyncio.wait_for(ws.send_json(await call('lease',key,priority=-1)),.5)
                        state=await call('snapshot',key)
                        event=dict(protocol_version=1,bridge_instance_id=state['bridge_instance_id'],session_generation=key,
                                   sequence=state['sequence'],event_type='snapshot',occurred_at=state['observed_at'],snapshot=state)
                        await asyncio.wait_for(ws.send_json(event),.5)
                    counter+=1;await asyncio.sleep(.05)
            async def receiver():
                while True:
                    raw=await ws.receive_text()
                    if len(raw)>4096:raise BridgeError('INVALID_REQUEST','응답 크기 제한 초과',413)
                    value=json.loads(raw)
                    if not isinstance(value,dict):raise BridgeError('INVALID_REQUEST','응답 형식 오류',400)
                    if value.get('type')=='supervision_ack':
                        if set(value)!={'type','payload'} or not isinstance(value['payload'],dict):raise BridgeError('INVALID_REQUEST','응답 프레임 형식 오류',400)
                        value=value['payload']
                    await call('acknowledge',key,value,priority=-1)
            tasks=[asyncio.create_task(sender()),asyncio.create_task(receiver())]
            try:
                done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for task in done:task.result()
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        except (BridgeError,WebSocketDisconnect,TimeoutError,ValueError,TypeError,RuntimeError):
            try:await ws.close(code=1008)
            except (RuntimeError,WebSocketDisconnect):pass
        finally:
            if attached:
                try:await call('close',key,priority=-1)
                except BridgeError:pass
    return app
