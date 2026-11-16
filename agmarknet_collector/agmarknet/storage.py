import csv
import hashlib
import json
import math
import sqlite3
from datetime import date,datetime,timezone,timedelta
from pathlib import Path

FIELDS=['date','state','district','market','commodity','variety','grade','min_price','max_price','modal_price','unit','source_unit','source_state','source_district','source_market','source_commodity','source_variety','source_grade','state_id','commodity_id','market_id','retrieved_at','batch_id','source']
DIMENSIONS=['date','state','district','market','commodity','variety','grade']
SOURCE='https://agmarknet.gov.in/daily-price-and-arrival-report'

def series_identity(record):
    dimensions=[record[k] for k in DIMENSIONS[1:]]
    identifier=hashlib.sha256(json.dumps([v.casefold() for v in dimensions],ensure_ascii=False).encode()).hexdigest()
    return identifier,dimensions

def now():return datetime.now(timezone.utc).isoformat()
def text(value):return ' '.join(str(value or '').split())
def number(value,required=False):
    if value is None or text(value) in ['', '-', 'NA','N/A']:
        if required:raise ValueError('Missing modal price')
        return None
    n=float(str(value).replace(',',''))
    if not math.isfinite(n) or n<0:raise ValueError('Invalid or negative price')
    return n

def normalize(raw,batch,filters,timestamp):
    d=None
    for fmt in ['%d-%m-%Y','%Y-%m-%d','%d/%m/%Y']:
        try:d=datetime.strptime(text(raw.get('arrival_date')),fmt).date();break
        except ValueError:pass
    if d is None:raise ValueError('Invalid date')
    if not date.fromisoformat(batch['start'])<=d<=date.fromisoformat(batch['end']):raise ValueError('Date outside requested interval')
    state=next(s['state_name'] for s in filters['state_data'] if s['state_id']==batch['state_id'])
    commodity=next(c['cmdt_name'] for c in filters['cmdt_data'] if c['cmdt_id']==batch['commodity_id'])
    result={'date':d.isoformat()}
    for field,key in [('state','state_name'),('district','district_name'),('market','market_name'),('commodity','cmdt_name'),('variety','variety_name'),('grade','grade_name')]:
        value=text(raw.get(key))
        if not value:
            if field in ['grade','variety']:value='Not reported'
            else:raise ValueError(f'Missing {field}')
        result[field]=value;result['source_'+field]=raw.get(key)
    if result['state'].casefold()!=text(state).casefold():raise ValueError('State does not match request')
    if result['commodity'].casefold()!=text(commodity).casefold():raise ValueError('Commodity does not match request')
    if batch.get('market_ids'):
        allowed={text(m['mkt_name']).casefold() for m in filters['market_data'] if m['id'] in batch['market_ids']}
        if result['market'].casefold() not in allowed:raise ValueError('Market outside requested group')
    if batch.get('market_id'):
        market=next(m['mkt_name'] for m in filters['market_data'] if m['id']==batch['market_id'])
        if result['market'].casefold()!=text(market).casefold():raise ValueError('Market does not match request')
    unit=text(raw.get('unit_name_price'));key=unit.lower().replace(' ','').replace('.','')
    conversions={'rs/quintal':1.,'rs/qtl':1.,'inr/quintal':1.,'₹/quintal':1.,'rs/kg':100.,'inr/kg':100.,'rs/tonne':.1,'rs/ton':.1,'inr/tonne':.1}
    if key not in conversions:raise ValueError('Unknown price unit: '+unit)
    factor=conversions[key]
    for name,source in [('min_price','min_price'),('max_price','max_price'),('modal_price','model_price')]:
        n=number(raw.get(source),required=name=='modal_price');result[name]=None if n is None else n*factor
    low,high,modal=result['min_price'],result['max_price'],result['modal_price']
    if (low is not None and low>modal) or (high is not None and high<modal):raise ValueError('Minimum/modal/maximum prices out of order')
    result.update(unit='INR/quintal',source_unit=unit,state_id=batch['state_id'],commodity_id=batch['commodity_id'],market_id=batch.get('market_id') or None,retrieved_at=timestamp,batch_id=batch['id'],source=SOURCE)
    return result

def batch_id(state,commodity,start,end,market=0):
    return f'{state}-{commodity}-{start}-{end}-{market}'

def month_windows(start,end):
    a=date.fromisoformat(start);last=date.fromisoformat(end)
    if a>last:raise ValueError('Start date must precede end date')
    while a<=last:
        following=date(a.year+1,1,1) if a.month==12 else date(a.year,a.month+1,1)
        b=min(last,following-timedelta(days=1));yield a.isoformat(),b.isoformat();a=b+timedelta(days=1)

class Store:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.root/'agmarknet.sqlite',timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,state_id INTEGER,commodity_id INTEGER,start TEXT,end TEXT,market_id INTEGER DEFAULT 0,parent TEXT,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,rows INTEGER DEFAULT 0,valid INTEGER DEFAULT 0,rejected INTEGER DEFAULT 0,error TEXT,updated TEXT);
        CREATE INDEX IF NOT EXISTS batch_queue ON batches(status,parent);
        CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY,date TEXT,state TEXT,district TEXT,market TEXT,commodity TEXT,variety TEXT,grade TEXT,min_price REAL,max_price REAL,modal_price REAL,unit TEXT,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS observations_series ON observations(state,district,market,commodity,variety,grade,date);
        CREATE TABLE IF NOT EXISTS quarantine(id INTEGER PRIMARY KEY,batch_id TEXT,reason TEXT,raw TEXT,created TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created TEXT,batch_id TEXT,event TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS pages(batch_id TEXT,run_id TEXT,page INTEGER,path TEXT,sha256 TEXT,rows INTEGER,total INTEGER,PRIMARY KEY(batch_id,run_id,page));
        CREATE TABLE IF NOT EXISTS series_state(id TEXT PRIMARY KEY,dimensions TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,observation_key TEXT,previous_payload TEXT,new_payload TEXT,created TEXT);
        ''')
        self.db.commit()

    def add(self,state,commodity,start,end,market=0,parent=None):
        identifier=batch_id(state,commodity,start,end,market)
        self.db.execute('INSERT OR IGNORE INTO batches(id,state_id,commodity_id,start,end,market_id,parent,updated) VALUES(?,?,?,?,?,?,?,?)',(identifier,state,commodity,start,end,market,parent,now()))
        return identifier

    def event(self,batch,event,detail=''):
        self.db.execute('INSERT INTO events(created,batch_id,event,detail) VALUES(?,?,?,?)',(now(),batch,event,str(detail)[:2000]));self.db.commit()

    def mark(self,batch,status,error=None):
        self.db.execute('UPDATE batches SET status=?,error=?,updated=? WHERE id=?',(status,error,now(),batch));self.db.commit()

    def raw(self,batch,run_id,page,body,rows,total):
        from .client import redact
        folder=self.root/'raw'/batch/run_id;folder.mkdir(parents=True,exist_ok=True)
        path=folder/f'{page:05d}.json';content=json.dumps(redact(body),ensure_ascii=False,indent=2).encode()
        path.write_bytes(content)
        self.db.execute('INSERT INTO pages VALUES(?,?,?,?,?,?,?)',(batch,run_id,page,str(path.relative_to(self.root)),hashlib.sha256(content).hexdigest(),rows,total));self.db.commit()

    def finish(self,batch,raw,filters,timestamp):
        valid=[];bad=[];seen=set();changed=set()
        for row in raw:
            try:
                record=normalize(row,batch,filters,timestamp)
                key=json.dumps([record[k].casefold() for k in DIMENSIONS],ensure_ascii=False)
                if key in seen:raise ValueError('Duplicate observation within report; review source conflict')
                seen.add(key);valid.append((key,record))
            except (ValueError,TypeError,StopIteration) as exc:bad.append((row,str(exc)))
        with self.db:
            # Rejected rows from an earlier unsuccessful/replaced attempt are superseded.
            self.db.execute('DELETE FROM quarantine WHERE batch_id=?',(batch['id'],))
            for key,r in valid:
                old=self.db.execute('SELECT payload FROM observations WHERE key=?',(key,)).fetchone()
                encoded=json.dumps(r,ensure_ascii=False)
                if old:
                    prev=json.loads(old['payload'])
                    if any(prev.get(k)!=r.get(k) for k in ['min_price','max_price','modal_price','unit']):
                        self.db.execute('INSERT INTO revisions(observation_key,previous_payload,new_payload,created) VALUES(?,?,?,?)',(key,old['payload'],encoded,timestamp))
                is_changed=not old or any(json.loads(old['payload']).get(k)!=r.get(k) for k in ['min_price','max_price','modal_price','unit'])
                if is_changed:
                    sid,dimensions=series_identity(r)
                    if sid not in changed:
                        self.db.execute('INSERT INTO series_state VALUES(?,?,1,?) ON CONFLICT(id) DO UPDATE SET revision=revision+1,updated=excluded.updated',(sid,json.dumps(dimensions,ensure_ascii=False),timestamp))
                        changed.add(sid)
                self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET min_price=excluded.min_price,max_price=excluded.max_price,modal_price=excluded.modal_price,unit=excluded.unit,payload=excluded.payload',
                    (key,*[r[k] for k in DIMENSIONS],r['min_price'],r['max_price'],r['modal_price'],r['unit'],encoded))
            for r,reason in bad:self.db.execute('INSERT INTO quarantine(batch_id,reason,raw,created) VALUES(?,?,?,?)',(batch['id'],reason,json.dumps(r,ensure_ascii=False),timestamp))
            status='complete_with_quarantine' if bad else ('complete' if raw else 'empty')
            self.db.execute('UPDATE batches SET status=?,rows=?,valid=?,rejected=?,error=NULL,updated=? WHERE id=?',(status,len(raw),len(valid),len(bad),timestamp,batch['id']))
        self.changed_series=changed
        return len(valid),len(bad)

    def summary(self):
        states={r[0]:r[1] for r in self.db.execute('SELECT status,COUNT(*) FROM batches GROUP BY status')}
        stats=dict(self.db.execute('SELECT COUNT(*) observations,MIN(date) earliest,MAX(date) latest,COUNT(DISTINCT state) states,COUNT(DISTINCT commodity) commodities FROM observations').fetchone())
        stats.update(generated=now(),batches=states,quarantined=self.db.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0],source=SOURCE,
            planned_queue_complete=bool(states) and not any(v for k,v in states.items() if k in ['pending','running','failed','blocked','complete_with_quarantine']))
        planfile=self.root/'plan.json'
        stats['last_plan']=json.loads(planfile.read_text()) if planfile.exists() else None
        return stats

    def export(self):
        with (self.root/'prices.csv').open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader()
            for row in self.db.execute('SELECT payload FROM observations ORDER BY state,district,market,commodity,variety,grade,date'):writer.writerow(json.loads(row[0]))
        with (self.root/'quarantine.csv').open('w',newline='',encoding='utf-8') as f:
            writer=csv.writer(f);writer.writerow(['batch_id','reason','raw','created'])
            writer.writerows(self.db.execute('SELECT batch_id,reason,raw,created FROM quarantine'))
        stats=self.summary()
        # Stream series-level missing dates, never generate synthetic observations.
        with (self.root/'series_coverage.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.writer(f);w.writerow(['state','district','market','commodity','variety','grade','first_date','last_date','observations','missing_between_first_last'])
            for r in self.db.execute('SELECT state,district,market,commodity,variety,grade,MIN(date),MAX(date),COUNT(*) FROM observations GROUP BY state,district,market,commodity,variety,grade'):
                span=(date.fromisoformat(r[7])-date.fromisoformat(r[6])).days+1;w.writerow([*r,span-r[8]])
        with (self.root/'quality_flags.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.writer(f);w.writerow(['state','district','market','commodity','variety','grade','date','previous_observed_date','previous_modal','modal','reason'])
            previous=None
            for r in self.db.execute('SELECT state,district,market,commodity,variety,grade,date,modal_price FROM observations ORDER BY state,district,market,commodity,variety,grade,date'):
                if previous and tuple(r[:6])==tuple(previous[:6]) and previous[7]>0 and (r[7]>previous[7]*2 or r[7]<previous[7]*.5):
                    w.writerow([*r[:7],previous[6],previous[7],r[7],'Large change between observed dates; retained for review'])
                previous=r
        (self.root/'coverage.json').write_text(json.dumps(stats,indent=2))
        with (self.root/'batch_status.csv').open('w',newline='',encoding='utf-8') as f:
            cur=self.db.execute('SELECT * FROM batches ORDER BY state_id,commodity_id,start');w=csv.writer(f);w.writerow([d[0] for d in cur.description]);w.writerows(cur)
        return stats
