# Public bundle allowlist. The maintainer's vocabulary and caches are excluded.
import os
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
a = Analysis([str(ROOT / "voice_fly_service.py")], pathex=[str(ROOT)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VoiceFlyService", debug=False,
          strip=False, upx=False, console=False, target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VoiceFlyService")
app = BUNDLE(coll, name="Voice Fly Service.app", bundle_identifier="com.simonli.voicefly.service",
             version="1.3.0", info_plist={
                 "CFBundleDisplayName": "Voice Fly 语音服务", "CFBundleName": "Voice Fly Service",
                 "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "21", "LSUIElement": True,
                 "LSMinimumSystemVersion": "26.4", "LSMultipleInstancesProhibited": True, "NSHighResolutionCapable": True,
                 "NSMicrophoneUsageDescription": "Voice Fly 需要麦克风，在本机把你说的话转成文字。录音不会上传。",
             })
