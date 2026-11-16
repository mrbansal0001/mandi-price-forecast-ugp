import hashlib,json,time,uuid
from datetime import date,timedelta
from pathlib import Path
from .client import Client,parameters,unpack,redact,CollectorError,AccessDenied,CaptchaExhausted,SourceChanged,TooLarge
from .storage import Store,normalize,month_windows,now

def catalogue(client,store,refresh=False):
    path=store.root/'catalogue.json'
    if refresh or not path.exists():
        path.write_text(json.dumps(client.filters(),ensure_ascii=False,indent=2))
        (store.root/'catalogue_metadata.json').write_text(json.dumps({'retrieved_at':now(),'source':'https://api.agmarknet.gov.in/v1/daily-price-arrival/filters'},indent=2))
    return json.loads(path.read_text())

def solver_version():
    root=Path(__file__).resolve().parent
    return hashlib.sha256((root/'ocr.py').read_bytes()+(root/'client.py').read_bytes()).hexdigest()

def feasibility(client,store,filters):
    directory=store.root/'feasibility';directory.mkdir(exist_ok=True)
    trials=[];started=time.monotonic();halted=False
    for i in range(10):
        before=dict(client.stats);begin=time.monotonic()
        b={'id':f'feasibility-{i+1}','state_id':20,'commodity_id':23,'start':f'2026-09-{i+1:02d}','end':f'2026-09-{i+1:02d}','market_id':0}
        trial={'number':i+1,'date':b['start'],'success':False,'rows':0}
        try:
            body=client.report(parameters(b,filters,limit=5),fresh=True)
            (directory/f'trial-{i+1:02d}.json').write_text(json.dumps(redact(body),ensure_ascii=False,indent=2))
            rows,meta=unpack(body)
            if not rows:raise SourceChanged('Pilot date has no observations; not an access-success trial.')
            if meta['current_page']!=1 or len(rows)>5:raise SourceChanged('Unexpected page returned.')
            for row in rows:normalize(row,b,filters,now())
            trial.update(success=True,rows=len(rows),available_rows=meta['total_count'])
        except CollectorError as exc:
            trial['error']=str(exc)
            if isinstance(exc,(AccessDenied,CaptchaExhausted)):halted=True
        except (ValueError,KeyError,TypeError) as exc:trial['error']=f'Validation failed: {exc}'
        trial.update(elapsed_seconds=round(time.monotonic()-begin,2),**{k:client.stats[k]-before[k] for k in client.stats})
        trials.append(trial);print(json.dumps(trial),flush=True)
        successes=sum(t['success'] for t in trials)
        result={'created':now(),'solver_version':solver_version(),'trials':trials,'successes':successes,'tested':len(trials),'passed':len(trials)==10 and successes>=9,'elapsed_seconds':round(time.monotonic()-started,2),'halted':halted}
        (store.root/'feasibility.json').write_text(json.dumps(result,indent=2))
        if halted:break
    return result

def require_gate(store):
    p=store.root/'feasibility.json'
    if not p.exists():raise CollectorError('Run feasibility first; bulk collection is gated.')
    result=json.loads(p.read_text())
    if not result.get('passed') or result.get('solver_version')!=solver_version():raise CollectorError('Ten-request feasibility gate did not pass for this client/OCR version.')

def collect_batch(client,store,filters,batch,split_threshold=5000):
    batch=dict(batch);identifier=batch['id'];run_id=uuid.uuid4().hex
    store.db.execute("UPDATE batches SET status='running',attempts=attempts+1,updated=? WHERE id=?",(now(),identifier));store.db.commit()
    rows=[];expected=None;total_pages=None;fingerprints=set();page=1
    try:
        while True:
            body=client.report(parameters(batch,filters,page=page))
            values,meta=unpack(body)
            store.raw(identifier,run_id,page,body,len(values),meta['total_count'])
            if meta['current_page']!=page:raise SourceChanged('Server returned the wrong page.')
            if expected is None:
                expected=meta['total_count'];total_pages=meta['total_pages']
                if expected>split_threshold and (batch['start']!=batch['end'] or not batch['market_id']):raise TooLarge('Split large report into smaller date/market batches.')
            if meta['total_count']!=expected or meta['total_pages']!=total_pages:raise TooLarge('Pagination changed during collection; split smaller windows.')
            if values:
                fingerprint=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
                if fingerprint in fingerprints:raise TooLarge('Repeated page detected.')
                fingerprints.add(fingerprint)
            rows.extend(values)
            if page>=total_pages:break
            if not values:raise SourceChanged('Empty page before end of report.')
            page+=1
            if page>10000:raise TooLarge('Pagination exceeds safety bound.')
        if len(rows)!=expected:raise TooLarge(f'Expected {expected} rows, received {len(rows)}; possible truncation.')
        valid,rejected=store.finish(batch,rows,filters,now())
        store.event(identifier,'finished',f'{len(rows)} rows; {valid} accepted; {rejected} quarantined; {page} pages')
        return {'batch':identifier,'status':'complete_with_quarantine' if rejected else ('complete' if rows else 'empty'),'rows':len(rows),'accepted':valid,'rejected':rejected,'pages':page}
    except TooLarge as exc:
        if batch['start']!=batch['end']:
            first,last=date.fromisoformat(batch['start']),date.fromisoformat(batch['end']);mid=first+(last-first)//2
            ranges=[(first.isoformat(),mid.isoformat(),batch['market_id']),((mid+timedelta(days=1)).isoformat(),last.isoformat(),batch['market_id'])]
        elif not batch['market_id']:
            ranges=[(batch['start'],batch['end'],m['id']) for m in filters['market_data'] if m['state_id']==batch['state_id'] and m['id']<100000]
        else:raise SourceChanged('Single-market single-day report failed pagination checks.') from exc
        if not ranges:raise SourceChanged('No markets available to split report.')
        with store.db:
            for a,b,m in ranges:store.add(batch['state_id'],batch['commodity_id'],a,b,m,identifier)
            store.db.execute("UPDATE batches SET status='split',error=?,updated=? WHERE id=?",(str(exc),now(),identifier))
        return {'batch':identifier,'status':'split','children':len(ranges),'reason':str(exc)}

def selected(items,spec,id_key,name_key):
    actual=[i for i in items if int(i[id_key])<100000]
    if spec=='all':return actual
    requested=[s.strip().casefold() for s in spec.split(',')]
    out=[i for i in actual if str(i[id_key]) in requested or str(i[name_key]).casefold() in requested]
    matched={x for x in requested if any(x in [str(i[id_key]),str(i[name_key]).casefold()] for i in out)}
    if len(matched)!=len(set(requested)):raise ValueError('Unknown selection: '+', '.join(set(requested)-matched))
    return out

def plan(store,filters,start,end,states='all',commodities='all'):
    if date.fromisoformat(start)>date.fromisoformat(end):raise ValueError('Invalid date range')
    price_range=next(r['price'] for r in filters['range_data'] if 'price' in r)
    if start<price_range['from_date'] or end>price_range['to_date']:raise ValueError('Dates exceed current Agmarknet Price report coverage.')
    state_list=selected(filters['state_data'],states,'state_id','state_name')
    commodity_list=selected(filters['cmdt_data'],commodities,'cmdt_id','cmdt_name')
    state_list.sort(key=lambda s:(s['state_id']!=20,s['state_name']))
    commodity_list.sort(key=lambda c:(c['cmdt_id'] not in [23,78,24,1],c['cmdt_name']))
    before=store.db.total_changes
    with store.db:
        for c in commodity_list:
            for s in state_list:
                for a,b in month_windows(start,end):store.add(s['state_id'],c['cmdt_id'],a,b)
    count=len(state_list)*len(commodity_list)*len(list(month_windows(start,end)))
    result={'start':start,'end':end,'states':len(state_list),'commodities':len(commodity_list),'planned_combinations':count,'new_batches':store.db.total_changes-before,'created':now(),'selection':{'states':states,'commodities':commodities}}
    (store.root/'plan.json').write_text(json.dumps(result,indent=2));return result

def run(client,store,filters,max_batches=0,retry_failed=False,scope=None):
    require_gate(store)
    if retry_failed:store.db.execute("UPDATE batches SET status='pending' WHERE status IN ('failed','blocked')")
    store.db.execute("UPDATE batches SET status='pending' WHERE status='running'");store.db.commit()
    finished=0;fatal=False;last_export=time.monotonic()
    clause="status='pending'";args=[]
    if scope:
        states=[s['state_id'] for s in selected(filters['state_data'],scope['states'],'state_id','state_name')]
        commodities=[c['cmdt_id'] for c in selected(filters['cmdt_data'],scope['commodities'],'cmdt_id','cmdt_name')]
        clause+=' AND state_id IN ('+','.join('?' for _ in states)+') AND commodity_id IN ('+','.join('?' for _ in commodities)+') AND start>=? AND end<=?'
        args=[*states,*commodities,scope['start'],scope['end']]
    try:
        while not max_batches or finished<max_batches:
            batch=store.db.execute("SELECT * FROM batches WHERE "+clause+" ORDER BY parent IS NULL, rowid LIMIT 1",args).fetchone()
            if batch is None:break
            started=time.monotonic()
            try:result=collect_batch(client,store,filters,batch)
            except (CollectorError,ValueError,KeyError,TypeError) as exc:
                blocked=isinstance(exc,(AccessDenied,CaptchaExhausted))
                store.mark(batch['id'],'blocked' if blocked else 'failed',str(exc));store.event(batch['id'],'error',str(exc))
                result={'batch':batch['id'],'status':'blocked' if blocked else 'failed','error':str(exc)};fatal=True
            except KeyboardInterrupt:
                store.mark(batch['id'],'pending','Interrupted; restart from page 1.');raise
            result['elapsed_seconds']=round(time.monotonic()-started,2);print(json.dumps(result),flush=True);finished+=1
            if time.monotonic()-last_export>=300:
                store.export();last_export=time.monotonic()
            if fatal:break
    finally:store.export()
    return {'processed_batches':finished,'stopped_on_error':fatal,'requests':client.stats}
