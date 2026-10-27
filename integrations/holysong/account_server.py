"""One loopback suno-api process per named account. Credentials never reach the worker."""
from pathlib import Path
import argparse, contextlib, fcntl, hashlib, json, os, re, secrets, signal, socket, stat, subprocess, sys, time
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError

REPO_ENV_KEYS={'TWOCAPTCHA_KEY','BROWSER','BROWSER_HEADLESS','BROWSER_LOCALE','BROWSER_GHOST_CURSOR',
               'BROWSER_DISABLE_GPU','SUNO_STUDIO_BROWSER_FALLBACK'}
ROOT=Path.home()/'.config/holysong/suno-accounts'
class AccountError(RuntimeError): pass

def profile_path(name=None):
    name=name or os.environ.get('HOLYSONG_SUNO_ACCOUNT','main')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',name): raise AccountError('INVALID_ACCOUNT_NAME')
    return ROOT/name/'profile.json'

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)

def read_private(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.getuid() or stat.S_IMODE(path.stat().st_mode)&0o077:
        raise AccountError('PRIVATE_FILE_REQUIRED')
    return path.read_text()

def profile(name=None):
    p=json.loads(read_private(profile_path(name)))
    if not isinstance(p.get('port'),int) or not 1024<=p['port']<=65535 or len(p.get('token',''))<32:
        raise AccountError('INVALID_ACCOUNT_PROFILE')
    return p

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args): return None

def request(p,path,body=None):
    if not path.startswith('/api/studio/'): raise AccountError('LOCAL_PATH_REFUSED')
    req=Request(f"http://127.0.0.1:{p['port']}"+path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+p['token'],'Content-Type':'application/json'})
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(req,timeout=180 if path=='/api/studio/generate' else 100) as r:
            return json.load(r)
    except HTTPError as e:
        # Only expose a restricted error code, never upstream/network exception objects.
        try: code=json.load(e).get('error','LOCAL_API_FAILED')
        except Exception: code='LOCAL_API_FAILED'
        if not isinstance(code,str) or not re.fullmatch('[A-Z_0-9]+',code):code='LOCAL_API_FAILED'
        raise AccountError(code) from None
    except (URLError,TimeoutError,OSError):raise AccountError('LOCAL_API_UNAVAILABLE') from None

def identity(p):
    data=request(p,'/api/studio/account')
    user=data.get('accountId')
    if not isinstance(user,str) or not user or data.get('apiVersion')!=1: raise AccountError('LOCAL_API_IDENTITY_INVALID')
    fp=hashlib.sha256(user.encode()).hexdigest()[:24]
    if p.get('accountFingerprint') and p['accountFingerprint']!=fp:raise AccountError('ACCOUNT_PROFILE_CHANGED')
    return data,fp

def environment(path):
    values={}
    for line in read_private(Path(path)).splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        if line.startswith('export '):line=line[7:]
        if '=' not in line:raise AccountError('INVALID_ENV_LINE')
        key,value=line.split('=',1);key=key.strip();value=value.strip()
        if value.startswith(('"',"'")):
            if len(value)<2 or value[-1]!=value[0]:raise AccountError('INVALID_ENV_QUOTES')
            value=value[1:-1]
        values[key]=value
    return values

def server_environment(values,p):
    env=dict(os.environ)
    for key in ('SUNO_COOKIE','SUNO_API_LOCAL_TOKEN','SUNO_EXPECTED_ACCOUNT'):env.pop(key,None)
    env.update({key:value for key,value in values.items() if key in REPO_ENV_KEYS})
    env.update(SUNO_COOKIE=values['SUNO_COOKIE'],SUNO_API_ACCOUNT_MODE='1',SUNO_API_LOCAL_TOKEN=p['token'],NEXT_TELEMETRY_DISABLED='1')
    return env

def ensure(name=None):
    path=profile_path(name);p=profile(name)
    with (path.parent/'server.lock').open('a') as guard:
        fcntl.flock(guard,fcntl.LOCK_EX)
        try:
            data,fp=identity(p)
        except AccountError as e:
            if str(e)!='LOCAL_API_UNAVAILABLE':raise
            # Refuse to start on an occupied port, even if it is an unrelated local server.
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1',p['port']))==0:raise AccountError('ACCOUNT_PORT_IN_USE')
            values=environment(p['envFile'])
            cookie=values.get('SUNO_COOKIE','')
            if '__client=' not in cookie:raise AccountError('SUNO_CLIENT_COOKIE_REQUIRED')
            repo=Path(p['repo']);node=Path(p['node'])
            if not (repo/'.next/BUILD_ID').is_file() or not node.is_file():raise AccountError('ACCOUNT_SERVER_BUILD_REQUIRED')
            env=server_environment(values,p)
            with (path.parent/'server.log').open('ab') as log:
                child=subprocess.Popen([str(node),str(repo/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port',str(p['port'])],
                    cwd=repo,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
            save(path.parent/'process.json',{'pid':child.pid,'repo':str(repo),'port':p['port']})
            deadline=time.monotonic()+90
            while True:
                try:data,fp=identity(p);break
                except AccountError as error:
                    if str(error)!='LOCAL_API_UNAVAILABLE' or child.poll() is not None or time.monotonic()>deadline:
                        raise AccountError('ACCOUNT_SERVER_START_FAILED') from None
                    time.sleep(1)
        if not p.get('accountFingerprint'):
            p['accountFingerprint']=fp;save(path,p)
        return p,data

def main():
    os.umask(0o077)
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('action',choices=['setup','start','status','stop']);ap.add_argument('--account',default='main')
    ap.add_argument('--env-file',type=Path);ap.add_argument('--project-id');ap.add_argument('--port',type=int,default=3057);ap.add_argument('--repo',type=Path);ap.add_argument('--node',type=Path)
    args=ap.parse_args();os.environ['HOLYSONG_SUNO_ACCOUNT']=args.account
    try:
        path=profile_path()
        if args.action=='setup':
            if path.exists():raise AccountError('PROFILE_EXISTS_USE_EXISTING_PROFILE')
            if not args.env_file or not args.repo or not args.node:raise AccountError('SETUP_ARGUMENTS_REQUIRED')
            import uuid
            values=environment(args.env_file)
            project=str(uuid.UUID(args.project_id or values.get('SUNO_STUDIO_PROJECT_ID','')))
            if not 1024<=args.port<=65535:raise AccountError('INVALID_PORT')
            for other in ROOT.glob('*/profile.json'):
                if json.loads(read_private(other)).get('port')==args.port:raise AccountError('ACCOUNT_PORT_ALREADY_ASSIGNED')
            save(path,dict(port=args.port,token=secrets.token_urlsafe(40),envFile=str(args.env_file.resolve()),projectId=project,
                           repo=str(args.repo.resolve()),node=str(args.node.resolve())))
            print(json.dumps({'success':True,'account':args.account,'port':args.port,'profile':str(path)}));return
        if args.action=='stop':
            p=profile();identity(p) # Verify our token/account before touching a PID.
            proc=json.loads(read_private(path.parent/'process.json'))
            command=subprocess.check_output(['/bin/ps','-p',str(proc['pid']),'-o','command='],text=True)
            if str(Path(p['repo'])/'node_modules/next/dist/bin/next') not in command and 'next-server' not in command:
                raise AccountError('PROCESS_IDENTITY_MISMATCH')
            os.kill(proc['pid'],signal.SIGTERM)
            print(json.dumps({'success':True,'status':'stop_requested','account':args.account}));return
        if args.action=='start':p,data=ensure()
        else:p=profile();data,_=identity(p)
        print(json.dumps({'success':True,'status':'account_server_ready','account':args.account,'port':p['port'],
                          'accountFingerprint':hashlib.sha256(data['accountId'].encode()).hexdigest()[:24],'paidRequests':0}))
    except Exception as e:
        print(json.dumps({'success':False,'code':str(e) if isinstance(e,AccountError) else type(e).__name__.upper()}));raise SystemExit(1)
if __name__=='__main__':main()
