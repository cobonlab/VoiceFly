#!/usr/bin/env python3
"""Download a pinned public model, verify it, and derive its public tokenizer."""
import argparse, hashlib, json, pathlib, subprocess, sys, tarfile, urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--archive',type=pathlib.Path,help='Use an already downloaded archive');args=parser.parse_args()
 manifest=json.loads((ROOT/'service/MODEL_MANIFEST.json').read_text())
 work=ROOT/'build/public-model';work.mkdir(parents=True,exist_ok=True)
 archive=args.archive or work/'public-model.tar.bz2'
 if not archive.exists():
  with urllib.request.urlopen(manifest['url']) as response,archive.open('wb') as out:
   while chunk:=response.read(1024*1024):out.write(chunk)
 if hashlib.sha256(archive.read_bytes()).hexdigest()!=manifest['archive_sha256']:raise ValueError('Public model archive hash mismatch')
 with tarfile.open(archive) as tar:tar.extractall(work,filter='data')
 model=work/manifest['directory']
 subprocess.run([sys.executable,str(ROOT/'service/lexicon/tools/extract_sp_vocab.py'),str(model/'bpe.model'),str(model/'bpe.vocab'),'--augment-cjk'],check=True)
 for name,digest in manifest['files'].items():
  if hashlib.sha256((model/name).read_bytes()).hexdigest()!=digest:raise ValueError('Public model file hash mismatch: '+name)
 print(model)
if __name__=='__main__':main()
