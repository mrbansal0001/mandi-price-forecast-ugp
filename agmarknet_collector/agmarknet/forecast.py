"""Causal daily forecasts. Null dates advance state; never become observations."""
from datetime import date,datetime,timedelta
from zoneinfo import ZoneInfo
import math,statistics

IST=ZoneInfo('Asia/Kolkata')
VERSION='selected-v4'   # bump whenever forecasting logic or calibration changes: cached forecasts are keyed by it
MODELS=['Adaptive Kalman','Last price','Median of last 5','7-day mean','Exponential smoothing (0.3)','Exponential smoothing (0.6)']
DEFAULT='Median of last 5'   # best single model in the offline comparison; used until a series has enough history to choose
SMOOTHING={'Exponential smoothing (0.3)':.3,'Exponential smoothing (0.6)':.6}
MIN_ORIGINS=20;HOLDOUT=30;MAX_ORIGINS=60
WINDOW_DAYS=180   # how far back the models look
MIN_ROWS=60     # reported dates needed in the model window
MIN_RECENT=15   # reported dates needed in the last 30 days
MAX_AGE_DAYS=7   # UP markets post prices late (newest days are mostly missing), so forecast from reports up to a week old
# Calibrated on held-out days of real UP series. Choosing the best model on past errors makes raw intervals too narrow, and sticky mandi prices
# (one price for days, then a jump) give some series near-zero past errors, so the range also keeps a floor relative to the price level.
WIDTH_INFLATION=1.3;WIDTH_FLOOR=.05
def today():return datetime.now(IST).date()

def daily(rows,start=None,end=None):
    if not rows:return []
    values={r['date']:float(r.get('price',r.get('modal_price'))) for r in rows}
    a=date.fromisoformat(start or min(values));b=date.fromisoformat(end or max(values))
    return [{'date':(a+timedelta(days=i)).isoformat(),'price':values.get((a+timedelta(days=i)).isoformat())} for i in range((b-a).days+1)]

def predict(points,model='Adaptive Kalman',horizon=1):
    """Forecast `horizon` calendar days past the last point; only the Kalman model reports its own uncertainty (sd)."""
    obs=[p for p in points if p['price'] is not None]
    if not obs:raise ValueError('No observed prices')
    if model!='Adaptive Kalman':
        value=obs[-1]['price']
        if model=='Median of last 5':value=statistics.median(p['price'] for p in obs[-5:])
        elif model=='7-day mean':
            recent=[p['price'] for p in points[-7:] if p['price'] is not None]
            value=sum(recent)/len(recent) if recent else value
        elif model in SMOOTHING:
            value=obs[0]['price']
            for p in obs[1:]:value=SMOOTHING[model]*p['price']+(1-SMOOTHING[model])*value
        elif model!='Last price':raise ValueError('Unknown model '+model)
        return [{'price':value,'sd':None} for _ in range(horizon)]
    first=next(i for i,p in enumerate(points) if p['price'] is not None)
    level=points[first]['price'];trend=0.;scale=max(1.,level*.03)**2
    a=scale;b=0.;c=scale/20;r=scale;q=scale*.02
    for point in points[first+1:]:
        level+=trend;pa=a+2*b+c+q;pb=b+c;pc=c+q*.01
        if point['price'] is None:a,b,c=pa,pb,pc;continue
        innovation=point['price']-level;variance=pa+r;k0=pa/variance;k1=pb/variance
        level+=k0*innovation;trend+=k1*innovation
        a=max(1e-8,(1-k0)*pa);b=(1-k0)*pb;c=max(1e-8,pc-k1*pb)
        r=min(scale*100,max(scale*.01,.95*r+.05*max(scale*.01,innovation**2-pa)))
        q=min(scale*10,max(scale*.001,.98*q+.02*(k0*innovation)**2))
    forecasts=[]
    for _ in range(horizon):
        level+=trend;pa=a+2*b+c+q;b+=c;c+=q*.01;a=pa
        forecasts.append({'price':max(0.,level),'sd':math.sqrt(a+r)})
    return forecasts

def metrics(pairs):
    if not pairs:return {'mae':None,'rmse':None,'mape':None,'n':0,'mape_n':0}
    errors=[p['predicted']-p['actual'] for p in pairs];nz=[p for p in pairs if p['actual']!=0]
    return {'mae':sum(abs(e) for e in errors)/len(errors),'rmse':math.sqrt(sum(e*e for e in errors)/len(errors)),
        'mape':sum(abs((p['predicted']-p['actual'])/p['actual'])*100 for p in nz)/len(nz) if nz else None,'n':len(pairs),'mape_n':len(nz)}

def origins(points,steps=1):
    """Days from which the price `steps` calendar days later is known, using only the history before them."""
    out=[];seen=0
    for i,p in enumerate(points[:len(points)-steps]):
        if p['price'] is not None:seen+=1
        previous=[x for x in points[max(0,i-2):i+1] if x['price'] is not None]
        recent=sum(x['price'] is not None for x in points[max(0,i-29):i+1])
        if seen>=60 and recent>=20 and previous and points[i+steps]['price'] is not None:out.append(i)
    return out[-MAX_ORIGINS:]

def backtest(points,steps=1):
    """Forecasts from every model, `steps` days ahead, at every origin. Training never includes the day being predicted."""
    records=[]
    for i in origins(points,steps):
        training=points[:i+1];records.append({'actual':points[i+steps]['price'],'predicted':{m:predict(training,m,steps)[-1]['price'] for m in MODELS}})
    return records

def rmse(errors):return math.sqrt(sum(e*e for e in errors)/len(errors))

def half_width(errors,level,steps=1):
    """95% half-width: empirical 95th percentile of past absolute errors, inflated and floored (see WIDTH_INFLATION, WIDTH_FLOOR)."""
    if len(errors)<8:return None
    if len(errors)<MIN_ORIGINS:quantile=1.96*rmse(errors)
    else:
        ordered=sorted(abs(e) for e in errors);position=.95*(len(ordered)-1);low=int(position)
        quantile=ordered[low]+(ordered[min(low+1,len(ordered)-1)]-ordered[low])*(position-low)
    return max(quantile*WIDTH_INFLATION,WIDTH_FLOOR*level*math.sqrt(steps))

def choose(records,points,steps=1):
    """Pick the model with the lowest MAE on the earlier origins; judge it on the latest origins it never influenced."""
    n=len(records);hold=min(HOLDOUT,n//2) if n>=MIN_ORIGINS else 0
    selection,evaluation=(records[:-hold],records[-hold:]) if hold else (records,records)
    pairs=lambda rs,m:[{'actual':r['actual'],'predicted':r['predicted'][m]} for r in rs]
    if hold:chosen=min(MODELS,key=lambda m:metrics(pairs(selection,m))['mae'])
    else:chosen=DEFAULT
    performance=[{'model':m,**metrics(pairs(evaluation,m)),'selected':m==chosen} for m in MODELS] if records else []
    errors=[r['predicted'][chosen]-r['actual'] for r in records]
    mean=lambda rs:sum(r['actual'] for r in rs)/len(rs)
    width=half_width(errors,mean(records),steps) if records else None
    coverage={'nominal':95,'observed':None,'n':0}
    if hold:
        # The width comes from the selection origins only, so coverage on the held-out origins is an honest check.
        sel_width=half_width([r['predicted'][chosen]-r['actual'] for r in selection],mean(selection),steps)
        covered=sum(abs(r['predicted'][chosen]-r['actual'])<=sel_width for r in evaluation)
        coverage={'nominal':95,'observed':100*covered/len(evaluation),'n':len(evaluation)}
    return {'model':chosen,'selection':'backtest' if hold else 'default','performance':performance,'width':width,'coverage':coverage,
        'selected_metrics':next((m for m in performance if m['selected']),None)}

def step_spread(points):
    """Fallback uncertainty from day-to-day price moves when there are too few back-test origins."""
    values=[p['price'] for p in points if p['price'] is not None];diffs=[b-a for a,b in zip(values,values[1:])]
    return rmse(diffs) if len(diffs)>=2 else max(1.,.05*values[-1])

def analyze(rows,as_of=None):
    as_of=as_of or today();start=as_of-timedelta(days=WINDOW_DAYS-1)
    rows=[r for r in rows if start.isoformat()<=r['date']<=as_of.isoformat()]
    rows.sort(key=lambda r:r['date']);target=as_of+timedelta(days=1)
    base={'target_date':target.isoformat(),'as_of':as_of.isoformat(),'model':None,'model_version':VERSION,'forecast':None,'performance':[],
        'winner':None,'selection':None,'selected_metrics':None,'interval_coverage':{'nominal':95,'observed':None,'n':0},'observations':len(rows),'recent_observations':0,'data_cutoff':rows[-1]['date'] if rows else None}
    if not rows:return {**base,'status':'insufficient','reason':f'No reported prices in the last {WINDOW_DAYS} days.'}
    points=daily(rows,end=as_of.isoformat());latest=rows[-1];age=(as_of-date.fromisoformat(latest['date'])).days
    recent=sum(p['price'] is not None and p['date']>=(as_of-timedelta(days=29)).isoformat() for p in points)
    base.update(latest_price=latest.get('price',latest.get('modal_price')),age_days=age,recent_observations=recent,missing_dates=sum(p['price'] is None for p in points))
    if age>MAX_AGE_DAYS:return {**base,'status':'delayed','reason':f'Latest market report is {age} days old. Tomorrow’s forecast is withheld.'}
    if len(rows)<MIN_ROWS or recent<MIN_RECENT:return {**base,'status':'insufficient','reason':f'Need {MIN_ROWS} reported dates, including {MIN_RECENT} in the last 30 days.'}
    steps=age+1;choice=choose(backtest(points,steps),points,steps)
    prediction=predict(points,choice['model'],horizon=steps)[-1];p=prediction['price'];latest_price=base['latest_price']
    # Width already comes from errors at this horizon; only the no-history fallback is scaled by the days since the last report.
    width=choice['width'] if choice['width'] is not None else max(1.96*step_spread(points),WIDTH_FLOOR*latest_price)*math.sqrt(steps)
    base.update(status='updated',reason=None,performance=choice['performance'],interval_coverage=choice['coverage'],model=choice['model'],selection=choice['selection'],horizon_days=steps,
        winner=choice['model'] if choice['selection']=='backtest' else None,selected_metrics=choice['selected_metrics'],
        forecast={'price':p,'lower':max(0,p-width),'upper':p+width,'change':p-latest_price,'change_pct':100*(p-latest_price)/latest_price if latest_price else None})
    return base
