import argparse,contextlib,fcntl,json,sys
from pathlib import Path
from .client import Client,CollectorError
from .storage import Store
from .runner import catalogue,feasibility,plan,run,require_gate

@contextlib.contextmanager
def lock(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'.collector.lock').open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise CollectorError('Another collector is using this output directory.')
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)

def main():
    p=argparse.ArgumentParser(description='Agmarknet daily-price collector. macOS/Linux, Python 3.11+.')
    p.add_argument('command',choices=['catalogue','feasibility','pilot','plan','collect','resume','export','status','serve'])
    p.add_argument('--output',default='output');p.add_argument('--start',default='2025-10-26');p.add_argument('--end',default='2026-10-25')
    p.add_argument('--states',default='all');p.add_argument('--commodities',default='all')
    p.add_argument('--max-batches',type=int,default=0,help='0 runs until queue completion or error.')
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8000)
    p.add_argument('--retry-failed',action='store_true');p.add_argument('--refresh-catalogue',action='store_true')
    a=p.parse_args()
    if a.max_batches<0:p.error('--max-batches must be non-negative')
    try:
        if a.command=='serve':
            import uvicorn
            from .app import create_app
            uvicorn.run(create_app(a.output),host=a.host,port=a.port,log_level='info');return 0
        if a.command=='status':
            store=Store(a.output)
            try:print(json.dumps(store.summary(),indent=2))
            finally:store.db.close()
            return 0
        with lock(a.output):
            store=Store(a.output)
            try:
                if a.command in ['export','status']:print(json.dumps(store.export(),indent=2));return 0
                client=Client();filters=catalogue(client,store,a.refresh_catalogue)
                if a.command=='catalogue':result={'states':len(filters['state_data'])-1,'commodities':len(filters['cmdt_data'])}
                elif a.command=='feasibility':
                    result=feasibility(client,store,filters);print(json.dumps(result,indent=2));return 0 if result['passed'] else 2
                elif a.command=='plan':result=plan(store,filters,a.start,a.end,a.states,a.commodities)
                else:
                    require_gate(store)
                    scope=None
                    if a.command=='pilot':scope={'start':'2026-09-01','end':'2026-09-30','states':'Maharashtra','commodities':'Onion'}
                    elif a.command=='collect':scope={'start':a.start,'end':a.end,'states':a.states,'commodities':a.commodities}
                    if scope:plan(store,filters,**scope)
                    result=run(client,store,filters,a.max_batches,a.retry_failed,scope)
                print(json.dumps(result,indent=2));return 2 if result.get('stopped_on_error') else 0
            finally:store.db.close()
    except KeyboardInterrupt:print('Stopped. Checkpoints saved.',file=sys.stderr);return 130
    except (CollectorError,ValueError) as exc:print(str(exc),file=sys.stderr);return 2

if __name__=='__main__':sys.exit(main())
