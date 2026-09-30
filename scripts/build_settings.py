#!/usr/bin/env python3
"""Build a native settings app; never copy personal data into the bundle."""
import pathlib
import plistlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

def build():
    subprocess.run(["swift", "build", "--package-path", str(ROOT / "input-method"),
                    "--configuration", "release", "--disable-sandbox", "--product", "VoiceFlySettings"], check=True)
    binary_dir = pathlib.Path(subprocess.check_output([
        "swift", "build", "--package-path", str(ROOT / "input-method"),
        "--configuration", "release", "--show-bin-path"], text=True).strip())
    app = ROOT / "dist/Voice Fly.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    shutil.copy2(binary_dir / "VoiceFlySettings", contents / "MacOS/VoiceFlySettings")
    shutil.copy2(ROOT / "input-method/data/voicefly/word-codes.json", contents / "Resources/word-codes.json")
    brand = ROOT / "input-method/resources/branding"
    shutil.copy2(brand / "VoiceFly.icns", contents / "Resources/VoiceFly.icns")
    shutil.copy2(brand / "VoiceFly.iconset/icon_128x128@2x.png", contents / "Resources/VoiceFlyMark.png")
    info = {
        "CFBundleIdentifier": "com.simonli.voicefly.settings",
        "CFBundleName": "Voice Fly", "CFBundleDisplayName": "Voice Fly",
        "CFBundleExecutable": "VoiceFlySettings", "CFBundlePackageType": "APPL",
        "CFBundleIconFile": "VoiceFly.icns",
        "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "24",
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    print(app)
    return app

if __name__ == "__main__":
    build()
