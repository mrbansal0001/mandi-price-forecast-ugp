import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from agmarknet.app import create_app
from tests.test_service import Base

class APITests(Base):
    def setUp(self):
        super().setUp();self.bootstrap()
        self.client=TestClient(create_app(service=self.service,run_background=False))
        self.sid=self.client.get('/api/selections').json()['series'][0]['id']
    def test_selections_history_forecast_csv(self):
        data=self.client.get('/api/selections').json();self.assertEqual(len(data['series']),len([x for x in data['series']]));self.assertGreater(len(data['series']),50);self.assertEqual(data['selected'],'Uttar Pradesh');self.assertTrue(next(x for x in data['states'] if x['name']=='Uttar Pradesh')['history_complete']);self.assertFalse(data['history_complete'],'Rajasthan is not built yet')
        hist=self.client.get(f'/api/series/{self.sid}/history?days=30').json()['rows'];self.assertTrue(0<len(hist)<=31)
        fc=self.client.get(f'/api/series/{self.sid}/forecast').json();self.assertEqual(fc['status'],'updated');self.assertEqual(fc['target_date'],'2026-10-27')
        csv=self.client.get(f'/api/series/{self.sid}/history.csv');self.assertEqual(csv.headers['content-type'].split(';')[0],'text/csv')
        self.assertTrue(csv.text.startswith('date,state,district,market,commodity'));self.assertGreater(csv.text.count('\n'),100)
    def test_state_selection(self):
        both=self.client.get('/api/selections?state=Rajasthan').json();self.assertEqual(both['selected'],'Rajasthan');self.assertEqual(both['series'],[])
        self.assertEqual([x['name'] for x in both['states']],['Uttar Pradesh','Rajasthan']);self.assertEqual(self.client.get('/api/selections?state=Kerala').status_code,400)
        self.service.enqueue('bootstrap',state='Rajasthan');self.service.run_next()
        rj=self.client.get('/api/selections?state=Rajasthan').json()['series'];self.assertTrue(rj);self.assertTrue(all(x['market'].startswith('RJ ') for x in rj))
        self.assertTrue(self.client.get(f"/api/series/{rj[0]['id']}/forecast").json()['last_successful_update'])
    def test_one_scheduled_update_a_day(self):
        from agmarknet.app import Runtime
        runtime=Runtime(self.service);runtime.schedule();jobs={j.id:str(j.trigger) for j in runtime.scheduler.get_jobs()}
        self.assertEqual(set(jobs),{'update','weekly','rollover'})
        self.assertIn("hour='7'",jobs['update']);self.assertIn("minute='0'",jobs['update']);self.assertIn("hour='0'",jobs['rollover'])
    def test_picker_flags_series_that_have_a_forecast(self):
        series=self.client.get('/api/selections').json()['series'];flags={s['commodity']:s['f'] for s in series}
        self.assertEqual(flags['Wheat'],1);self.assertEqual(flags['Rare Herb'],0,'too little history for a forecast')
        self.assertEqual(set(series[0]),{'id','group','commodity','variety','grade','district','market','count','f'})
    def test_unknown_series_is_404_and_opening_pages_never_scrapes(self):
        self.assertEqual(self.client.get('/api/series/nope/forecast').status_code,404)
        from tests.test_service import FakeClient
        FakeClient.calls=[]
        for url in ['/','/admin','/api/selections',f'/api/series/{self.sid}/forecast',f'/api/series/{self.sid}/history']:self.assertEqual(self.client.get(url).status_code,200)
        self.assertEqual(FakeClient.calls,[])
    def test_stale_forecast_enqueues_local_rebuild_only(self):
        self.today=self.today.replace(day=27)
        self.assertEqual(self.client.get(f'/api/series/{self.sid}/forecast').json()['status'],'updating')
        with self.service.store() as s:kinds=[r[0] for r in s.db.execute("SELECT kind FROM app_runs WHERE status='queued'")]
        self.assertEqual(kinds,['forecast'])
    def test_admin_is_local_only(self):
        self.assertEqual(self.client.get('/api/admin/status').status_code,200)
        remote=TestClient(create_app(service=self.service,run_background=False),client=('203.0.113.9',5000))
        self.assertEqual(remote.get('/api/admin/status').status_code,403);self.assertEqual(remote.post('/api/admin/enqueue').status_code,403)
        self.assertEqual(self.client.post('/api/admin/enqueue',headers={'origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.post('/api/admin/enqueue?kind=bad').status_code,400)
        self.assertEqual(self.client.post('/api/admin/enqueue?kind=update',headers={'origin':'http://localhost:8000'}).status_code,200)
        self.assertEqual(len(self.client.post('/api/admin/enqueue?kind=update').json()['ids']),2,'no state chosen means every state')
        self.assertEqual(self.client.post('/api/admin/enqueue?kind=bootstrap').status_code,400);self.assertEqual(self.client.post('/api/admin/enqueue?kind=update&state=Kerala').status_code,400)
    def test_admin_status_contents(self):
        s=self.client.get('/api/admin/status').json()
        self.assertEqual(s['series'],len(self.client.get('/api/selections').json()['series']));self.assertEqual(s['commodities'],5);self.assertEqual(s['markets'],12);self.assertEqual(s['districts'],3)
        self.assertTrue(s['performance']['models']);self.assertIn('stale_count',s);self.assertTrue(s['runs']);self.assertIn('Cereals',s['groups']);self.assertGreater(s['performance']['statuses']['updated'],0)
