"""Remove only published chapter media; retain all paid-attempt/resume records."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
from adapters import Adapters
from core import Blocked, atomic, canonical, digest as hash_bytes, lock, read, require, sha, utc


def exact_path(path):
    path = Path(path).absolute()
    require(path.resolve() == path, 'CLEANUP_SYMLINK_REFUSED')
    return path


def job_root(config, job):
    require(set(job) == {'book', 'chapter', 'genre'}, 'CLEANUP_JOB_INVALID')
    root = exact_path(config['stateDirectory'])
    folder = exact_path(root/(job['book']+'-'+str(job['chapter'])+'-'+job['genre']))
    require(folder.parent == root, 'CLEANUP_JOB_PATH_INVALID')
    return folder


def identity_for(folder, job):
    identity = read(exact_path(folder/'identity.json'))
    require(all(identity.get(key) == value for key, value in job.items())
            and identity.get('target') == 'production', 'CLEANUP_IDENTITY_MISMATCH')
    return identity


def file_pin(path):
    exact_path(path)
    before = path.stat()
    digest = sha(path)
    after = path.stat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
    require(all(getattr(before, k) == getattr(after, k) for k in fields), 'CLEANUP_FILE_CHANGED')
    return dict(sha256=digest, device=after.st_dev, inode=after.st_ino, bytes=after.st_size, mtimeNs=after.st_mtime_ns)


def resume_cleaned(config, job):
    """A cleaned paid job may only return after a fresh existing-live check."""
    if not config.get('stateDirectory'):
        return None
    try:
        folder = job_root(config, job)
        marker = folder/'local-media-cleanup.json'
        if not marker.exists() and not marker.is_symlink():
            return None
        with lock(exact_path(folder/'job.lock'), nonblocking=True):
            identity = identity_for(folder, job)
            saved = read(exact_path(marker))
            require(saved['identitySha256'] == sha(folder/'identity.json'), 'CLEANUP_IDENTITY_CHANGED')
            base = read(config['workflowConfig'])
            existing, _ = Adapters(base).existing(identity, folder, {})
            require(existing.get('found') and existing['recordingId'] == saved['recordingId']
                    and existing['bundleRevision'] == saved['bundleRevision'], 'CLEANED_RECORDING_NOT_LIVE')
            return dict(success=True, status='already_live', **job, recordingId=existing['recordingId'],
                        runDirectory=str(folder), localMediaCleaned=True)
    except (Blocked, OSError, ValueError, KeyError, TypeError) as error:
        return dict(success=False, status='stopped', **job,
                    code=error.code if isinstance(error, Blocked) else 'CLEANED_JOB_CHECK_FAILED')


def cleanup_completed(config, job, result):
    """Validate every directory/receipt before unlinking a fixed list of files."""
    if not result.get('runDirectory'):
        return {'status': 'not_applicable'}
    try:
        require(result.get('success') is True and result.get('status') in ('live', 'already_live'), 'CLEANUP_REQUIRES_LIVE')
        folder = job_root(config, job)
        require(exact_path(result['runDirectory']) == folder, 'CLEANUP_RUN_PATH_MISMATCH')
        with lock(exact_path(folder/'job.lock'), nonblocking=True):
            identity = identity_for(folder, job)
            marker = folder/'local-media-cleanup.json'
            previous = read(exact_path(marker)) if marker.exists() or marker.is_symlink() else None
            if previous:
                require(previous['identitySha256'] == sha(folder/'identity.json')
                        and previous['recordingId'] == result['recordingId'], 'CLEANUP_IDENTITY_CHANGED')
                if previous.get('completed') is True:
                    return {'status': 'already_cleaned'}
            receipt_path = folder/'studio/fast-publish-submit.receipt.json'
            if not receipt_path.exists():
                return {'status': 'not_applicable', 'reason': 'No local upload receipt'}
            receipt = read(exact_path(receipt_path))
            packages = [item for item in receipt['outputs'] if item.get('state') == 'ready_for_upload']
            uploads = [item for item in receipt['outputs'] if item.get('job_id')]
            require(len(packages) == len(uploads) == 1, 'CLEANUP_RECEIPT_INVALID')
            package_info, upload = packages[0], uploads[0]
            require(result['status'] != 'live' or result.get('jobId') == upload['job_id'], 'CLEANUP_UPLOAD_IDENTITY_MISMATCH')
            digest = package_info['packageSha256']
            require(re.fullmatch('[a-f0-9]{64}', digest) is not None and upload['job_id'] == 'local_'+digest[:32], 'CLEANUP_DIGEST_INVALID')
            fast = read(config['fastPublisherConfig'])
            package = exact_path(package_info['folder'])
            require(package.parent == exact_path(fast['stateDirectory']), 'CLEANUP_PACKAGE_PATH_INVALID')
            chapter = read(exact_path(package/'chapter.json'))
            require(chapter['recordingId'] == result['recordingId'] == package_info['recordingId']
                    and chapter['editionId'] == identity['edition']
                    and all(chapter[key] == job[field] for key, field in
                            [('bookId', 'book'), ('chapterNumber', 'chapter'), ('genreId', 'genre')]), 'CLEANUP_PACKAGE_IDENTITY_MISMATCH')
            require(all(chapter['files'][role] == name for role, name in [('audio', 'audio.m4a'), ('artwork', 'artwork.png')]), 'CLEANUP_ASSET_PATH_INVALID')
            proof = read(exact_path(folder/('existing-live-verification.json' if result['status'] == 'already_live' else 'live-verification.json')))
            require(proof.get('verified') is True and proof['recordingId'] == result['recordingId']
                    and proof['bundleRevision'] == chapter['bundleRevision'], 'CLEANUP_LIVE_PROOF_REQUIRED')
            require(result['status'] == 'already_live' or proof.get('productionVerified') is True, 'CLEANUP_PRODUCTION_PROOF_REQUIRED')
            uploader = read(config['uploaderConfig'])
            spool = exact_path(uploader['spoolDirectory'])
            database = exact_path(spool/'jobs.sqlite3')
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as connection:
                row = connection.execute('SELECT digest,target,status,snapshot,request FROM jobs WHERE job_id=?', (upload['job_id'],)).fetchone()
            require(row is not None and row[:3] == (digest, 'production', 'live'), 'CLEANUP_UPLOAD_NOT_LIVE')
            request = json.loads(row[4])
            require(request.get('packageSha256') == digest
                    and hash_bytes(canonical({k:v for k,v in request.items() if k != 'packageSha256'})) == digest
                    and request['target'] == 'production' and request['chapter'] == chapter,
                    'CLEANUP_REQUEST_BINDING_INVALID')
            records = {item['role']:item for item in request['files']}
            require(all(records[role]['filename'] == chapter['files'][role] for role in ('audio', 'artwork')),
                    'CLEANUP_ASSET_PATH_INVALID')
            expected_assets = {role:records[role]['sha256'] for role in ('audio', 'artwork')}
            review = read(exact_path(package/'review.json'))
            require(records['review']['filename'] == 'review.json' and sha(package/'review.json') == records['review']['sha256']
                    and sha(exact_path(package/'review-evidence.json')) == review['evidenceSha256'], 'CLEANUP_REVIEW_BINDING_INVALID')
            evidence = read(package/'review-evidence.json')
            original_sha = read(exact_path(folder/'studio/result.json'))['sha256']
            require(evidence['audio']['originalSha256'] == original_sha
                    and evidence['audio']['aacSha256'] == expected_assets['audio'], 'CLEANUP_ORIGINAL_BINDING_INVALID')
            snapshot = exact_path(row[3])
            require(snapshot == exact_path(spool/'snapshots'/digest), 'CLEANUP_SNAPSHOT_PATH_INVALID')
            targets = [folder/'artwork.png', folder/'studio/studio-export.m4a',
                       package/'audio.m4a', package/'artwork.png', package/'evidence/original-audio.m4a',
                       snapshot/'audio.m4a', snapshot/'artwork.png']
            validated = exact_path(package.with_name(package.name+'-validated'))
            if validated.exists():
                manifest = read(exact_path(validated/'manifest.json'))
                require(all(manifest[k] == chapter[k] for k in ('recordingId', 'bundleRevision', 'editionId')),
                        'CLEANUP_VALIDATED_IDENTITY_MISMATCH')
                require(all(next(a['sha256'] for a in manifest['assets'] if a['role'] == role) == expected_assets[role]
                            for role in ('audio', 'artwork')), 'CLEANUP_VALIDATED_ASSET_MISMATCH')
                targets += [validated/'audio.m4a', validated/'artwork.png']
            # Validate the entire list before any unlink; never recurse or delete directories.
            for path in targets:
                exact_path(path)
                require(not path.exists() or path.is_file(), 'CLEANUP_NON_FILE_REFUSED')
            if previous:
                require(previous['packageSha256'] == digest and previous['bundleRevision'] == chapter['bundleRevision']
                        and previous['targets'] == [str(p) for p in targets], 'CLEANUP_BINDING_CHANGED')
                saved = previous
            else:
                saved = dict(identitySha256=sha(folder/'identity.json'), recordingId=result['recordingId'],
                             bundleRevision=chapter['bundleRevision'], packageSha256=digest,
                             targets=[str(p) for p in targets], completed=False, at=utc(),
                             files={str(p): file_pin(p) for p in targets if p.exists()})
                for path in targets:
                    if path.exists():
                        expected = (original_sha if path.name in ('studio-export.m4a', 'original-audio.m4a')
                                    else expected_assets['artwork' if path.suffix == '.png' else 'audio'])
                        require(saved['files'][str(path)]['sha256'] == expected, 'CLEANUP_ASSET_CHANGED')
            for path in targets:
                if path.exists():
                    require(str(path) in saved['files'] and file_pin(path) == saved['files'][str(path)], 'CLEANUP_FILE_CHANGED')
            atomic(marker, saved)
            removed, freed = 0, 0
            for path in targets:
                if path.exists():
                    require(file_pin(path) == saved['files'][str(path)], 'CLEANUP_FILE_CHANGED')
                    size = path.stat().st_size
                    path.unlink(); removed += 1; freed += size
            atomic(marker, {**saved, 'completed': True, 'removedFiles': removed, 'removedBytes': freed})
            return {'status': 'cleaned', 'removedFiles': removed, 'removedBytes': freed}
    except (Blocked, OSError, ValueError, KeyError, TypeError, StopIteration, sqlite3.Error) as error:
        return {'status': 'retained', 'code': error.code if isinstance(error, Blocked) else 'CLEANUP_CHECK_FAILED'}
