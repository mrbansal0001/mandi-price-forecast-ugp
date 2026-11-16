import base64,copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageFont,ImageDraw
from agmarknet.ocr import TemplateOCR,OCRFailure
from agmarknet.client import Client,RateLimited,CaptchaExhausted,SourceChanged,AccessDenied,unpack,parameters,redact
from agmarknet.storage import Store,normalize,month_windows
from agmarknet.runner import collect_batch,require_gate,selected,plan

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=json.loads((ROOT/'tests/fixtures/report.json').read_text())
FILTERS={'state_data':[{'state_id':100000,'state_name':'All'},{'state_id':20,'state_name':'Maharashtra'}],
 'cmdt_data':[{'cmdt_id':23,'cmdt_name':'Onion','cmdt_group_id':6}],
 'market_data':[{'id':10,'state_id':20,'mkt_name':'APMC Akole'}],
 'range_data':[{'price':{'from_date':'2021-01-01','to_date':'2026-10-26'}}]}
BATCH={'id':'test','state_id':20,'commodity_id':23,'start':'2026-09-01','end':'2026-09-30','market_id':0}
RAW=FIXTURE['data']['records'][0]['data'][0]

def response(rows,total=None,page=1,pages=1):
 return {'status':True,'data':{'records':[{'data':rows,'pagination':[{'total_count':len(rows) if total is None else total,'total_pages':pages,'current_page':page,'items_per_page':100}]}]}}

class OCRTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.solver=TemplateOCR()
 def image(self,value):
  f=ImageFont.truetype(str(ROOT/'assets/DejaVuSans.ttf'),40)
  im=Image.new('RGB',(260,80),(200,200,200));d=ImageDraw.Draw(im);x=10
  for c in value:d.text((x,15),c,font=f,fill=(50,50,50));x+=int(d.textlength(c,font=f))+8
  out=io.BytesIO();im.save(out,format='PNG');return out.getvalue()
 def test_exact_case_and_ambiguous_glyphs(self):
  for value in ['0tKquc','O0Il1i','S5Z2B8','qgypfj','aAbBdD','uUvVxX','123456','ABCDEF']:
   with self.subTest(value=value):self.assertEqual(self.solver.solve(self.image(value))[0],value)
 def test_reject_missing_characters(self):
  with self.assertRaises(OCRFailure):self.solver.solve(self.image('abc'))
 def test_reject_blank(self):
  with self.assertRaises(OCRFailure):self.solver.solve(self.image(''))

class ValidationTests(unittest.TestCase):
 def test_real_source_shape(self):
  rows,meta=unpack(FIXTURE);self.assertEqual(len(rows),10);self.assertEqual(meta['total_count'],137)
 def test_normalization(self):
  r=normalize(RAW,BATCH,FILTERS,'now');self.assertEqual(r['modal_price'],4311);self.assertEqual(r['market'],'APMC Akole');self.assertEqual(r['date'],'2026-09-01');self.assertEqual(r['source_market'],'APMC Akole ')
 def test_missing_grade(self):
  r=normalize({**RAW,'grade_name':None},BATCH,FILTERS,'now');self.assertEqual(r['grade'],'Not reported')
 def test_wrong_state_date_unit_or_price(self):
  for changes in [{'state_name':'Punjab'},{'arrival_date':'31-02-2026'},{'arrival_date':'01-10-2026'},{'unit_name_price':'Rs/Bag'},{'model_price':'NaN'},{'model_price':'-1'},{'min_price':'99999'},{'model_price':''}]:
   with self.subTest(changes=changes),self.assertRaises(ValueError):normalize({**RAW,**changes},BATCH,FILTERS,'now')
 def test_unit_conversion(self):
  r=normalize({**RAW,'unit_name_price':'Rs./Kg'},BATCH,FILTERS,'now');self.assertEqual(r['modal_price'],431100)
 def test_no_fabricated_empty(self):
  with self.assertRaises(SourceChanged):unpack({'status':True,'data':{'records':[]}})
 def test_explicit_empty(self):self.assertEqual(unpack(response([]))[0],[])
 def test_month_boundaries(self):
  windows=list(month_windows('2025-10-26','2026-10-25'));self.assertEqual(len(windows),13);self.assertEqual(windows[0],('2025-10-26','2025-10-31'));self.assertEqual(windows[-1],('2026-10-01','2026-10-25'));self.assertEqual(list(month_windows('2024-02-28','2024-03-01'))[0][1],'2024-02-29')
 def test_unknown_filter_rejected(self):
  with self.assertRaises(ValueError):selected(FILTERS['state_data'],'bad','state_id','state_name')
 def test_redact_access_tokens(self):self.assertEqual(redact({'report_access':{'report_ticket':'SECRET'},'x':1}),{'x':1})

class StoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
  self.id=self.store.add(20,23,'2026-09-01','2026-09-30');self.store.db.commit();self.batch={**BATCH,'id':self.id}
 def tearDown(self):self.store.db.close();self.temp.cleanup()
 def count(self):return self.store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
 def test_idempotent_and_revision(self):
  self.store.finish(self.batch,[RAW],FILTERS,'now');self.store.finish(self.batch,[RAW],FILTERS,'later');self.assertEqual(self.count(),1)
  self.store.finish(self.batch,[{**RAW,'model_price':'4,000'}],FILTERS,'latest');self.assertEqual(self.count(),1);self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM revisions').fetchone()[0],1)
 def test_quarantine(self):
  good,bad=self.store.finish(self.batch,[RAW,{**RAW,'model_price':'-10'}],FILTERS,'now');self.assertEqual((good,bad),(1,1));self.assertEqual(self.count(),1)
 def test_missing_days_remain_missing(self):
  self.store.finish(self.batch,[RAW,{**RAW,'arrival_date':'03-09-2026'}],FILTERS,'now');self.assertEqual(self.count(),2);self.store.export();self.assertIn(',2,1\n',(Path(self.temp.name)/'series_coverage.csv').read_text())
 def test_forecast_gate(self):
  with self.assertRaises(Exception):require_gate(self.store)
 def test_atomic_ingest_on_database_error(self):
  self.store.db.execute("CREATE TRIGGER reject_row BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT,'test'); END");self.store.db.commit()
  with self.assertRaises(Exception):self.store.finish(self.batch,[RAW],FILTERS,'now')
  self.assertEqual(self.count(),0)
 def test_pagination_complete(self):
  class Fake:
   def report(self,p):
    pg=int(p['page']);return response([{**RAW,'arrival_date':f'0{pg}-09-2026'}],2,pg,2)
  result=collect_batch(Fake(),self.store,FILTERS,self.batch);self.assertEqual(result['accepted'],2);self.assertEqual(result['pages'],2)
 def test_truncation_splits(self):
  class Fake:
   def report(self,p):return response([RAW],2,1,1)
  result=collect_batch(Fake(),self.store,FILTERS,self.batch);self.assertEqual(result['status'],'split');self.assertEqual(self.count(),0)
 def test_repeat_page_splits(self):
  class Fake:
   def report(self,p):return response([RAW],2,int(p['page']),2)
  result=collect_batch(Fake(),self.store,FILTERS,self.batch);self.assertEqual(result['status'],'split')
 def test_plan_deduplication(self):
  plan(self.store,FILTERS,'2025-10-26','2026-10-25');n=self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0];plan(self.store,FILTERS,'2025-10-26','2026-10-25');self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],n)

class AccessTests(unittest.TestCase):
 def test_three_challenge_limit(self):
  class OCR:
   def solve(self,image):return 'ABCDEF',[]
  c=Client(ocr=OCR());calls=[]
  def fake(path,payload):
   calls.append(path)
   return ({'captcha_image':base64.b64encode(b'image').decode(),'captcha_key':'key'},False) if path.endswith('generator') else ({'detail':'Invalid captcha'},True)
  c.request=fake
  with self.assertRaises(CaptchaExhausted):c.report({})
  self.assertEqual(c.stats['challenges'],3);self.assertEqual(len(calls),6)
 def test_ticket_reused(self):
  import time
  c=Client(ocr=object());c.ticket='test';c.remaining=2;c.ticket_expiry=time.time()+100;c.ticket_scope=c.scope({})
  with patch.object(c,'request',return_value=(response([RAW]),False)) as request:
   c.report({});self.assertEqual(request.call_args.args[1]['report_ticket'],'test')
 def test_ticket_not_reused_across_scope(self):
  import time
  c=Client(ocr=object());c.ticket='test';c.remaining=2;c.ticket_expiry=time.time()+100;c.ticket_scope=c.scope({'commodity':'1'})
  class OCR:
   def solve(self,image):return 'ABCDEF',[]
  c.ocr=OCR();sent=[]
  def fake(path,payload):
   sent.append(payload)
   if path.endswith('generator'):return {'captcha_image':base64.b64encode(b'image').decode(),'captcha_key':'key'},False
   return response([RAW]),False
  c.request=fake
  c.report({'commodity':'2','page':'1'})
  self.assertNotIn('report_ticket',sent[-1]);self.assertEqual(sent[-1]['captcha_key'],'key')
  # Page changes alone stay within the same scope and reuse the new ticket.
  c.ticket='fresh';c.remaining=2;c.ticket_expiry=time.time()+100
  c.report({'commodity':'2','page':'2'});self.assertEqual(sent[-1]['report_ticket'],'fresh')
 def test_too_many_requests_is_a_rate_limit_not_a_captcha_failure(self):
  class R:
   status_code=400;headers={}
   def json(self):return {'code':'TOKEN_OR_CAPTCHA_REQUIRED','detail':'Too many requests. Please try again later.'}
  events=[];c=Client(ocr=object(),diagnostic=events.append);c.session.request=lambda *a,**k:R()
  with patch('agmarknet.client.time.sleep'),self.assertRaises(RateLimited) as caught:c.request('/daily-price-arrival/report',{})
  self.assertEqual(events[0]['category'],'rate_limit');self.assertEqual(caught.exception.minutes,15);self.assertEqual(c.stats['captcha_rejections'],0)
 def test_reject_access_denial(self):
  class R:
   status_code=403;headers={}
   def json(self):return {'detail':'Access denied'}
  c=Client(ocr=object());c.session.request=lambda *a,**k:R()
  with patch('agmarknet.client.time.sleep'),self.assertRaises(AccessDenied):c.request('/report',{})


class EmptyContractTests(unittest.TestCase):
 def test_verified_404_empty(self):
  rows,meta=unpack({'_collector_http_status':404,'detail':'No data available'});self.assertEqual(rows,[]);self.assertEqual(meta['total_count'],0)
 def test_other_404_not_empty(self):
  with self.assertRaises(SourceChanged):unpack({'_collector_http_status':404,'detail':'Route missing'})
 def test_empty_body_not_empty(self):
  with self.assertRaises(SourceChanged):unpack({})
 def test_rate_limit_long_retry_after_stops(self):
  class R:
   status_code=429;headers={'Retry-After':'300'}
  c=Client(ocr=object());c.session.request=lambda *a,**k:R()
  with patch('agmarknet.client.time.sleep'),self.assertRaisesRegex(Exception,'Retry after 300'):c.request('/report',{})

class ResumeTests(unittest.TestCase):
 setUp=StoreTests.setUp
 tearDown=StoreTests.tearDown
 count=StoreTests.count
 def test_scope_does_not_download_other_months(self):
  from agmarknet.runner import run
  self.store.add(20,23,'2026-08-01','2026-08-31');self.store.db.commit()
  self.store.finish(self.batch,[RAW],FILTERS,'now')
  class Fake:
   stats={}
   def report(self,p):raise AssertionError('Completed scope should not download')
  with patch('agmarknet.runner.require_gate'):
   result=run(Fake(),self.store,FILTERS,scope={'start':'2026-09-01','end':'2026-09-30','states':'Maharashtra','commodities':'Onion'})
  self.assertEqual(result['processed_batches'],0)
 def test_interrupt_preserves_pending_checkpoint_and_raw_page(self):
  from agmarknet.runner import run
  class Fake:
   stats={}
   def report(self,p):
    if p['page']=='2':raise KeyboardInterrupt()
    return response([RAW],2,1,2)
  with patch('agmarknet.runner.require_gate'),self.assertRaises(KeyboardInterrupt):run(Fake(),self.store,FILTERS)
  self.assertEqual(self.count(),0);self.assertEqual(self.store.db.execute('SELECT status FROM batches WHERE id=?',(self.id,)).fetchone()[0],'pending')
  self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0],1)

if __name__=='__main__':unittest.main()
