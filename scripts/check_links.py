"""Check repository-relative Markdown links without network or credentials."""
import re
from pathlib import Path

def main():
    root=Path(__file__).resolve().parents[1]
    errors=[];count=0
    for path in [root/'README.md',root/'handoff.md',root/'CONTRIBUTING.md',root/'SECURITY.md',*sorted((root/'docs').glob('*.md'))]:
        for target in re.findall(r'\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
            if '://' in target or target.startswith('#'):continue
            count+=1
            if not (path.parent/target.split('#')[0]).is_file():errors.append(str(path.relative_to(root))+': '+target)
    if errors:raise SystemExit('\n'.join(errors))
    print('Repository links passed:',count)

if __name__=='__main__':main()
