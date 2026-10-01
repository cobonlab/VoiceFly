#!/usr/bin/env python3
"""Package only the three independently built public applications."""
import argparse, json, pathlib, plistlib, shutil, subprocess, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
APPS = [
 (ROOT / 'input-method/build/Build/Products/Release/VoiceFly.app', 'Library/Input Methods/VoiceFly.app', 'com.simonli.inputmethod.VoiceFly'),
 (ROOT / 'dist/Voice Fly.app', 'Applications/Voice Fly.app', 'com.simonli.voicefly.settings'),
 (ROOT / 'dist/Voice Fly Service.app', 'Applications/Voice Fly Service.app', 'com.simonli.voicefly.service'),
]
def run(*args): subprocess.run(list(map(str,args)),check=True)
def build(appearance=False,hot_appearance=False):
 appearance=appearance or hot_appearance
 work=ROOT/('build/hot-appearance-installer' if hot_appearance else 'build/appearance-installer' if appearance else 'build/installer')
 if work.exists():shutil.rmtree(work)
 payload=work/'payload';payload.mkdir(parents=True)
 frontend_info=plistlib.loads((APPS[0][0]/'Contents/Info.plist').read_bytes())
 package_version=f"{frontend_info['CFBundleShortVersionString']}.{frontend_info['CFBundleVersion']}"
 for source,target,identity in (APPS[:2] if appearance else APPS):
  info=plistlib.loads((source/'Contents/Info.plist').read_bytes())
  if info['CFBundleIdentifier'] != identity:raise ValueError('Unexpected application identity')
  run('codesign','--verify','--deep','--strict',source)
  if subprocess.check_output(['lipo','-archs',str(source/'Contents/MacOS'/info['CFBundleExecutable'])],text=True).strip()!='arm64':raise ValueError('This preview is Apple Silicon only')
  destination=payload/target;destination.parent.mkdir(parents=True,exist_ok=True)
  run('/usr/bin/ditto','--rsrc','--extattr',source,destination)
  if not appearance and identity == 'com.simonli.inputmethod.VoiceFly':
   run(sys.executable, ROOT/'scripts/bundle_portable_frontend.py', destination)
 component=work/'components.plist'
 run('pkgbuild','--analyze','--root',payload,component)
 items=plistlib.loads(component.read_bytes())
 for item in items:item['BundleIsRelocatable']=False;item['BundleIsVersionChecked']=False
 component.write_bytes(plistlib.dumps(items))
 pkg=work/'VoiceFly-component.pkg'
 scripts=ROOT/('packaging/appearance-scripts' if appearance else 'packaging/scripts')
 identity='com.simonli.voicefly.appearance' if appearance else 'com.simonli.voicefly.installer'
 if hot_appearance or not appearance:
  scripts=work/'scripts';shutil.copytree(ROOT/('packaging/hot-appearance-scripts' if hot_appearance else 'packaging/scripts'),scripts)
  probe=scripts/'voicefly-update-probe'
  run('swiftc','-module-cache-path','/private/tmp/voicefly-swift-cache',ROOT/'scripts/update_probe.swift','-o',probe)
  run('codesign','--force','--sign','-',probe)
  if hot_appearance:identity='com.simonli.voicefly.hot-appearance'
 run('pkgbuild','--root',payload,'--component-plist',component,'--scripts',scripts,'--identifier',identity,'--version',package_version,'--install-location','/',pkg)
 distribution=work/'Distribution.xml'
 distribution_text='''<?xml version="1.0" encoding="UTF-8"?>
<installer-gui-script minSpecVersion="2">
<title>Voice Fly</title><options customize="never" require-scripts="true" hostArchitectures="arm64"/>
<welcome file="welcome.html" mime-type="text/html"/>
<conclusion file="conclusion.html" mime-type="text/html"/>
<allowed-os-versions><os-version min="26.4"/></allowed-os-versions>
<choices-outline><line choice="default"/></choices-outline>
<choice id="default" visible="false"><pkg-ref id="com.simonli.voicefly.installer"/></choice>
<pkg-ref id="com.simonli.voicefly.installer" version="PACKAGE_VERSION" onConclusion="none">VoiceFly-component.pkg</pkg-ref>
</installer-gui-script>'''
 distribution_text=distribution_text.replace('PACKAGE_VERSION',package_version)
 if appearance:
  distribution_text=distribution_text.replace('com.simonli.voicefly.installer',identity).replace('conclusion.html','appearance.html')
 if hot_appearance:
  distribution_text=distribution_text.replace('file="appearance.html"','file="hot-appearance.html"').replace('onConclusion="RequireLogout"','onConclusion="none"')
 distribution.write_text(distribution_text)
 release=json.loads((ROOT/'SOURCE_MANIFEST.json').read_text())['version']
 output=ROOT/('dist/VoiceFly-HotLogo-0.2.0-arm64.pkg' if hot_appearance else 'dist/VoiceFly-Appearance-0.2.0-arm64.pkg' if appearance else f'dist/VoiceFly-{release}-arm64.pkg')
 run('productbuild','--distribution',distribution,'--resources',ROOT/'packaging/resources','--package-path',work,output)
 print(output)
if __name__=='__main__':
 parser=argparse.ArgumentParser();group=parser.add_mutually_exclusive_group()
 group.add_argument('--appearance',action='store_true',help='Frontend/settings update with logout; requires an existing installation')
 group.add_argument('--hot-appearance',action='store_true',help='Existing-install no-logout trial; requires idle speech service and ABC selected')
 args=parser.parse_args();build(args.appearance,args.hot_appearance)
