# Public bundle allowlist. The maintainer's vocabulary and caches are excluded.
import os
import importlib.metadata
import sysconfig
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPEC).resolve().parent
MODEL = Path(os.environ["VOICEFLY_MODEL_DIR"]).resolve()
datas, binaries, hiddenimports = [], [], []
for package in ("sherpa_onnx", "pynini", "tn", "itn"):
    package_data, package_binaries, package_hidden = collect_all(package)
    datas += package_data; binaries += package_binaries; hiddenimports += package_hidden
for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt", "bpe.vocab"):
    if not (MODEL / name).is_file():
        raise FileNotFoundError("Missing required public model asset: " + name)
    datas.append((str(MODEL / name), "models/streaming-zipformer"))
for name in ("hotwords.txt", "spoken_number_exceptions.txt"):
    datas.append((str(ROOT / "public-resources" / name), "lexicon"))
datas.append((str(ROOT / "MODEL_MANIFEST.json"), "."))
# Keep distribution notices; omit local pip acquisition paths and unused tests.
datas = [(source, destination) for source, destination in datas
         if Path(source).name != "direct_url.json" and "/test/" not in source]
for package in ("numpy", "sounddevice", "sherpa-onnx", "pynini", "wetextprocessing",
                "pyobjc-framework-Cocoa", "cffi", "pyinstaller"):
    distribution = importlib.metadata.distribution(package)
    for file in distribution.files or []:
        if "/licenses/" in str(file):
            source = distribution.locate_file(file)
            datas.append((str(source), "ThirdPartyLicenses/" + package + "/" + str(Path(str(file).split("/licenses/", 1)[1]).parent)))
datas.append((str(Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"), "ThirdPartyLicenses/python"))
datas.append((str(ROOT.parent / "LICENSE"), "ThirdPartyLicenses/voicefly"))
for notice in (ROOT.parent / "third-party/binary-notices").glob("*/*"):
    if notice.is_file():
        datas.append((str(notice), "ThirdPartyLicenses/" + notice.parent.name))
a = Analysis([str(ROOT / "voice_fly_service.py")], pathex=[str(ROOT)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, noarchive=False)
# Package hooks can re-add metadata after the initial allowlist is prepared.
a.datas = [entry for entry in a.datas if Path(entry[0]).name != "direct_url.json"]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VoiceFlyService", debug=False,
          strip=False, upx=False, console=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VoiceFlyService")
app = BUNDLE(coll, name="Voice Fly Service.app", bundle_identifier="com.simonli.voicefly.service",
             version="0.2.0", info_plist={
                 "CFBundleDisplayName": "Voice Fly 语音服务", "CFBundleName": "Voice Fly Service",
                 "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "23", "LSUIElement": True,
                 "LSMinimumSystemVersion": "26.4", "LSMultipleInstancesProhibited": True, "NSHighResolutionCapable": True,
                 "NSMicrophoneUsageDescription": "Voice Fly 需要麦克风，在本机把你说的话转成文字。录音不会上传。",
             })
