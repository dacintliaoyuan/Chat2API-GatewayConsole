"""Audit the source upload tree without printing secret values."""
from __future__ import annotations
import hashlib
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {'.git','node_modules','__pycache__','dist','.venv'}
PRIVATE_DIRS = {'data','tmp','output','release','.mimosa','.zcode','.edge-profile','edge-profile'}
PRIVATE_SUFFIXES = {'.log','.jsonl','.db','.sqlite','.sqlite3','.pid','.bak','.lock'}
PRIVATE_NAMES = {'token.json','admin-control-token','usage-session-code','share-attribution.key'}
MANIFEST = 'export-manifest.json'
TEXT_SUFFIXES = {'.py','.ps1','.bat','.vbs','.cs','.ts','.js','.mjs','.json','.md','.txt','.css','.html','.yml','.yaml'}
RULES = {
    'personal_path': re.compile(r'[CD]:[\\/](?:Users[\\/](?!Public)|git[\\/]github[\\/]personal_files)',re.I),
    'private_identity': re.compile(r'\b(?:DAICNT|dacintliaoyuan|um_share_d4)\b',re.I),
    'credential_prefix': re.compile(r'\b(?:ghp_|github_pat_|sk-proj-|sk-ant-)[A-Za-z0-9_-]{20,}'),
    'jwt_value': re.compile(r'\beyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}'),
}

def inspect():
    errors=[]; files=[]
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file(): continue
        relative=path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts): continue
        key=relative.as_posix()
        if (any(part in PRIVATE_DIRS for part in relative.parts) or
                path.suffix.lower() in PRIVATE_SUFFIXES or path.name in PRIVATE_NAMES or
                (path.name.startswith('.env') and path.name!='.env.example')):
            errors.append({'path':key,'rule':'runtime_or_secret_file'}); continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            try: content=path.read_text(encoding='utf-8-sig')
            except UnicodeError:
                errors.append({'path':key,'rule':'unrecognized_text_encoding'}); continue
            for name, pattern in RULES.items():
                if path.resolve()==Path(__file__).resolve(): continue
                if pattern.search(content): errors.append({'path':key,'rule':name})
        files.append({'path':key,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    return files, errors

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest',action='store_true',help='Write clean source hashes after the audit passes')
    args=parser.parse_args()
    files, errors=inspect()
    print(json.dumps({'source_files':len(files),'findings':errors},ensure_ascii=False,indent=2))
    if args.write_manifest and not errors:
        document={'schemaVersion':1,'scope':{'gateways':['chat2api'],'consumers':['ZCode']},
                  'files':[entry for entry in files if entry['path']!=MANIFEST]}
        (ROOT/MANIFEST).write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    raise SystemExit(bool(errors))
