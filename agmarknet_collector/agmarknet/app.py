"""Local API, scheduler and static farmer/admin pages. Opening a page never triggers scraping."""
import contextlib,csv,io,json,logging,threading
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from .forecast import IST
from .service import Service,STATES,DEFAULT_STATE

WEB=Path(__file__).resolve().parents[1]/'web'
LOCAL={'127.0.0.1','::1','localhost','testclient'}
log=logging.getLogger('agmarknet.app')
CATCH_UP_HOURS=24   # one scheduled update a day: a restart catches up only if the last one is older than that

class Runtime:
    """Owns the scheduler and the single worker thread that drains queued jobs."""
    def __init__(self,service,poll=10):
        self.service=service;self.poll=poll;self.stop=threading.Event();self.thread=None
        self.scheduler=BackgroundScheduler(timezone=IST,job_defaults={'coalesce':True,'max_instances':1,'misfire_grace_time':3600})

    def schedule(self):
        s=self.service
        # Schedule times are our checking times, not Agmarknet publication times.
        # Agmarknet states upload with a lag of days, so one daily check loses almost nothing; the arrival log on /admin shows when data really lands.
        self.scheduler.add_job(lambda:s.enqueue_all('update'),'cron',hour=7,minute=0,id='update')
        self.scheduler.add_job(lambda:s.enqueue_all('weekly'),'cron',day_of_week='sun',hour=3,minute=0,id='weekly')
        self.scheduler.add_job(lambda:s.enqueue('forecast'),'cron',hour=0,minute=5,id='rollover')

    def start(self):
        s=self.service;s.recover();self.schedule()
        threading.Thread(target=s.warm,name='warm-cache',daemon=True).start()   # the first visitor should not wait for the lists
        self.scheduler.start();self.catch_up()
        self.thread=threading.Thread(target=self.loop,name='collector-worker',daemon=True);self.thread.start()

    def catch_up(self):
        """After a restart queue one catch-up instead of replaying every missed schedule."""
        s=self.service
        for state in STATES:
            last=s.state_get(state,'last_successful_update')
            if not s.state_get(state,'history_complete',False):s.enqueue('bootstrap',state=state)
            elif not last or datetime.now(timezone.utc)-datetime.fromisoformat(last)>timedelta(hours=CATCH_UP_HOURS):s.enqueue('update',state=state)
        s.enqueue('forecast')

    def loop(self):
        while not self.stop.is_set():
            try:
                while self.service.run_next():pass
            except Exception:log.exception('worker iteration failed')
            self.stop.wait(self.poll)

    def shutdown(self):
        self.stop.set()
        with contextlib.suppress(Exception):self.scheduler.shutdown(wait=False)

    def jobs(self):
        return [{'id':j.id,'next_run':j.next_run_time.isoformat() if j.next_run_time else None} for j in self.scheduler.get_jobs()]

def create_app(root='output',run_background=True,service=None):
    service=service or Service(root);runtime=Runtime(service)
    service.background_refresh=run_background   # the live server serves a slightly old list while it rebuilds, instead of making a visitor wait
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if run_background:runtime.start()
        try:yield
        finally:runtime.shutdown()
    app=FastAPI(title='Mandi price forecasts',lifespan=lifespan);app.state.service=service;app.state.runtime=runtime
    app.add_middleware(GZipMiddleware,minimum_size=1000)
    nocache={'Cache-Control':'no-store'}

    def series_or_404(sid):
        try:return service.history(sid)
        except KeyError:raise HTTPException(404,'Market selection not found.')

    def admin_only(request:Request):
        host=request.client.host if request.client else ''
        origin=request.headers.get('origin')
        if host not in LOCAL or (origin and urlparse(origin).hostname not in LOCAL):raise HTTPException(403,'Admin is available from this computer only.')

    @app.get('/api/status')
    def status():return JSONResponse(service.public_status(),headers=nocache)

    @app.get('/api/selections')
    def selections(state:str=DEFAULT_STATE):
        if state not in STATES:raise HTTPException(400,'Unknown state.')
        slim=service.picker(state)   # only the fields the filters need, so one state's list stays small
        return JSONResponse({'series':slim,'selected':state,**service.public_status()},headers=nocache)

    @app.get('/api/series/{sid}/history')
    def history(sid:str,days:int=180):
        record,rows=series_or_404(sid)
        if days>0:
            cutoff=(service.clock()-timedelta(days=days)).isoformat();rows=[r for r in rows if r['date']>=cutoff]
        return JSONResponse({'id':sid,'rows':rows},headers=nocache)

    @app.get('/api/series/{sid}/history.csv')
    def history_csv(sid:str):
        record,rows=series_or_404(sid)
        dims=dict(zip(['state','district','market','commodity','variety','grade'],json.loads(record['dimensions'])))
        out=io.StringIO();w=csv.writer(out);w.writerow(['date','state','district','market','commodity','variety','grade','min_price','max_price','modal_price','unit'])
        for r in rows:w.writerow([r['date'],*dims.values(),r['min_price'],r['max_price'],r['price'],'INR/quintal'])
        return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="prices-{sid[:8]}.csv"'})

    @app.get('/api/series/{sid}/forecast')
    def forecast(sid:str):
        series_or_404(sid);result=service.forecast(sid)
        # Missing target-date forecasts (midnight rollover) are rebuilt locally; no scraping is involved.
        if result['status']=='updating':service.enqueue('forecast')
        record,_=series_or_404(sid);state=json.loads(record['dimensions'])[0]
        return JSONResponse({**result,'last_successful_update':service.state_get(state,'last_successful_update')},headers=nocache)

    @app.get('/api/admin/status')
    def admin_status(request:Request):
        admin_only(request);return JSONResponse(service.admin_status(runtime.jobs()),headers=nocache)

    @app.post('/api/admin/enqueue')
    def enqueue(request:Request,kind:str='update',state:str=''):
        admin_only(request)
        if kind=='bootstrap' and not state:raise HTTPException(400,'Choose a state to rebuild its history.')
        try:
            ids=[service.enqueue(kind,manual=True,state=state)] if state else service.enqueue_all(kind,manual=True)
            return {'id':ids[0],'ids':ids}
        except ValueError as exc:raise HTTPException(400,str(exc))

    @app.get('/admin')
    def admin_page():return FileResponse(WEB/'admin.html',headers=nocache)

    @app.get('/')
    def index():return FileResponse(WEB/'index.html',headers=nocache)

    app.mount('/static',StaticFiles(directory=WEB),name='static')
    return app
