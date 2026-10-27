"""Local repo adapter. No Suno credentials or direct Suno API transport."""
from pathlib import Path
import hashlib,json,os,time,urllib.parse
from account_server import ensure,request,AccountError

class Client:
    def __init__(self,out):
        self.out=Path(out);self.profile,_=ensure();self.refreshed=0;self.token='local-only'
    def auth(self):
        data=request(self.profile,'/api/studio/account');user=data['accountId']
        expected=os.environ.get('SUNO_EXPECTED_ACCOUNT')
        fp=hashlib.sha256(user.encode()).hexdigest()[:24]
        if fp!=self.profile['accountFingerprint'] or (expected and expected!=user):raise RuntimeError('ACCOUNT_CHANGED_DURING_JOB')
        self.refreshed=time.monotonic();return user
    def raw(self,url,data=None,headers=None):
        # Existing publisher expects raw exported-word alignment. Only this exact route is allowed.
        import re
        match=re.fullmatch(r'https://studio-api\.prod\.suno\.com/api/gen/([a-f0-9-]{36})/aligned_lyrics/v2/',url)
        if not match or data is not None:raise RuntimeError('DIRECT_SUNO_CALL_REFUSED')
        return request(self.profile,'/api/studio/alignment?id='+match[1])
    def call(self,path,data=None,label=None):
        if time.monotonic()-self.refreshed>35:self.auth()
        target=None;body=None
        if path.startswith('studio/project/'):
            target='/api/studio/project?id='+path.split('/')[-1]
        elif path=='billing/info/':target='/api/studio/billing'
        elif path=='c/check':target='/api/studio/check'
        elif path=='feed/v3':target='/api/studio/status?ids='+','.join(data['filters']['ids']['clipIds'])
        elif path.startswith('download/clip/'):
            target='/api/studio/download?id='+path.split('/')[2].split('?')[0]
        elif path=='generate/v2-web/':
            target='/api/studio/generate';body=dict(project_id=data['metadata']['from_studio_project_id'],
                prompt=data['attached_lyrics'],tags=data['attached_styles'],model=data['mv'],title=json.loads((self.out/'identity.json').read_text())['title'])
        elif path=='studio/render-state':
            target='/api/studio/export';clip=data['state']['tracks'][0]['clips'][0]
            body=dict(project_id=data['studio_project_id'],clip_id=clip['clipId'],duration_seconds=data['end_beats']/2,
                      title=data['title'],lyrics=data['lyrics'],tags=data['tags'])
            # Clean export schema now lives in the repo API, not the client.
        else:raise RuntimeError('UNSUPPORTED_REPO_OPERATION')
        if label:
            from studio_flow_generic import write,now
            response=self.out/(label+'-response.json');intent=self.out/(label+'-intent.json')
            binding=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if intent.exists():
                saved=json.loads(intent.read_text())
                if saved.get('requestSha256')!=binding:raise RuntimeError('SAVED_SUBMISSION_INPUT_CHANGED')
                if response.exists():return json.loads(response.read_text())
                raise RuntimeError('SUBMISSION_UNKNOWN_RECONCILE_WITHOUT_REGENERATING')
            write(intent,dict(at=now(),path=target,state='started',requestSha256=binding))
        result=request(self.profile,target,body)
        if label:write(response,result)
        return result
