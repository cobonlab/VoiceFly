#!/usr/bin/env python3
"""Embed the frontend's known native dependencies and reject host-only links.

Only modifies a staged bundle. Never points at an installed input method.
"""
import argparse
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

MAGIC = {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'}
SYSTEM = ('/usr/lib/', '/System/Library/')
LIBRARIES = {
    'libglog.2.dylib': ('glog', ('COPYING', 'AUTHORS')),
    'libgflags.2.3.dylib': ('gflags', ('COPYING.txt',)),
}

def run(*args):
    subprocess.run(list(map(str, args)), check=True, stdout=subprocess.DEVNULL)

def binaries(app):
    for path in sorted(app.rglob('*')):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as file:
            if file.read(4) in MAGIC:
                yield path

def dependencies(path):
    output = subprocess.check_output(['otool', '-L', str(path)], text=True)
    return [line.strip().split(' (', 1)[0] for line in output.splitlines()
            if '(compatibility version ' in line]

def prepare(app):
    app = app.resolve()
    if app.name != 'VoiceFly.app' or '/payload/' not in str(app):
        raise ValueError('Only a staged installer payload may be modified')
    frameworks = app / 'Contents/Frameworks'
    # The six supported schemes use built-in Rime components. Do not ship
    # unrelated optional plugin binaries acquired by an older local checkout.
    plugins = frameworks / 'rime-plugins'
    if plugins.exists():
        shutil.rmtree(plugins)
    notices = app / 'Contents/Resources/ThirdPartyLicenses'
    notices.mkdir(parents=True, exist_ok=True)
    for source, name in ((ROOT / 'LICENSE', 'VoiceFly-LICENSE.txt'),
                         (ROOT / 'THIRD_PARTY_NOTICES.md', 'NOTICE.md')):
        shutil.copy2(source, notices / name)
    for folder in ('licenses', 'third-party/binary-notices', 'third-party/rime-schemes'):
        shutil.copytree(ROOT / folder, notices / pathlib.Path(folder).name, dirs_exist_ok=True)
    # An older development checkout can contain extra schemas or altered data.
    # Stage only the reviewed public schemes and their pinned common resources.
    shared = app / 'Contents/SharedSupport'
    shutil.rmtree(shared)
    shared.mkdir()
    for source in (ROOT / 'input-method/data/voicefly').iterdir():
        if source.is_file():
            shutil.copy2(source, shared / source.name)
    for folder in (ROOT / 'third-party/rime-common').iterdir():
        if not folder.is_dir():
            continue
        for source in folder.iterdir():
            if source.suffix == '.yaml' or source.name == 'essay.txt':
                shutil.copy2(source, shared / source.name)
        shutil.copytree(folder, notices / ('rime-' + folder.name))
    shutil.copy2(ROOT / 'input-method/data/squirrel.yaml', shared / 'squirrel.yaml')
    shutil.copytree(ROOT / 'input-method/data/opencc', shared / 'opencc')
    queue = list(binaries(app))
    copied = set()
    modified = set()
    for binary in queue:
        for dependency in dependencies(binary):
            if not dependency.startswith('/') or dependency.startswith(SYSTEM):
                continue
            source = pathlib.Path(dependency)
            name = source.name
            if name not in LIBRARIES or not source.is_file():
                raise ValueError('Unbundled native dependency: ' + dependency)
            if name not in copied:
                destination = frameworks / name
                if destination.exists():
                    raise ValueError('Refusing to overwrite a staged dependency: ' + name)
                shutil.copy2(source.resolve(), destination)
                formula, files = LIBRARIES[name]
                package_root = source.resolve().parent.parent
                target = notices / formula
                target.mkdir(exist_ok=True)
                for filename in files:
                    license_file = package_root / filename
                    if not license_file.is_file():
                        raise ValueError('Missing dependency notice: ' + str(license_file))
                    shutil.copy2(license_file, target / filename)
                run('install_name_tool', '-id', '@rpath/' + name, destination)
                modified.add(destination)
                copied.add(name)
                queue.append(destination)
            relative = pathlib.Path(__import__('os').path.relpath(frameworks / name, binary.parent))
            run('install_name_tool', '-change', dependency, '@loader_path/' + str(relative), binary)
            modified.add(binary)
    for binary in binaries(app):
        for dependency in dependencies(binary):
            if dependency.startswith('/') and not dependency.startswith(SYSTEM):
                raise ValueError('Host-only link remains: ' + dependency)
        if binary in modified:
            run('codesign', '--force', '--sign', '-', binary)
    run('codesign', '--force', '--sign', '-', app)
    run('codesign', '--verify', '--deep', '--strict', app)
    print('Portable frontend dependencies: ' + ', '.join(sorted(copied)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=pathlib.Path)
    prepare(parser.parse_args().app)
