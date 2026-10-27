"""Install only new -auto launchers; never edit the existing generic commands."""
from pathlib import Path
import argparse,hashlib,json,os,shlex

def main():
    os.umask(0o077);ap=argparse.ArgumentParser();ap.add_argument('--python',type=Path,required=True);args=ap.parse_args()
    root=Path(__file__).resolve().parent;binroot=Path.home()/'.local/bin';binroot.mkdir(parents=True,exist_ok=True)
    # Run the fork's pinned checkout. The operator can move to a new tested checkout explicitly.
    entries={'holysong-generate-publish-batch-generic-auto':'generate_publish_batch.py',
             'holysong-generate-publish-concurrent-generic-auto':'generic.py',
             'holysong-suno-account':'account_server.py'}
    before={}
    for name in ['holysong-generate-publish-batch-generic','holysong-generate-publish-concurrent-generic']:
        p=binroot/name
        if p.exists():before[name]=hashlib.sha256(p.read_bytes()).hexdigest()
    for name,entry in entries.items():
        body='#!/bin/sh\nexec '+shlex.quote(str(args.python.absolute()))+' '+shlex.quote(str(root/entry))+' "$@"\n'
        p=binroot/name;tmp=p.with_suffix('.auto.tmp');tmp.write_text(body);tmp.chmod(0o700);tmp.replace(p)
    for name,digest in before.items():
        assert hashlib.sha256((binroot/name).read_bytes()).hexdigest()==digest
    print(json.dumps({'installed':[str(binroot/n) for n in entries],'existingCommandHashes':before}))
if __name__=='__main__':main()
