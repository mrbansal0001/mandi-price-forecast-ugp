"""Bounded diagnostic using normal report access, with secrets omitted."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agmarknet.client import Client,parameters,unpack
f=json.loads(Path('output/catalogue.json').read_text())
with Path('output/access-diagnostics.jsonl').open('a') as log:
 def record(e):log.write(json.dumps(e)+'\n');log.flush();print(json.dumps(e),flush=True)
 c=Client(diagnostic=record)
 for state in [20,1]:
  b={'state_id':state,'commodity_id':78,'start':'2025-10-26','end':'2025-10-31'}
  try:
   response=c.report(parameters(b,f,limit=5));rows,meta=unpack(response);print(json.dumps({'state':state,'rows':len(rows),'total':meta['total_count']}),flush=True)
  except Exception as e:print(type(e).__name__,str(e),flush=True);break
 print(json.dumps(c.stats),flush=True)
