"""Account-portable copy of the concurrent HolySong pipeline. No HAR required."""
from __future__ import annotations
import argparse, contextlib, hashlib, http.cookies, json, os, stat, sys, uuid
from pathlib import Path
from core import Blocked, private, read, require, sha, lock, atomic, utc, identity
import generate_publish_concurrent as pipeline
from repo_client import Client
from account_server import profile,AccountError

ROOT = Path(__file__).resolve().parent

def local_path(value):
    return str(Path(str(value).replace('{home}',str(Path.home()))).expanduser().absolute())

def settings(config_path):
    base=read(private(config_path))
    local=profile()
    project=local['projectId']
    maximum=os.environ.get('HOLYSONG_MAX_CONCURRENT','2')
    require(maximum.isdigit() and 1<=int(maximum)<=8,'INVALID_CONCURRENCY_LIMIT')
    # Only machine-local publishing/artwork paths are inherited. Legacy account/HAR/runner settings are ignored.
    fields=['workflowConfig','artworkStateDirectory','fastPublisherConfig','uploader','uploaderConfig']
    config={key:local_path(base[key]) for key in fields}
    state_root=private(Path(os.environ.get('HOLYSONG_AUTO_STATE_DIRECTORY',str(Path.home()/'.local/share/holysong-generate-publish-concurrent-generic-auto'))),True)
    config.update(modelPython=sys.executable,studioRunner=str(ROOT/'studio_flow_generic.py'),
        studioRunnerSha256=sha(ROOT/'studio_flow_generic.py'),studioTemplateSha256=sha(ROOT/'studio_templates.py'),
        studioProjectId=project,fastPublisher=str(Path.home()/'.local/bin/holysong-suno-publish'),
        stateRoot=str(state_root),maximumConcurrent=int(maximum),allowFailedStudioRestart=False)
    config['allowFailedStudioRestart']=False
    config['coordinationRoot']=os.environ.get('HOLYSONG_STATE_DIRECTORY',str(Path.home()/'.local/share/holysong-generate-publish-concurrent-generic'))
    return config

def account(config):
    client=Client(Path(config['stateRoot']))
    user=client.auth()
    project=client.call('studio/project/'+config['studioProjectId'])
    require(project.get('owner_user_id')==user,'PROJECT_OWNER_MISMATCH')
    fingerprint=hashlib.sha256(user.encode()).hexdigest()[:24]
    config['coordinationDirectory']=str(private(Path(config['coordinationRoot'])/('account-'+fingerprint),True))
    config.update(accountFingerprint=fingerprint,stateDirectory=str(private(Path(config['stateRoot'])/('account-'+fingerprint),True)))
    os.environ['SUNO_EXPECTED_ACCOUNT']=user
    return config

@contextlib.contextmanager
def lane(config):
    with contextlib.ExitStack() as stack:
        for n in range(config['maximumConcurrent']):
            try:
                stack.enter_context(lock(Path(config['coordinationDirectory'])/('account-slot-'+str(n)+'.lock'),nonblocking=True))
                break
            except Blocked as e:
                if e.code!='JOB_ALREADY_RUNNING': raise
        else: raise Blocked('ACCOUNT_CONCURRENCY_LIMIT',pending=True)
        yield

@contextlib.contextmanager
def chapter_lane(config, book, chapter, genre):
    base=read(config['workflowConfig'])
    require(genre in base['recipes'],'GENRE_RECIPE_NOT_CONFIGURED')
    request=identity(book,chapter,genre,read(base['recipes'][genre]),'production',read(base['bookIndex'])['books'])
    root=private(Path(config['coordinationRoot'])/'chapter-locks',True)
    with lock(root/(request['book']+'-'+str(chapter)+'-'+request['genre']+'.lock'),nonblocking=True):
        yield

def main(argv=None):
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',default=os.environ.get('HOLYSONG_SUNO_ACCOUNT','main'))
    parser.add_argument('--env-file',type=Path,help='Compatibility option; credentials are managed by holysong-suno-account setup')
    parser.add_argument('--config',type=Path,help='Existing local artwork/publishing configuration; legacy Suno fields ignored')
    parser.add_argument('--book');parser.add_argument('--chapter',type=int);parser.add_argument('--genre',default='upbeat')
    parser.add_argument('--wait-seconds',type=int,default=600)
    parser.add_argument('--check',action='store_true',help='Read-only account/project/dependency preflight; no song/artwork/export/upload requests')
    parser.add_argument('--offline',action='store_true',help='With --check, validate local configuration without contacting Suno')
    args=parser.parse_args(argv)
    retry_allowed = False
    try:
        require(not args.offline or args.check,'OFFLINE_REQUIRES_CHECK')
        require(args.check or bool(args.book and args.chapter),'BOOK_AND_CHAPTER_REQUIRED')
        require(0<=args.wait_seconds<=3600,'INVALID_WAIT_LIMIT')
        os.environ['HOLYSONG_SUNO_ACCOUNT']=args.account
        # Worker uses only a local token; Suno cookie belongs to the account server.
        os.environ.pop('SUNO_COOKIE',None)
        path=args.config or Path(os.environ.get('HOLYSONG_CONFIG',str(Path.home()/'.config/holysong/generate-publish/config.json')))
        config=settings(path)
        retry_allowed = config['allowFailedStudioRestart']
        # Check local dependencies first, before a network request or chapter job.
        base=read(config['workflowConfig']);pipeline.check_dependencies(config,base)
        if args.offline:
            print(json.dumps({'success':True,'status':'offline_config_valid','accountVerified':False,
                'transport':'local_suno_api_repo','harRequired':False,'maximumConcurrent':config['maximumConcurrent']}));return 0
        config=account(config)
        if args.check:
            print(json.dumps({'success':True,'status':'account_and_project_verified','transport':'local_suno_api_repo','harRequired':False,
                'accountFingerprint':config['accountFingerprint'],'maximumConcurrent':config['maximumConcurrent'],
                'paidRequests':0,'stateDirectory':config['stateDirectory']}));return 0
        with chapter_lane(config,args.book,args.chapter,args.genre), lane(config):
            result=pipeline.run(config,args.book,args.chapter,args.genre,args.wait_seconds)
        print(json.dumps(result),flush=True);return 0
    except Exception as e:
        # Never include raw cookie/token/network exception strings in stdout or logs.
        code=e.code if isinstance(e,Blocked) else str(e) if isinstance(e,AccountError) else str(e) if isinstance(e,RuntimeError) and str(e).startswith(('HTTP_','ACCOUNT_','SUNO_')) else type(e).__name__.upper()
        print(json.dumps({'success':False,'status':'stopped','code':code,
            'recovery':('Resolve the error, then rerun the same command or batch -c. Failed pre-download attempts may regenerate and use credits; old evidence is archived.' if retry_allowed else 'Resolve configuration/access or inspect the private job evidence. Resume the same account/job; do not delete paid intents or automatically regenerate.')}),flush=True)
        return 1

if __name__=='__main__':raise SystemExit(main())
