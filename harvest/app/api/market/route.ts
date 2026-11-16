import {env} from 'cloudflare:workers';
import {repository} from '@/lib/store.mjs';
import {samples,sampleReport} from '@/lib/sample.mjs';
export async function GET(request:Request){
 try{
  if(!env.DB)throw new Error('Database binding is unavailable.');
  const repo=repository(env.DB),params=new URL(request.url).searchParams,key=params.get('key'),sample=params.get('sample')==='1';
  if(sample)return Response.json(key?sampleReport(key):{series:samples(),sample:true},{headers:{'Cache-Control':'no-store'}});
  const result=key?await repo.report(key):{series:await repo.catalog(),sample:false};
  return Response.json(result,{headers:{'Cache-Control':'no-store'}});
 }catch(e){return Response.json({error:(e as Error).message},{status:503});}
}
