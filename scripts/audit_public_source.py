"""Audit tracked source before a clean public export. Never read or print secrets."""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def audit():
    paths=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    failures=[]
    private_path=re.compile(r'^docs/screenshots/|(^|/)(secrets|data|backups|work|\.runtime|\.git|\.venv|\.build)(/|$)|(^|/)\.env$|Local\.xcconfig$|\.sqlite')
    private_text=re.compile(r'\.tail[a-z0-9]+\.ts\.net|/(?:Users|home)/[a-zA-Z0-9_-]+/|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b')
    fixed_card=re.compile(r'scoreCard\([^\n]*value:\s*"\d')
    for name in filter(None,paths):
        if private_path.search(name):failures.append((name,'private local file is tracked'));continue
        content=(ROOT/name).read_bytes()
        if b'\0' in content:continue
        try:text=content.decode()
        except UnicodeDecodeError:continue
        if private_text.search(text):failures.append((name,'personal path or Tailscale host'))
        if name.startswith('Sources/') and fixed_card.search(text):failures.append((name,'fixed physiological card'))
        if name.startswith('Sources/') and re.search(r'(?:mock|demo|fixture|synthetic).*?(?:hrv|strain|sleep)',text,re.I):failures.append((name,'review possible runtime sample data'))
    dashboard=json.loads((ROOT/'grafana/dashboards/whoop.json').read_text())
    for panel in dashboard['panels']:
        for target in panel.get('targets',[]):
            if re.search(r'\bvector\s*\(\s*[\d.]',target.get('expr','')):failures.append((panel['title'],'constant dashboard series'))
    for path,message in failures:print(f'FAIL {path}: {message}')
    if failures:return 1
    print(f'PASS: {len(list(filter(None,paths)))} tracked files; no private deployment paths, local data files, fixed score cards or constant dashboard series.')
    print('This is a focused source audit, not a complete credential or licensing audit. Run Gitleaks and review history separately.')
    return 0

if __name__=='__main__':sys.exit(audit())
