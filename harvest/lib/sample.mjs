// Deliberately synthetic data. Never saved to the production observation tables.
import {day,analyze} from './forecast.mjs';
import {seriesKey} from './import.mjs';
export function samples(){
 const places=[['Maharashtra','Nashik','Lasalgaon'],['Maharashtra','Pune','Pune'],['Karnataka','Bengaluru','Bengaluru'],['Uttar Pradesh','Agra','Agra']];
 return places.flatMap(([state,district,market],j)=>['Onion','Tomato','Potato','Wheat'].map((commodity,k)=>{
 const s={state,district,market,commodity,variety:k===3?'Lokwan':'Local',grade:'FAQ'};return {...s,key:seriesKey(s),seed:j*7+k,base:[2200,1800,1450,2650][k]+j*80};}));
}
export function sampleReport(key){const s=samples().find(s=>s.key===key)??samples()[0];
 const rows=Array.from({length:120},(_,i)=>({date:day('2026-06-28',i),price:Math.round(s.base+i*1.4+Math.sin((i+s.seed)/7)*115+Math.sin(i*1.7+s.seed)*65)})).filter((_,i)=>i%13!==4);
 return {...analyze(rows),series:s,sample:true};
}
