#!/usr/bin/env python3
"""Verify an extracted installer without installing or starting its apps."""
from pathlib import Path
import subprocess,plistlib,hashlib,re,json,sys
if len(sys.argv) != 4:
 raise SystemExit('Usage: verify_installer.py EXPANDED_PACKAGE PACKAGE_FILE REPORT_FILE')
root=Path(sys.argv[1]).resolve()/'VoiceFly-component.pkg/Payload'
apps=[root/'Library/Input Methods/VoiceFly.app',root/'Applications/Voice Fly.app',root/'Applications/Voice Fly Service.app']
magic={b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe',b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf'};counts=[]
for app in apps:
 subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
 n=0;symlinks=0
 for p in app.rglob('*'):
  if p.is_symlink():
   symlinks+=1;assert p.resolve().is_relative_to(app.resolve()),p;assert p.exists(),p;continue
  if not p.is_file():continue
  assert p.name not in ('personal_usage.json','voicefly_learned_phrases.txt','domain_dict.txt','direct_url.json'),p
  assert not any(x in p.parts for x in ('Backups','Logs','Caches','.git','.env')),p
  with p.open('rb') as f:m=f.read(4)
  if m not in magic:continue
  n+=1
  out=subprocess.check_output(['otool','-L',str(p)],text=True)
  for line in out.splitlines():
   if '(compatibility version ' not in line:continue
   dep=line.strip().split(' (')[0]
   assert not dep.startswith('/') or dep.startswith(('/usr/lib/','/System/Library/')), (p,dep)
  arch=subprocess.check_output(['lipo','-archs',str(p)],text=True).split();assert 'arm64' in arch,(p,arch)
  load=subprocess.check_output(['otool','-l',str(p)],text=True)
  for block in re.split(r'Load command \d+',load):
   if not re.search(r'cmd LC_(BUILD_VERSION|VERSION_MIN_MACOSX)\b',block):continue
   match=re.search(r'^\s+(?:minos|version) (\d+)\.(\d+)',block,re.M)
   if match:assert tuple(map(int,match.groups()))<=(26,4),(p,match.groups())
 info=plistlib.loads((app/'Contents/Info.plist').read_bytes());counts.append({'app':app.name,'identifier':info['CFBundleIdentifier'],'version':info['CFBundleShortVersionString'],'build':info['CFBundleVersion'],'native_binaries':n,'internal_symlinks':symlinks})
for script in ('preinstall','postinstall'):
 subprocess.run(['bash','-n',str(root.parent/'Scripts'/script)],check=True)
dist=(root.parent.parent/'Distribution').read_text();assert 'RequireLogout' not in dist;assert 'hostArchitectures="arm64"' in dist;assert 'min="26.4"' in dist
pkg=Path(sys.argv[2]).resolve()
release=json.loads((Path(__file__).resolve().parents[1]/'SOURCE_MANIFEST.json').read_text())
report={'version':release['version'],'date':release['date'],'package':pkg.name,'bytes':pkg.stat().st_size,'sha256':hashlib.sha256(pkg.read_bytes()).hexdigest(),'apps':counts,'external_non_system_load_links':0,'bundle_signature_verification':'ad-hoc signatures valid; no Developer ID/notarization','installer_scripts':'bash syntax passed','automatic_logout':False,'private_runtime_files':'absent','validation_scope':'Expanded-package and isolated-profile checks on maintainer macOS 26.6.2 Apple Silicon; independent first-install check pending'}
Path(sys.argv[3]).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
