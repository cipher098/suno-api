import contextlib,hashlib,http.server,importlib,json,os,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import account_server,repo_client
from studio_flow_generic import write

ID='22222222-2222-4222-8222-222222222222'
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.calls=[]
        self.p={'port':3057,'token':'x'*40,'accountFingerprint':hashlib.sha256(b'owner').hexdigest()[:24]}
        self.ensure=patch.object(repo_client,'ensure',return_value=(self.p,{'accountId':'owner'}));self.ensure.start()
        def transport(p,path,body=None):
            self.calls.append((path,body))
            if path.endswith('/account'):return {'accountId':'owner'}
            return {'clips':[{'id':ID}], 'id':ID}
        self.transport=patch.object(repo_client,'request',side_effect=transport);self.transport.start()
        write(self.root/'identity.json',{'title':'Genesis 2'})
    def tearDown(self):self.ensure.stop();self.transport.stop();self.tmp.cleanup()
    def generation(self):return {'metadata':{'from_studio_project_id':ID},'attached_lyrics':'Exact scripture','attached_styles':'upbeat','mv':'chirp-hawk'}
    def test_generation_calls_repo_and_reuses_saved_response_without_resubmitting(self):
        c=repo_client.Client(self.root);a=c.call('generate/v2-web/',self.generation(),'generation')
        b=c.call('generate/v2-web/',self.generation(),'generation');self.assertEqual(a,b)
        self.assertEqual(len([p for p,b in self.calls if p.endswith('/generate')]),1)
        sent=[b for p,b in self.calls if p.endswith('/generate')][0]
        self.assertEqual(sent['prompt'],'Exact scripture');self.assertEqual(sent['tags'],'upbeat');self.assertEqual(sent['title'],'Genesis 2')
    def test_unknown_submission_is_not_retried(self):
        def failed(p,path,body=None):
            if path.endswith('/account'):return {'accountId':'owner'}
            raise account_server.AccountError('LOCAL_API_UNAVAILABLE')
        with patch.object(repo_client,'request',side_effect=failed) as transport:
            c=repo_client.Client(self.root)
            with self.assertRaises(account_server.AccountError):c.call('generate/v2-web/',self.generation(),'generation')
            with self.assertRaisesRegex(RuntimeError,'SUBMISSION_UNKNOWN'):c.call('generate/v2-web/',self.generation(),'generation')
            self.assertEqual(transport.call_count,2) # One identity GET and one paid POST; no repeat.
    def test_changed_input_cannot_replay_saved_generation(self):
        c=repo_client.Client(self.root);c.call('generate/v2-web/',self.generation(),'generation')
        g=self.generation();g['attached_lyrics']='Changed'
        with self.assertRaisesRegex(RuntimeError,'INPUT_CHANGED'):c.call('generate/v2-web/',g,'generation')
    def test_export_schema_is_owned_by_server_and_response_reused(self):
        c=repo_client.Client(self.root);body={'studio_project_id':ID,'state':{'tracks':[{'clips':[{'clipId':ID}]}]},'end_beats':400,'title':'Title','lyrics':'Words','tags':'pop'}
        c.call('studio/render-state',body,'render');c.call('studio/render-state',body,'render')
        calls=[b for p,b in self.calls if p.endswith('/export')];self.assertEqual(len(calls),1)
        self.assertNotIn('state',calls[0]);self.assertEqual(calls[0]['duration_seconds'],200)
    def test_alignment_bound_to_exported_id_and_unrecognized_direct_api_refused(self):
        c=repo_client.Client(self.root);c.raw(f'https://studio-api.prod.suno.com/api/gen/{ID}/aligned_lyrics/v2/')
        self.assertEqual(self.calls[-1][0],'/api/studio/alignment?id='+ID)
        with self.assertRaisesRegex(RuntimeError,'DIRECT_SUNO_CALL_REFUSED'):c.raw('https://suno.com/anything')
    def test_account_change_refused(self):
        with patch.object(repo_client,'request',return_value={'accountId':'other'}):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_CHANGED'):repo_client.Client(self.root).auth()
    def test_pending_poll_and_download_never_submit_generation(self):
        c=repo_client.Client(self.root);c.call('feed/v3',{'filters':{'ids':{'clipIds':[ID]}}});c.call('download/clip/'+ID+'?format=m4a')
        self.assertTrue(all(not p.endswith(('/generate','/export')) for p,b in self.calls))

class RunnerRecoveryTests(unittest.TestCase):
    def test_full_runner_resumes_same_generation_then_exports_downloads_and_aligns(self):
        import studio_flow_generic as runner
        import io,sys
        render='33333333-3333-4333-8333-333333333333'
        calls=[];interrupted=[False]
        profile={'port':3057,'token':'x'*40,'accountFingerprint':hashlib.sha256(b'owner').hexdigest()[:24]}
        def service(p,path,body=None):
            calls.append(path)
            if path.endswith('/account'):return {'accountId':'owner'}
            if '/project?' in path:return {'owner_user_id':'owner'}
            if path.endswith('/billing'):return {'total_credits_left':100,'download_usage':{'current_period_downloads_used':0}}
            if path.endswith('/check'):return {'required':False}
            if path.endswith('/generate'):return {'clips':[{'id':ID}]}
            if '/status?' in path:
                if not interrupted[0]:interrupted[0]=True;raise account_server.AccountError('LOCAL_API_UNAVAILABLE')
                selected=render if render in path else ID
                return {'clips':[{'id':selected,'status':'complete','metadata':{'duration':20}}]}
            if path.endswith('/export'):return {'id':render,'status':'complete'}
            if '/download?' in path:return {'status':'ready','download_url':f'https://suno-data-uploads.s3.amazonaws.com/studio/uploads/{render}_lyrics.m4a'}
            if '/alignment?' in path:return {'aligned_words':[{'word':'Words','start_s':0,'end_s':1}]}
            raise AssertionError(path)
        class Audio(io.BytesIO):
            status=200
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);request_file=root/'request.json';write(request_file,{'prompt':'Words','tags':'upbeat'})
            argv=['runner','--out',str(root/'studio'),'--execute-one','--request-json',str(request_file),'--project-id',ID]
            with patch.object(repo_client,'ensure',return_value=(profile,{})),patch.object(repo_client,'request',side_effect=service),\
                 patch.object(runner.urllib.request,'urlopen',return_value=Audio(b'audio')),\
                 patch.object(runner,'validate_audio',return_value={'technicalPass':True}),\
                 patch.object(runner,'post_download') as publish,patch.object(sys,'argv',argv),patch.dict(os.environ,{'SUNO_EXPECTED_ACCOUNT':'owner'}):
                with self.assertRaises(account_server.AccountError):runner.main()
                runner.main()
                self.assertEqual(calls.count('/api/studio/generate'),1)
                self.assertEqual(calls.count('/api/studio/export'),1)
                self.assertIn('/api/studio/alignment?id='+render,calls)
                self.assertEqual(json.loads((root/'studio/result.json').read_text())['renderId'],render)
                self.assertTrue((root/'studio/export-aligned-lyrics.json').exists())
                publish.assert_called_once()

class LocalServerTests(unittest.TestCase):
    def test_two_profiles_send_distinct_local_tokens_and_no_suno_cookies(self):
        received=[];servers=[];threads=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers));self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
            def log_message(self,*args):pass
        try:
            for token in ['a'*40,'b'*40]:
                s=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);servers.append(s)
                t=threading.Thread(target=s.serve_forever,daemon=True);t.start();threads.append(t)
                self.assertEqual(account_server.request({'port':s.server_port,'token':token},'/api/studio/account'),{'ok':True})
            self.assertEqual([r['Authorization'] for r in received],['Bearer '+'a'*40,'Bearer '+'b'*40])
            self.assertTrue(all('Cookie' not in r for r in received))
        finally:
            for s in servers:s.shutdown();s.server_close()
    def test_profile_name_cannot_escape_private_root(self):
        for name in ['../other','a/b','', 'a'*41]:
            if not name:continue
            with self.assertRaises(account_server.AccountError):account_server.profile_path(name)
    def test_literal_cookie_parser_does_not_execute_shell(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.env';p.write_text('SUNO_COOKIE="__client=$HOME; other=`echo test`"\n');p.chmod(0o600)
            self.assertEqual(account_server.environment(p)['SUNO_COOKIE'],'__client=$HOME; other=`echo test`')
            p.chmod(0o644)
            with self.assertRaises(account_server.AccountError):account_server.environment(p)
    def test_auto_batch_routes_only_to_auto_worker(self):
        text=Path(__file__).with_name('generate_publish_batch.py').read_text()
        self.assertIn(".local/bin/holysong-generate-publish-concurrent-generic-auto",text)
        self.assertNotIn(".local/bin/holysong-generate-publish-concurrent-generic'",text)

class BrowserConfigTests(unittest.TestCase):
    def test_allowlisted_per_account_settings_forwarded_without_security_overrides(self):
        env=account_server.server_environment({'SUNO_COOKIE':'__client=fake','TWOCAPTCHA_KEY':'test-key',
             'BROWSER':'firefox','BROWSER_HEADLESS':'false','SUNO_STUDIO_BROWSER_FALLBACK':'true',
             'SUNO_API_LOCAL_TOKEN':'attacker','SUNO_API_ACCOUNT_MODE':'0','PATH':'changed'}, {'token':'local-token'})
        self.assertEqual(env['TWOCAPTCHA_KEY'],'test-key');self.assertEqual(env['BROWSER'],'firefox')
        self.assertEqual(env['SUNO_STUDIO_BROWSER_FALLBACK'],'true');self.assertEqual(env['SUNO_API_ACCOUNT_MODE'],'1')
        self.assertEqual(env['SUNO_API_LOCAL_TOKEN'],'local-token');self.assertNotEqual(env.get('PATH'),'changed')
    def test_worker_defers_captcha_policy_to_repo(self):
        text=Path(__file__).with_name('studio_flow_generic.py').read_text()
        self.assertNotIn("check=({'required':False}",text)
        self.assertIn('Generation auth belongs to the repo',text)

if __name__=='__main__':unittest.main()
