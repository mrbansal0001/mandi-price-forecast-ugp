/** Daily local-linear-trend Kalman filter. No interpolation or future observations. */
export const MODELS = ['Adaptive Kalman', 'Last price', '7-day mean', 'Exponential smoothing'];
export const HORIZONS = [1, 3, 7, 14];
export const day = (s, n = 0) => new Date(Date.parse(s + 'T00:00:00Z') + n * 86400000).toISOString().slice(0, 10);
export function daily(rows) {
  if (!rows.length) return [];
  const sorted = [...rows].sort((a,b) => a.date.localeCompare(b.date));
  const map = new Map(sorted.map(r => [r.date, r.price]));
  const length = Math.round((Date.parse(sorted.at(-1).date) - Date.parse(sorted[0].date)) / 86400000) + 1;
  if (length > 15000) throw new Error('History exceeds 41 years. Split the dataset.');
  return Array.from({ length }, (_,i) => ({date:day(sorted[0].date,i), price:map.get(day(sorted[0].date,i)) ?? null}));
}
export function kalman(points, horizon = 14) {
  const first = points.findIndex(p => p.price != null);
  if (first < 0) throw new Error('No observations');
  let level = points[first].price, trend = 0;
  const scale = Math.max(1, level * .03) ** 2;
  let a = scale, b = 0, c = scale / 20, r = scale, q = scale * .02;
  // F=[[1,1],[0,1]]; observation H=[1,0]. Q is diagonal.
  for (let i = first + 1; i < points.length; i++) {
    level += trend;
    const pa = a + 2*b + c + q, pb = b + c, pc = c + q*.01;
    const value = points[i].price;
    if (value == null) { a=pa; b=pb; c=pc; continue; }
    const innovation = value-level, variance=pa+r, k0=pa/variance, k1=pb/variance;
    level += k0*innovation; trend += k1*innovation;
    // Scalar update, symmetric covariance, floors prevent numerical degeneration.
    a=Math.max(1e-8,(1-k0)*pa); b=(1-k0)*pb; c=Math.max(1e-8,pc-k1*pb);
    // Innovation covariance matching with EWMA and bounded variance adaptation.
    r=Math.min(scale*100,Math.max(scale*.01,.95*r+.05*Math.max(scale*.01,innovation**2-pa)));
    q=Math.min(scale*10,Math.max(scale*.001,.98*q+.02*(k0*innovation)**2));
  }
  return Array.from({length:horizon}, (_,i) => {
    level+=trend; const pa=a+2*b+c+q; b+=c; c+=q*.01; a=pa;
    return {date:day(points.at(-1).date,i+1),price:Math.max(0,level),sd:Math.sqrt(a+r)};
  });
}
export function predict(points, name, horizon=14) {
  if (name === MODELS[0]) return kalman(points,horizon);
  const observed = points.filter(p=>p.price != null);
  if (!observed.length) throw new Error('No observations');
  let price=observed.at(-1).price;
  if (name===MODELS[2]) {
    const recent=points.slice(-7).filter(p=>p.price != null);
    price=recent.length ? recent.reduce((s,p)=>s+p.price,0)/recent.length : price;
  }
  if (name===MODELS[3]) { price=observed[0].price; for(const p of observed.slice(1)) price=.3*p.price+.7*price; }
  return Array.from({length:horizon},(_,i)=>({date:day(points.at(-1).date,i+1),price}));
}
export function metrics(pairs) {
  if (!pairs.length) return {mae:null,rmse:null,mape:null,n:0,mapeN:0};
  const nz=pairs.filter(p=>p.actual!==0);
  return {mae:pairs.reduce((s,p)=>s+Math.abs(p.actual-p.predicted),0)/pairs.length,
    rmse:Math.sqrt(pairs.reduce((s,p)=>s+(p.actual-p.predicted)**2,0)/pairs.length),
    mape:nz.length ? nz.reduce((s,p)=>s+Math.abs((p.actual-p.predicted)/p.actual),0)/nz.length*100 : null,
    n:pairs.length,mapeN:nz.length};
}
export function analyze(rows) {
  // Bound compute to the last 730 calendar days; leading missing dates are retained.
  const points=daily(rows).slice(-730), observed=points.filter(p=>p.price!=null);
  if(observed.length<20) return {history:points,forecasts:[],performance:[],winner:{},reason:'At least 20 observed dates are needed to forecast.',observations:observed.length};
  const pairs=Object.fromEntries(MODELS.map(m=>[m,Object.fromEntries(HORIZONS.map(h=>[h,[]]))]));
  // Up to 30 common origins. Every model sees the same training dates and targets.
  const origins=[];
  for(let i=points.length-2;i>=19 && origins.length<30;i--) {
    if(points[i].price!=null && points.slice(0,i+1).filter(p=>p.price!=null).length>=20) origins.unshift(i);
  }
  for(const i of origins) for(const name of MODELS) {
    const predicted=predict(points.slice(0,i+1),name,14);
    for(const h of HORIZONS) {
      const actual=points[i+h]?.price;
      if(actual!=null) pairs[name][h].push({actual,predicted:predicted[h-1].price});
    }
  }
  const performance=MODELS.flatMap(model=>HORIZONS.map(horizon=>({model,horizon,...metrics(pairs[model][horizon])})));
  const winner=Object.fromEntries(HORIZONS.map(h=>[h,performance.filter(p=>p.horizon===h&&p.n>=5).sort((a,b)=>a.rmse-b.rmse)[0]?.model??null]));
  const base=kalman(points,14), latest=observed.at(-1).price;
  const forecasts=base.map((p,i)=>{
    const h=HORIZONS.find(h=>h>=i+1), empirical=performance.find(m=>m.model===MODELS[0]&&m.horizon===h);
    // Conservative normal approximation: do not narrow model uncertainty using residuals.
    const sd=Math.max(p.sd,empirical?.n>=5 ? empirical.rmse : 0);
    return {...p,lower:Math.max(0,p.price-1.96*sd),upper:p.price+1.96*sd,change:p.price-latest,changePct:latest?100*(p.price-latest)/latest:null};
  });
  return {history:points,forecasts,performance,winner,observations:observed.length,missing:points.filter(p=>p.price==null).length,
    origin:points.at(-1).date,latest,reason:null};
}
