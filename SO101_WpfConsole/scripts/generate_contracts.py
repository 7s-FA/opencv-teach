"""Generate the wire schemas and OpenAPI from one explicit protocol definition."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
JOBS=['BUILD_A','LOAD_A','BUILD_B','LOAD_B','BUILD_LOAD_A','BUILD_LOAD_B','PC_BUILD_LOAD_A','PC_BUILD_LOAD_B','BUILD_SLIDE','LOAD_SLIDE','PREPARE_HOME']
CONTROLS=['EMER_STOP','RESTART','RESET','CLEAR_ERROR']
ID={'type':'string','pattern':'^[0-9a-f]{32}$'}
VERSION={'type':'integer','const':1}
TEXT={'type':'string'}
STAMP={'type':'string','format':'date-time'}
def enum(*values): return {'type':'string','enum':list(values)}
def ref(name):return {'$ref':'#/$defs/'+name}
def nullable(name):return {'anyOf':[ref(name),{'type':'null'}]}
def obj(**props):return {'type':'object','additionalProperties':False,'properties':props,'required':list(props)}
state=enum('ACCEPTED','RUNNING','BUILD_DONE','PAUSED','UNKNOWN','SUCCEEDED','FAILED','CANCELLED','CLEARED')
connection=enum('ONLINE','COMM_LOST')
defs={
 'SessionRequest':obj(protocol_version=VERSION),
 'SessionInfo':obj(protocol_version=VERSION,session_generation=ID,bridge_instance_id=ID,demo={'type':'boolean'}),
 'CommandRequest':obj(protocol_version=VERSION,session_generation=ID,request_id=ID,command=enum(*(JOBS+CONTROLS))),
 'RequestRecord':obj(request_id=ID,command=enum(*(JOBS+CONTROLS)),arm=enum('arm2','arm3'),kind=enum('JOB','CONTROL'),state=state,detail=TEXT,updated_at=STAMP,
   result_code={'anyOf':[enum('IDLE','RUNNING','PAUSED','BUILD_DONE','ERROR','PENDING','UNKNOWN','STORAGE_PENDING','NOT_FOUND','CLEARED'),{'type':'null'}]}),
 'CommandReply':obj(protocol_version=VERSION,accepted={'type':'boolean'},code=enum('ACCEPTED','EXISTING_REQUEST','VERSION_MISMATCH','SESSION_EXPIRED','INVALID_REQUEST','INVALID_COMMAND','ID_CONFLICT','NOT_ALLOWED','STORAGE_ERROR','RESPONSE_UNKNOWN'),detail=TEXT,request=nullable('RequestRecord')),
 'ArmSnapshot':obj(arm=enum('arm2','arm3'),connection=connection,job=nullable('RequestRecord'),control=nullable('RequestRecord')),
 'Snapshot':obj(protocol_version=VERSION,bridge_instance_id=ID,session_generation=ID,sequence={'type':'integer','minimum':0},demo={'type':'boolean'},bridge_connection=connection,observed_at=STAMP,arms={'type':'array','items':ref('ArmSnapshot'),'minItems':2,'maxItems':2},storage_error={'type':['string','null']},active_arm=enum('arm2','arm3')),
 'GatewayEvent':obj(protocol_version=VERSION,bridge_instance_id=ID,session_generation=ID,sequence={'type':'integer','minimum':1},event_type=enum('snapshot','state_changed','connection_changed'),occurred_at=STAMP,snapshot=ref('Snapshot')),
 'SupervisionChallenge':obj(protocol_version=VERSION,session_generation=ID,bridge_instance_id=ID,arm=enum('arm2','arm3'),epoch={'type':'string','minLength':1,'maxLength':128},nonce={'type':'string','minLength':1,'maxLength':128},expires_in_ms={'type':'integer','minimum':1,'maximum':1000}),
 'SupervisionAck':obj(protocol_version=VERSION,session_generation=ID,bridge_instance_id=ID,arm=enum('arm2','arm3'),epoch={'type':'string','minLength':1,'maxLength':128},nonce={'type':'string','minLength':1,'maxLength':128}),
 'ErrorResponse':obj(protocol_version=VERSION,code=enum('UNAUTHORIZED','SESSION_EXPIRED','SESSION_IN_USE','INVALID_REQUEST','INVALID_COMMAND','VERSION_MISMATCH','NOT_FOUND','STORAGE_PENDING','UNAVAILABLE','REQUEST_EXPIRED'),detail=TEXT),
 'SessionClosed':obj(protocol_version=VERSION,session_generation=ID,closed={'const':True}),
}
defs['UiLeaseChallenge']=obj(type={'const':'lease'},protocol_version=VERSION,bridge_instance_id=ID,session_generation=ID,nonce=ID,expires_in_ms={'type':'integer','minimum':1,'maximum':1000})
defs['UiLeaseAck']=obj(type={'const':'lease_ack'},protocol_version=VERSION,bridge_instance_id=ID,session_generation=ID,nonce=ID)
defs['SupervisionFrame']={'oneOf':[obj(type={'const':'supervision_challenge'},payload=ref('SupervisionChallenge')),obj(type={'const':'supervision_ack'},payload=ref('SupervisionAck'))]}
schema={'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:so101:control:v1','title':'SO-101 control wire protocol v1','$defs':defs}
(ROOT/'contracts/protocol.schema.json').write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
def component_refs(value):
 if isinstance(value,dict):return {k:(v.replace('#/$defs/','#/components/schemas/') if k=='$ref' else component_refs(v)) for k,v in value.items()}
 if isinstance(value,list):return [component_refs(v) for v in value]
 return value
def content(name):return {'application/json':{'schema':{'$ref':'#/components/schemas/'+name}}}
def response(name,description):return {'description':description,'content':content(name)}
header={'name':'X-Session-Generation','in':'header','required':True,'schema':ID,'description':'Active operator session generation; reject stale or mismatched values.'}
def operation(summary,responses,body=None,params=None):
 value={'summary':summary,'responses':responses}
 if body:value['requestBody']={'required':True,'content':content(body)}
 if params:value['parameters']=params
 return value
errors={'400':response('ErrorResponse','Invalid request'),'408':response('ErrorResponse','Request expired before dispatch'),'401':response('ErrorResponse','Authentication required'),'409':response('ErrorResponse','Session or state conflict'),'503':response('ErrorResponse','Unavailable / storage pending')}
api={'openapi':'3.1.0','info':{'title':'SO-101 operator bridge contract','version':'1.0.0','description':'HTTPS/WSS bridge and offline demo share this protocol. Full snapshots carry the stream cursor; authenticated session lease frames also test UI responsiveness.'},
 'security':[{'BearerToken':[]}],
 'paths':{
  '/v1/session':{
   'post':operation('Create the single operator session',{'201':response('SessionInfo','Created'),'422':response('ErrorResponse','Invalid version/body'),**errors},'SessionRequest'),
   'delete':operation('End supervision without sending RESET',{'200':response('SessionClosed','Closed'),**errors},params=[header])},
  '/v1/snapshot':{'get':operation('Read current state and stream cursor',{'200':response('Snapshot','Snapshot'),**errors},params=[header])},
  '/v1/commands':{'post':operation('Submit a new execution/control intent with an idempotency ID',{
   '202':response('CommandReply','Accepted for processing, not robot success'),
   '200':response('CommandReply','Existing request, no re-execution'),
   '409':response('CommandReply','Stale session, ID conflict or unavailable command'),
   '422':response('CommandReply','Invalid command, ID or protocol version'),
   '401':response('ErrorResponse','Authentication required'),
   '503':response('ErrorResponse','Cannot persist request; do not send normal motion')},'CommandRequest',[header])},
  '/v1/requests/{request_id}':{'get':operation('Query an existing request, never execute',{'200':response('RequestRecord','Request state'),'404':response('ErrorResponse','ID absent from bridge ledger; not proof of no robot execution'),**errors},params=[header,{'name':'request_id','in':'path','required':True,'schema':ID},{'name':'command','in':'query','required':False,'schema':enum(*(JOBS+CONTROLS)),'description':'Original command for a missing API receipt; query only.'},{'name':'arm','in':'query','required':False,'schema':enum('arm2','arm3'),'description':'Original target hint; authoritative stored target wins.'}])},
  '/v1/events':{'get':operation('Upgrade to WebSocket; see websocket.md',{'101':{'description':'Authenticated WebSocket: initial snapshot then ordered events and supervision frames'},'401':response('ErrorResponse','Authentication required'),'409':response('ErrorResponse','Session conflict')},params=[header])}
 },'components':{'securitySchemes':{'BearerToken':{'type':'http','scheme':'bearer'}},'schemas':component_refs(defs)}}
command_responses=api['paths']['/v1/commands']['post']['responses']
command_responses['400']=response('ErrorResponse','Invalid request or protocol version')
command_responses['408']=response('ErrorResponse','Expired before dispatch')
command_responses['413']=response('ErrorResponse','Request body exceeds limit')
command_responses['409']={'description':'Command/state conflict or stale session','content':{'application/json':{'schema':{'oneOf':[{'$ref':'#/components/schemas/CommandReply'},{'$ref':'#/components/schemas/ErrorResponse'}]}}}}
command_responses['503']={'description':'Storage or runtime unavailable','content':{'application/json':{'schema':{'oneOf':[{'$ref':'#/components/schemas/CommandReply'},{'$ref':'#/components/schemas/ErrorResponse'}]}}}}
api['paths']['/v1/events']['get']['responses']['403']={'description':'WebSocket upgrade denied before accept (authentication or session)'}
(ROOT/'contracts/openapi.json').write_text(json.dumps(api,ensure_ascii=False,indent=2)+'\n')
print('Generated protocol.schema.json and openapi.json')
