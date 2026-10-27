"""Durable deterministic orchestration; no provider calls in this module."""
from __future__ import annotations
import contextlib, datetime, fcntl, hashlib, json, os, re, sqlite3, tempfile, time
from pathlib import Path

VERSION = 'chapter-workflow-v1'
EDITION = 'web-updated-us-lord'
STAGES = ('existing', 'source', 'generation', 'audio', 'evaluation', 'synchronization', 'artwork', 'package', 'publication', 'verification')

class Blocked(Exception):
    def __init__(self, code, *, pending=False):
        self.code, self.pending = code, pending
        super().__init__(code)

def require(ok, code):
    if not ok: raise Blocked(code)

def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
def digest(raw): return hashlib.sha256(raw).hexdigest()
def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()
def read(path): return json.loads(Path(path).read_text())
def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def atomic(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd,name=tempfile.mkstemp(dir=path.parent, prefix='.'+path.name)
    try:
        with os.fdopen(fd,'wb') as f: f.write(canonical(value)+b'\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)

def private(path, directory=False):
    path=Path(path).expanduser().absolute()
    if directory: path.mkdir(mode=0o700,parents=True,exist_ok=True)
    require(not path.is_symlink() and path.stat().st_uid==os.getuid() and not path.stat().st_mode&0o077, 'PRIVATE_PATH_REQUIRED')
    return path.resolve()

@contextlib.contextmanager
def lock(path, nonblocking=False):
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|(fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError: raise Blocked('JOB_ALREADY_RUNNING',pending=True) from None
        yield
    finally: os.close(fd)

def identity(book, chapter, genre, recipe, target, books):
    book=re.sub(r'[ _]+','-',book.strip().lower()); genre=genre.strip().lower()
    match=next((b for b in books if b['bookId']==book or b['label'].lower().replace(' ','-')==book),None)
    require(match is not None and type(chapter) is int and 1<=chapter<=match['chapterCount'], 'INVALID_BOOK_OR_CHAPTER')
    require(genre in ('upbeat','peaceful','blues') and recipe.get('id')==genre and recipe.get('accepted') is True, 'GENRE_RECIPE_NOT_ACCEPTED')
    require(target in ('staging','production'), 'INVALID_TARGET')
    return dict(book=match['bookId'],chapter=chapter,label=match['label'],genre=genre,edition=EDITION,
                recipeSha256=digest(canonical(recipe)),target=target,workflowVersion=VERSION)

class Ledger:
    def __init__(self, root):
        self.root=private(root,True)
        for d in ('jobs','locks'): private(self.root/d,True)
        self.db=self.root/'jobs.sqlite3'
        require(not self.db.is_symlink(),'INVALID_DATABASE')
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL, stage TEXT, reason TEXT, updated REAL NOT NULL)')
        os.chmod(self.db,0o600)
    @contextlib.contextmanager
    def connect(self):
        c=sqlite3.connect(self.db,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA synchronous=FULL')
        try:
            with c: yield c
        finally: c.close()
    def admit(self, request):
        ident='chapter_'+digest(canonical(request))[:32]
        private(self.root/'jobs'/ident,True)
        fd=os.open(self.root/'jobs',os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for prior in c.execute('SELECT id,request FROM jobs'):
                old=json.loads(prior['request'])
                fields=('book','chapter','genre','edition','target')
                if all(old.get(k)==request.get(k) for k in fields) and bool(old.get('importManifest'))==bool(request.get('importManifest')):
                    require(prior['id']==ident,'EXISTING_JOB_REQUEST_CHANGED_RESUME_OR_EXPLICIT_REVISION')
            c.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?)',(ident,canonical(request).decode(),'queued',None,None,time.time()))
        (self.root/'jobs'/ident).mkdir(mode=0o700,exist_ok=True)
        return ident
    def update(self, ident, status, stage=None, reason=None):
        with self.connect() as c: c.execute('UPDATE jobs SET status=?,stage=?,reason=?,updated=? WHERE id=?',(status,stage,reason,time.time(),ident))
    def result(self, ident):
        with self.connect() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
        require(row is not None,'JOB_NOT_FOUND')
        return dict(success=row['status']=='live',jobId=ident,status=row['status'],stage=row['stage'],reason=row['reason'])
    def jobs(self):
        with self.connect() as c: return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE status IN ('queued','running','pending') ORDER BY updated")]

class Runner:
    def __init__(self, ledger, adapter): self.ledger,self.adapter=ledger,adapter
    def run(self, ident, stop_before=None):
        require(re.fullmatch(r'chapter_[a-f0-9]{32}',ident) is not None,'INVALID_JOB_ID')
        root=self.ledger.root/'jobs'/ident
        try:
            with lock(self.ledger.root/'locks'/(ident+'.lock'),True):
                with self.ledger.connect() as c: row=c.execute('SELECT * FROM jobs WHERE id=?',(ident,)).fetchone()
                require(row is not None,'JOB_NOT_FOUND'); request=json.loads(row['request'])
                root=private(root,True)
                context={}
                for stage in STAGES:
                    if stage==stop_before:
                        self.ledger.update(ident,'blocked',stage,'DRY_RUN_STOP'); return self.ledger.result(ident)
                    self.ledger.update(ident,'running',stage)
                    path=root/(stage+'.json')
                    if path.exists() and stage not in ('existing','verification'):
                        receipt=read(path)
                        require(receipt['requestSha256']==digest(canonical(request)), 'RECEIPT_IDENTITY_CHANGED')
                        for record in receipt['files']:
                            p=root/record['path']; require(p.resolve().is_relative_to(root.resolve()) and not p.is_symlink() and sha(p)==record['sha256'],'SAVED_ARTIFACT_CHANGED')
                        context[stage]=receipt['result']; continue
                    result,files=self.adapter.execute(stage,request,root,context)
                    records=[]
                    for p in files:
                        p=Path(p).resolve(); require(p.is_relative_to(root.resolve()),'ARTIFACT_OUTSIDE_JOB')
                        records.append(dict(path=str(p.relative_to(root)),sha256=sha(p)))
                    # Adapter outputs become reusable only after exact-file receipt commit.
                    atomic(path,dict(requestSha256=digest(canonical(request)),result=result,files=records,completedAt=utc()))
                    context[stage]=result
                    if stage=='existing' and result.get('verified') is True:
                        self.ledger.update(ident,'live','existing'); return self.ledger.result(ident)
                require(context['verification'].get('productionVerified') is True if request['target']=='production' else context['verification'].get('verified') is True, 'LIVE_NOT_VERIFIED')
                self.ledger.update(ident,'live','verification')
        except Blocked as e:
            if e.code=='JOB_ALREADY_RUNNING': return dict(success=False,jobId=ident,status='running',reason=e.code)
            self.ledger.update(ident,'pending' if e.pending else 'blocked',locals().get('stage'),e.code)
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            self.ledger.update(ident,'blocked',locals().get('stage'),'LOCAL_OPERATION_FAILED')
        result=self.ledger.result(ident); atomic(root/'result.json',result); return result
