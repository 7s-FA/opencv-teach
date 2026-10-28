"""Validate C# serializer fixtures and negative wire cases with JSON Schema."""
import copy,json
from pathlib import Path
from jsonschema import Draft202012Validator,FormatChecker
ROOT=Path(__file__).resolve().parents[1]
schema=json.loads((ROOT/'contracts/protocol.schema.json').read_text())
Draft202012Validator.check_schema(schema)
mapping={'session':'SessionInfo','command':'CommandRequest','reply':'CommandReply','snapshot':'Snapshot','event':'GatewayEvent','challenge':'SupervisionChallenge','ack':'SupervisionAck'}
count=0
for name,definition in mapping.items():
 value=json.loads((ROOT/f'contracts/examples/{name}.json').read_text())
 validator=Draft202012Validator({**schema,'$ref':'#/$defs/'+definition},format_checker=FormatChecker())
 validator.validate(value);count+=1
 invalid=copy.deepcopy(value);invalid['protocol_version']=2
 assert not validator.is_valid(invalid),name+' accepted wrong version';count+=1
 invalid=copy.deepcopy(value);invalid['unexpected']='field'
 assert not validator.is_valid(invalid),name+' accepted extra field';count+=1
 invalid=copy.deepcopy(value);del invalid['protocol_version']
 assert not validator.is_valid(invalid),name+' accepted missing version';count+=1
command=json.loads((ROOT/'contracts/examples/command.json').read_text());validator=Draft202012Validator({**schema,'$ref':'#/$defs/CommandRequest'})
for field,bad in [('request_id','not-an-id'),('command','RETRY'),('command','BUILD_C')]:
 assert not validator.is_valid({**command,field:bad});count+=1
frame_validator=Draft202012Validator({**schema,'$ref':'#/$defs/SupervisionFrame'})
for kind in ('challenge','ack'):
 payload=json.loads((ROOT/f'contracts/examples/{kind}.json').read_text())
 frame={'type':'supervision_'+kind,'payload':payload}
 frame_validator.validate(frame);count+=1
 bad=copy.deepcopy(frame);bad['payload']['nonce']=''
 assert not frame_validator.is_valid(bad);count+=1
try:
 from openapi_spec_validator import validate_spec
 validate_spec(json.loads((ROOT/'contracts/openapi.json').read_text()))
 openapi='validated'
except ImportError:
 openapi='not run: install openapi-spec-validator from scripts/requirements-validation.txt'
report={'schema_assertions_passed':count,'csharp_examples':len(mapping),'openapi':openapi}
(ROOT/'verification/contracts.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
