const fs=require('node:fs');const ts=require('typescript');const test=require('node:test');const assert=require('node:assert/strict');
require.extensions['.ts']=(mod,file)=>mod._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true,resolveJsonModule:true}}).outputText,file);
const {StudioApi,fullSongState}=require('../src/lib/StudioApi.ts');
const {middleware}=require('../src/middleware.ts');const {NextRequest}=require('next/server');
const project='11111111-1111-4111-8111-111111111111';const clip='22222222-2222-4222-8222-222222222222';
test('clean schema provides full unprocessed export without an account-specific state',()=>{
  const out=fullSongState({project_id:project,clip_id:clip,duration_seconds:300});
  assert.equal(out.end,600);assert.equal(out.state.majorVersion,2);assert.equal(out.state.tracks.length,1);
  const track=out.state.tracks[0];assert.deepEqual(track.signalChain,[]);assert.equal(track.eq.enabled,false);
  assert.equal(track.clips[0].clipId,clip);assert.equal(track.clips[0].readStartBeats,0);assert.equal(track.clips[0].warp.enabled,false);
});
test('account identity is scoped to the existing authenticated instance; alignment preserves raw response',async()=>{
  const calls=[];const response={aligned_words:[{word:'Test',start_s:0,end_s:0.4,custom:'preserved'}]};
  const api=new StudioApi(async(p,m,b)=>{calls.push([p,m,b]);return response},()=> 'test-owner');
  assert.equal(api.account().accountId,'test-owner');assert.equal(api.account().apiVersion,1);
  assert.deepEqual(await api.alignment(clip),response);assert.deepEqual(calls,[[`studio-alignment/${clip}`,'GET',undefined]]);
});
test('new read-only account endpoints cannot generate or export',async()=>{
  const calls=[];const api=new StudioApi(async(p,m,b)=>{calls.push([p,m]);return {}},()=> 'owner');
  api.account();await api.billing();await api.check();assert.deepEqual(calls,[['billing/info/','GET'],['c/check','POST']]);
});
test('account-server middleware requires local token and rejects incoming account/browser overrides',()=>{
  const old={...process.env};process.env.SUNO_API_ACCOUNT_MODE='1';process.env.SUNO_API_LOCAL_TOKEN='x'.repeat(40);
  try{
    for(const headers of [{},{authorization:'Bearer wrong'},{authorization:'Bearer '+'x'.repeat(40),cookie:'__client=other'},
      {authorization:'Bearer '+'x'.repeat(40),origin:'https://unrelated.example'}]){
      assert.equal(middleware(new NextRequest('http://127.0.0.1:3057/api/studio/account',{headers})).status,401);
    }
    assert.equal(middleware(new NextRequest('http://127.0.0.1:3057/api/studio/account',{headers:{authorization:'Bearer '+'x'.repeat(40)}})).headers.get('x-middleware-next'),'1');
    delete process.env.SUNO_API_ACCOUNT_MODE;
    assert.equal(middleware(new NextRequest('http://127.0.0.1/api/studio/account')).headers.get('x-middleware-next'),'1');
  }finally{process.env=old;}
});
