import {analyze} from './forecast.mjs';
import {normalizeRows,DIMENSIONS} from './import.mjs';
export function repository(db) {
 const all=async(sql,...args)=>(await db.prepare(sql).bind(...args).all()).results;
 const run=async(sql,...args)=>db.prepare(sql).bind(...args).run();
 const revision=async()=> (await all("SELECT value FROM metadata WHERE key='revision'"))[0]?.value??0;
 const log=async(context,message)=>run('INSERT INTO errors VALUES (?,?,?,?)',crypto.randomUUID(),new Date().toISOString(),context,String(message).slice(0,1000));
 async function report(key) {
   const rev=await revision(), cached=(await all('SELECT * FROM forecasts WHERE key=? AND revision=?',key,rev))[0];
   if(cached)return {...JSON.parse(cached.result),generated:cached.generated};
   const s=(await all('SELECT * FROM series WHERE key=?',key))[0];if(!s)throw new Error('Market selection not found.');
   const rows=await all('SELECT date,price,min,max,source FROM prices WHERE key=? ORDER BY date',key);
   const result={...analyze(rows),series:s,sample:false,sources:[...new Set(rows.map(r=>r.source))]},generated=new Date().toISOString();
   await run('INSERT INTO forecasts VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET revision=excluded.revision,generated=excluded.generated,result=excluded.result WHERE forecasts.revision<=excluded.revision',key,rev,generated,JSON.stringify(result));
   return {...result,generated};
 }
 async function ingest(input,source='CSV import') {
   source=String(source).trim().slice(0,180)||'CSV import';
   const id=crypto.randomUUID(),created=new Date().toISOString();let count=0,duplicates=0;
   try {
     const validated=normalizeRows(input),rows=validated.rows;duplicates=validated.duplicates;count=rows.length;
     const series=[...new Map(rows.map(r=>[r.key,r])).values()],stmts=[];
     for(const r of series)stmts.push(db.prepare('INSERT OR IGNORE INTO series VALUES (?,?,?,?,?,?,?)').bind(r.key,...DIMENSIONS.map(k=>r[k])));
     // Ten rows per statement stays under D1's bound-parameter limit.
     for(let i=0;i<rows.length;i+=10){const chunk=rows.slice(i,i+10);stmts.push(db.prepare(`INSERT INTO prices (key,date,price,min,max,source,import_id) VALUES ${chunk.map(()=>'(?,?,?,?,?,?,?)').join(',')} ON CONFLICT(key,date) DO UPDATE SET price=excluded.price,min=excluded.min,max=excluded.max,source=excluded.source,import_id=excluded.import_id`).bind(...chunk.flatMap(r=>[r.key,r.date,r.price,r.min,r.max,source,id])));}
     stmts.push(db.prepare("INSERT INTO metadata VALUES ('revision',1) ON CONFLICT(key) DO UPDATE SET value=value+1"));
     stmts.push(db.prepare('INSERT INTO imports VALUES (?,?,?,?,?,?,?)').bind(id,created,source,count,duplicates,'refreshing',null));
     await db.batch(stmts); // D1 batch is an atomic transaction.
     let failures=0;
     for(const s of series)try{await report(s.key);}catch(e){failures++;await log('forecast refresh',e.message);}
     const status=failures?'partial':'complete',message=failures?`${failures} forecast refreshes failed; data saved. Retry by opening those series.`:`${series.length} series refreshed. Existing dates updated; ${duplicates} duplicate rows in file merged.`;
     await run('UPDATE imports SET status=?,message=? WHERE id=?',status,message,id);
     return {id,rows:count,duplicates,series:series.length,status,message};
   }catch(e){await run('INSERT OR IGNORE INTO imports VALUES (?,?,?,?,?,?,?)',id,created,source,0,0,'failed',e.message);await log('import',e.message);throw e;}
 }
 async function status(){
   const totals=(await all('SELECT COUNT(*) rows,MIN(date) first,MAX(date) latest,COUNT(DISTINCT key) series FROM prices'))[0];
   const rev=await revision();
   const caches=await all('SELECT result FROM forecasts WHERE revision=?',rev);
   const performance=caches.flatMap(c=>JSON.parse(c.result).performance??[]);
   // Micro-aggregate by observation count, including a proper pooled RMSE.
   const pooled=[];
   for(const model of [...new Set(performance.map(p=>p.model))])for(const horizon of [1,3,7,14]){
    const a=performance.filter(p=>p.model===model&&p.horizon===horizon&&p.n>0),n=a.reduce((s,p)=>s+p.n,0),mapeN=a.reduce((s,p)=>s+p.mapeN,0);
    if(n)pooled.push({model,horizon,n,mae:a.reduce((s,p)=>s+p.mae*p.n,0)/n,rmse:Math.sqrt(a.reduce((s,p)=>s+p.rmse**2*p.n,0)/n),mape:mapeN?a.reduce((s,p)=>s+(p.mape??0)*p.mapeN,0)/mapeN:null});
   }
   return {database:'Connected',...totals,revision:rev,cachedSeries:caches.length,performance:pooled,imports:await all('SELECT * FROM imports ORDER BY created DESC LIMIT 30'),errors:await all('SELECT * FROM errors ORDER BY created DESC LIMIT 20')};
 }
 return {catalog:()=>all('SELECT * FROM series ORDER BY state,district,market,commodity,variety,grade'),report,ingest,status,log};
}
