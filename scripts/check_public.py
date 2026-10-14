"""Scan files intended for publication, without echoing potentially sensitive text."""
import argparse,re,subprocess
from pathlib import Path

PATTERNS={
 'AWS access key':re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
 'private key':re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
 'account identifier':re.compile(r'\b\d{12}\b'),
 'personal workspace':re.compile(r'C:\\Users\\|/home/ubuntu/Qcli/|codex-cli|CodingWorkspace|/api/coding'),
 'IPv4 address':re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
}

def main():
    p=argparse.ArgumentParser();p.add_argument('--tree',action='store_true');args=p.parse_args()
    if args.tree:
        paths=[f for f in Path('.').rglob('*') if f.is_file() and not any(x in f.parts for x in ('.git','.venv','.state','.secrets','node_modules','dist','__pycache__','.pytest_cache')) and f.suffix!='.tsbuildinfo']
    else:
        paths=[Path(s) for s in subprocess.check_output(['git','ls-files'],text=True).splitlines()]
    failures=[]
    for path in paths:
        text=path.read_text(encoding='utf-8',errors='replace')
        for rule,pattern in PATTERNS.items():
            # The scanner's own forbidden literals are not leaked application data.
            if path.as_posix()=='scripts/check_public.py' and rule=='personal workspace':continue
            matches=list(pattern.finditer(text))
            if rule=='IPv4 address':matches=[m for m in matches if m.group() not in ('127.0.0.1','0.0.0.0')]
            if matches:failures.append({'file':path.as_posix(),'rule':rule,'count':len(matches)})
    for f in failures:print(f['file'],f['rule'],f['count'])
    if failures:raise SystemExit('Public scan failed; inspect files locally')
    print('Public scan passed:',len(paths),'files')

if __name__=='__main__':main()
