import json,math,tempfile,threading,time,unittest
from datetime import date,datetime,timedelta
from unittest.mock import patch
from agmarknet import forecast,service as service_module
from agmarknet.client import AccessDenied,CaptchaExhausted,CollectorError,RateLimited,Unavailable
from agmarknet.service import Service,MAX_DEFERRALS

TODAY=date(2026,10,26)
WHEAT,RICE,TOMATO,GARLIC,RARE,EMPTY,MARIGOLD,CUMIN=100,101,102,103,104,105,106,107
CROPS={WHEAT:'Wheat',RICE:'Rice',TOMATO:'Tomato',GARLIC:'Garlic',RARE:'Rare Herb',EMPTY:'Empty Crop',MARIGOLD:'Marigold',CUMIN:'Cummin Seed(Jeera)'}
GROUPS={WHEAT:1,RICE:1,TOMATO:2,GARLIC:2,RARE:3,EMPTY:3,MARIGOLD:4,CUMIN:5}
STATE_NAMES={34:'Uttar Pradesh',29:'Rajasthan'}
SOLD={34:{WHEAT,RICE,TOMATO,GARLIC,RARE,MARIGOLD},29:{WHEAT,CUMIN,RICE}}   # Rajasthan sells jeera, which Uttar Pradesh does not
MARKETS=[(f'District {i%3}',f'Mandi {i}') for i in range(12)]
FILTERS={'state_data':[{'state_id':100000,'state_name':'All States/UTs'},{'state_id':34,'state_name':'Uttar Pradesh'},{'state_id':29,'state_name':'Rajasthan'}],
 'cmdt_data':[{'cmdt_id':i,'cmdt_name':n,'cmdt_group_id':GROUPS[i]} for i,n in CROPS.items()],
 'cmdt_group_data':[{'id':1,'cmdt_grp_name':'Cereals'},{'id':2,'cmdt_grp_name':'Vegetables'},{'id':3,'cmdt_grp_name':'Others'},{'id':4,'cmdt_grp_name':'Flowers'},{'id':5,'cmdt_grp_name':'Spices'}],
 'district_data':[],'market_data':[],'range_data':[{'price':{'from_date':'2021-01-01','to_date':'2026-10-26'}}]}

def price(crop,market,day):
    n=(day-date(2026,1,1)).days
    return round({WHEAT:2400,RICE:3000,TOMATO:1500,GARLIC:9000,RARE:500,MARIGOLD:800,CUMIN:25000}[crop]+market*3+n*1.5+60*math.sin(n/6))

class FakeClient:
    """Serves synthetic Uttar Pradesh and Rajasthan reports. Knobs on the class simulate corrections and source trouble."""
    overrides={};calls=[];captcha_failures={};throttle_call=None;outage_call=None
    intervals=[]
    def __init__(self,diagnostic=None,last_day=TODAY,interval=3.0):self.last_day=last_day;FakeClient.intervals.append(interval)
    def filters(self):return FILTERS
    def report(self,params,fresh=False):
        FakeClient.calls.append(params);crop=int(params['commodity'])
        if FakeClient.throttle_call is not None and len(FakeClient.calls)==FakeClient.throttle_call:
            FakeClient.throttle_call=None;raise RateLimited('too many requests',minutes=15)
        if FakeClient.outage_call is not None and len(FakeClient.calls)>=FakeClient.outage_call:raise Unavailable('Network request failed after five attempts: ConnectionError')
        if FakeClient.captcha_failures.get(crop,0)>0:
            FakeClient.captcha_failures[crop]-=1;raise CaptchaExhausted('Three fresh challenges failed')
        sid=json.loads(params['state'])[0];assert sid in STATE_NAMES;name=STATE_NAMES[sid];tag='' if sid==34 else 'RJ '
        start=max(date.fromisoformat(params['from_date']),date(2026,1,1));end=min(date.fromisoformat(params['to_date']),self.last_day)
        rows=[];d=start
        while d<=end:
            for m,(district,market_name) in enumerate(MARKETS):
                if crop not in SOLD[sid] or crop==EMPTY or (crop in (RARE,MARIGOLD) and (m>1 or d<date(2026,10,20))):continue
                for variety in (['Dara','Mill Quality'] if crop==WHEAT and m<2 else ['Dara']):
                    p=self.overrides.get((CROPS[crop],m,d.isoformat()),price(crop,m,d))
                    rows.append({'arrival_date':d.strftime('%d-%m-%Y'),'state_name':name,'district_name':tag+district,'market_name':tag+market_name,'cmdt_name':CROPS[crop],
                        'variety_name':variety,'grade_name':'FAQ','unit_name_price':'Rs./Quintal','min_price':p-50,'max_price':p+50,'model_price':p})
            d+=timedelta(days=1)
        if not rows:return {'_collector_http_status':404,'detail':'No data available'}
        page=int(params['page']);limit=int(params['limit']);pages=math.ceil(len(rows)/limit)
        return {'status':True,'data':{'records':[{'data':rows[(page-1)*limit:page*limit],'pagination':[{'total_count':len(rows),'total_pages':pages,'current_page':page,'items_per_page':limit}]}]}}

class Base(unittest.TestCase):
    def setUp(self):
        FakeClient.overrides={};FakeClient.calls=[];FakeClient.captcha_failures={};FakeClient.throttle_call=None;FakeClient.outage_call=None;FakeClient.intervals=[]
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.today=TODAY
        self.service=Service(self.tmp.name,clock=lambda:self.today,client_factory=FakeClient)
    def latest_run(self,kind):
        with self.service.store() as s:return dict(s.db.execute('SELECT * FROM app_runs WHERE kind=? ORDER BY created DESC LIMIT 1',(kind,)).fetchone())
    def run_job(self,kind):
        self.service.enqueue(kind);self.assertTrue(self.service.run_next());return self.latest_run(kind)
    def retry_now(self,run):
        with self.service.store() as s:s.db.execute("UPDATE app_runs SET retry_at='2000-01-01T00:00:00+00:00' WHERE id=?",(run['id'],));s.db.commit()
        self.assertTrue(self.service.run_next());return self.latest_run(run['kind'])
    def bootstrap(self):
        run=self.run_job('bootstrap');self.assertEqual(run['status'],'succeeded',run['detail']);return run

class BootstrapTests(Base):
    def test_history_one_request_per_commodity_and_forecasts(self):
        self.bootstrap()
        self.assertEqual(len(FakeClient.calls),len(CROPS)-1,'small crops need a single whole-period request each, and unlikely groups are left to the census')
        self.assertNotIn(MARIGOLD,{int(c['commodity']) for c in FakeClient.calls})
        self.assertTrue(all(c['limit']=='5000' and c['from_date']=='2025-10-27' and c['to_date']=='2026-10-26' for c in FakeClient.calls))
        self.assertTrue(self.service.state_get('Uttar Pradesh','history_complete'))
        series=self.service.series();self.assertEqual({s['group'] for s in series},{'Cereals','Vegetables','Others'})
        self.assertNotIn('Empty Crop',{s['commodity'] for s in series})
        wheat=[s for s in series if s['commodity']=='Wheat'];self.assertEqual(len(wheat),14,'12 markets, two of them with a second variety')
        f=self.service.forecast(wheat[0]['id'])
        self.assertEqual(f['status'],'updated');self.assertEqual(f['target_date'],'2026-10-27');self.assertEqual(f['data_cutoff'],'2026-10-26')
        self.assertLess(f['forecast']['lower'],f['forecast']['price']);self.assertIn('generated_at',f);self.assertTrue(all(m['n']>0 for m in f['performance']))
        rare=self.service.forecast(next(s['id'] for s in series if s['commodity']=='Rare Herb'));self.assertEqual(rare['status'],'insufficient')
    def test_history_pages_are_archived_but_updates_are_not(self):
        self.bootstrap()
        with self.service.store() as s:archived=s.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
        self.assertGreaterEqual(archived,5)
        self.run_job('update')
        with self.service.store() as s:self.assertEqual(s.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0],archived)

class UpdateTests(Base):
    def setUp(self):super().setUp();self.bootstrap();FakeClient.calls=[]
    def revisions(self):return {s['id']:s['revision'] for s in self.service.series()}
    def test_update_asks_only_for_active_commodities_and_weekly_asks_for_all(self):
        self.run_job('update');asked={int(c['commodity']) for c in FakeClient.calls}
        self.assertEqual(asked,{WHEAT,RICE,TOMATO,GARLIC,RARE});self.assertTrue(all(c['from_date']==(TODAY-timedelta(days=service_module.UPDATE_DAYS)).isoformat() for c in FakeClient.calls))
        FakeClient.calls=[];self.run_job('weekly');self.assertEqual({int(c['commodity']) for c in FakeClient.calls},set(CROPS))
        self.assertIn('Marigold',{s['commodity'] for s in self.service.series()},'the census discovers a commodity the history build skipped')
    def test_census_probes_only_the_least_recently_checked_extras(self):
        with patch.object(service_module,'CENSUS_EXTRA',1):self.run_job('weekly')
        asked={int(c['commodity']) for c in FakeClient.calls};self.assertEqual(asked,{WHEAT,RICE,TOMATO,GARLIC,RARE,MARIGOLD})
        self.assertNotIn(EMPTY,asked,'the empty crop was checked during the history build, the marigold never')
    def test_unchanged_update_creates_no_duplicates_or_forecasts(self):
        before=self.revisions()
        with self.service.store() as s:count=s.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0];forecasts=s.db.execute('SELECT COUNT(*) FROM app_forecasts').fetchone()[0]
        self.assertEqual(self.run_job('update')['status'],'succeeded')
        with self.service.store() as s:
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],count)
            self.assertEqual(s.db.execute('SELECT COUNT(*) FROM app_forecasts').fetchone()[0],forecasts)
        self.assertEqual(self.revisions(),before)
    def test_correction_refreshes_only_affected_series(self):
        before=self.revisions();FakeClient.overrides[('Wheat',0,'2026-10-24')]=9999
        self.run_job('update');after=self.revisions();changed=[k for k in after if after[k]!=before[k]]
        self.assertEqual(len(changed),2,'market 0 reports two wheat varieties with the same override')
        record,rows=self.service.history(changed[0]);self.assertEqual(next(r['price'] for r in rows if r['date']=='2026-10-24'),9999)
        self.assertEqual(self.service.forecast(changed[0])['data_revision'],after[changed[0]])
        with self.service.store() as s:self.assertEqual(s.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],2)
    def test_new_day_adds_a_forecast_for_the_next_date(self):
        self.today=TODAY+timedelta(days=1);self.service.client_factory=lambda diagnostic=None,interval=3.0:FakeClient(diagnostic,last_day=self.today,interval=interval);self.run_job('update')
        f=self.service.forecast(self.service.series()[0]['id']);self.assertEqual(f['target_date'],'2026-10-28');self.assertEqual(f['data_cutoff'],'2026-10-27')

class DeferralTests(Base):
    def test_captcha_failure_defers_one_crop_and_keeps_the_rest(self):
        FakeClient.captcha_failures={RICE:1}
        run=self.run_job('bootstrap');self.assertEqual(run['status'],'retry_wait');self.assertIn('1 of 7 commodities deferred',run['detail']);self.assertFalse(self.service.get_setting('collection_paused'))
        names={s['commodity'] for s in self.service.series()};self.assertIn('Wheat',names);self.assertNotIn('Rice',names)
        self.assertFalse(self.service.run_next(),'waits for the cooldown');FakeClient.calls=[]
        done=self.retry_now(run);self.assertEqual(done['status'],'succeeded');self.assertEqual({int(c['commodity']) for c in FakeClient.calls},{RICE},'only the deferred crop is requested again')
        self.assertIn('Rice',{s['commodity'] for s in self.service.series()});self.assertTrue(self.service.state_get('Uttar Pradesh','history_complete'))
    def test_a_network_outage_waits_and_retries_instead_of_pausing(self):
        FakeClient.outage_call=3;pace=self.service.pace()
        run=self.run_job('bootstrap');self.assertEqual(run['status'],'retry_wait');self.assertFalse(self.service.get_setting('collection_paused'))
        self.assertIn('commodities deferred',run['detail']);self.assertEqual(self.service.pace(),pace,'an outage neither slows nor speeds the pace')
        self.assertEqual(self.retry_now(run)['status'],'retry_wait','still down: it keeps waiting instead of pausing after one retry');self.assertFalse(self.service.get_setting('collection_paused'))
        FakeClient.outage_call=None;self.assertEqual(self.retry_now(self.latest_run('bootstrap'))['status'],'succeeded')
    def test_throttling_stops_the_sweep_and_waits(self):
        FakeClient.throttle_call=3
        run=self.run_job('bootstrap');self.assertEqual(run['status'],'retry_wait');self.assertEqual(len(FakeClient.calls),3,'no further requests after being throttled')
        self.assertIn('5 of 7 commodities deferred',run['detail']);self.assertGreater(run['retry_at'],service_module.now())
        self.assertEqual(self.retry_now(run)['status'],'succeeded');self.assertEqual(FakeClient.intervals,[service_module.PACE_START,service_module.PACE_START*1.5],'a throttle slows the next attempt');self.assertEqual(self.service.pace(),round(service_module.PACE_START*1.5*.9,2),'a clean sweep speeds up a little')
    def test_repeated_throttling_backs_off_for_longer(self):
        FakeClient.throttle_call=1;first=self.run_job('bootstrap');FakeClient.throttle_call=len(FakeClient.calls)+1
        with self.service.store() as s:s.db.execute("UPDATE app_runs SET retry_at='2000-01-01T00:00:00+00:00' WHERE id=?",(first['id'],));s.db.commit()
        self.assertTrue(self.service.run_next());second=self.latest_run('bootstrap')
        wait=lambda r:(datetime.fromisoformat(r['retry_at'])-datetime.fromisoformat(r['finished'])).total_seconds()/60
        self.assertAlmostEqual(wait(first),15,delta=1);self.assertAlmostEqual(wait(second),30,delta=1)
    def test_repeated_deferrals_eventually_pause(self):
        FakeClient.captcha_failures={RICE:99};run=self.run_job('bootstrap')
        with self.service.store() as s:s.db.execute('UPDATE app_runs SET attempt=? WHERE id=?',(MAX_DEFERRALS,run['id']));s.db.commit()
        self.assertEqual(self.retry_now(run)['status'],'paused');self.assertTrue(self.service.get_setting('collection_paused'))
    def test_enqueue_deduplicates_and_pause_blocks_scheduled_jobs(self):
        a=self.service.enqueue('update');self.assertEqual(self.service.enqueue('update'),a)
        self.service.setting('collection_paused',True);self.assertIsNone(self.service.enqueue('weekly'))
        self.assertTrue(self.service.enqueue('weekly',manual=True));self.assertFalse(self.service.get_setting('collection_paused'))
        with self.assertRaises(ValueError):self.service.enqueue('nope')
    def test_network_failure_retries_once_then_pauses_and_keeps_data(self):
        self.bootstrap();first=self.service.series()[0]['id'];rows=self.service.history(first)[1]
        class Broken(FakeClient):
            def report(self,*a,**k):raise CollectorError('network down')
        self.service.client_factory=Broken
        run=self.run_job('update');self.assertEqual(run['status'],'retry_wait')
        self.assertEqual(self.retry_now(run)['status'],'paused');self.assertTrue(self.service.get_setting('collection_paused'))
        self.assertEqual(self.service.history(first)[1],rows)
    def test_access_denied_pauses_immediately(self):
        self.bootstrap()
        class Denied(FakeClient):
            def report(self,*a,**k):raise AccessDenied('denied')
        self.service.client_factory=Denied;self.assertEqual(self.run_job('update')['status'],'paused')
    def test_recover_requeues_interrupted_run(self):
        run_id=self.service.enqueue('update')
        with self.service.store() as s:s.db.execute("UPDATE app_runs SET status='running' WHERE id=?",(run_id,));s.db.commit()
        self.service.recover()
        with self.service.store() as s:self.assertEqual(s.db.execute('SELECT status FROM app_runs WHERE id=?',(run_id,)).fetchone()[0],'queued')

class SecondStateTests(Base):
    """Rajasthan is added next to Uttar Pradesh without disturbing it."""
    def second(self):
        self.bootstrap();FakeClient.calls=[]
        self.service.enqueue('bootstrap',state='Rajasthan');self.assertTrue(self.service.run_next())
        with self.service.store() as s:return dict(s.db.execute("SELECT * FROM app_runs WHERE kind='bootstrap' AND state='Rajasthan'").fetchone())
    def test_new_state_starts_from_commodities_seen_elsewhere_plus_its_specialities(self):
        run=self.second();self.assertEqual(run['status'],'succeeded',run['detail'])
        asked={int(c['commodity']) for c in FakeClient.calls}
        self.assertEqual(asked,{WHEAT,RICE,TOMATO,GARLIC,RARE,CUMIN},'what Uttar Pradesh reports, plus jeera; never-seen crops (empty, marigold) are left to the weekly census')
        self.assertTrue(all(json.loads(c['state'])==[29] for c in FakeClient.calls))
        self.assertTrue(self.service.state_get('Rajasthan','history_complete'));self.assertTrue(self.service.state_get('Uttar Pradesh','history_complete'))
    def test_states_are_listed_separately(self):
        self.second()
        up,rj=self.service.picker('Uttar Pradesh'),self.service.picker('Rajasthan')
        self.assertTrue(up and rj);self.assertNotIn('Cummin Seed(Jeera)',{x['commodity'] for x in up});self.assertIn('Cummin Seed(Jeera)',{x['commodity'] for x in rj})
        self.assertTrue(all(x['market'].startswith('RJ ') for x in rj));self.assertFalse(any(x['market'].startswith('RJ ') for x in up))
        self.assertEqual({x['state'] for x in self.service.series()},{'Uttar Pradesh','Rajasthan'})
        self.assertEqual(self.service.forecast(rj[0]['id'])['status'],'updated')
    def test_one_state_updating_does_not_touch_the_others_clock(self):
        self.second();before=self.service.state_get('Uttar Pradesh','last_successful_update');FakeClient.calls=[]
        self.service.enqueue('update',state='Rajasthan');self.assertTrue(self.service.run_next())
        self.assertTrue(FakeClient.calls and all(json.loads(c['state'])==[29] for c in FakeClient.calls))
        self.assertEqual(self.service.state_get('Uttar Pradesh','last_successful_update'),before)
    def test_enqueue_all_queues_each_state_once(self):
        ids=self.service.enqueue_all('update');self.assertEqual(len(set(ids)),2);self.assertEqual(self.service.enqueue_all('update'),ids)
        self.assertEqual(len(set(self.service.enqueue_all('forecast'))),1);self.assertRaises(ValueError,self.service.enqueue,'update',state='Kerala')
    def test_updates_run_before_a_waiting_history_build(self):
        self.bootstrap();first=self.service.enqueue('bootstrap',state='Rajasthan');second=self.service.enqueue('update',state='Uttar Pradesh')
        self.assertTrue(self.service.run_next())
        with self.service.store() as s:status={r[0]:r[1] for r in s.db.execute('SELECT id,status FROM app_runs WHERE id IN (?,?)',(first,second))}
        self.assertEqual(status[second],'succeeded');self.assertEqual(status[first],'queued')
    def test_settings_from_the_single_state_version_move_to_uttar_pradesh(self):
        self.service.setting('up_history_complete',True);self.service.setting('last_successful_update','2026-10-26T01:00:00+00:00')
        with self.service.store() as s:
            s.db.execute("DELETE FROM app_settings WHERE key LIKE '%:%'");s.db.execute('ALTER TABLE app_runs DROP COLUMN state');s.db.commit()
        reopened=Service(self.tmp.name,clock=lambda:self.today,client_factory=FakeClient)
        self.assertTrue(reopened.state_get('Uttar Pradesh','history_complete'));self.assertFalse(reopened.state_get('Rajasthan','history_complete',False))
        self.assertEqual(reopened.state_get('Uttar Pradesh','last_successful_update'),'2026-10-26T01:00:00+00:00')

class ArrivalLogTests(Base):
    def test_each_update_records_what_it_added_by_date(self):
        self.bootstrap();self.assertEqual(self.service.arrivals(),[],'the history build is not an arrival')
        self.run_job('update');first=self.service.arrivals()[0]
        self.assertEqual((first['state'],first['kind'],first['new_rows'],first['newest']),('Uttar Pradesh','update',None,'2026-10-26'),'the first snapshot has nothing to compare with')
        self.today=TODAY+timedelta(days=1);self.service.client_factory=lambda diagnostic=None,interval=3.0:FakeClient(diagnostic,last_day=self.today,interval=interval)
        self.run_job('update');second=self.service.arrivals()[0]
        self.assertEqual(second['newest'],'2026-10-27');self.assertTrue(second['new_rows']>0);self.assertEqual(second['added'][0][0],'2026-10-27')
        self.assertEqual(sum(n for _,n in second['added']),second['new_rows'],'only the new day was added, nothing older')
        self.run_job('update');third=self.service.arrivals()[0];self.assertEqual((third['new_rows'],third['changed_series']),(0,0),'a quiet re-check shows nothing new')
    def test_states_are_compared_with_their_own_previous_snapshot(self):
        self.bootstrap();self.service.enqueue('bootstrap',state='Rajasthan');self.service.run_next()
        self.run_job('update');self.service.enqueue('update',state='Rajasthan');self.service.run_next()
        by_state={a['state']:a for a in self.service.arrivals()};self.assertEqual(set(by_state),{'Uttar Pradesh','Rajasthan'});self.assertIsNone(by_state['Rajasthan']['new_rows'])

class RolloverTests(Base):
    def test_midnight_rollover_and_stale_data(self):
        self.bootstrap();sid=next(s['id'] for s in self.service.series() if s['commodity']=='Wheat')
        self.today=TODAY+timedelta(days=1)
        self.assertEqual(self.service.forecast(sid)['status'],'updating','yesterday’s forecast must not be relabelled as today’s')
        self.assertTrue(self.service.refresh_forecasts())
        f=self.service.forecast(sid);self.assertEqual(f['status'],'updated');self.assertEqual(f['target_date'],'2026-10-28');self.assertEqual(f['age_days'],1)
        self.today=TODAY+timedelta(days=5);self.service.refresh_forecasts();f=self.service.forecast(sid)
        self.assertEqual(f['status'],'updated');self.assertEqual(f['age_days'],5);self.assertEqual(f['horizon_days'],6,'tomorrow is six days past the last report')
        self.today=TODAY+timedelta(days=9);self.service.refresh_forecasts();f=self.service.forecast(sid)
        self.assertEqual(f['status'],'delayed');self.assertIsNone(f['forecast']);self.assertIn('9 days old',f['reason'])
    def test_series_silent_for_a_month_are_hidden_from_farmers_but_counted(self):
        self.bootstrap();total=len(self.service.series());self.today=TODAY+timedelta(days=45)
        self.assertEqual(self.service.series(),[]);self.assertEqual(len(self.service.series(listed_days=0)),total)
        self.assertEqual(self.service.admin_status()['stale_count'],0,'hidden series are not reported as merely stale')

class ForecastTests(unittest.TestCase):
    def rows(self,n,end=TODAY,gap=lambda i:False):
        return [{'date':(end-timedelta(days=n-1-i)).isoformat(),'price':1000+i*2+(i%7)*5} for i in range(n) if not gap(i)]
    def test_insufficient_and_delayed(self):
        self.assertEqual(forecast.analyze(self.rows(30),TODAY)['status'],'insufficient')
        self.assertEqual(forecast.analyze([],TODAY)['status'],'insufficient')
        self.assertEqual(forecast.analyze(self.rows(100,end=TODAY-timedelta(days=8)),TODAY)['status'],'delayed')
        self.assertEqual(forecast.analyze(self.rows(100,end=TODAY-timedelta(days=7)),TODAY)['status'],'updated','a week-old report is still forecastable')
    def test_missing_dates_remain_gaps(self):
        rows=self.rows(120,gap=lambda i:i%10==3);r=forecast.analyze(rows,TODAY)
        self.assertEqual(r['status'],'updated');self.assertGreater(r['missing_dates'],0);self.assertEqual(r['observations'],len(rows))
    def test_model_is_chosen_per_series_and_judged_on_held_out_days(self):
        r=forecast.analyze(self.rows(150),TODAY);self.assertEqual(r['status'],'updated');self.assertEqual(r['selection'],'backtest')
        self.assertIn(r['model'],forecast.MODELS);self.assertEqual(r['winner'],r['model'])
        self.assertEqual([m['model'] for m in r['performance'] if m['selected']],[r['model']])
        self.assertEqual(r['interval_coverage']['n'],forecast.HOLDOUT);self.assertEqual(r['selected_metrics']['n'],forecast.HOLDOUT)
        self.assertTrue(all(m['n']==forecast.HOLDOUT for m in r['performance']))
    def test_a_trending_series_prefers_a_model_that_follows_the_trend(self):
        rows=[{'date':(TODAY-timedelta(days=149-i)).isoformat(),'price':1000+10*i} for i in range(150)]
        r=forecast.analyze(rows,TODAY);self.assertNotEqual(r['model'],'Exponential smoothing (0.3)');self.assertGreater(r['forecast']['price'],1000+10*140)
    def test_short_history_uses_the_default_model_without_claiming_a_backtest(self):
        r=forecast.analyze(self.rows(65),TODAY);self.assertEqual(r['status'],'updated');self.assertEqual(r['selection'],'default');self.assertEqual(r['model'],forecast.DEFAULT)
        self.assertIsNone(r['winner']);self.assertIsNone(r['interval_coverage']['observed']);self.assertLess(r['forecast']['lower'],r['forecast']['upper'])
    def test_stale_forecast_is_judged_at_its_own_horizon(self):
        old=forecast.analyze(self.rows(170,end=TODAY-timedelta(days=5)),TODAY)
        self.assertEqual((old['status'],old['age_days'],old['horizon_days']),('updated',5,6));self.assertEqual(old['interval_coverage']['n'],forecast.HOLDOUT)
        self.assertEqual(old['target_date'],(TODAY+timedelta(days=1)).isoformat());self.assertEqual(old['data_cutoff'],(TODAY-timedelta(days=5)).isoformat())
    def test_interval_widens_when_the_latest_report_is_older(self):
        fresh=forecast.analyze(self.rows(150),TODAY);old=forecast.analyze(self.rows(150,end=TODAY-timedelta(days=2)),TODAY)
        self.assertEqual(old['age_days'],2);width=lambda r:r['forecast']['upper']-r['forecast']['lower']
        self.assertGreater(width(old),width(fresh)*1.5)
    def test_backtest_never_sees_the_day_it_predicts(self):
        rows=self.rows(120);points=forecast.daily(rows);seen=[];original=forecast.predict
        def spy(training,model='Adaptive Kalman',horizon=1):
            seen.append((training[-1]['date'],len(training)));return original(training,model,horizon)
        with patch.object(forecast,'predict',spy):forecast.backtest(points)
        by_len={}
        for end,length in seen:by_len[length]=end
        for length,end in by_len.items():self.assertEqual(end,points[length-1]['date']);self.assertLess(length,len(points))
    def test_metrics_exclude_zero_actuals_from_mape(self):
        m=forecast.metrics([{'actual':0,'predicted':10},{'actual':100,'predicted':110}]);self.assertEqual(m['n'],2);self.assertEqual(m['mape_n'],1);self.assertAlmostEqual(m['mape'],10)

if __name__=='__main__':unittest.main()

class CacheTests(Base):
    def test_live_server_serves_the_old_list_while_rebuilding(self):
        self.bootstrap();self.service.background_refresh=True
        before=self.service.picker('Uttar Pradesh');self.assertTrue(before)
        gate=threading.Event();real=self.service.series
        def slow(*a,**k):gate.wait(5);return real(*a,**k)
        self.service.clear_cache()
        with patch.object(self.service,'series',slow):
            started=time.monotonic();again=self.service.picker('Uttar Pradesh')
            self.assertLess(time.monotonic()-started,1,'answers at once from the previous result');self.assertEqual(again,before)
            gate.set()
        for _ in range(100):
            if not self.service.refreshing:break
            time.sleep(.05)
        self.assertFalse(self.service.refreshing)
    def test_forecast_building_sees_new_data_even_on_the_live_server(self):
        self.service.background_refresh=True;self.bootstrap();self.service.series()   # a list is cached, then a window adds series behind it
        self.service.enqueue('bootstrap',state='Rajasthan');self.assertTrue(self.service.run_next())
        self.assertTrue(self.service.refresh_forecasts()>=0)
        target=(self.today+timedelta(days=1)).isoformat()
        with self.service.store() as s:have={r[0] for r in s.db.execute('SELECT series_id FROM app_forecasts WHERE target_date=?',(target,))}
        with self.service.fresh():wanted_rows=self.service.series()
        wanted={x['id'] for x in wanted_rows}
        rajasthan={x['id'] for x in wanted_rows if x['state']=='Rajasthan'};self.assertTrue(rajasthan);self.assertEqual(wanted-have,set())
    def test_a_finished_job_leaves_the_lists_fresh_for_the_next_visitor(self):
        self.service.background_refresh=True;self.bootstrap();self.service.picker('Uttar Pradesh')
        self.today=self.today+timedelta(days=1);self.run_job('update')   # a new day: the cached list is out of date until something rebuilds it
        entry=self.service.cache[('picker','Uttar Pradesh')]
        self.assertEqual((entry[2],entry[3]),(self.today,self.service.generation),'the job rebuilt the list itself instead of leaving it for a visitor')
    def test_tests_and_cli_always_read_fresh(self):
        self.bootstrap();self.assertFalse(self.service.background_refresh)
        self.service.picker('Uttar Pradesh');self.service.clear_cache();self.assertEqual(len(self.service.cache),0)
