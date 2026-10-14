"""Create private deployment and login files; print paths, never passwords."""
import json,secrets
from pathlib import Path

def main():
    state=Path('.state');vault=Path('.secrets')
    for d in (state,vault):d.mkdir(exist_ok=True,mode=0o700)
    target=state/'deployment.json'
    if not target.exists():
        target.write_text(Path('examples/deployment.example.json').read_text(),encoding='utf-8');target.chmod(0o600)
    web=vault/'web.json'
    if not web.exists():
        web.write_text(json.dumps({'ADMIN_PASSWORD':secrets.token_urlsafe(24),'SESSION_SECRET':secrets.token_urlsafe(48),
                                  'CLOUDOPS_BRIDGE_SECRET':secrets.token_urlsafe(32)},indent=2));web.chmod(0o600)
    print('Edit .state/deployment.json; login values are in private .secrets/web.json')

if __name__=='__main__':main()
