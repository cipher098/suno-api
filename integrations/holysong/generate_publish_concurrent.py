"""Concurrent-by-book Studio generation → existing asynchronous publisher.

One durable job per chapter/genre. Owner-approved failed attempts may restart.
Only an actual production catalog check returns success=true.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
from adapters import Adapters, STORY
import sys
from upbeat_director import prepare_request
from core import Blocked, atomic, canonical, digest, identity, lock, private, read, require, sha, utc


def progress(stage):
    print(json.dumps({'stage': stage, 'at': utc()}), file=sys.stderr, flush=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_failed_studio(job):
    """Called under job + book locks after explicit approval to spend again."""
    studio = job/'studio'
    paths = [studio, job/'studio.log', job/'studio-attempt.json']
    require(all(not path.is_symlink() for path in paths), 'FAILED_ATTEMPT_SYMLINK_REFUSED')
    require(not studio.exists() or studio.is_dir(), 'FAILED_ATTEMPT_PATH_INVALID')
    require(not (studio/'result.json').exists() and not (studio/'fast-publish-submit.receipt.json').exists(),
            'DOWNLOADED_OR_SUBMITTED_RUN_MUST_RESUME')
    history = private(job/'previous-attempts', True)
    archive = Path(tempfile.mkdtemp(prefix='studio-', dir=history))
    atomic(archive/'restart.json', dict(at=utc(), regenerationApproved=True,
           previousGenerationIntent=(studio/'generation-intent.json').exists(),
           reason='Owner approved a fresh attempt on rerun, including possible duplicate credits'))
    sync_directory(history); sync_directory(job)
    # Keep all old evidence. Move the outer intent last; no new provider request here.
    for path in paths:
        if path.exists():
            path.rename(archive/path.name)
            sync_directory(archive); sync_directory(job)
    progress('restarting_failed_studio')


def inputs(config, book, chapter, genre):
    base = read(config['workflowConfig'])
    require(genre in base['recipes'], 'GENRE_RECIPE_NOT_CONFIGURED')
    recipe = read(base['recipes'][genre])
    request = identity(book, chapter, genre, recipe, 'production', read(base['bookIndex'])['books'])
    require(len(recipe['style']) <= 900, 'STYLE_TOO_LONG')
    source_root = Path(base['sourceDirectory'])
    pin = read(source_root/'index.json')[request['book']+':'+str(chapter)]
    request['sourceSha256'] = pin['sha256']
    request['sunoAccountFingerprint'] = config['accountFingerprint']
    request['sunoProjectId'] = config['studioProjectId']
    # Fixed job identity survives code upgrades; changed source/recipe needs explicit reconciliation.
    state = Path(config['stateDirectory'])
    require(state.parent.is_dir(), 'STATE_PARENT_NOT_PROVISIONED')
    state = private(state, True)
    sync_directory(state.parent)
    job = private(state/(request['book']+'-'+str(chapter)+'-'+genre), True)
    sync_directory(state)
    return base, recipe, request, job


def studio_command(config, request, job, resume):
    command = [config['modelPython'], config['studioRunner'], '--out', str(job/'studio'),
               '--book', request['book'], '--chapter', str(request['chapter']),
               '--genre', request['genre'], '--project-id', config['studioProjectId'],
               '--title', request['label']+' '+str(request['chapter'])+' WEB '+request['genre'],
               '--post-download', 'submit', '--publish-source', str(job/'source-verses.json'),
               '--artwork', str(job/'artwork.png')]
    if resume:
        command.append('--resume-post-download')
    else:
        command += ['--execute-one', '--request-json', str(job/'request.json')]
    command += ['--publisher', config['fastPublisher'], '--publisher-config', config['fastPublisherConfig']]
    return command


def check_dependencies(config, base):
    for field in ('modelPython', 'studioRunner', 'uploader', 'uploaderConfig', 'fastPublisher'):
        require(Path(config[field]).is_file(), 'MISSING_DEPENDENCY_'+field.upper())
    require(sha(config['studioRunner']) == config['studioRunnerSha256'], 'STUDIO_RUNNER_CHANGED')
    require(sha(Path(config['studioRunner']).with_name('studio_templates.py')) == config['studioTemplateSha256'], 'STUDIO_TEMPLATE_CHANGED')
    require(re.fullmatch(r'[a-f0-9-]{36}', config['studioProjectId']) is not None, 'INVALID_PROJECT')
    # Same existing uploader configuration for preparation, submission and status.
    fast = read(config['fastPublisherConfig'])
    require(fast['uploader'] == config['uploader'] and fast['uploaderConfig'] == config['uploaderConfig'], 'UPLOADER_CONFIG_MISMATCH')
    require(read(config['uploaderConfig'])['apiBaseUrl'].rstrip('/') == 'https://api.holysong.org', 'PRODUCTION_UPLOADER_REQUIRED')
    policy, story = read(fast['ownerPolicyFile']), read(fast['storyAuthorizationFile'])
    require(all(policy.get(k) is True for k in ('publicationApproved','contentApproved','timingsApproved','rightsApproved','exportApproved')), 'OWNER_AUTHORITY_REQUIRED')
    require(sha(fast['storyAuthorizationFile']) == policy['storyAuthorizationSha256'] and story.get('exportApproved') is True, 'STORY_AUTHORITY_REQUIRED')
    require(all(story.get(k) == v for k, v in STORY.items() if k != 'allowed'), 'STORY_AUTHORITY_INVALID')
    require(Path(base['geminiKeyFile']).is_file(), 'ARTWORK_CREDENTIAL_UNAVAILABLE')


def load_receipt(job):
    receipt = read(job/'studio/fast-publish-submit.receipt.json')
    require(receipt.get('status') == 'queued_not_live', 'UPLOAD_RECEIPT_INVALID')
    ready = [x for x in receipt['outputs'] if x.get('state') == 'ready_for_upload']
    jobs = [x for x in receipt['outputs'] if x.get('job_id')]
    require(len(ready) == len(jobs) == 1, 'UPLOAD_RECEIPT_INVALID')
    package, ack = ready[0], jobs[0]
    require(re.fullmatch(r'local_[a-f0-9]{32}', ack['job_id']) is not None, 'UPLOAD_JOB_INVALID')
    folder = Path(package['folder'])
    chapter = read(folder/'chapter.json')
    return package, ack, folder, chapter


def validate_status(status, ack, package):
    require(status.get('job_id') == ack['job_id'] and status.get('target') == 'production', 'UPLOAD_STATUS_IDENTITY_MISMATCH')
    require(status.get('status') not in ('failed','needs_review'), 'PUBLICATION_REJECTED')
    if status.get('statusCheckError') or status.get('status') != 'live':
        return False
    remote = status.get('remote', {})
    require(remote.get('status') == 'live' and remote.get('uploadCommitted') is True
            and remote.get('target') == 'production' and remote.get('jobId') == status.get('remote_id')
            and remote.get('packageSha256') == package['packageSha256'], 'LIVE_PUBLICATION_UNCONFIRMED')
    return True


def wait_live(config, request, job, adapter, seconds):
    package, ack, folder, chapter = load_receipt(job)
    for field, expected in [('bookId',request['book']),('chapterNumber',request['chapter']),
                            ('genreId',request['genre']),('editionId',request['edition'])]:
        require(chapter[field] == expected, 'PACKAGE_IDENTITY_MISMATCH')
    deadline = time.monotonic()+seconds
    while True:
        command = [config['uploader'], '--config', config['uploaderConfig'], 'status', ack['job_id']]
        done = subprocess.run(command, capture_output=True, text=True, timeout=70)
        require(done.returncode == 0, 'UPLOAD_STATUS_UNAVAILABLE')
        status = json.loads(done.stdout)
        atomic(job/'upload-status.json', status)
        if validate_status(status, ack, package):
            adapter.verification(request, job, {'package': {'folder':str(folder), 'chapter':chapter}})
            result = dict(success=True, status='live', book=request['book'], chapter=request['chapter'],
                          genre=request['genre'], jobId=ack['job_id'], recordingId=chapter['recordingId'],
                          runDirectory=str(job), humanReview='pending', finishedAt=utc())
            atomic(job/'RESULT.json', result)
            return result
        require(time.monotonic() < deadline, 'PUBLICATION_PENDING_RUN_SAME_COMMAND_TO_RESUME')
        time.sleep(min(5, max(0, deadline-time.monotonic())))


def run(config, book, chapter, genre, wait_seconds=600):
    base, recipe, request, job = inputs(config, book, chapter, genre)
    check_dependencies(config, base)
    with lock(job/'job.lock', nonblocking=True):
        saved = job/'identity.json'
        if saved.exists():
            require(read(saved) == request, 'JOB_INPUT_CHANGED_RECONCILE_BEFORE_RETRY')
        else:
            atomic(saved, request)
        artwork_state = private(config['artworkStateDirectory'], True)
        private(artwork_state/'locks', True)
        adapter_config = {**base, 'allowImageGeneration': True, 'stateDirectory': config['artworkStateDirectory']}
        adapter = Adapters(adapter_config)
        # If this job has already submitted, verify its exact publication instead of accepting an unrelated replacement.
        receipt = job/'studio/fast-publish-submit.receipt.json'
        if receipt.exists():
            progress('checking_publication')
            return wait_live(config, request, job, adapter, wait_seconds)
        progress('checking_catalog')
        existing, _ = adapter.existing(request, job, {})
        if existing['found']:
            result = dict(success=True, status='already_live', book=request['book'], chapter=chapter,
                          genre=genre, recordingId=existing['recordingId'], runDirectory=str(job), finishedAt=utc())
            atomic(job/'RESULT.json', result)
            return result
        progress('preparing_source')
        source_context, _ = adapter.source(request, job, {})
        source = read(job/'source-verses.json')
        require([v['verse'] for v in source['verses']] == list(range(1,len(source['verses'])+1)), 'UNSUPPORTED_OMITTED_VERSE_IDENTITIES')
        lyrics = (job/'lyrics.txt').read_text()
        require(len(lyrics) <= 5000, 'CHAPTER_EXCEEDS_PROVIDER_LIMIT')
        require(request['genre'] in read(read(config['fastPublisherConfig'])['ownerPolicyFile'])['genreIds'], 'GENRE_NOT_AUTHORIZED')
        body = prepare_request(job, request, recipe, lyrics)
        request_file = job/'request.json'
        if request_file.exists():
            require(read(request_file) == body, 'GENERATION_INPUT_CHANGED')
        else:
            atomic(request_file, body)
        progress('preparing_artwork')
        adapter.artwork(request, job, {'source':source_context})
        studio = job/'studio'
        resume = (studio/'result.json').is_file()
        attempt = job/'studio-attempt.json'
        failed = not resume and (studio.exists() or attempt.exists())
        # Reserve one provider lane per book; duplicate-job guards and shared job records stay unchanged.
        with lock(Path(config['coordinationDirectory'])/('suno-'+request['book']+'.lock'), nonblocking=True):
            # A saved response resumes the same IDs. An uncertain POST remains blocked
            # by repo_client's durable intent; never archive and generate again.
            progress('resuming_downloaded_song' if resume else 'generating_song')
            command = studio_command(config, request, job, resume)
            if not resume:
                atomic(attempt, dict(at=utc(), command=command, requestSha256=sha(request_file),
                                    sourceSha256=sha(job/'source-verses.json'), artworkSha256=sha(job/'artwork.png')))
            with (job/'studio.log').open('a') as log:
                done = subprocess.run(command, stdout=log, stderr=log, timeout=1100)
            if done.returncode != 0:
                captcha = 'STOP: CAPTCHA_REQUIRED;' in (job/'studio.log').read_text(errors='replace')
                code = ('SUNO_CAPTCHA_REQUIRED' if captcha else 'STUDIO_STOPPED_RUN_SAME_COMMAND_TO_RETRY'
                        if config.get('allowFailedStudioRestart') is True else 'STUDIO_STOPPED_CHECK_PRIVATE_LOG_AND_RESUME_WITHOUT_REGENERATING')
                raise Blocked(code)
        progress('waiting_for_publication')
        return wait_live(config, request, job, adapter, wait_seconds)


def main(argv=None):
    os.umask(0o077)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', type=Path, default=Path.home()/'.config/holysong/generate-publish/config.json')
    ap.add_argument('--book', required=True)
    ap.add_argument('--chapter', type=int, required=True)
    ap.add_argument('--genre', default='upbeat')
    ap.add_argument('--wait-seconds', type=int, default=600)
    args = ap.parse_args(argv)
    retry_allowed = False
    try:
        require(0 <= args.wait_seconds <= 3600, 'INVALID_WAIT_LIMIT')
        config = read(private(args.config))
        retry_allowed = config.get('allowFailedStudioRestart') is True
        result = run(config, args.book, args.chapter, args.genre, args.wait_seconds)
        print(json.dumps(result), flush=True)
        return 0
    except (Blocked, ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        # Do not echo provider exception bodies, credentials or signed download URLs.
        code = e.code if isinstance(e, Blocked) else type(e).__name__.upper()
        print(json.dumps(dict(success=False, status='stopped', code=code,
                              book=args.book, chapter=args.chapter, genre=args.genre,
                              recovery=('Resolve the error, then rerun this command. Failed Studio attempts may regenerate and use credits; old evidence is archived.'
                                        if retry_allowed else 'Run the same command after resolving the saved error; never delete a paid generation intent.'))), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
