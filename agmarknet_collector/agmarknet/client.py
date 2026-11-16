"""Normal report workflow with bounded local OCR and in-memory report tickets."""
import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import requests
from .ocr import TemplateOCR, OCRFailure

BASE = 'https://api.agmarknet.gov.in/v1'
class CollectorError(Exception): pass
class AccessDenied(CollectorError): pass
class CaptchaExhausted(CollectorError): pass
class SourceChanged(CollectorError): pass
class TooLarge(CollectorError): pass
class RateLimited(CollectorError):
    """The source asked us to slow down. Callers back off instead of spending more CAPTCHA attempts."""
    def __init__(self,message,minutes=15):super().__init__(message);self.minutes=minutes

class Unavailable(RateLimited):
    """The source or our connection is down (network error, HTTP 5xx). Callers wait and retry later, without slowing the request pace."""

def redact(value):
    if isinstance(value, dict):
        return {k:redact(v) for k,v in value.items() if k not in {'report_access','report_ticket','captcha_key','captcha','access_token'}}
    if isinstance(value, list): return [redact(v) for v in value]
    return value

def unpack(response):
    if response.get('_collector_http_status') == 404 and str(response.get('detail', response.get('message',''))).lower() == 'no data available':
        return [], {'total_count':0,'total_pages':0,'current_page':1,'items_per_page':0}
    if response.get('status') is not True:
        raise SourceChanged(str(response.get('message',response.get('detail','Report unsuccessful')))[:300])
    data=response.get('data')
    if not isinstance(data,dict) or not isinstance(data.get('records'),list):
        raise SourceChanged('Report records structure changed.')
    groups=data['records']
    if not groups:
        # Never infer empty data without explicit total metadata.
        pagination=data.get('pagination') or response.get('pagination')
        if isinstance(pagination,list): pagination=pagination[0] if pagination else None
        if isinstance(pagination,dict) and pagination.get('total_count')==0:
            return [], {'total_count':0,'total_pages':0,'current_page':1,'items_per_page':0}
        raise SourceChanged('No records and no explicit zero-count pagination.')
    if len(groups)!=1: raise SourceChanged('Multiple record groups: pagination needs review.')
    group=groups[0]; meta=group.get('pagination')
    if isinstance(meta,list) and len(meta)==1: meta=meta[0]
    if not isinstance(group.get('data'),list) or not isinstance(meta,dict):
        raise SourceChanged('Missing data or pagination.')
    for key in ['total_count','total_pages','current_page','items_per_page']:
        if isinstance(meta.get(key),bool) or not isinstance(meta.get(key),int) or meta[key]<0:
            raise SourceChanged(f'Invalid pagination field: {key}')
    rows=group['data']
    if bool(rows) != bool(meta['total_count']): raise SourceChanged('Inconsistent empty-page count.')
    return rows,meta

class Client:
    def __init__(self, interval=3.0, session=None, ocr=None, diagnostic=None):
        self.session=session or requests.Session()
        self.session.headers.update({'User-Agent':'HarvestResearchCollector/0.1','Origin':'https://agmarknet.gov.in','Referer':'https://agmarknet.gov.in/'})
        self.interval=max(3.0,interval);self.last=0.0;self.ocr=ocr or TemplateOCR()
        self.diagnostic=diagnostic or (lambda event: None);self.ticket_scope=None
        self.ticket=None;self.ticket_expiry=0;self.remaining=0;self.stats={'requests':0,'challenges':0,'ocr_failures':0,'captcha_rejections':0,'ticket_refreshes':0}

    @staticmethod
    def scope(params):
        return hashlib.sha256(json.dumps({k:v for k,v in params.items() if k not in {'page','captcha','captcha_key','report_ticket'}},sort_keys=True).encode()).hexdigest()

    def reset_access(self):
        self.ticket=None;self.remaining=0;self.ticket_expiry=0;self.ticket_scope=None

    def request(self,path,payload=None):
        for retry in range(5):
            time.sleep(max(0,self.interval-(time.monotonic()-self.last)))
            self.last=time.monotonic();self.stats['requests']+=1
            try:
                response=self.session.request('GET' if payload is None else 'POST',BASE+path,json=payload,timeout=(15,90))
            except requests.RequestException as exc:
                if retry==4:raise Unavailable(f'Network request failed after five attempts: {type(exc).__name__}',minutes=15) from exc
                time.sleep(min(60,2**(retry+1)));continue
            if response.status_code==429 or response.status_code>=500:
                if retry==4:raise Unavailable(f'Upstream HTTP {response.status_code} after five attempts.',minutes=15)
                wait=min(60,2**(retry+1));header=response.headers.get('Retry-After')
                if header:
                    try:wait=max(wait,float(header))
                    except ValueError:
                        try:wait=max(wait,(parsedate_to_datetime(header)-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError,TypeError):pass
                # Do not occupy a process for arbitrarily long server cooldowns.
                if wait>60:raise RateLimited(f'Rate limited. Retry after {header}.',minutes=max(5,int(wait//60)+1))
                time.sleep(wait);continue
            try:body=response.json()
            except ValueError:raise SourceChanged(f'Upstream returned non-JSON (HTTP {response.status_code}).')
            if not isinstance(body,dict):raise SourceChanged('Unexpected upstream JSON shape.')
            detail=str(body.get('detail',body.get('message',''))).lower()
            if 'too many requests' in detail:
                # Observed as HTTP 400 / TOKEN_OR_CAPTCHA_REQUIRED; it is throttling, not a wrong CAPTCHA answer.
                self.diagnostic({'at':datetime.now(timezone.utc).isoformat(),'path':path,'http_status':response.status_code,'code':str(body.get('code',''))[:100],'category':'rate_limit','detail':detail[:250]})
                raise RateLimited('Source reported too many requests; backing off.',minutes=15)
            # Verified upstream empty-report contract, not a blanket 404 fallback.
            if response.status_code==404 and path=='/daily-price-arrival/report' and detail=='no data available':
                return {**body,'_collector_http_status':404},False
            challenge_error=('captcha' in detail or 'report ticket' in detail or 'report access' in detail or str(body.get('code','')).startswith(('CAPTCHA','REPORT_TICKET','TOKEN_OR_CAPTCHA')))
            if response.status_code>=400 or challenge_error:
                code=str(body.get('code',''))[:100]
                category='scope' if 'scope' in (code+' '+detail).lower() else ('ticket' if 'ticket' in (code+' '+detail).lower() else ('captcha' if challenge_error else 'http'))
                safe=detail[:250]
                for key in ['captcha','captcha_key','report_ticket']:
                    if payload and payload.get(key):safe=safe.replace(str(payload[key]).lower(),'[redacted]')
                self.diagnostic({'at':datetime.now(timezone.utc).isoformat(),'path':path,'http_status':response.status_code,'code':code,'category':category,'detail':safe})
            if response.status_code in (401,403) and not challenge_error:
                raise AccessDenied(f'Upstream access denied (HTTP {response.status_code}).')
            if response.status_code>=400 and not challenge_error:
                raise CollectorError(f'Upstream HTTP {response.status_code}: {detail[:250]}')
            return body,challenge_error
        raise CollectorError('Request retry limit reached.')

    def filters(self):
        response,error=self.request('/daily-price-arrival/filters')
        if error or not response.get('status'):raise SourceChanged('Filter catalogue unavailable.')
        return response['data']

    def _remember(self,body,scope):
        self.ticket_scope=scope
        access=body.get('report_access') or {}
        self.ticket=access.get('report_ticket');self.remaining=int(access.get('remaining_calls') or 0)
        try:self.ticket_expiry=datetime.fromisoformat(access['expires_at'].replace('Z','+00:00')).timestamp()
        except (KeyError,ValueError):self.ticket_expiry=0

    def report(self,params,fresh=False):
        scope=self.scope(params)
        if fresh or self.ticket_scope!=scope:self.reset_access()
        if self.ticket and self.remaining>0 and self.ticket_expiry>time.time()+5:
            body,challenge_error=self.request('/daily-price-arrival/report',{**params,'report_ticket':self.ticket})
            if not challenge_error:
                self._remember(body,scope);return body
            self.stats['ticket_refreshes']+=1;self.reset_access()
        for attempt in range(3):
            challenge,error=self.request('/captcha/generator',{})
            self.stats['challenges']+=1
            if error:raise AccessDenied('Challenge generator denied access.')
            try:
                image=base64.b64decode(challenge['captcha_image'],validate=True)
                answer,_=self.ocr.solve(image)
            except OCRFailure:
                self.stats['ocr_failures']+=1;continue
            except (KeyError,ValueError) as exc:raise SourceChanged('Challenge response changed.') from exc
            body,challenge_error=self.request('/daily-price-arrival/report',{**params,'captcha':answer,'captcha_key':challenge['captcha_key']})
            if challenge_error:
                self.stats['captcha_rejections']+=1;continue
            self._remember(body,scope);return body
        raise CaptchaExhausted('Three fresh challenges failed; collection stopped for review.')

def parameters(batch,filters,page=1,limit=100):
    commodity=next(c for c in filters['cmdt_data'] if c['cmdt_id']==batch['commodity_id'])
    return {'from_date':batch['start'],'to_date':batch['end'],'data_type':'100004',
        'group':str(commodity['cmdt_group_id']),'commodity':str(batch['commodity_id']),
        'state':json.dumps([batch['state_id']]),'district':'[100001]',
        'market':json.dumps(batch.get('market_ids') or [batch.get('market_id') or 100002]),'grade':'[100003]','variety':'[100007]',
        'page':str(page),'limit':str(limit)}
