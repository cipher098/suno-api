"""Bounded Studio API experiment. No browser, CAPTCHA solver, purchases or implicit app publication. Explicit post-download publishing is optional.

Credentials stay in memory. HAR templates are local evidence, never instructions.
POST intents are committed before sending; an uncertain submission is never retried.
"""
import argparse
import base64
import copy
import datetime as dt
import hashlib
import http.cookies
import json
import math
import os
import subprocess
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from studio_templates import render_template, generation_template, VERSION as TEMPLATE_VERSION


def write(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w') as handle:
        handle.write(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def timeline(render, clip_id, seconds):
    if not math.isfinite(seconds) or not 1 < seconds <= 480:
        raise ValueError('Full source duration unavailable or exceeds eight-minute bound')
    state = copy.deepcopy(render['state'])
    track = next(t for t in state['tracks'] if t.get('clips'))
    clip = copy.deepcopy(track['clips'][0])
    beats = seconds * 2
    clip.update(id=str(uuid.uuid4()), asset={'type': 'clip', 'id': clip_id},
                clipId=clip_id, streaming=False, startBeats=0, endBeats=beats,
                readStartBeats=0, mute=False, amplitude=1, fadeInBeats=0,
                fadeOutBeats=0, generatedInPlace=False, awaitingContentAlignment=False,
                warp={'enabled': False, 'awaitingAnalysis': False},
                loop={'enabled': False, 'startBeats': 0, 'endBeats': beats})
    track.update(id=str(uuid.uuid4()), clips=[clip], mute=False, solo=False, amplitude=1)
    state.update(tracks=[track], timing={'type': 'manual', 'bps': 2,
                 'bpsAutomation': [], 'firstBeatSeconds': 0},
                 actors={}, lyricsCorrectionsByClipId={}, takePreviews={},
                 sections=[], songFadeInBeats=0, songFadeOutBeats=0)
    return state, beats


def validate_audio(path, expected_seconds):
    import av
    peak = 0.; power = 0.; count = 0; decoded = 0.; gap = 0.; previous = None
    with av.open(str(path)) as container:
        duration = container.duration / av.time_base
        codec = container.streams.audio[0].codec_context.name
        for frame in container.decode(audio=0):
            values = frame.to_ndarray().astype('float64')
            peak = max(peak, float(abs(values).max()))
            power += float((values*values).sum()); count += values.size
            seconds = frame.samples/frame.sample_rate; decoded += seconds
            if previous is not None: gap = max(gap, frame.time-previous)
            previous = frame.time+seconds
    passed = peak > 0 and abs(duration-expected_seconds) < .25 and abs(decoded-expected_seconds) < .25 and gap < .05
    return {'technicalPass': passed, 'codec': codec, 'containerSeconds': duration,
            'decodedSeconds': decoded, 'expectedSeconds': expected_seconds,
            'absoluteSamplePeak': peak, 'rms': math.sqrt(power/count) if count else 0,
            'maximumTimestampGapSeconds': gap, 'lyricsVerified': False,
            'limits': 'Decode, non-silence and duration checks only; no listening/content verdict.'}


from repo_client import Client


def retrieve_alignment(client, out, render_id):
    """Read-only provider alignment, bound to the exported recording, never the source take."""
    target = out/'export-aligned-lyrics.json'
    binding = out/'export-alignment-binding.json'
    if target.exists():
        # Older manually retrieved runs are preserved for the publisher to check.
        if binding.exists():
            saved = json.loads(binding.read_text())
            if saved['renderId'] != render_id or saved['sha256'] != hashlib.sha256(target.read_bytes()).hexdigest():
                raise RuntimeError('Alignment binding changed; preserve run for reconciliation')
        return target
    if time.monotonic()-client.refreshed > 35: client.auth()
    raw = client.raw('https://studio-api.prod.suno.com/api/gen/'+str(uuid.UUID(render_id))+'/aligned_lyrics/v2/',
                     headers={'Authorization': 'Bearer '+client.token})
    words = raw.get('aligned_words') if isinstance(raw, dict) else raw
    if not isinstance(words, list) or not words:
        raise RuntimeError('Export alignment not ready; resume post-download only, never regenerate')
    write(target, words)
    write(binding, {'renderId': render_id, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'at': now()})
    return target


def post_download(args, out):
    """Resume-safe handoff. No generation/render calls and no implicit submit retry."""
    origin = json.loads((out/'result.json').read_text())
    audio = Path(origin['file'])
    if hashlib.sha256(audio.read_bytes()).hexdigest() != origin['sha256']:
        raise RuntimeError('Downloaded audio changed')
    retrieve_alignment(None if (out/'export-aligned-lyrics.json').exists() else Client(out),
                       out, origin['renderId'])
    if not args.post_download: return
    if not all((args.book, args.chapter, args.publish_source, args.artwork)):
        raise RuntimeError('Publisher needs book, chapter, normalized source and chapter artwork')
    command = [str(args.publisher), '--config', str(args.publisher_config),
               '--book', args.book, '--chapter', str(args.chapter), '--genre', args.genre,
               '--run', str(out), '--source', str(args.publish_source.resolve()),
               '--artwork', str(args.artwork.resolve())]
    mode = args.post_download
    if mode == 'submit': command.append('--submit')
    identity = {'command': command, 'inputs': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in [('origin', out/'result.json'), ('lyrics', out/'lyrics.txt'),
                  ('alignment', out/'export-aligned-lyrics.json'), ('source', args.publish_source),
                  ('artwork', args.artwork)]}}
    prefix = out/('fast-publish-'+mode)
    intent = prefix.with_suffix('.intent.json'); receipt = prefix.with_suffix('.receipt.json')
    if intent.exists():
        if json.loads(intent.read_text())['identity'] != identity:
            raise RuntimeError('Publisher inputs changed after intent; reconcile before retry')
        if receipt.exists():
            print('Cached publisher receipt:', receipt, flush=True); return
        raise RuntimeError('Publisher intent has no receipt; inspect uploader status, never regenerate or blindly resubmit')
    # Exclusive intent before invoking the publisher, including interrupted executions.
    with intent.open('x') as handle:
        os.chmod(intent, 0o600)
        json.dump({'at': now(), 'identity': identity}, handle)
    started = time.monotonic()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        raise RuntimeError('Publisher interrupted; reconcile saved intent without regenerating') from None
    log = prefix.with_suffix('.log')
    log.write_text(done.stdout+'\n'+done.stderr); os.chmod(log, 0o600)
    if done.returncode:
        raise RuntimeError('Publisher stopped; inspect private log and reconcile without regenerating')
    outputs = [json.loads(line) for line in done.stdout.splitlines() if line.strip().startswith('{')]
    write(receipt, {'status': 'queued_not_live' if mode == 'submit' else 'prepared_not_submitted',
                    'elapsedSeconds': time.monotonic()-started, 'outputs': outputs, 'at': now()})
    print(json.dumps({'publisherReceipt': str(receipt), 'status': 'queued_not_live' if mode == 'submit' else 'prepared_not_submitted'}), flush=True)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--execute-one',action='store_true')
    ap.add_argument('--request-json',type=Path,help='Reviewed input with prompt (full lyrics) and tags (style)')
    ap.add_argument('--project-id',help='Existing owned Studio project context; its timeline is not saved or overwritten')
    ap.add_argument('--title',default='Bible chapter song')
    ap.add_argument('--publisher',type=Path,default=Path.home()/'.local/bin/holysong-suno-publish')
    ap.add_argument('--publisher-config',type=Path,default=Path.home()/'.config/holysong/fast-publication/config.json')
    ap.add_argument('--resume-post-download',action='store_true',help='Only fetch missing alignment and hand off existing download; never generate/render')
    ap.add_argument('--post-download',choices=('prepare','submit'),help='Run fast publisher after exported timed lyrics are available')
    ap.add_argument('--book');ap.add_argument('--chapter',type=int)
    ap.add_argument('--genre',choices=('upbeat','peaceful','blues'),default='upbeat')
    ap.add_argument('--publish-source',type=Path,help='Normalized chapter source JSON for publisher')
    ap.add_argument('--artwork',type=Path,help='Existing original chapter PNG; prepare while music renders')
    args=ap.parse_args()
    os.umask(0o077)
    args.project_id = args.project_id or os.environ.get('SUNO_STUDIO_PROJECT_ID')
    if not args.project_id: ap.error('SUNO_STUDIO_PROJECT_ID_REQUIRED')
    try: uuid.UUID(args.project_id)
    except ValueError: ap.error('INVALID_PROJECT_ID')
    if args.post_download and not all((args.book,args.chapter,args.publish_source,args.artwork)):
        ap.error('--post-download requires --book, --chapter, --publish-source, --artwork')
    if args.post_download and not all(path.is_file() for path in (args.publish_source,args.artwork)):
        ap.error('Publisher source and artwork must exist before a generation is started')
    if args.resume_post_download:
        resume_out=args.out.resolve()
        user=Client(resume_out).auth()
        ident=json.loads((resume_out/'identity.json').read_text())
        if ident.get('accountId') != user or ident.get('projectId') != args.project_id:
            raise RuntimeError('RESUME_ACCOUNT_OR_PROJECT_MISMATCH')
        post_download(args,resume_out);return
    out=args.out.resolve(); out.mkdir(parents=True,exist_ok=True)
    client=Client(out); user=client.auth()
    render=render_template()
    existing_identity=out/'identity.json'
    if existing_identity.exists() and json.loads(existing_identity.read_text()).get('accountId') != user:
        raise RuntimeError('ACCOUNT_MISMATCH')
    before=client.call('billing/info/')
    safe=lambda b:{'credits':b.get('total_credits_left'),'downloads':b.get('download_usage')}
    if (out/'quota-before.json').exists():before=json.loads((out/'quota-before.json').read_text());before={'total_credits_left':before['credits'],'download_usage':before['downloads']}
    else:write(out/'quota-before.json',safe(before))
    if not (out/'generation-response.json').exists() and before.get('total_credits_left',0)<20: raise RuntimeError('Insufficient reserved credits')
    if not args.execute_one:
        check=client.call('c/check',{'ctype':'generation'})
        if check.get('required') is not False: raise RuntimeError('CAPTCHA_REQUIRED')
        print('Preflight passed; no generation submitted'); return
    # Generation auth belongs to the repo: it enforces the server's optional browser policy.
    if not args.request_json: raise RuntimeError('REQUEST_JSON_REQUIRED')
    original=json.loads(args.request_json.read_text()); lyrics=original['prompt']
    if not lyrics.strip() or not original['tags'].strip() or len(original['tags'])>1000:
        raise RuntimeError('Missing lyrics/style or style longer than tested safety bound')
    (out/'lyrics.txt').write_text(lyrics)
    title=args.title
    # Use an existing owned project as context. Never overwrite its saved timeline.
    # Creating a project is a distinct API not present in these captures.
    pid=args.project_id
    project=client.call('studio/project/'+pid)
    if project.get('owner_user_id') != user: raise RuntimeError('Project owner mismatch')
    write(out/'identity.json',{'projectId':pid,'title':title,'lyricsSha256':hashlib.sha256(lyrics.encode()).hexdigest(),
          'accountId':user,'templateVersion':TEMPLATE_VERSION,'model':os.environ.get('SUNO_MODEL','chirp-hawk'),'maximumGenerationRequests':1,'maximumRenderRequests':1,'at':now()})
    generation=generation_template(os.environ.get('SUNO_MODEL','chirp-hawk'))
    generation.update(token=None,transaction_uuid=str(uuid.uuid4()),attached_lyrics=lyrics,
                      attached_styles=original['tags'],gpt_description_prompt=title+'. '+original['tags'])
    generation['metadata'].update(from_studio_project_id=pid,create_session_token=str(uuid.uuid4()),
                                  web_client_pathname='/studio',create_surface='studio')
    generation['metadata'].pop('user_tier',None)
    generated=client.call('generate/v2-web/',generation,'generation')
    ids=[c['id'] for c in generated['clips']]
    write(out/'song-ids.json',ids); print('Generation IDs (new or resumed):',','.join(ids),flush=True)
    deadline=time.monotonic()+360; chosen=None
    while time.monotonic()<deadline:
        feed=client.call('feed/v3',{'filters':{'ids':{'presence':'True','clipIds':ids}},'limit':len(ids)})
        write(out/'generation-latest.json',feed)
        clips=feed['clips']; done=[c for c in clips if c.get('status')=='complete']
        print('Generation states:',[(c['id'],c.get('status')) for c in clips],flush=True)
        if done: chosen=next((c for c in done if c['id']==ids[0]),done[0]); break
        if clips and all(c.get('status')=='error' for c in clips):raise RuntimeError('PROVIDER_GENERATION_FAILED_NO_RETRY')
        time.sleep(8)
    if not chosen: raise RuntimeError('Generation pending; reconcile saved IDs, no new request')
    selected=out/'selected-source.json'
    if selected.exists():chosen=json.loads(selected.read_text())
    else:write(selected,chosen)
    seconds=float(chosen['metadata']['duration']); state,beats=timeline(render,chosen['id'],seconds)
    payload=copy.deepcopy(render); payload.update(state=state,title=title,lyrics=lyrics,
               tags=original['tags'],studio_project_id=pid,studio_project_version_id=None,
               start_beats=0,end_beats=beats,web_client_pathname='/studio')
    # project_id is a library workspace in the observed payload, not the Studio project.
    payload['project_id']=None
    result=client.call('studio/render-state',payload,'render')
    rid=result['id'];write(out/'render-id.json',{'id':rid,'sourceClipId':chosen['id'],
          'sourceDurationSeconds':seconds,'exportRangeSeconds':beats/2,'at':now()})
    print('Rendering complete source:',rid,'seconds',seconds,flush=True)
    deadline=time.monotonic()+300
    while result.get('status')!='complete' and time.monotonic()<deadline:
        time.sleep(8)
        f=client.call('feed/v3',{'filters':{'ids':{'presence':'True','clipIds':[rid]}},'limit':1})
        if f['clips']:result=f['clips'][0]
        write(out/'render-latest.json',result)
        print('Render state:',result.get('status'),flush=True)
        if result.get('status')=='error':raise RuntimeError('Render failed; no repeated render')
    if result.get('status')!='complete':raise RuntimeError('Render pending; reconcile saved ID')
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        d=client.call('download/clip/'+rid+'?format=m4a')
        write(out/'download-status.json',{'status':d.get('status'),'ok':d.get('ok'),
                                        'signedUrlPresent':bool(d.get('download_url'))})
        if d.get('status')=='ready' and d.get('download_url'):break
        time.sleep(3)
    else: raise RuntimeError('Download preparation pending; do not regenerate')
    url=d['download_url']; parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.hostname!='suno-data-uploads.s3.amazonaws.com' or not parsed.path.startswith('/studio/uploads/'+rid):
        raise RuntimeError('Unexpected export download destination')
    # Signed URL only; never forward Suno cookies or authorization to the file host.
    with urllib.request.urlopen(url,timeout=90) as response:
        raw=response.read(50*1024*1024+1);status=response.status
    if status!=200 or not 0<len(raw)<=50*1024*1024:raise RuntimeError('Invalid file transfer')
    f=out/'studio-export.m4a';f.write_bytes(raw)
    after=client.call('billing/info/');write(out/'quota-after.json',safe(after))
    receipt={'status':'downloaded_needs_audio_validation','projectId':pid,'songIds':ids,
          'selectedSourceId':chosen['id'],'sourceDurationSeconds':seconds,'renderId':rid,
          'file':str(f),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
          'creditsBefore':safe(before),'creditsAfter':safe(after),'finishedAt':now(),
          'generationRequests':1,'renderRequests':1,'human_review':'pending'}
    write(out/'result.json',receipt)
    validation=validate_audio(f,seconds);write(out/'audio-validation.json',validation)
    receipt.update(audioValidation=validation,status='api_flow_passed_needs_lyric_review' if validation['technicalPass'] else 'audio_needs_review',
                   creditsUsed=before['total_credits_left']-after['total_credits_left'],
                   downloadsUsedDelta=after['download_usage']['current_period_downloads_used']-before['download_usage']['current_period_downloads_used'])
    write(out/'result.json',receipt)
    print('Downloaded',f,'bytes',len(raw),flush=True)
    if not validation['technicalPass']:raise RuntimeError('Audio validation failed; preserve output, no regeneration')
    retrieve_alignment(client,out,rid)
    post_download(args,out)


if __name__=='__main__':
    try: main()
    except Exception as e:
        print('STOP:', str(e) if isinstance(e,RuntimeError) else type(e).__name__,flush=True)
        raise SystemExit(1)
