"""Per-state workflow (Uttar Pradesh, Rajasthan) and persisted jobs; never consumes the national queue."""
import contextlib,fcntl,hashlib,json,threading,time,uuid
from collections import Counter
from datetime import datetime,timedelta,timezone,date
from pathlib import Path
from .client import Client,CollectorError,AccessDenied,CaptchaExhausted,RateLimited,Unavailable,SourceChanged,parameters,unpack
from .storage import Store,now,series_identity
from .forecast import analyze,today,VERSION,MAX_AGE_DAYS

STATES={'Uttar Pradesh':34,'Rajasthan':29}   # tracked states and their source ids; the first is the default
DEFAULT_STATE='Uttar Pradesh'
# A new state starts from the commodities already seen in the other tracked states plus these local specialities; the weekly census finds the rest.
EXTRAS={'Rajasthan':('Ajwan','Camel Hair','Castor Seed','Cummin Seed(Jeera)','Guar','Guar Seed(Cluster Beans Seed)','Isabgul(Psyllium)','Methi Seeds','Moath Dal','Taramira',
    'Kulthi(Horse Gram)','Jowar(Sorghum)','Coriander(Leaves)','Cotton','Groundnut','Soyabean','Sesamum(Sesame,Gingelly,Til)','Barley(Jau)','Dry Chillies','Goat','Sheep')}
HISTORY_DAYS=365          # the source only serves ranges within one year
PAGE_LIMIT=5000           # verified: the source serves 5,000-row pages and reuses one ticket across pages
MAX_WINDOW_ROWS=250_000   # larger reports are split by date; UP's biggest crop (wheat) is about 90,000 a year
LISTED_DAYS=30            # series silent for longer are hidden from farmers
UPDATE_DAYS=14            # reports arrive days late, so each update re-requests two weeks
# Measured: the source throttles after roughly 100 requests in a burst and then sustains only about 2.5 per minute, so requests are the scarce resource.
UNLIKELY=('Flowers','Forest Products','Drug and Narcotics','Medicinal and Aromatic Plants','Live Stock,Poultry,Fisheries')   # groups with no prices in the first 21 Uttar Pradesh probes
CENSUS_EXTRA=40           # least recently checked commodities probed in each weekly census
PACE_START,PACE_MIN,PACE_MAX=7.5,4.0,30.0   # seconds between requests; learned from throttling and kept across restarts
MAX_DEFERRALS=12          # automatic retries of one run before it pauses for attention
CACHE_SECONDS=60
ARRIVAL_DAYS=21           # recent days whose row counts are compared between updates, to see when new data lands
FIRST=('Wheat','Potato','Rice','Tomato','Onion','Green Chilli','Maize','Mustard','Garlic','Brinjal','Cauliflower','Cabbage','Bajra(Pearl Millet/Cumbu)')  # fetched first so useful data arrives early

class Deferred(CollectorError):
    """Some crops could not be fetched now (CAPTCHA/rate limit); the rest is saved and the run is retried later."""
    def __init__(self,message,minutes=10):super().__init__(message);self.minutes=minutes

class Service:
    def __init__(self,root,clock=today,client_factory=Client):
        self.root=Path(root).resolve();self.clock=clock;self.client_factory=client_factory;self.mutex=threading.Lock()
        self.cache={};self.cache_lock=threading.Lock();self.groups=None;self.generation=0;self.refreshing=set();self.local=threading.local();self.background_refresh=False
        self.initialize()

    @contextlib.contextmanager
    def store(self):
        s=Store(self.root)
        try:yield s
        finally:s.db.close()

    def initialize(self):
        with self.store() as s:
            s.db.executescript('''
            CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS app_runs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,attempt INTEGER DEFAULT 0,created TEXT,started TEXT,finished TEXT,retry_at TEXT,detail TEXT,progress INTEGER DEFAULT 0,total INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS app_diagnostics(id INTEGER PRIMARY KEY,run_id TEXT,created TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS app_forecasts(series_id TEXT,target_date TEXT,revision INTEGER,model_version TEXT,generated TEXT,payload TEXT,PRIMARY KEY(series_id,target_date,revision,model_version));
            CREATE TABLE IF NOT EXISTS app_arrivals(id INTEGER PRIMARY KEY,run_id TEXT,state TEXT,kind TEXT,at TEXT,changed_series INTEGER,newest TEXT,counts TEXT);
            CREATE TABLE IF NOT EXISTS app_windows(run_id TEXT,window_key TEXT,status TEXT,batch_id TEXT,PRIMARY KEY(run_id,window_key));
            ''')
            for row in s.db.execute('SELECT state,district,market,commodity,variety,grade FROM observations GROUP BY state,district,market,commodity,variety,grade').fetchall():
                r=dict(row);sid,dims=series_identity(r);s.db.execute('INSERT OR IGNORE INTO series_state VALUES(?,?,1,?)',(sid,json.dumps(dims,ensure_ascii=False),now()))
            s.db.execute("INSERT OR IGNORE INTO app_settings VALUES ('collection_paused','false')")
            if 'state' not in {r[1] for r in s.db.execute('PRAGMA table_info(app_runs)')}:
                s.db.execute('ALTER TABLE app_runs ADD COLUMN state TEXT')
                s.db.execute("UPDATE app_runs SET state=? WHERE kind!='forecast'",(DEFAULT_STATE,))
                # Settings written while Uttar Pradesh was the only state become that state's settings.
                for old,new in [('up_history_complete','history_complete'),('backfill_range','backfill_range'),('last_successful_update','last_successful_update'),
                        ('trial_started','trial_started'),('last_weekly_update','last_weekly_update')]:
                    s.db.execute('INSERT OR IGNORE INTO app_settings SELECT ?,value FROM app_settings WHERE key=?',(f'{new}:{DEFAULT_STATE}',old))
            s.db.commit()

    def get_setting(self,key,default=None):
        with self.store() as s:
            r=s.db.execute('SELECT value FROM app_settings WHERE key=?',(key,)).fetchone();return json.loads(r[0]) if r else default
    def setting(self,key,value):
        with self.store() as s:s.db.execute('INSERT INTO app_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)));s.db.commit()

    def state_get(self,state,key,default=None):return self.get_setting(f'{key}:{state}',default)
    def state_set(self,state,key,value):self.setting(f'{key}:{state}',value)
    def last_update(self):
        """Most recent successful price check in any state (used to detect that stored data changed)."""
        return max((self.state_get(st,'last_successful_update') or '' for st in STATES),default='') or None

    def memo(self,key,build):
        """Short-lived cache for list/summary queries that scan every series; any saved window invalidates it.
        With background_refresh (the live server) an expired entry is served while one thread rebuilds it, so a visitor never waits behind the collector."""
        with self.cache_lock:
            hit=self.cache.get(key);generation=self.generation
            if hit and time.monotonic()-hit[0]<CACHE_SECONDS and hit[2]==self.clock() and hit[3]==generation:return hit[1]
            if hit and self.background_refresh and not getattr(self.local,'rebuilding',False):
                if key not in self.refreshing:
                    self.refreshing.add(key);threading.Thread(target=self.rebuild,args=(key,build),name='cache-refresh',daemon=True).start()
                return hit[1]
        value=build()
        with self.cache_lock:self.cache[key]=(time.monotonic(),value,self.clock(),generation)
        return value
    @contextlib.contextmanager
    def fresh(self):
        """Reads inside this block never get a stale cached list (forecast building must see every saved window)."""
        before=getattr(self.local,'rebuilding',False);self.local.rebuilding=True
        try:yield
        finally:self.local.rebuilding=before
    def warm(self):
        """Rebuild the farmer lists now, so the first page load after a job already sees its results."""
        with self.fresh():
            for state in STATES:self.picker(state)
    def rebuild(self,key,build):
        self.local.rebuilding=True   # lists this one is built from must be read fresh, not served stale
        try:
            with self.cache_lock:generation=self.generation
            value=build()
            with self.cache_lock:self.cache[key]=(time.monotonic(),value,self.clock(),generation)
        except Exception:pass   # the stale entry keeps being served and the next request tries again
        finally:
            self.local.rebuilding=False
            with self.cache_lock:self.refreshing.discard(key)
    def clear_cache(self):
        with self.cache_lock:
            self.generation+=1
            if not self.background_refresh:self.cache.clear()

    def recover(self):
        # Only reset interrupted work when no collector owns the common lock.
        with (self.root/'.collector.lock').open('a') as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            with self.store() as s:
                s.db.execute("UPDATE app_runs SET status='queued',detail='Recovered interrupted run; completed windows preserved.' WHERE status='running'");s.db.commit()
            fcntl.flock(handle,fcntl.LOCK_UN)

    def enqueue_all(self,kind,manual=False):
        """The same job for every tracked state (forecasts are shared and queued once)."""
        if kind=='forecast':return [self.enqueue(kind,manual)]
        return [self.enqueue(kind,manual,state) for state in STATES]

    def enqueue(self,kind='update',manual=False,state=None):
        if kind not in ['bootstrap','update','weekly','forecast']:raise ValueError('Unknown job kind')
        state=None if kind=='forecast' else (state or DEFAULT_STATE)
        if state is not None and state not in STATES:raise ValueError('Unknown state')
        if self.get_setting('collection_paused',False) and kind!='forecast':
            if not manual:return None
            self.setting('collection_paused',False)
        with self.store() as s:
            s.db.execute('BEGIN IMMEDIATE')
            existing=s.db.execute("SELECT id FROM app_runs WHERE kind=? AND state IS ? AND status IN ('queued','running','retry_wait') ORDER BY created LIMIT 1",(kind,state)).fetchone()
            if existing:
                s.db.commit();return existing[0]
            identifier=uuid.uuid4().hex
            s.db.execute('INSERT INTO app_runs(id,kind,state,status,created) VALUES(?,?,?,?,?)',(identifier,kind,state,'queued',now()));s.db.commit();return identifier

    def diagnostic(self,run_id,event):
        with self.store() as s:s.db.execute('INSERT INTO app_diagnostics(run_id,created,payload) VALUES(?,?,?)',(run_id,now(),json.dumps(event)));s.db.commit()

    def filters(self,client):
        p=self.root/'catalogue.json'
        if not p.exists():p.write_text(json.dumps(client.filters(),ensure_ascii=False,indent=2))
        return json.loads(p.read_text())

    def commodities(self,filters):
        groups={g['id']:g['cmdt_grp_name'] for g in filters.get('cmdt_group_data',[])}
        return [{'id':c['cmdt_id'],'name':c['cmdt_name'],'group':groups.get(c.get('cmdt_group_id'),'Other')} for c in filters['cmdt_data']]

    def group_of(self,commodity):
        if self.groups is None:
            p=self.root/'catalogue.json'
            self.groups={c['name'].casefold():c['group'] for c in self.commodities(json.loads(p.read_text()))} if p.exists() else {}
        return self.groups.get(commodity.casefold(),'Other')

    def by_priority(self,items):
        """Known big UP crops first, then the groups most likely to have prices, alphabetically within a group."""
        rank={g:i for i,g in enumerate(['Vegetables','Cereals','Pulses','Fruits','Spices','Oil Seeds','Oils and Fats','Dry Fruits','Fibre Crops','Beverages','Others'])}
        return sorted(items,key=lambda c:(FIRST.index(c['name']) if c['name'] in FIRST else len(FIRST),rank.get(c['group'],99),c['name']))

    def checked(self,state):
        """When each commodity last had a window fetched successfully for this state, from the batch records."""
        with self.store() as s:
            return {r[0]:r[1] for r in s.db.execute("SELECT commodity_id,MAX(updated) FROM batches WHERE state_id=? AND status IN ('complete','complete_with_quarantine','empty','split') GROUP BY commodity_id",(STATES[state],))}

    def census_items(self,filters,state=DEFAULT_STATE):
        """Active commodities plus the least recently checked others, so new ones are found without a full 605-query sweep."""
        active=self.active_commodities(filters,state);ids={c['id'] for c in active};checked=self.checked(state)
        others=sorted((c for c in self.commodities(filters) if c['id'] not in ids),key=lambda c:(checked.get(c['id'],''),c['name']))
        return active+others[:CENSUS_EXTRA]

    def active_commodities(self,filters,state=DEFAULT_STATE):
        """Commodities the state has reported recently; the weekly census adds newly appearing ones."""
        cutoff=(self.clock()-timedelta(days=60)).isoformat()
        with self.store() as s:names={r[0].casefold() for r in s.db.execute('SELECT DISTINCT commodity FROM observations WHERE state=? AND date>=?',(state,cutoff))}
        return [c for c in self.commodities(filters) if c['name'].casefold() in names]

    def bootstrap_items(self,filters,state=DEFAULT_STATE):
        """Commodities to build a state's history from. A state added later skips commodities nobody else reports, which would each cost a CAPTCHA to learn they are empty."""
        items=self.by_priority(self.commodities(filters))
        if state not in EXTRAS:return [c for c in items if c['group'] not in UNLIKELY]   # the unlikely groups are probed by the weekly census
        with self.store() as s:known={r[0].casefold() for r in s.db.execute('SELECT DISTINCT commodity FROM observations WHERE state!=?',(state,))}
        known|={n.casefold() for n in EXTRAS[state]}
        return [c for c in items if c['name'].casefold() in known]

    def progress(self,run_id,detail,done=None,total=None):
        with self.store() as s:
            s.db.execute('UPDATE app_runs SET detail=? WHERE id=?',(detail,run_id))
            if done is not None:s.db.execute('UPDATE app_runs SET progress=? WHERE id=?',(done,run_id))
            if total is not None:s.db.execute('UPDATE app_runs SET total=? WHERE id=?',(total,run_id))
            s.db.commit()

    def window(self,client,filters,run_id,state,crop_id,start,end,archive=True):
        """One state report (all districts and markets) for one commodity and date interval, committed atomically."""
        key=json.dumps([crop_id,start,end]);batch_id='w-'+hashlib.sha256((run_id+key).encode()).hexdigest()[:24]
        with self.store() as s:
            done=s.db.execute("SELECT status FROM app_windows WHERE run_id=? AND window_key=?",(run_id,key)).fetchone()
            if done and done[0]=='complete':return set()
            s.db.execute("INSERT OR IGNORE INTO batches(id,state_id,commodity_id,start,end,market_id,updated) VALUES(?,?,?,?,?,0,?)",(batch_id,STATES[state],crop_id,start,end,now()))
            s.db.execute("INSERT OR IGNORE INTO app_windows VALUES(?,?,'pending',?)",(run_id,key,batch_id));s.db.commit()
        batch={'id':batch_id,'state_id':STATES[state],'commodity_id':crop_id,'start':start,'end':end,'market_id':0}
        rows=[];total=None;pages=None;fingerprints=set();page=1;attempt=uuid.uuid4().hex
        while True:
            body=client.report(parameters(batch,filters,page=page,limit=PAGE_LIMIT));chunk,meta=unpack(body)
            if archive:
                with self.store() as s:s.raw(batch_id,attempt,page,body,len(chunk),meta['total_count'])
            if meta['current_page']!=page:raise SourceChanged('Wrong page in report.')
            if total is None:
                total=meta['total_count'];pages=meta['total_pages']
                if total>MAX_WINDOW_ROWS:
                    a,b=date.fromisoformat(start),date.fromisoformat(end)
                    if a>=b:raise SourceChanged('A single-day report exceeds the size limit.')
                    mid=a+(b-a)//2
                    changed=self.window(client,filters,run_id,state,crop_id,start,mid.isoformat(),archive)|self.window(client,filters,run_id,state,crop_id,(mid+timedelta(days=1)).isoformat(),end,archive)
                    with self.store() as s:
                        s.db.execute("UPDATE app_windows SET status='complete' WHERE run_id=? AND window_key=?",(run_id,key));s.mark(batch_id,'split')
                    return changed
            if meta['total_count']!=total or meta['total_pages']!=pages:raise SourceChanged('Source pagination changed; retry this window later.')
            if chunk:
                fp=hashlib.sha256(json.dumps(chunk,sort_keys=True).encode()).hexdigest()
                if fp in fingerprints:raise SourceChanged('Repeated source page; window not committed.')
                fingerprints.add(fp)
            rows.extend(chunk)
            if page>=pages:break
            if not chunk:raise SourceChanged('Unexpected empty page.')
            page+=1
            self.progress(run_id,f'Downloading page {page}/{pages} ({total:,} rows)')
            if page>10000:raise SourceChanged('Unexpected page count.')
        if len(rows)!=total:raise SourceChanged('Report total does not match downloaded rows.')
        with self.store() as s:
            valid,rejected=s.finish(batch,rows,filters,now())
            s.db.execute("UPDATE app_windows SET status='complete' WHERE run_id=? AND window_key=?",(run_id,key));s.db.commit()
            changed=s.changed_series
        self.clear_cache()
        return changed

    def sweep(self,client,filters,run_id,state,items,start,end,archive):
        """One window per commodity. A CAPTCHA failure defers that crop; throttling stops the sweep for a cooldown."""
        deferred=[];minutes=10;total=len(items);changed=set();throttled=False;outage=False
        self.progress(run_id,f'{state}: fetching {total} commodities',done=0,total=total)
        for i,item in enumerate(items):
            try:changed|=self.window(client,filters,run_id,state,item['id'],start,end,archive)
            except RateLimited as exc:deferred=items[i:];minutes=exc.minutes;outage=isinstance(exc,Unavailable);throttled=not outage;break
            except CaptchaExhausted:deferred.append(item)
            self.progress(run_id,f'{item["name"]} ({i+1}/{total})',done=i+1)
        if not outage:self.adjust_pace(throttled)   # an outage says nothing about the source's request limit
        return changed,deferred,minutes

    def pace(self):return float(self.get_setting('pace_seconds',PACE_START))
    def adjust_pace(self,throttled):
        """Slow down by half after a throttle; speed up by a tenth after a clean sweep. The source's real limit is not published."""
        pace=self.pace();self.setting('pace_seconds',round(min(PACE_MAX,pace*1.5) if throttled else max(PACE_MIN,pace*.9),2))

    def conclude(self,deferred,total,minutes):
        if deferred:raise Deferred(f'{len(deferred)} of {total} commodities deferred (CAPTCHA/throttling); retrying automatically. Saved data is kept.',minutes)

    def history_days(self):return int(self.get_setting('history_days',HISTORY_DAYS))

    def bootstrap(self,client,filters,run_id,state=DEFAULT_STATE):
        """One-time history: a single whole-period report per commodity. Small crops need one request in total."""
        as_of=self.clock();start=(as_of-timedelta(days=self.history_days()-1)).isoformat()
        items=self.bootstrap_items(filters,state)
        _,deferred,minutes=self.sweep(client,filters,run_id,state,items,start,as_of.isoformat(),archive=True)
        self.refresh_forecasts();self.conclude(deferred,len(items),minutes)
        self.state_set(state,'history_complete',True);self.state_set(state,'backfill_range',{'from':start,'to':as_of.isoformat()})
        self.state_set(state,'last_successful_update',now())
        if not self.state_get(state,'trial_started'):self.state_set(state,'trial_started',now())

    def update(self,client,filters,run_id,as_of,days,census=False,state=DEFAULT_STATE):
        """Recent days for commodities the state currently reports; census=True also probes the least recently checked others to find new ones."""
        items=self.census_items(filters,state) if census else self.active_commodities(filters,state)
        if not items:raise SourceChanged(f'No {state} commodities known; build the history first.')
        start=(as_of-timedelta(days=days)).isoformat()
        changed,deferred,minutes=self.sweep(client,filters,run_id,state,items,start,as_of.isoformat(),archive=False)
        self.refresh_forecasts();self.conclude(deferred,len(items),minutes)
        self.state_set(state,'last_successful_update',now());self.log_arrival(state,'weekly' if census else 'update',run_id,changed);return changed

    def log_arrival(self,state,kind,run_id,changed):
        """Snapshot of rows per recent date after a completed update; the difference between snapshots shows when the source publishes new data."""
        since=(self.clock()-timedelta(days=ARRIVAL_DAYS)).isoformat()
        with self.store() as s:
            counts={r[0]:r[1] for r in s.db.execute('SELECT date,COUNT(*) FROM observations WHERE state=? AND date>=? GROUP BY date',(state,since))}
            s.db.execute('INSERT INTO app_arrivals(run_id,state,kind,at,changed_series,newest,counts) VALUES(?,?,?,?,?,?,?)',(run_id,state,kind,now(),len(changed),max(counts,default=None),json.dumps(counts)));s.db.commit()

    def arrivals(self,limit=14):
        """Recent completed updates with the rows each one added per date, compared with the previous snapshot of the same state."""
        with self.store() as s:rows=[dict(r) for r in s.db.execute('SELECT * FROM app_arrivals ORDER BY id DESC LIMIT ?',(limit+len(STATES)*2,))]
        out=[]
        for i,r in enumerate(rows):
            if len(out)>=limit:break
            counts=json.loads(r['counts']);previous=next((json.loads(o['counts']) for o in rows[i+1:] if o['state']==r['state']),None)
            added={d:n-previous.get(d,0) for d,n in counts.items() if n-previous.get(d,0)>0} if previous is not None else None
            out.append({'state':r['state'],'kind':r['kind'],'at':r['at'],'changed_series':r['changed_series'],'newest':r['newest'],
                'new_rows':sum(added.values()) if added is not None else None,'added':sorted(added.items(),reverse=True)[:6] if added is not None else None})
        return out

    def series(self,listed_days=LISTED_DAYS,state=None):
        """Tracked-state series (one state, or all) that reported within listed_days (0 = all), with data revisions. One grouped query."""
        def build():
            cutoff=(self.clock()-timedelta(days=listed_days)).isoformat() if listed_days else ''
            with self.store() as s:
                revisions={r['id']:r['revision'] for r in s.db.execute('SELECT id,revision FROM series_state')}
                rows=s.db.execute('SELECT state,district,market,commodity,variety,grade,MAX(date) latest,COUNT(*) count FROM observations WHERE state IN (%s) GROUP BY state,district,market,commodity,variety,grade'%','.join('?'*len(STATES)),tuple(STATES)).fetchall()
            merged={}
            for r in rows:
                sid,_=series_identity(dict(r));row=merged.get(sid)
                if row:row['count']+=r['count'];row['latest']=max(row['latest'],r['latest']);continue
                merged[sid]={'id':sid,'revision':revisions.get(sid,1),'group':self.group_of(r['commodity']),'state':r['state'],'district':r['district'],'market':r['market'],
                    'commodity':r['commodity'],'variety':r['variety'],'grade':r['grade'],'latest':r['latest'],'count':r['count']}
            return sorted((v for v in merged.values() if v['latest']>=cutoff),key=lambda r:(r['state'],r['group'],r['commodity'],r['variety'],r['grade'],r['district'],r['market']))
        everything=self.memo(('series',listed_days),build)
        return [r for r in everything if r['state']==state] if state else everything

    def picker(self,state=DEFAULT_STATE):
        """One state's series list for the farmer page: the fields the filters need plus f=1 when tomorrow's forecast is available."""
        def build():
            target=(self.clock()+timedelta(days=1)).isoformat()
            with self.store() as s:
                ready={(r[0],r[1]) for r in s.db.execute("SELECT series_id,revision FROM app_forecasts WHERE target_date=? AND model_version=? AND json_extract(payload,'$.status')='updated'",(target,VERSION))}
            return [{**{k:r[k] for k in ('id','group','commodity','variety','grade','district','market','count')},'f':int((r['id'],r['revision']) in ready)} for r in self.series(state=state)]
        return self.memo(('picker',state),build)

    def history(self,sid):
        with self.store() as s:
            record=s.db.execute('SELECT * FROM series_state WHERE id=?',(sid,)).fetchone()
            if not record:raise KeyError('Series not found')
            dims=json.loads(record['dimensions'])
            rows=[dict(r) for r in s.db.execute('SELECT date,modal_price price,min_price,max_price FROM observations WHERE state=? AND district=? AND market=? AND commodity=? AND variety=? AND grade=? ORDER BY date',dims)]
            return dict(record),rows

    def refresh_forecasts(self):
        """Forecast every listed series that lacks a result for tomorrow and its current data revision."""
        target=(self.clock()+timedelta(days=1)).isoformat();computed=0
        with self.store() as s:have={(r[0],r[1]) for r in s.db.execute('SELECT series_id,revision FROM app_forecasts WHERE target_date=? AND model_version=?',(target,VERSION))}
        with self.fresh():listed=self.series()
        for series in listed:
            if (series['id'],series['revision']) in have:continue
            record,rows=self.history(series['id']);result=analyze(rows,self.clock());stamp=now()
            with self.store() as s:
                with s.db:
                    current=s.db.execute('SELECT revision FROM series_state WHERE id=?',(series['id'],)).fetchone()[0]
                    if current!=record['revision']:continue
                    s.db.execute('INSERT OR IGNORE INTO app_forecasts VALUES(?,?,?,?,?,?)',(series['id'],target,record['revision'],VERSION,stamp,json.dumps(result,allow_nan=False)))
            computed+=1
        self.clear_cache()
        return computed

    def forecast(self,sid):
        record,rows=self.history(sid);target=(self.clock()+timedelta(days=1)).isoformat()
        with self.store() as s:cached=s.db.execute('SELECT * FROM app_forecasts WHERE series_id=? AND target_date=? AND revision=? AND model_version=?',(sid,target,record['revision'],VERSION)).fetchone()
        if not cached:return {'status':'updating','target_date':target,'data_revision':record['revision'],'forecast':None,'performance':[],'reason':'Preparing a forecast for the current date and data revision.'}
        return {**json.loads(cached['payload']),'generated_at':cached['generated'],'data_revision':cached['revision']}

    def run_next(self):
        if not self.mutex.acquire(blocking=False):return False
        handle=(self.root/'.collector.lock').open('a')
        try:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return False
            with self.store() as s:
                paused=self.get_setting('collection_paused',False)
                where="AND kind='forecast'" if paused else ''
                # Short updates go before a long history build so tomorrow's forecasts do not wait hours behind it.
                run=s.db.execute("SELECT * FROM app_runs WHERE (status='queued' OR (status='retry_wait' AND retry_at<=?)) "+where+" ORDER BY kind='bootstrap',created LIMIT 1",(now(),)).fetchone()
                if not run:return False
                run=dict(run);s.db.execute("UPDATE app_runs SET status='running',started=?,finished=NULL WHERE id=?",(now(),run['id']));s.db.commit()
            try:
                if run['kind']=='forecast':self.refresh_forecasts()
                else:
                    client=self.client_factory(diagnostic=lambda e:self.diagnostic(run['id'],e),interval=self.pace());filters=self.filters(client);state=run['state'] or DEFAULT_STATE
                    if run['kind']=='bootstrap':self.bootstrap(client,filters,run['id'],state)
                    elif run['kind']=='weekly':self.update(client,filters,run['id'],self.clock(),30,census=True,state=state)
                    else:self.update(client,filters,run['id'],self.clock(),UPDATE_DAYS,state=state)
                with self.store() as s:
                    s.db.execute("UPDATE app_runs SET status='succeeded',finished=?,retry_at=NULL,detail='Completed. Validated prices and forecasts saved.' WHERE id=?",(now(),run['id']));s.db.commit()
                if run['kind']=='weekly':self.state_set(run['state'] or DEFAULT_STATE,'last_weekly_update',now())
                with contextlib.suppress(Exception):self.warm()
            except Exception as exc:
                immediate=isinstance(exc,(AccessDenied,SourceChanged))
                if isinstance(exc,(Deferred,RateLimited)):retry,wait=run['attempt']<MAX_DEFERRALS,exc.minutes*2**min(run['attempt'],1)   # 15 then 30 minutes: the source refills roughly 40 requests per 15 minutes; an outage waits the same way
                else:retry,wait=(not immediate and run['attempt']==0),30
                status='retry_wait' if retry else 'paused'
                retry_at=(datetime.now(timezone.utc)+timedelta(minutes=wait)).isoformat() if retry else None
                with self.store() as s:
                    s.db.execute('UPDATE app_runs SET status=?,attempt=attempt+1,finished=?,retry_at=?,detail=? WHERE id=?',(status,now(),retry_at,f'{type(exc).__name__}: {str(exc)[:700]}',run['id']));s.db.commit()
                if not retry:self.setting('collection_paused',True)
            return True
        finally:
            fcntl.flock(handle,fcntl.LOCK_UN);handle.close();self.mutex.release()

    def public_status(self):
        with self.store() as s:
            running=s.db.execute("SELECT kind,state,detail,progress,total,started FROM app_runs WHERE status='running' ORDER BY started DESC LIMIT 1").fetchone()
            waiting=s.db.execute("SELECT kind,state,detail,retry_at FROM app_runs WHERE status='retry_wait' ORDER BY created DESC LIMIT 1").fetchone()
        states=[{'name':st,'history_complete':bool(self.state_get(st,'history_complete',False)),'last_successful_update':self.state_get(st,'last_successful_update')} for st in STATES]
        return {'state':DEFAULT_STATE,'states':states,'today':self.clock().isoformat(),'last_successful_update':self.last_update(),
            'collection_paused':self.get_setting('collection_paused',False),'history_complete':all(x['history_complete'] for x in states),
            'running':dict(running) if running else None,'waiting':dict(waiting) if waiting else None}

    def performance(self):
        """Pooled out-of-sample accuracy across today's stored forecasts, weighted by evaluation counts."""
        def build():
            target=(self.clock()+timedelta(days=1)).isoformat();pooled={};coverage=[0.,0];wins={};statuses=Counter()
            with self.store() as s:
                rows=[(r['series_id'],r['revision'],r['payload']) for r in s.db.execute('SELECT series_id,revision,payload FROM app_forecasts WHERE target_date=? AND model_version=?',(target,VERSION))]
            current={x['id']:x['revision'] for x in self.series()}
            for sid,revision,payload in rows:
                if current.get(sid)!=revision:continue
                result=json.loads(payload);statuses[result['status']]+=1
                if result.get('winner'):wins[result['winner']]=wins.get(result['winner'],0)+1
                c=result.get('interval_coverage') or {}
                if c.get('observed') is not None:coverage[0]+=c['observed']*c['n']/100;coverage[1]+=c['n']
                for m in result.get('performance',[])+([{**result['selected_metrics'],'model':'Per-series choice'}] if result.get('selected_metrics') else []):
                    if not m['n']:continue
                    p=pooled.setdefault(m['model'],{'n':0,'abs':0.,'sq':0.,'pct':0.,'pct_n':0})
                    p['n']+=m['n'];p['abs']+=m['mae']*m['n'];p['sq']+=m['rmse']**2*m['n']
                    if m['mape'] is not None:p['pct']+=m['mape']*m['mape_n'];p['pct_n']+=m['mape_n']
            models=[{'model':k,'n':v['n'],'mae':v['abs']/v['n'],'rmse':(v['sq']/v['n'])**.5,'mape':v['pct']/v['pct_n'] if v['pct_n'] else None,'wins':wins.get(k,0)} for k,v in pooled.items()]
            return {'models':models,'statuses':dict(statuses),'interval_coverage':{'nominal':95,'observed':100*coverage[0]/coverage[1] if coverage[1] else None,'n':coverage[1]}}
        return self.memo(('performance',),build)

    def admin_status(self,jobs=()):
        series=self.series();today_=self.clock()
        stale=[s for s in series if (today_-date.fromisoformat(s['latest'])).days>MAX_AGE_DAYS]
        with self.store() as s:
            per_state={r['state']:dict(r) for r in s.db.execute('SELECT state,COUNT(*) observations,MIN(date) earliest,MAX(date) latest FROM observations WHERE state IN (%s) GROUP BY state'%','.join('?'*len(STATES)),tuple(STATES))}
            totals={'observations':sum(v['observations'] for v in per_state.values()),'earliest':min((v['earliest'] for v in per_state.values()),default=None),'latest':max((v['latest'] for v in per_state.values()),default=None)}
            quarantined=s.db.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0]
            runs=[dict(r) for r in s.db.execute('SELECT * FROM app_runs ORDER BY created DESC LIMIT 15')]
            diagnostics=[{**json.loads(r['payload']),'run_id':r['run_id']} for r in s.db.execute('SELECT run_id,payload FROM app_diagnostics ORDER BY id DESC LIMIT 20')]
        groups=Counter(x['group'] for x in series);markets={(x['state'],x['district'],x['market']) for x in series}
        status=self.public_status();target=(self.clock()+timedelta(days=1)).isoformat()
        with self.store() as s:ready={(r[0],r[1]) for r in s.db.execute("SELECT series_id,revision FROM app_forecasts WHERE target_date=? AND model_version=? AND json_extract(payload,'$.status')='updated'",(target,VERSION))}
        breakdown=[]
        for entry in status['states']:
            mine=[x for x in series if x['state']==entry['name']];own=per_state.get(entry['name'],{})
            breakdown.append({**entry,'observations':own.get('observations',0),'earliest':own.get('earliest'),'latest':own.get('latest'),'series':len(mine),'forecasts':sum((x['id'],x['revision']) in ready for x in mine),
                'markets':len({(x['district'],x['market']) for x in mine}),'districts':len({x['district'] for x in mine}),'commodities':len({x['commodity'] for x in mine}),
                'backfill_range':self.state_get(entry['name'],'backfill_range'),'trial_started':self.state_get(entry['name'],'trial_started'),'last_weekly_update':self.state_get(entry['name'],'last_weekly_update')})
        return {**status,'by_state':breakdown,'database':totals,'quarantined':quarantined,'series':len(series),'commodities':len({x['commodity'] for x in series}),
            'markets':len(markets),'districts':len({x['district'] for x in series}),'groups':dict(groups.most_common()),
            'arrivals':self.arrivals(),'stale_count':len(stale),'stale_series':stale[:100],'runs':runs,'diagnostics':diagnostics,'performance':self.performance(),'scheduled_jobs':list(jobs)}
