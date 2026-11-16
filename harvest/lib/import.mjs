export const DIMENSIONS=['state','district','market','commodity','variety','grade'];
const aliases={arrivaldate:'date',arrival_date:'date',reported_date:'date',reportdate:'date',modal_price:'price',modalprice:'price',modal:'price',min_price:'min',minprice:'min',max_price:'max',maxprice:'max',state_name:'state',district_name:'district',market_name:'market',commodity_name:'commodity',variety_name:'variety',grade_name:'grade'};
export function parseCSV(text) {
  if(text.length>8_000_000) throw new Error('CSV exceeds 8 MB. Split it into smaller files.');
  const rows=[];let row=[],cell='',quoted=false;
  for(let i=0;i<text.length;i++) {const ch=text[i];
    if(ch==='"') {if(quoted&&text[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}
    else if(!quoted&&(ch===','||ch==='\n'||ch==='\r')) {row.push(cell);cell='';if(ch!==','){if(row.some(c=>c.trim()))rows.push(row);row=[];if(ch==='\r'&&text[i+1]==='\n')i++;}}
    else cell+=ch;
  }
  if(quoted)throw new Error('CSV contains an unclosed quoted field.');
  row.push(cell);if(row.some(c=>c.trim()))rows.push(row);
  if(rows.length<2)throw new Error('CSV needs a header and at least one data row.');
  const headers=rows.shift().map(h=>h.replace(/^\uFEFF/,'').trim().toLowerCase().replace(/[\s()-]+/g,'_').replace(/_+$/,''));
  return rows.map((r,i)=>{if(r.length!==headers.length)throw new Error(`CSV row ${i+2}: expected ${headers.length} columns, found ${r.length}.`);return Object.fromEntries(headers.map((h,j)=>[aliases[h]??h,r[j]]));});
}
export function normalizeDate(value) {
  let s=String(value??'').trim(),m;
  if((m=s.match(/^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$/)))s=`${m[3]}-${m[2].padStart(2,'0')}-${m[1].padStart(2,'0')}`;
  if(!/^\d{4}-\d{2}-\d{2}$/.test(s)||!Number.isFinite(Date.parse(s+'T00:00:00Z'))||new Date(s+'T00:00:00Z').toISOString().slice(0,10)!==s)throw new Error('Date must be YYYY-MM-DD or DD/MM/YYYY and valid.');
  const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata'}).format(new Date());
  if(s>today)throw new Error('Future observation dates are not allowed.');
  if(s<'1980-01-01')throw new Error('Observation date must be 1980 or later.');
  return s;
}
export const seriesKey = row => JSON.stringify(DIMENSIONS.map(k=>row[k]));
export function normalizeRows(input) {
  if(!Array.isArray(input)||!input.length||input.length>2000)throw new Error('Import 1–2,000 rows per batch. The command-line importer splits larger datasets.');
  const seen=new Map();let duplicates=0;
  input.forEach((raw,i)=>{try{
    const r=Object.fromEntries(Object.entries(raw).map(([k,v])=>{const key=k.trim().toLowerCase().replace(/[\s()-]+/g,'_');return [aliases[key]??key,v];}));
    for(const k of DIMENSIONS){r[k]=String(r[k]??(k==='grade'?'Not reported':'')).trim().replace(/\s+/g,' ');if(!r[k]||r[k].length>150)throw new Error(`Missing or invalid ${k}.`);}
    r.date=normalizeDate(r.date);
    const number=(v)=>v===null||v===undefined||String(v).trim()===''?null:Number(String(v).replace(/,/g,''));
    r.price=number(r.price);r.min=number(r.min);r.max=number(r.max);
    if(r.price==null||!Number.isFinite(r.price)||r.price<0||r.price>1e8)throw new Error('Modal price must be a non-negative number in INR/quintal.');
    for(const k of ['min','max'])if(r[k]!=null&&(!Number.isFinite(r[k])||r[k]<0))throw new Error(`Invalid ${k} price.`);
    if((r.min!=null&&r.min>r.price)||(r.max!=null&&r.max<r.price))throw new Error('Expected minimum ≤ modal ≤ maximum.');
    if(r.unit&& !['inr/quintal','rs/quintal','quintal','rs./quintal'].includes(String(r.unit).toLowerCase()))throw new Error('Convert prices to INR/quintal before importing.');
    r.key=seriesKey(r);const key=r.key+'|'+r.date;if(seen.has(key))duplicates++;seen.set(key,r);
  }catch(e){throw new Error(`Row ${i+2}: ${e.message}`);}});
  const rows=[...seen.values()];if(new Set(rows.map(r=>r.key)).size>100)throw new Error('Limit each batch to 100 distinct market series.');
  return {rows,duplicates};
}
