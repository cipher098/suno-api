"""Explicit provider/tool adapters. Never invoke an LLM agent or a shell."""
from __future__ import annotations
import base64, difflib, importlib.util, json, math, os, re, shutil, struct, subprocess, tempfile, time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from core import Blocked, require, read, sha, digest, canonical, atomic, utc, private, lock, EDITION

TOKEN=re.compile(r"[A-Za-z0-9]+(?:[’'][A-Za-z0-9]+)*")
STORY=dict(allowed=True,offlineAllowed=True,clipDurationMs=15000,templateId='holysong-captioned-story-v1')
def read_pinned(path,expected):
    raw=Path(path).read_bytes();require(digest(raw)==expected,'INPUT_CHANGED');return json.loads(raw)

def copy_verified(source,destination,expected):
    require(sha(source)==expected,'INPUT_CHANGED')
    shutil.copyfile(source,destination)
    require(sha(destination)==expected,'INPUT_CHANGED_DURING_COPY')

def tokens(text): return [m.group().lower().replace('’',"'") for m in TOKEN.finditer(text)]
def u16(text): return len(text.encode('utf-16-le'))//2

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def request_json(url, *, body=None, headers=None, maximum=8*1024*1024, timeout=45):
    req=Request(url, data=canonical(body) if body is not None else None,
                headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with build_opener(NoRedirect()).open(req,timeout=timeout) as r:
            raw=r.read(maximum+1); require(len(raw)<=maximum,'PROVIDER_RESPONSE_TOO_LARGE'); return json.loads(raw)
    except HTTPError as e:
        # No URL/body/header exception strings: provider errors may echo credentials.
        raise Blocked('HTTP_'+str(e.code),pending=e.code in (408,425,429,500,502,503,504)) from None
    except (URLError, TimeoutError, ConnectionError): raise Blocked('NETWORK_UNAVAILABLE',pending=True) from None
    except ValueError: raise Blocked('PROVIDER_RESPONSE_INVALID') from None

def command(argv, *, timeout=300, env=None):
    try: r=subprocess.run([str(x) for x in argv],capture_output=True,timeout=timeout,env=env)
    except subprocess.TimeoutExpired: raise Blocked('TOOL_TIMEOUT',pending=True) from None
    except OSError: raise Blocked('TOOL_UNAVAILABLE') from None
    require(r.returncode==0,'TOOL_FAILED')
    return r.stdout

def check_withdrawals(head, recording, bundle):
    for w in head.get('withdrawals',[]):
        require(isinstance(w,dict) and type(w.get('disableAudio')) is bool and type(w.get('disableExport')) is bool,'WITHDRAWAL_INVALID')
        if w.get('recordingId')==recording and w.get('bundleRevision') in (None,bundle):
            require(not w['disableAudio'] and not w['disableExport'],'RECORDING_WITHDRAWN')

def source_valid(source, request):
    require(source.get('bookId')==request['book'] and source.get('chapterNumber')==request['chapter'] and source.get('editionId')==EDITION,'SOURCE_IDENTITY_MISMATCH')
    verses=source.get('verses',[])
    numbers=[v.get('verse') for v in verses]
    require(1<=len(verses)<=200 and all(type(n) is int and 1<=n<=200 for n in numbers) and numbers==sorted(set(numbers)),'SOURCE_VERSE_GAP')
    gaps=sorted(set(range(1,max(numbers)+1))-set(numbers))
    declared=source.get('omittedVerseNumbers',[])
    require(set(gaps).issubset(declared) and all(n not in numbers for n in declared),'SOURCE_VERSE_GAP')
    require(all(isinstance(v.get('text'),str) and tokens(v['text']) for v in verses),'SOURCE_EMPTY_VERSE')
    require(source.get('sourceURL',source.get('url','')).startswith('https://ebible.org/engwebu/'),'SOURCE_EDITION_NOT_TRUSTED')
    return source

def synchronization(source, alignment, duration_ms):
    """Exact token correspondence and observed intervals; never interpolate."""
    expected=[t for v in source['verses'] for t in tokens(v['text'])]
    require(isinstance(alignment,list) and len(alignment)<=20000,'ALIGNMENT_INVALID')
    observed=[]; rows=[]; last=0
    for row in alignment:
        words=tokens(row.get('word',''))
        if not words: continue
        require(len(words)==1,'ALIGNMENT_COARSE_WORD_BOUNDARY')
        a,b=row.get('start_s'),row.get('end_s')
        require(type(a) in (int,float) and type(b) in (int,float) and math.isfinite(a) and math.isfinite(b),'ALIGNMENT_NONFINITE')
        start,end=round(a*1000),round(b*1000)
        require(last<=start<end<=duration_ms,'ALIGNMENT_UNSUPPORTED_INTERVAL')
        require(row.get('success') is True,'ALIGNMENT_UNSUCCESSFUL_WORD')
        observed+=words; rows.append(dict(startMs=start,endMs=end)); last=end
    require(observed==expected,'ALIGNMENT_SCRIPTURE_MISMATCH')
    return rows

def lyric_documents(source, rows, recording, media, performed_revision, canonical_revision, duration, audio_sha):
    """Readable phrase occurrences with original punctuation and UTF16 offsets."""
    verses=[]; occurrences=[]; cues=[]; position=0
    for v in source['verses']:
        text=v['text']; vid=source['bookId']+'.'+str(source['chapterNumber'])+'.'+str(v['verse']); verses.append(dict(verseId=vid,verseNumber=v['verse'],text=text))
        matches=list(TOKEN.finditer(text)); groups=[]; current=[]
        for m in matches:
            if current and u16(text[current[0].start():m.end()])>80:
                groups.append(current); current=[]
            current.append(m)
        if current: groups.append(current)
        if len(groups[-1])==1 and len(groups)>1:
            require(len(groups[-2])>=3,'ISOLATED_WORD_LYRIC_LINE')
            groups[-1].insert(0,groups[-2].pop())
        require(all(len(g)>=2 for g in groups),'ISOLATED_WORD_LYRIC_LINE')
        for gi,group in enumerate(groups):
            begin=0 if gi==0 else group[0].start()
            finish=groups[gi+1][0].start() if gi+1<len(groups) else len(text)
            phrase=text[begin:finish].rstrip(); oid=vid+'-p'+str(gi+1)
            require(u16(phrase)<=84,'PHRASE_TOO_LONG')
            occurrences.append(dict(occurrenceId=oid,text=phrase))
            for m in group:
                timing=rows[position]; position+=1
                cues.append(dict(cueId='cue-'+str(position),occurrenceId=oid,**timing,
                    performedRange=dict(start=u16(text[begin:m.start()]),end=u16(text[begin:m.end()])),
                    canonicalRanges=[dict(verseId=vid,start=u16(text[:m.start()]),end=u16(text[:m.end()]))]))
    require(position==len(rows),'ALIGNMENT_COVERAGE_MISMATCH')
    common=dict(schemaVersion=1,recordingId=recording,mediaRevision=media,performedTextRevision=performed_revision)
    return (dict(schemaVersion=1,bookId=source['bookId'],chapterNumber=source['chapterNumber'],editionId=EDITION,
                 canonicalTextRevision=canonical_revision,verses=verses),
            dict(**common,occurrences=occurrences),
            dict(**common,canonicalTextRevision=canonical_revision,durationMs=duration,rangeEncoding='utf16',audioSha256=audio_sha,cues=cues))

class Adapters:
    def __init__(self,config):
        self.c=config;self.env=os.environ.copy()
        model_env=config.get('modelEnvironment',{})
        require(set(model_env)<={'BIBLE_SONG_HOME','BIBLE_SONG_SKILL','BIBLE_SONG_CATALOG','HF_HUB_OFFLINE'} and all(isinstance(v,str) for v in model_env.values()),'MODEL_ENVIRONMENT_INVALID')
        self.env.update(model_env)
    def _tool(self,argv,*,timeout=300):return command(argv,timeout=timeout,env=self.env)
    def execute(self,stage,request,root,context): return getattr(self,stage)(request,root,context)
    def existing(self,r,root,ctx):
        if r.get('importManifest'): return dict(found=False,offlineReplay=True),[]
        _,head=self._fetch(r,'/v1/catalog/head')
        require(re.fullmatch('[A-Za-z0-9_.-]+',head['catalogRevision']) is not None,'CATALOG_REVISION_INVALID')
        raw,catalog=self._fetch(r,'/v1/catalog/revisions/'+head['catalogRevision']); require(digest(raw)==head['sha256'],'CATALOG_HASH_MISMATCH')
        matches=[x for x in catalog['recordings'] if x.get('bookId')==r['book'] and x.get('chapterNumber')==r['chapter'] and x.get('genreId')==r['genre'] and x.get('editionId')==r['edition']]
        if not matches:return dict(found=False),[]
        require(len(matches)==1,'MULTIPLE_CURRENT_RECORDINGS')
        row=matches[0]
        for key in ('recordingId','bundleRevision'):require(re.fullmatch('[A-Za-z0-9_.-]+',row[key]) is not None,'CATALOG_ID_INVALID')
        raw,manifest=self._fetch(r,'/v1/recordings/'+row['recordingId']+'/bundles/'+row['bundleRevision'])
        require(digest(raw)==row['manifestSha256'],'MANIFEST_HASH_MISMATCH')
        for key,value in [('bookId',r['book']),('chapterNumber',r['chapter']),('editionId',r['edition']),('genreId',r['genre']),('recordingId',row['recordingId']),('bundleRevision',row['bundleRevision'])]:require(manifest[key]==value,'EXISTING_IDENTITY_MISMATCH')
        require(all(manifest['exportPolicy'].get(k)==v for k,v in STORY.items()),'EXISTING_STORY_POLICY_MISMATCH')
        check_withdrawals(head,row['recordingId'],row['bundleRevision'])
        _,after=self._fetch(r,'/v1/catalog/head');require(after==head,'CATALOG_CHANGED_DURING_CHECK')
        result=dict(found=True,verified=True,alreadyLive=True,recordingId=row['recordingId'],bundleRevision=row['bundleRevision'],checkedAt=utc())
        atomic(root/'existing-live-verification.json',result);return result,[root/'existing-live-verification.json']
    def source(self,r,root,ctx):
        registry=read(Path(self.c['sourceDirectory'])/'index.json'); key=r['book']+':'+str(r['chapter'])
        require(key in registry,'SOURCE_NOT_CACHED')
        pin=registry[key]; path=Path(self.c['sourceDirectory'])/pin['file']
        require(path.resolve().is_relative_to(Path(self.c['sourceDirectory']).resolve()),'SOURCE_HASH_CHANGED')
        trusted=source_valid(read_pinned(path,pin['sha256']),r)
        require(len(trusted['verses'])==pin['verseCount'],'SOURCE_VERSE_COUNT_MISMATCH')
        require(pin['sha256']==r.get('sourceSha256',pin['sha256']),'SOURCE_PIN_CHANGED')
        expected=pin['sha256']
        if r.get('importManifest'):
            m=read_pinned(r['importManifest'],r['importSha256'])
            imported=source_valid(read_pinned(m['source'],m['sourceSha256']),r)
            require(imported['verses']==trusted['verses'],'IMPORT_CANONICAL_MISMATCH')
            path=Path(m['source']); expected=m['sourceSha256']
        file=root/'source-verses.json'; copy_verified(path,file,expected)
        source=source_valid(read_pinned(file,expected),r)
        require(source['verses']==trusted['verses'],'SOURCE_SNAPSHOT_MISMATCH')
        lyrics='\n'.join(v['text'] for v in source['verses'])+'\n'; (root/'lyrics.txt').write_text(lyrics)
        return dict(sourceSha256=expected,verseCount=len(source['verses'])),[file,root/'lyrics.txt']
    def _provider(self,path,body=None):
        base=self.c.get('sunoBaseUrl','http://127.0.0.1:3047').rstrip('/'); u=urlsplit(base)
        require(u.scheme=='http' and u.hostname in ('localhost','127.0.0.1') and not u.username and not u.path,'SUNO_MUST_BE_LOOPBACK')
        env=private(self.c['sunoEnvFile']).read_text(); pairs=dict(l.split('=',1) for l in env.splitlines() if '=' in l and not l.startswith('#'))
        cookie=pairs.get('SUNO_COOKIE','').strip().strip('"\''); require(cookie and '\n' not in cookie,'SUNO_AUTH_REQUIRED')
        require(pairs.get('SUNO_NO_BROWSER','').strip().strip('"\'').lower()=='true','SUNO_BROWSER_GUARD_REQUIRED')
        return request_json(base+path,body=body,headers={'Cookie':cookie})
    def generation(self,r,root,ctx):
        if r.get('importManifest'):
            m=read_pinned(r['importManifest'],r['importSha256'])
            return dict(candidates=m['candidates'],imported=True),[]
        require(self.c.get('allowGeneration') is True,'GENERATION_DISABLED')
        # Lock the account for intent reservation and submission, not its render wait.
        with lock(Path(self.c['stateDirectory'])/'locks/suno-account.lock'):
            intent=root/'generation-intent.json'; response=root/'generation-response.json'
            if not response.exists():
                require(not intent.exists(),'GENERATION_OUTCOME_UNKNOWN')
                quota=self._provider('/api/get_limit'); credits=quota.get('credits_left')
                require(type(credits) in (int,float) and credits>=self.c.get('minimumSunoCredits',10),'SUNO_QUOTA_UNAVAILABLE')
                recipe=read(self.c['recipes'][r['genre']]); require(digest(canonical(recipe))==r['recipeSha256'],'RECIPE_CHANGED')
                body=dict(prompt=(root/'lyrics.txt').read_text(),tags=recipe['style'],title=r['label']+' '+str(r['chapter'])+' · '+r['genre'],make_instrumental=False,model='chirp-hawk',wait_audio=False)
                # Never truncate a chapter to fit a provider prompt.
                require(len(body['prompt'])<=self.c.get('maxLyricsCharacters',5000),'CHAPTER_EXCEEDS_PROVIDER_LIMIT')
                require(len(body['tags'])<=self.c.get('maxStyleCharacters',900),'STYLE_EXCEEDS_CONFIGURED_LIMIT')
                atomic(intent,dict(createdAt=utc(),requestSha256=digest(canonical(body)),request=body,creditsBefore=credits,maxRequests=1))
                data=self._provider('/api/custom_generate',body); atomic(response,data)
            data=read(response); require(isinstance(data,list) and 1<=len(data)<=2,'GENERATION_RESPONSE_INVALID')
            ids=[x['id'] for x in data]; require(len(set(ids))==len(ids) and all(re.fullmatch('[a-f0-9-]{36}',s) for s in ids),'GENERATION_IDS_INVALID')
        status=self._provider('/api/get?'+urlencode({'ids':','.join(ids)}))
        require(isinstance(status,list) and {x.get('id') for x in status}==set(ids),'GENERATION_STATUS_IDENTITY_MISMATCH')
        if any(x.get('status') not in ('complete','error') for x in status): raise Blocked('GENERATION_RENDERING',pending=True)
        ready=[dict(id='take'+str(i+1),songId=x['id'],audioUrl=x.get('audio_url'),duration=x.get('duration')) for i,x in enumerate(status) if x.get('status')=='complete']
        require(ready,'GENERATION_FAILED'); return dict(candidates=ready,imported=False),[response,intent]
    def audio(self,r,root,ctx):
        candidates=[]; files=[]; audio_dir=root/'originals'; audio_dir.mkdir(exist_ok=True)
        for c in ctx['generation']['candidates']:
            ident=c['id']; require(re.fullmatch('[A-Za-z0-9_-]{1,60}',ident),'CANDIDATE_ID_INVALID')
            dest=audio_dir/(ident+'.m4a'); aligned=root/(ident+'-aligned.json')
            if ctx['generation']['imported']:
                original=Path(c['audio']); require(sha(original)==c['audioSha256'],'IMPORT_AUDIO_CHANGED')
                copy_verified(original,dest,c['audioSha256'])
                require(sha(c['alignment'])==c['alignmentSha256'],'IMPORT_ALIGNMENT_CHANGED'); copy_verified(c['alignment'],aligned,c['alignmentSha256'])
            else:
                url=c.get('audioUrl') or ''; u=urlsplit(url)
                require(u.scheme=='https' and not u.username and u.port in (None,443) and u.hostname in ('cdn1.suno.ai','cdn2.suno.ai','cdn1.suno.com','cdn2.suno.com'),'AUDIO_DOWNLOAD_UNAVAILABLE')
                if not dest.exists():
                    tmp=dest.with_suffix('.partial')
                    try:
                        with build_opener(NoRedirect()).open(Request(url),timeout=60) as response, tmp.open('wb') as output:
                            total=0
                            while chunk:=response.read(1024*1024):
                                total+=len(chunk); require(total<=100*1024*1024,'AUDIO_TOO_LARGE'); output.write(chunk)
                            output.flush(); os.fsync(output.fileno())
                        tmp.rename(dest)
                    except (HTTPError,URLError): raise Blocked('AUDIO_DOWNLOAD_UNAVAILABLE') from None
                    finally: tmp.unlink(missing_ok=True)
                if not aligned.exists(): atomic(aligned,self._provider('/api/get_aligned_lyrics?'+urlencode({'song_id':c['songId']})))
            probe=json.loads(self._tool([self.c['ffprobe'],'-v','error','-show_format','-show_streams','-of','json',dest],timeout=45))
            duration=float(probe['format']['duration']); require(math.isfinite(duration) and duration>1,'AUDIO_DURATION_INVALID')
            self._tool([self.c['ffmpeg'],'-v','error','-xerror','-i',dest,'-map','0:a:0','-f','null','-'],timeout=180)
            candidates.append({**c,'audio':str(dest),'audioSha256':sha(dest),'alignment':str(aligned),'durationMs':round(duration*1000)})
            files.extend([dest,aligned])
        return dict(candidates=candidates),files
    def evaluation(self,r,root,ctx):
        candidates=ctx['audio']['candidates']; files=[]; rows=[]
        for c in candidates:
            transcript=root/(c['id']+'-transcript.json'); audit=root/(c['id']+'-audit.json')
            if ctx['generation']['imported']:
                for name,dest in [('transcript',transcript),('audit',audit)]:
                    require(sha(c[name])==c[name+'Sha256'],'IMPORT_EVALUATION_CHANGED'); copy_verified(c[name],dest,c[name+'Sha256'])
                score=c['scores']
            else:
                if not transcript.exists(): self._tool([self.c['modelPython'],str(Path(self.c['bibleSkill'])/'scripts/transcribe.py'),c['audio'],'--output',transcript],timeout=600)
                if not audit.exists(): self._tool([self.c['modelPython'],str(Path(self.c['bibleSkill'])/'scripts/workflow.py'),'audit','--lyrics',root/'lyrics.txt','--transcript',transcript,'--out',audit],timeout=90)
                inputs=root/(c['id']+'-score-input.json'); atomic(inputs,[dict(id=c['id'],path=c['audio'],lyrics_status='needs_review')]); score_dir=root/(c['id']+'-scores')
                if not (score_dir/'scores.json').exists(): self._tool([self.c['modelPython'],str(Path(self.c['bibleSkill'])/'scripts/workflow.py'),'rate','--inputs',inputs,'--out',score_dir],timeout=600)
                track=read(score_dir/'scores.json')['tracks'][0]; require(track['sha256']==c['audioSha256'],'SCORE_AUDIO_CHANGED'); score=track['mean']; files.append(score_dir/'scores.json')
            require(all(type(score.get(k)) in (int,float) and math.isfinite(score[k]) for k in ('CE','CU','PC','PQ')),'SCORE_INVALID')
            data=read(transcript); asr=' '.join(s.get('text','') for s in data.get('segments',[])); expected=(root/'lyrics.txt').read_text()
            diff=[dict(kind=tag,canonical=[i,j],recognized=[a,b]) for tag,i,j,a,b in difflib.SequenceMatcher(None,tokens(expected),tokens(asr),autojunk=False).get_opcodes() if tag!='equal']
            check=read(audit); known=bool(check.get('confirmed_substantive_failure') or check.get('confirmed_lyric_failure') or check.get('lyrics_status')=='failed')
            rows.append({**c,'scores':score,'transcript':str(transcript),'audit':str(audit),'independentAsrDifferences':diff,'confirmedContentFailure':known})
            files.extend([transcript,audit])
        rows.sort(key=lambda c:(-c['scores']['CE'],-c['scores']['PQ'],c['id']))
        return dict(candidates=rows,selectionRule='eligible first; CE descending, PQ exact tie, stable candidate ID'),files
    def _asr_blocks_synchronization(self,c,provider_rows):
        return bool(c['independentAsrDifferences'])
    def synchronization(self,r,root,ctx):
        attempts=[]; source=read(root/'source-verses.json')
        for c in ctx['evaluation']['candidates']:
            if c['confirmedContentFailure']: attempts.append(dict(candidate=c['id'],reason='CONFIRMED_CONTENT_FAILURE')); continue
            errors=[]; rows=None; timing_source=None
            try:
                rows=synchronization(source,read(c['alignment']),c['durationMs']); timing_source='provider_alignment'
            except Blocked as e: errors.append(e.code)
            if self._asr_blocks_synchronization(c,rows):
                attempts.append(dict(candidate=c['id'],reason='INDEPENDENT_ASR_UNRESOLVED',timingErrors=errors));continue
            if rows is None:
                # One bounded alternative using independently observed word times.
                # No canonical prompt, interpolation, time stretching or generation.
                transcript=read(c['transcript'])
                measured=[dict(word=w['word'],start_s=w['start'],end_s=w['end'],success=True)
                          for segment in transcript.get('segments',[]) for w in segment.get('words',[])]
                try: rows=synchronization(source,measured,c['durationMs']); timing_source='independent_asr_measured_words'
                except Blocked as e:
                    attempts.append(dict(candidate=c['id'],reason=e.code,timingErrors=errors));continue
            file=root/'measured-timings.json'; atomic(file,rows)
            report=root/'synchronization-evidence.json'; atomic(report,dict(candidate=c['id'],timingSource=timing_source,providerTimingIsNotLyricVerification=True,automaticLyricsVerified=False,human_review='pending',alignmentSha256=sha(c['alignment']),transcriptSha256=sha(c['transcript']),providerTimingErrors=errors,rejectedCandidates=attempts))
            return dict(selected=c,rowsFile=str(file)),[file,report]
        atomic(root/'synchronization-failures.json',attempts)
        raise Blocked('NO_DEFENSIBLE_SYNCHRONIZED_CANDIDATE')
    def artwork(self,r,root,ctx):
        art_root=private(Path(self.c['stateDirectory'])/'artwork',True)
        key=r['book']+'-'+str(r['chapter']); cached=art_root/(key+'.json')
        with lock(Path(self.c['stateDirectory'])/'locks'/(key+'-art.lock')):
            if not cached.exists():
                m=read(r['importManifest']) if r.get('importManifest') else None
                if m:
                    p=Path(m['artwork']); require(sha(p)==m['artworkSha256'],'ARTWORK_CHANGED')
                    provenance=read(m['artworkProvenance']); require(provenance.get('original') is True and provenance['imageSha256']==sha(p) and provenance['sourceVersesSha256']==ctx['source']['sourceSha256'],'ARTWORK_PROVENANCE_MISMATCH')
                    image=p.read_bytes(); generator=provenance['generator']
                else:
                    require(self.c.get('allowImageGeneration') is True,'IMAGE_GENERATION_DISABLED')
                    shared=private(art_root/key,True)
                    source_file=shared/'source-verses.json'
                    if source_file.exists():require(sha(source_file)==ctx['source']['sourceSha256'],'SHARED_ARTWORK_SOURCE_CHANGED')
                    else:shutil.copyfile(root/'source-verses.json',source_file)
                    image,generator=self.gemini_image(shared,r)
                require(image[:8]==b'\x89PNG\r\n\x1a\n' and len(image)>=24 and min(struct.unpack('>II',image[16:24]))>=512,'ARTWORK_INVALID')
                target=art_root/(key+'-'+digest(image)[:16]+'.png'); target.write_bytes(image)
                atomic(cached,dict(path=str(target),imageSha256=sha(target),sourceVersesSha256=ctx['source']['sourceSha256'],generator=generator,original=True))
            metadata=read(cached); require(metadata['sourceVersesSha256']==ctx['source']['sourceSha256'] and sha(metadata['path'])==metadata['imageSha256'],'SHARED_ARTWORK_SOURCE_CHANGED')
            target=root/'artwork.png'; copy_verified(metadata['path'],target,metadata['imageSha256']); atomic(root/'artwork-provenance.json',metadata)
        return metadata,[target,root/'artwork-provenance.json']
    def gemini_image(self,root,r):
        key=private(self.c['geminiKeyFile']).read_text().strip(); require(key,'GEMINI_AUTH_REQUIRED')
        model=self.c.get('geminiImageModel','gemini-3.1-flash-lite-image'); require(re.fullmatch(r'gemini-[A-Za-z0-9.-]+',model),'IMAGE_MODEL_INVALID')
        intent=root/'image-intent.json'; response=root/'image-response.json'
        require(response.exists() or not intent.exists(),'IMAGE_OUTCOME_UNKNOWN')
        if not response.exists():
            source=read(root/'source-verses.json')
            prompt='Create ONE original square cinematic painterly Bible chapter illustration for '+r['label']+' '+str(r['chapter'])+'. Depict the chapter meaning respectfully, historically evocative ancient setting, beautiful warm sunlight, olive and gold palette, atmospheric depth, no text, letters, numbers, watermark or logo, no copied app artwork. This is a conceptual illustration, not a historical photograph. Full canonical chapter for scene selection:\n'+'\n'.join(v['text'] for v in source['verses'])
            body=dict(contents=[dict(parts=[dict(text=prompt)])],generationConfig=dict(responseModalities=['TEXT','IMAGE'],imageConfig=dict(aspectRatio='1:1')))
            atomic(intent,dict(requestSha256=digest(canonical(body)),model=model,createdAt=utc(),maxRequests=1))
            data=request_json('https://generativelanguage.googleapis.com/v1beta/models/'+model+':generateContent',body=body,headers={'x-goog-api-key':key},maximum=32*1024*1024,timeout=180); atomic(response,data)
        require(read(intent)['model']==model,'IMAGE_MODEL_CHANGED_AFTER_INTENT')
        data=read(response); images=[p['inlineData'] for candidate in data.get('candidates',[]) for p in candidate.get('content',{}).get('parts',[]) if 'inlineData' in p and not p.get('thought')]
        require(len(images)==1 and images[0].get('mimeType') in ('image/png','image/jpeg'),'IMAGE_RESPONSE_UNSUPPORTED')
        image=base64.b64decode(images[0]['data'],validate=True)
        if images[0]['mimeType']=='image/jpeg':
            original=root/'generated-original.jpg'; original.write_bytes(image)
            png=root/'generated-image.png'
            if not png.exists(): self._tool([self.c['ffmpeg'],'-v','error','-i',original,'-frames:v','1',png],timeout=45)
            image=png.read_bytes()
        return image,'Google Gemini '+model
    def _contract(self):
        path=Path(self.c['uploader']).expanduser().resolve().parents[1]/'content-tools/async/package.py'
        require(path.is_file(),'UPLOADER_CONTRACT_UNAVAILABLE')
        spec=importlib.util.spec_from_file_location('chapter_uploader_contract',path); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
    def package(self,r,root,ctx):
        require(self.c.get('ownerPolicyFile'),'OWNER_POLICY_REQUIRED')
        policy_file=Path(self.c['ownerPolicyFile']); policy=read(policy_file)
        require(all(policy.get(k) is True for k in ('publicationApproved','contentApproved','timingsApproved','rightsApproved','exportApproved')),'OWNER_POLICY_NOT_APPROVED')
        require(policy.get('editionId')==EDITION and r['genre'] in policy.get('genreIds',[]) and policy.get('human_review')=='pending' and policy.get('humanListeningClaimed') is False,'OWNER_POLICY_SCOPE_MISMATCH')
        story_file=Path(policy['storyAuthorizationPath']); require(sha(story_file)==policy['storyAuthorizationSha256'],'STORY_AUTHORIZATION_CHANGED')
        story=read(story_file); require(story.get('exportApproved') is True and story.get('humanListeningClaimed') is False and all(story.get(k)==v for k,v in STORY.items() if k!='allowed'),'STORY_AUTHORIZATION_REQUIRED')
        selected=ctx['synchronization']['selected']; source=read(root/'source-verses.json')
        # Honor the existing recording-specific confirmed failure registry.
        if self.c.get('pipelineScripts'):
            import sys
            sys.path.insert(0,self.c['pipelineScripts'])
            from recording_reviews import require_publishable
            try: require_publishable(selected['audioSha256'])
            except ValueError: raise Blocked('RECORDING_BLOCKED_BY_REVIEW') from None
        output=root/'package'; contract=self._contract()
        if output.exists():
            require(read(output/'review-evidence.json')['request']==r,'PACKAGE_CONFLICT')
            with tempfile.TemporaryDirectory(dir=root) as td:
                checked=contract.prepare(output,Path(td)/'validated',r['target'],['content-lead'],self.c['ffprobe'])
            return dict(folder=str(output),chapter=read(output/'chapter.json'),packageSha256=checked['packageSha256']),list(output.iterdir())
        with tempfile.TemporaryDirectory(prefix='.package-',dir=root) as td:
            stage=Path(td)/'input'; stage.mkdir()
            audio=stage/'audio.m4a'
            # AAC derivative is explicit; original and mapping evidence remain immutable.
            self._tool([self.c['ffmpeg'],'-v','error','-i',selected['audio'],'-map','0:a:0','-c:a','aac','-b:a','192k','-movflags','+faststart',audio],timeout=180)
            probe=json.loads(self._tool([self.c['ffprobe'],'-v','error','-show_format','-of','json',audio],timeout=45)); duration=round(float(probe['format']['duration'])*1000)
            require(abs(duration-selected['durationMs'])<=50,'TRANSCODE_TIMELINE_CHANGED')
            rows=read(ctx['synchronization']['rowsFile']); require(rows[-1]['endMs']<=duration,'TRANSCODE_TRUNCATED_LYRICS')
            fingerprint=digest(canonical(dict(request=r,audio=sha(audio),art=sha(root/'artwork.png'),timings=sha(ctx['synchronization']['rowsFile']),documentGenerator='qualified-verse-ids-v1')))[:16]
            prefix=r['book']+'-'+str(r['chapter'])+'-'+r['genre']; recording=prefix+'-'+fingerprint
            media=prefix+'-audio-'+sha(audio)[:16]; canonical_rev=r['book']+'-'+str(r['chapter'])+'-webu-'+ctx['source']['sourceSha256'][:16]; performed_rev=prefix+'-phrases-'+fingerprint
            docs=lyric_documents(source,rows,recording,media,performed_rev,canonical_rev,duration,sha(audio))
            docs[0]['source']=dict(url=source.get('sourceURL',source.get('url')),revision=canonical_rev,sha256=ctx['source']['sourceSha256'])
            files=dict(audio='audio.m4a',artwork='artwork.png',canonical='canonical.json',performed='performed.json',cues='cues.json',review='review.json')
            shutil.copyfile(root/'artwork.png',stage/'artwork.png')
            for name,data in zip(('canonical','performed','cues'),docs): atomic(stage/files[name],data)
            chapter=dict(schemaVersion=1,bookId=r['book'],chapterNumber=r['chapter'],editionId=EDITION,genreId=r['genre'],recordingId=recording,bundleRevision=prefix+'-bundle-'+fingerprint,mediaRevision=media,canonicalTextRevision=canonical_rev,performedTextRevision=performed_rev,durationMs=duration,accessClass='subscriber',chapterVisualId=r['book']+'-'+str(r['chapter'])+'-default',visualRevision=r['book']+'-'+str(r['chapter'])+'-art-'+sha(root/'artwork.png')[:16],exportAllowed=True,files=files)
            atomic(stage/'chapter.json',chapter)
            evidence=dict(schemaVersion=1,request=r,documentGenerator='qualified-verse-ids-v1',ownerPolicySha256=sha(policy_file),ownerPolicy=policy,storyAuthorizationSha256=sha(story_file),human_review='pending',humanListeningClaimed=False,automaticLyricsVerified=False,approvalBasis='owner consent and automatic structural checks; no human listening claim',originalAudioSha256=selected['audioSha256'],derivedAudioSha256=sha(audio),providerAlignmentSha256=sha(selected['alignment']),independentTranscriptSha256=sha(selected['transcript']),synchronizationEvidenceSha256=sha(root/'synchronization-evidence.json'),artworkProvenance=ctx['artwork'],independentAsrDifferences=selected['independentAsrDifferences'])
            atomic(stage/'review-evidence.json',evidence)
            receipt=dict(schemaVersion=1,reviewId=recording+'-owner-auto',reviewerId='content-lead',reviewedAt=utc().replace('+00:00','Z'),status='approved',contentApproved=True,timingsApproved=True,rightsApproved=True,exportApproved=True,canonicalVerseCount=len(source['verses']),evidenceSha256=sha(stage/'review-evidence.json'),assetSha256={k:sha(stage/v) for k,v in files.items() if k!='review'},human_review='pending',humanListeningClaimed=False,automaticLyricsVerified=False,approvalBasis='owner-authorized automated checks')
            atomic(stage/'review.json',receipt)
            checked=contract.prepare(stage,Path(td)/'validated',r['target'],['content-lead'],self.c['ffprobe'])
            stage.rename(output)
        return dict(folder=str(output),chapter=chapter,packageSha256=checked['packageSha256']),list(output.iterdir())
    def publication(self,r,root,ctx):
        require(self.c.get('allowPublication') is True and not r.get('importManifest'),'PUBLICATION_DISABLED_FOR_REPLAY')
        if self.c.get('publicationHoldFile'):
            require(read(self.c['publicationHoldFile']).get('active') is False,'PUBLICATION_HELD')
        folder=Path(ctx['package']['folder']); contract=self._contract(); actual=contract.collect(folder,r['target'])
        require(actual['packageSha256']==ctx['package']['packageSha256'],'PACKAGE_CHANGED')
        prefix=[self.c['uploader'],'--config',self.c['uploaderConfig']]
        # The creator submit operation itself is content-addressed and idempotent.
        # Reissuing this exact immutable package reconciles a lost local ack safely.
        ack=root/'upload-ack.json'; intent=root/'upload-intent.json'
        if not ack.exists():
            atomic(intent,dict(packageSha256=actual['packageSha256'],target=r['target'],createdAt=utc()))
            data=json.loads(self._tool(prefix+['submit','--folder',str(folder),'--target',r['target'],'--detach'],timeout=60))
            require(re.fullmatch(r'local_[a-f0-9]{32}',data.get('job_id','')) is not None,'UPLOAD_ACK_INVALID'); atomic(ack,data)
        data=read(ack); status=json.loads(self._tool(prefix+['status',data['job_id']],timeout=60))
        require(status.get('job_id')==data['job_id'] and status.get('target')==r['target'],'UPLOAD_STATUS_IDENTITY_MISMATCH')
        if status.get('status') in ('failed','needs_review'): raise Blocked('PUBLICATION_REJECTED')
        if status.get('status')!='live' or status.get('statusCheckError'): raise Blocked('PUBLICATION_PENDING',pending=True)
        remote=status.get('remote',{}); require(remote.get('status')=='live' and remote.get('uploadCommitted') is True and remote.get('packageSha256')==actual['packageSha256'],'LIVE_PUBLICATION_UNCONFIRMED')
        atomic(root/'upload-live.json',status)
        return dict(jobId=data['job_id'],recordingId=ctx['package']['chapter']['recordingId']),[ack,intent,root/'upload-live.json']
    def _fetch(self,r,path):
        base='https://api.holysong.org' if r['target']=='production' else 'https://staging-api.holysong.org'
        try:
            with build_opener(NoRedirect()).open(base+path,timeout=25) as response:
                raw=response.read(8*1024*1024+1); require(len(raw)<=8*1024*1024,'CATALOG_TOO_LARGE'); return raw,json.loads(raw)
        except (HTTPError,URLError,TimeoutError): raise Blocked('LIVE_VERIFICATION_UNAVAILABLE',pending=True) from None
    def verification(self,r,root,ctx):
        chapter=ctx['package']['chapter']; _,head=self._fetch(r,'/v1/catalog/head')
        require(re.fullmatch('[A-Za-z0-9_.-]+',head['catalogRevision']) is not None,'CATALOG_REVISION_INVALID')
        raw,catalog=self._fetch(r,'/v1/catalog/revisions/'+head['catalogRevision']); require(digest(raw)==head['sha256'],'CATALOG_HASH_MISMATCH')
        rows=[x for x in catalog['recordings'] if x['recordingId']==chapter['recordingId'] and x['bundleRevision']==chapter['bundleRevision']]
        require(len(rows)==1,'EXPECTED_RECORDING_NOT_LIVE')
        check_withdrawals(head,chapter['recordingId'],chapter['bundleRevision'])
        raw,manifest=self._fetch(r,'/v1/recordings/'+chapter['recordingId']+'/bundles/'+chapter['bundleRevision'])
        require(digest(raw)==rows[0]['manifestSha256'],'MANIFEST_HASH_MISMATCH')
        for k in ('recordingId','bookId','chapterNumber','editionId','genreId','bundleRevision','mediaRevision','durationMs'):
            require(manifest[k]==chapter[k],'PUBLISHED_IDENTITY_MISMATCH')
        require(all(manifest['exportPolicy'].get(k)==v for k,v in STORY.items()) and manifest['exportPolicy'].get('rightsRevision'),'STORY_POLICY_MISMATCH')
        folder=Path(ctx['package']['folder']); assets={a['role']:a for a in manifest['assets']}
        require(set(assets)=={'audio','artwork','canonical','performed','cues'},'PUBLISHED_ASSETS_INVALID')
        for role,asset in assets.items(): require(asset['sha256']==sha(folder/chapter['files'][role]),'PUBLISHED_ASSET_MISMATCH')
        _,after=self._fetch(r,'/v1/catalog/head'); require(after==head,'CATALOG_CHANGED_DURING_CHECK')
        result=dict(verified=True,productionVerified=r['target']=='production',recordingId=chapter['recordingId'],bundleRevision=chapter['bundleRevision'],catalogRevision=head['catalogRevision'],checkedAt=utc())
        atomic(root/'live-verification.json',result); return result,[root/'live-verification.json']
