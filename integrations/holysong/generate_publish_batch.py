"""Sequential book/chapter loop around the existing concurrent command."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import termios
import tty
from batch_cleanup import cleanup_completed, resume_cleaned
from core import atomic, sha


def read(path):
    return json.loads(Path(path).read_text())


def plan(books, starting_book, starting_chapter, book_count):
    books = sorted(books, key=lambda book: book['order'])
    normalize = lambda value: re.sub(r'[ _]+', '-', value.strip().lower())
    matches = [i for i, book in enumerate(books)
               if normalize(starting_book) in (normalize(book['bookId']), normalize(book['label']))]
    if len(matches) != 1:
        raise ValueError('UNKNOWN_BOOK')
    start = matches[0]
    if not 1 <= starting_chapter <= books[start]['chapterCount']:
        raise ValueError('INVALID_STARTING_CHAPTER')
    if book_count < 1 or start + book_count > len(books):
        raise ValueError('INVALID_BOOK_COUNT')
    return [{'book': book['bookId'], 'chapter': chapter, 'genre': 'upbeat'}
            for i, book in enumerate(books[start:start + book_count])
            for chapter in range(starting_chapter if i == 0 else 1, book['chapterCount'] + 1)]


def continue_jobs(log_root, books, config_path):
    """Resume at the five-failure streak, otherwise the interrupted next chapter."""
    log_root = Path(log_root).resolve()
    pointer = log_root/'latest-batch.json'
    if pointer.is_symlink():
        raise ValueError('INVALID_BATCH_CHECKPOINT')
    if pointer.exists():
        saved = read(pointer)
        name = saved['directory']
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
            raise ValueError('INVALID_BATCH_CHECKPOINT')
        directory = log_root/name
        if directory.is_symlink() or directory.resolve().parent != log_root:
            raise ValueError('INVALID_BATCH_CHECKPOINT')
        if (directory/'plan.json').is_symlink() or (directory/'results.jsonl').is_symlink():
            raise ValueError('INVALID_BATCH_CHECKPOINT')
        if saved['configPath'] != str(Path(config_path).resolve()) or sha(directory/'plan.json') != saved['planSha256']:
            raise ValueError('BATCH_CHECKPOINT_CHANGED')
    else:
        # Existing batches predate -c; their saved plans/results are sufficient.
        candidates = [p for p in log_root.iterdir() if p.is_dir() and not p.is_symlink()
                      and (p/'plan.json').is_file() and not (p/'plan.json').is_symlink()
                      and (p/'results.jsonl').is_file() and not (p/'results.jsonl').is_symlink()] if log_root.is_dir() else []
        if not candidates:
            raise ValueError('NO_BATCH_TO_CONTINUE')
        directory = max(candidates, key=lambda p: (p/'plan.json').stat().st_mtime_ns)
    if (directory/'plan.json').is_symlink() or (directory/'results.jsonl').is_symlink():
        raise ValueError('INVALID_BATCH_CHECKPOINT')
    jobs = read(directory/'plan.json')
    chapter_order = {(book['bookId'], chapter): n
                     for n, (book, chapter) in enumerate((book, chapter)
                     for book in sorted(books, key=lambda b: b['order'])
                     for chapter in range(1, book['chapterCount'] + 1))}
    previous = -1
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= len(chapter_order):
        raise ValueError('INVALID_BATCH_PLAN')
    for job in jobs:
        if (not isinstance(job, dict) or set(job) != {'book','chapter','genre'} or not isinstance(job['book'], str)
                or type(job['chapter']) is not int or job['genre'] != 'upbeat'):
            raise ValueError('INVALID_BATCH_PLAN')
        position = chapter_order.get((job['book'], job['chapter']), -1)
        if position <= previous:
            raise ValueError('INVALID_BATCH_PLAN')
        previous = position
    ledger = directory/'results.jsonl'
    if not ledger.is_file():
        raise ValueError('INVALID_BATCH_RESULTS')
    rows = []
    lines = ledger.read_text().splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError:
            if i == len(lines)-1 and not line.endswith('\n'):
                break  # Interrupted append: leave that chapter unresolved.
            raise ValueError('INVALID_BATCH_RESULTS') from None
        if (i >= len(jobs) or not isinstance(row, dict) or type(row.get('success')) is not bool
                or any(row.get(k) != v for k,v in jobs[i].items())):
            raise ValueError('INVALID_BATCH_RESULTS')
        rows.append(row)
    start = len(rows)
    if len(rows) >= 5 and all(row['success'] is False for row in rows[-5:]):
        start -= 5
    pending = jobs[start:]
    return pending, str(directory)


def generic_cleanup_config(config_path, command):
    """Bind cleanup/resume to the account verified by the installed generic runner."""
    checked = subprocess.run([str(command), '--config', str(config_path), '--check'],
                             stdout=subprocess.PIPE, text=True, timeout=120)
    try:
        value = json.loads(checked.stdout.strip().splitlines()[-1])
        if checked.returncode != 0 or value.get('success') is not True or value.get('status') != 'account_and_project_verified':
            raise ValueError('GENERIC_ACCOUNT_PREFLIGHT_FAILED')
        folder = Path(value['stateDirectory'])
        fingerprint = value['accountFingerprint']
        if (not re.fullmatch('[a-f0-9]{24}', fingerprint)
                or not folder.is_absolute() or folder.resolve() != folder
                or folder.name != 'account-' + fingerprint or not folder.is_dir()):
            raise ValueError('GENERIC_ACCOUNT_STATE_INVALID')
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError('GENERIC_ACCOUNT_PREFLIGHT_INVALID') from None
    config = read(config_path)
    config['stateDirectory'] = str(folder)
    return config


def run_batch(jobs, command, config_path, log_root, cleanup_config=None):
    """One child at a time, no retries; stop after five consecutive failures."""
    log_root = Path(log_root)
    log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log_root.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    run_dir = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=log_root))
    atomic(run_dir/'plan.json', jobs)
    results = []
    config = cleanup_config if cleanup_config is not None else read(config_path)
    consecutive_failures = 0
    stop_reason = None
    print(json.dumps({'stage': 'batch_started', 'chapters': len(jobs), 'logs': str(run_dir)}), file=sys.stderr, flush=True)
    with (run_dir/'results.jsonl').open('x') as ledger:
        ledger.flush(); os.fsync(ledger.fileno())
        fd = os.open(run_dir, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        atomic(log_root/'latest-batch.json', dict(directory=run_dir.name,
               configPath=str(Path(config_path).resolve()), planSha256=sha(run_dir/'plan.json')))
        for number, job in enumerate(jobs, 1):
            print(json.dumps({'stage': 'batch_chapter', 'number': number, 'total': len(jobs), **job}), file=sys.stderr, flush=True)
            args = [str(command), '--config', str(config_path), '--book', job['book'],
                    '--chapter', str(job['chapter']), '--genre', job['genre']]
            # stderr stays attached to Terminal, preserving the existing live progress.
            resumed = resume_cleaned(config, job)
            child = None if resumed is not None else subprocess.run(args, stdout=subprocess.PIPE, text=True)
            raw = json.dumps(resumed) if resumed is not None else (child.stdout or '')
            exit_code = (0 if resumed['success'] else 1) if resumed is not None else child.returncode
            (run_dir/f'{number:04d}-{job["book"]}-{job["chapter"]}.stdout.log').write_text(raw)
            try:
                value = json.loads(raw.strip().splitlines()[-1])
                success = exit_code == 0 and value.get('success') is True
                code = value.get('code', value.get('status', 'UNRECOGNIZED_RESULT'))
            except (ValueError, IndexError, AttributeError):
                success, code = False, 'UNRECOGNIZED_RESULT'
            result = {**job, 'success': success, 'exitCode': exit_code, 'code': code}
            if success:
                result['cleanup'] = cleanup_completed(config, job, value)
            ledger.write(json.dumps(result) + '\n'); ledger.flush(); os.fsync(ledger.fileno())
            results.append(result)
            print(json.dumps({'stage': 'batch_chapter_finished', **result}), file=sys.stderr, flush=True)
            consecutive_failures = 0 if success else consecutive_failures + 1
            if consecutive_failures >= 5:
                stop_reason = 'FIVE_CONSECUTIVE_FAILURES'
                print(json.dumps({'stage': 'batch_stopped', 'code': stop_reason,
                                  'notAttempted': len(jobs) - len(results)}), file=sys.stderr, flush=True)
                break
    failed = [result for result in results if not result['success']]
    summary = {'success': not failed, 'total': len(results), 'succeeded': len(results) - len(failed),
               'failed': failed, 'logs': str(run_dir), 'planned': len(jobs),
               'notAttempted': len(jobs) - len(results), 'stopReason': stop_reason}
    (run_dir/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def loop_alert(song, terminal):
    """Replay until one key is pressed; always restore Terminal and stop audio."""
    fd = terminal.fileno()
    previous = termios.tcgetattr(fd)
    player = None
    try:
        tty.setcbreak(fd)
        while True:
            if player is None:
                player = subprocess.Popen(['/usr/bin/afplay', str(song)], stdin=subprocess.DEVNULL)
            if select.select([terminal], [], [], 0.2)[0]:
                os.read(fd, 1)
                break
            status = player.poll()
            if status is not None:
                if status != 0:
                    raise OSError('Audio player failed')
                player = None
    finally:
        try:
            if player is not None and player.poll() is None:
                player.terminate()
                try:
                    player.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    player.kill(); player.wait()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def play_failure_alert():
    """Loop the installed 5:09.6 recording until a Terminal keypress."""
    song = Path.home()/'.local/share/holysong-generate-publish-batches-generic-auto/failure-alert/genesis-18-upbeat.m4a'
    print(json.dumps({'stage': 'failure_alert', 'song': 'Genesis 18 · Upbeat',
                      'durationSeconds': 309.6, 'instruction': 'Press any key in this Terminal to stop the music and exit.'}), file=sys.stderr, flush=True)
    try:
        if not song.is_file():
            raise FileNotFoundError('Failure alert recording is missing')
        with open('/dev/tty', 'rb', buffering=0) as terminal:
            loop_alert(song, terminal)
    except OSError as error:
        # Audio failure must not discard the batch results or resume generation.
        print(json.dumps({'stage': 'failure_alert_unavailable', 'code': type(error).__name__}), file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Sequentially generate/publish Upbeat chapters. Book count includes the starting book.')
    parser.add_argument('starting_book', nargs='?')
    parser.add_argument('starting_chapter', type=int, nargs='?')
    parser.add_argument('book_count', type=int, nargs='?')
    parser.add_argument('-c', '--continue', dest='continue_latest', action='store_true', help='Restart at the five consecutive failures in the latest batch, then continue its remaining range')
    parser.add_argument('--dry-run', action='store_true', help='Print the range without generation, upload or new files')
    parser.add_argument('--test-alert', action='store_true', help='Only play the failure alert until any key is pressed; no generation or upload')
    parser.add_argument('--config', type=Path, default=Path.home()/'.config/holysong/generate-publish/config.json')
    args = parser.parse_args(argv)
    supplied_range = any(value is not None for value in (args.starting_book, args.starting_chapter, args.book_count))
    if args.test_alert and (args.dry_run or args.continue_latest or supplied_range):
        parser.error('--test-alert must be used without a chapter range or --dry-run')
    if args.continue_latest and supplied_range:
        parser.error('-c must be used without a chapter range')
    if not args.test_alert and not args.continue_latest and any(value is None for value in (args.starting_book, args.starting_chapter, args.book_count)):
        parser.error('starting_book, starting_chapter and book_count are required')
    try:
        if args.test_alert:
            play_failure_alert()
            return 0
        config = read(args.config)
        workflow = read(config['workflowConfig'])
        books = read(workflow['bookIndex'])['books']
        from account_server import profile_path
        profile_path() # Validate account name before using it as a path.
        log_root = Path.home()/'.local/share/holysong-generate-publish-batches-generic-auto'/os.environ.get('HOLYSONG_SUNO_ACCOUNT','main')
        if args.continue_latest:
            jobs, previous_batch = continue_jobs(log_root, books, args.config)
            print(json.dumps({'stage': 'continuing_batch', 'previousBatch': previous_batch, 'remainingChapters': len(jobs)}), file=sys.stderr, flush=True)
        else:
            jobs = plan(books, args.starting_book, args.starting_chapter, args.book_count)
        if args.dry_run:
            print(json.dumps({'dryRun': True, 'total': len(jobs), 'chapters': jobs}))
            return 0
        if not jobs:
            print(json.dumps({'success': True, 'status': 'batch_already_complete', 'remainingChapters': 0}))
            return 0
        command = Path.home()/'.local/bin/holysong-generate-publish-concurrent-generic-auto'
        if not command.is_file() or not os.access(command, os.X_OK):
            raise ValueError('CONCURRENT_COMMAND_NOT_INSTALLED')
        cleanup_config = generic_cleanup_config(args.config, command)
        result = run_batch(jobs, command, args.config, log_root, cleanup_config=cleanup_config)
        print(json.dumps(result), flush=True)
        if result.get('stopReason') == 'FIVE_CONSECUTIVE_FAILURES':
            play_failure_alert()
        return 0 if result['success'] else 1
    except KeyboardInterrupt:
        print(json.dumps({'success': False, 'code': 'INTERRUPTED', 'recovery': 'Run holysong-generate-publish-batch-generic-auto -c to continue the saved batch.'}))
        return 130
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'success': False, 'code': str(error) if isinstance(error, ValueError) else type(error).__name__}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
