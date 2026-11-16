import {env} from 'cloudflare:workers';
import {repository} from '@/lib/store.mjs';
import {parseCSV} from '@/lib/import.mjs';
// Production is owner-private at the Sites gateway. Do not make public without adding roles.
export async function GET(){
 try{if(!env.DB)throw new Error('Database binding unavailable.');return Response.json(await repository(env.DB).status(),{headers:{'Cache-Control':'no-store'}});}
 catch(e){return Response.json({error:(e as Error).message},{status:503});}
}
export async function POST(request:Request){
 const origin=request.headers.get('origin');
 if(origin&&origin!==new URL(request.url).origin)return Response.json({error:'Cross-origin imports are not allowed.'},{status:403});
 if(!request.headers.get('content-type')?.includes('application/json'))return Response.json({error:'Use application/json.'},{status:415});
 if(Number(request.headers.get('content-length')??0)>8_000_000)return Response.json({error:'Batch too large.'},{status:413});
 try{
  if(!env.DB)throw new Error('Database binding unavailable.');
  const text=await request.text();if(text.length>8_000_000)throw new Error('Batch exceeds 8 MB.');
  const body=JSON.parse(text);
  const rows=body.csv?parseCSV(body.csv):body.rows;
  return Response.json(await repository(env.DB).ingest(rows,body.source));
 }catch(e){return Response.json({error:(e as Error).message},{status:400});}
}
