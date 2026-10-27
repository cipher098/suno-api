const fs=require('node:fs'),ts=require('typescript'),test=require('node:test'),assert=require('node:assert/strict');
require.extensions['.ts']=(mod,file)=>mod._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true,resolveJsonModule:true}}).outputText,file);
const {prepareGenerationAuth,GenerationAuthError,GenerationAuthGate}=require('../src/lib/GenerationAuth.ts');
const {captureBrowserGenerationToken,generationRequestPattern}=require('../src/lib/BrowserGenerationToken.ts');
const {StudioApi}=require('../src/lib/StudioApi.ts');
const project='11111111-1111-4111-8111-111111111111';
const bearer=user=>'header.'+Buffer.from(JSON.stringify({sub:user})).toString('base64url')+'.signature';
test('shared auth does not launch browser when CAPTCHA is unnecessary',async()=>{
 let solves=0;assert.equal(await prepareGenerationAuth({check:async()=>false,solve:async()=>{solves++;return 'token'},allowBrowser:true}),null);assert.equal(solves,0);
});
test('default-off and malformed checks stop before browser or generation',async()=>{
 for(const required of [true,undefined,null,'false']){
  let solves=0;await assert.rejects(prepareGenerationAuth({check:async()=>required,allowBrowser:false,solve:async()=>{solves++;return 'token'}}),required===true?/CAPTCHA_REQUIRED/:/CAPTCHA_CHECK_FAILED/);assert.equal(solves,0);
 }
});
test('opted-in auth calls existing browser solver exactly once and returns its token',async()=>{
 let solves=0;assert.equal(await prepareGenerationAuth({check:async()=>true,allowBrowser:true,solve:async()=>{solves++;return 'challenge-token'}}),'challenge-token');assert.equal(solves,1);
});
test('missing token and browser failure are safe errors without retries or private messages',async()=>{
 for(const value of [null,'', '   '])await assert.rejects(prepareGenerationAuth({check:async()=>true,allowBrowser:true,solve:async()=>value}),/CAPTCHA_TOKEN_MISSING/);
 let solves=0;await assert.rejects(prepareGenerationAuth({check:async()=>true,allowBrowser:true,solve:async()=>{solves++;throw Error('private bearer and cookies')}}),e=>e.message==='CAPTCHA_BROWSER_FAILED');assert.equal(solves,1);
});
test('account auth gate serializes challenges without reusing a token',async()=>{
 const gate=new GenerationAuthGate();let active=0,max=0,count=0;
 const options={check:async()=>true,allowBrowser:true,solve:async()=>{active++;max=Math.max(max,active);await new Promise(r=>setTimeout(r,5));const token='token-'+(++count);active--;return token}};
 assert.deepEqual(await Promise.all([gate.prepare(options),gate.prepare(options)]),['token-1','token-2']);assert.equal(max,1);
});
for(const url of ['https://studio-api.prod.suno.com/api/generate/v2/','https://suno.com/a/api/generate/v2-web/']){
 test('browser intercepts '+url+' before UI trigger and aborts incidental generation',async()=>{
  const events=[];let handler,accepted;
  const page={route:async(pattern,fn)=>{assert.match(url,pattern);handler=fn;events.push('registered')}};
  const token=await captureBrowserGenerationToken({page,expectedUser:'owner',acceptBearer:t=>accepted=t,close:async()=>events.push('closed'),
   trigger:async()=>{events.push('clicked');await handler({abort:async()=>events.push('aborted'),request:()=>({url:()=>url,method:()=> 'POST',headers:()=>({authorization:'Bearer '+bearer('owner')}),postDataJSON:()=>({token:'challenge-token'})})})}});
  assert.equal(token,'challenge-token');assert.equal(accepted,bearer('owner'));assert.deepEqual(events,['registered','clicked','aborted','closed']);
 });
}
for(const kind of ['wrong-account','missing-token','future-endpoint'])test('browser fails closed: '+kind,async()=>{
 let handler,aborted=0,closed=0,accepted=0;
 const url=kind==='future-endpoint'?'https://suno.com/a/api/generate/v3/':'https://suno.com/a/api/generate/v2-web/';
 assert.match(url,generationRequestPattern);
 await assert.rejects(captureBrowserGenerationToken({page:{route:async(p,h)=>handler=h},expectedUser:'owner',acceptBearer:()=>accepted++,close:async()=>closed++,
 trigger:async()=>handler({abort:async()=>aborted++,request:()=>({url:()=>url,method:()=> 'POST',headers:()=>({authorization:'Bearer '+bearer(kind==='wrong-account'?'other':'owner')}),postDataJSON:()=>({token:kind==='missing-token'?'':'challenge-token'})})})}),
 kind==='wrong-account'?/CAPTCHA_ACCOUNT_MISMATCH/:kind==='missing-token'?/CAPTCHA_TOKEN_MISSING/:/ENDPOINT_UNSUPPORTED/);
 assert.equal(aborted,1);assert.equal(closed,1);assert.equal(accepted,0);
});
test('browser timeout aborts its signal and closes without a generation',async()=>{
 let closed=0,signal;
 await assert.rejects(captureBrowserGenerationToken({page:{route:async()=>{}},expectedUser:'owner',acceptBearer:()=>{},close:async()=>closed++,trigger:async s=>{signal=s},timeoutMs:5}),/CAPTCHA_BROWSER_TIMEOUT/);
 assert.equal(signal.aborted,true);assert.equal(closed,1);
});
test('browser UI error closes without leaking the underlying exception',async()=>{
 let closed=0;await assert.rejects(captureBrowserGenerationToken({page:{route:async()=>{}},expectedUser:'owner',acceptBearer:()=>{},close:async()=>closed++,trigger:async()=>{throw Error('private-cookie')}}),e=>e.message==='CAPTCHA_BROWSER_FAILED');assert.equal(closed,1);
});
test('Studio uses shared auth token, preserves Studio payload and submits exactly once',async()=>{
 const calls=[];let solves=0;
 const studio=new StudioApi(async(path,method,body)=>{calls.push({path,method,body});if(path.startsWith('studio/project/'))return {owner_user_id:'owner'};return {clips:[]}},()=> 'owner',
 ()=>prepareGenerationAuth({check:async()=>true,allowBrowser:true,solve:async()=>{solves++;return 'challenge-token'}}));
 await studio.generate({project_id:project,prompt:'Exact Scripture',tags:'upbeat',title:'Genesis 2'});
 const submit=calls.filter(c=>c.path==='generate/v2-web/');assert.equal(submit.length,1);assert.equal(solves,1);
 assert.equal(submit[0].body.token,'challenge-token');assert.equal(submit[0].body.task,'agentic_instant');assert.equal(submit[0].body.attached_lyrics,'Exact Scripture');assert.equal(submit[0].body.metadata.from_studio_project_id,project);
});
test('auth failure never submits and submission failure never re-solves or retries',async()=>{
 for(const failAuth of [true,false]){
 let submits=0,auths=0;const studio=new StudioApi(async(path)=>{if(path.startsWith('studio/project/'))return {owner_user_id:'owner'};submits++;throw Error('uncertain')},()=> 'owner',async()=>{auths++;if(failAuth)throw new GenerationAuthError('CAPTCHA_BROWSER_FAILED');return 'token'});
 await assert.rejects(studio.generate({project_id:project,prompt:'Words',tags:'upbeat'}));assert.equal(submits,failAuth?0:1);assert.equal(auths,1);
 }
});

test('real SunoApi wiring shares helper: legacy default enabled, Studio fallback explicitly opted in',async()=>{
 const Module=require('node:module');const file=require('node:path').resolve(__dirname,'../src/lib/SunoApi.ts');
 const code=fs.readFileSync(file,'utf8').replace(/'@\/lib\//g,"'./");
 const mod=new Module(file,module);mod.filename=file;mod.paths=Module._nodeModulePaths(require('node:path').dirname(file));
 mod._compile(ts.transpileModule(code,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true}}).outputText+'\nexports.TestSunoApi=SunoApi;',file);
 const api=new mod.exports.TestSunoApi('__client=fake');api.keepAlive=async()=>{};api.currentToken=bearer('owner');
 let solves=0,submits=0;api.captchaRequired=async()=>true;api.solveCaptchaInBrowser=async()=>{solves++;return 'challenge-token'};
 assert.equal(await api.getCaptcha(),'challenge-token');assert.equal(solves,1);
 api.client.request=async({url,data})=>{
  if(url.endsWith('studio/project/'+project))return {data:{owner_user_id:'owner'}};
  if(url.endsWith('c/check'))return {data:{required:true}};
  if(url.endsWith('generate/v2-web/')){submits++;assert.equal(data.token,'challenge-token');return {data:{clips:[]}};}
  throw Error('Unexpected route');
 };
 const old=process.env.SUNO_STUDIO_BROWSER_FALLBACK;
 try{
  delete process.env.SUNO_STUDIO_BROWSER_FALLBACK;
  await assert.rejects(api.studio().generate({project_id:project,prompt:'Words',tags:'upbeat'}),/CAPTCHA_REQUIRED/);
  assert.equal(solves,1);assert.equal(submits,0);
  process.env.SUNO_STUDIO_BROWSER_FALLBACK='true';
  await api.studio().generate({project_id:project,prompt:'Words',tags:'upbeat'});
  assert.equal(solves,2);assert.equal(submits,1);
 }finally{if(old===undefined)delete process.env.SUNO_STUDIO_BROWSER_FALLBACK;else process.env.SUNO_STUDIO_BROWSER_FALLBACK=old;}
});

test('browser controls support current Create song UI and retain legacy custom textarea',async()=>{
 const {selectGenerationControls}=require('../src/lib/BrowserGenerationToken.ts');
 for(const legacy of [true,false]){
  const visible={first(){return this},waitFor:async()=>{}};const old={count:async()=>legacy?1:0,first(){return this}};
  const button={first(){return this},waitFor:async()=>{}};
  const page={locator:selector=>selector==='.custom-textarea:visible'?old:visible,getByRole:(role,options)=>{assert.equal(role,'button');assert.match('Create song',options.name);assert.match('Create',options.name);return button}};
  const controls=await selectGenerationControls(page);assert.equal(controls.textarea,legacy?old:visible);assert.equal(controls.button,button);
 }
});

test('auth browser normalizes remembered Advanced mode and dismisses lyricist onboarding',async()=>{
 const {prepareGenerationPage}=require('../src/lib/BrowserGenerationToken.ts');let tip=true;const events=[];
 const page={waitForFunction:async()=>{},getByRole:(role,{name})=>({first(){return this},count:async()=>1,isVisible:async()=>tip,click:async()=>{events.push(name);if(name==='Simple')assert.equal(role,'tab');if(name==='Got it')tip=false}})};
 await prepareGenerationPage(page);assert.deepEqual(events,['Got it','Simple']);
});

test('Turnstile browser adapter observes render parameters and delivers one solved token',async()=>{
 const vm=require('node:vm');const {installTurnstileCapture,solveObservedTurnstile}=require('../src/lib/TurnstileBrowserAuth.ts');
 let delivered,rendered=0,calls=0;const attributes={};const events={};const document={documentElement:{setAttribute:(k,v)=>attributes[k]=v,getAttribute:k=>attributes[k]??null,removeAttribute:k=>delete attributes[k]},addEventListener:(n,f)=>events[n]=f,dispatchEvent:e=>events[e.type]?.(e)};
 const context={window:{},document,location:{origin:'https://suno.com',pathname:'/create'},navigator:{userAgent:'test-browser'}};
 const isolated={window:{},document,CustomEvent:class {constructor(type,{detail}){this.type=type;this.detail=detail}}};
 const page={addInitScript:async fn=>vm.runInNewContext('('+fn.toString()+')()',context),evaluate:async(fn,arg)=>vm.runInNewContext('('+fn.toString()+')('+JSON.stringify(arg)+')',isolated)};
 await installTurnstileCapture(page);context.window.turnstile={render(){rendered++;return 'widget'}};
 context.window.turnstile.render('#widget',{sitekey:'public-sitekey',action:'generation',cData:'challenge-data',callback:t=>delivered=t});
 await solveObservedTurnstile(page,async params=>{calls++;assert.equal(params.sitekey,'public-sitekey');assert.equal(params.pageurl,'https://suno.com/create');assert.equal(params.data,'challenge-data');return {data:'solved-token'}},new AbortController().signal);
 assert.equal(delivered,'solved-token');assert.equal(rendered,1);assert.equal(calls,1);
});
test('Turnstile rejects unobserved/foreign widgets before spending and never retries a solver error',async()=>{
 const {solveObservedTurnstile}=require('../src/lib/TurnstileBrowserAuth.ts');let calls=0;
 for(const params of [null,{sitekey:'x',pageurl:'https://foreign.example/create'}]){
  await assert.rejects(solveObservedTurnstile({evaluate:async()=>params},async()=>{calls++;return {data:'token'}},new AbortController().signal),/TURNSTILE_PARAMETERS_UNAVAILABLE/);
 }assert.equal(calls,0);
 await assert.rejects(solveObservedTurnstile({evaluate:async()=>({sitekey:'public',pageurl:'https://suno.com/create'})},async()=>{calls++;throw Error('private credentials')},new AbortController().signal),/TURNSTILE_SOLVER_FAILED/);assert.equal(calls,1);
});
test('Studio forwards the captured token provider rather than assuming the old provider',async()=>{
 let payload;const api=new StudioApi(async(path,m,body)=>{if(path.startsWith('studio/project/'))return {owner_user_id:'owner'};payload=body;return {clips:[]}},()=> 'owner',async()=>({token:'turnstile-token',tokenProvider:1}));
 await api.generate({project_id:project,prompt:'Exact Scripture',tags:'upbeat'});assert.equal(payload.token,'turnstile-token');assert.equal(payload.token_provider,1);
});

test('download accepts observed bare-render M4A path and rejects another recording',async()=>{
 const render='33333333-3333-4333-8333-333333333333';
 const api=new StudioApi(async()=>({status:'ready',download_url:`https://suno-data-uploads.s3.amazonaws.com/studio/uploads/${render}.m4a?signature=mock`}),()=> 'owner');
 assert.equal((await api.getDownload(render)).status,'ready');
 await assert.rejects(api.getDownload(project),/UNEXPECTED_DOWNLOAD_DESTINATION/);
});
