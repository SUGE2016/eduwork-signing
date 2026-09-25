"""Sign the frozen application without running any code from the input package."""
import base64
import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import stat
import subprocess
import tempfile
import zipfile

from service import run

MAGIC = {bytes.fromhex(x) for x in ('feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}
BUNDLES = {'.app', '.framework', '.xpc', '.docktileplugin', '.bundle'}


def validate_zip(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 150000 or sum(e.file_size for e in entries) > 12 * 1024**3:
            raise ValueError('Archive exceeds size limits')
        links = set()
        names = []
        for entry in entries:
            name = PurePosixPath(entry.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in entry.filename:
                raise ValueError('Unsafe ZIP path')
            names.append(name)
            if stat.S_ISLNK(entry.external_attr >> 16):
                links.add(name)
                target = archive.read(entry).decode()
                if target.startswith('/'):
                    raise ValueError('Absolute ZIP symlink')
                resolved = os.path.normpath(str(name.parent / target))
                if resolved == '..' or resolved.startswith('../'):
                    raise ValueError('Escaping ZIP symlink')
        if any(parent in links for name in names for parent in name.parents):
            raise ValueError('ZIP writes through a symlink')


def validate_app(app):
    root = app.resolve()
    for path in app.rglob('*'):
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(root)
            except (ValueError, FileNotFoundError, RuntimeError):
                raise ValueError(f'Unsafe or broken application symlink: {path}')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'org.eduwork.eduwork.electron':
        raise ValueError('Unexpected application bundle identifier')
    return info


@contextlib.contextmanager
def mounted(image, readonly=True):
    with tempfile.TemporaryDirectory(prefix='eduwork-mount-') as directory:
        mount = Path(directory) / 'volume'
        args = ['hdiutil', 'attach', image, '-nobrowse', '-noautoopen', '-mountpoint', mount]
        if readonly:
            args.append('-readonly')
        run(*args)
        try:
            yield mount
        finally:
            if not readonly:
                run('sync')
            try:
                run('hdiutil', 'detach', mount)
            except subprocess.CalledProcessError:
                # Only detach the temporary image mounted by this context.
                run('hdiutil', 'detach', '-force', mount)


@contextlib.contextmanager
def signing_identity():
    temp = Path(os.environ['RUNNER_TEMP'])
    keychain = temp / 'eduwork-signing.keychain-db'
    p12 = temp / 'identity.p12'
    password = os.urandom(32).hex()
    p12.write_bytes(base64.b64decode(os.environ.pop('MACOS_CERTIFICATE_P12_BASE64'), validate=True))
    p12.chmod(0o600)
    p12_password = os.environ.pop('MACOS_CERTIFICATE_PASSWORD')
    try:
        run('security', 'create-keychain', '-p', password, keychain)
        run('security', 'set-keychain-settings', '-lut', '21600', keychain)
        run('security', 'unlock-keychain', '-p', password, keychain)
        run('security', 'import', p12, '-k', keychain, '-P', p12_password, '-T', '/usr/bin/codesign')
        run('security', 'set-key-partition-list', '-S', 'apple-tool:,apple:', '-s', '-k', password, keychain)
        run('security', 'list-keychains', '-d', 'user', '-s', keychain)
        identities = run('security', 'find-identity', '-v', '-p', 'codesigning', keychain)
        import re
        team = os.environ['APPLE_TEAM_ID']
        matches = re.findall(r'\b([A-F0-9]{40}) "Developer ID Application: [^"\n]+ \(' + re.escape(team) + r'\)"', identities)
        if len(matches) != 1:
            raise ValueError('Expected exactly one valid Developer ID Application identity for configured team')
        yield matches[0]
    finally:
        p12.unlink(missing_ok=True)
        if keychain.exists():
            run('security', 'delete-keychain', keychain)


def needs_jit(path):
    return path.name in {'node', 'Electron', 'EduWork.app'} or path.name.startswith(('Electron Helper', 'Google Chrome for Testing'))


def sign_app(app, identity, work):
    validate_app(app)
    jit = work / 'jit.plist'
    main = work / 'main.plist'
    jit.write_bytes(plistlib.dumps({'com.apple.security.cs.allow-jit': True}))
    main.write_bytes(plistlib.dumps({'com.apple.security.cs.allow-jit': True,
                                     'com.apple.security.device.audio-input': True,
                                     'com.apple.security.device.camera': True}))
    binaries, bundles = [], []
    for path in app.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir() and path.suffix in BUNDLES:
            bundles.append(path)
        elif path.is_file():
            with path.open('rb') as stream:
                if stream.read(4) in MAGIC:
                    binaries.append(path)
    if not binaries:
        raise ValueError('No Mach-O binaries')
    def sign(path):
        args = ['codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime']
        if path == app:
            args += ['--entitlements', main]
        elif needs_jit(path) and path.suffix != '.framework':
            args += ['--entitlements', jit]
        run(*args, path)
    for path in sorted(binaries, key=lambda p: len(p.parts), reverse=True):
        sign(path)
    for path in sorted(bundles, key=lambda p: len(p.parts), reverse=True) + [app]:
        sign(path)
    run('codesign', '--verify', '--deep', '--strict', app)
    print(f'Signed {len(binaries)} native binaries and {len(bundles) + 1} bundles.')


def package(input_path, app, output, work):
    if input_path.suffix == '.dmg':
        # Preserve the source DMG's volume metadata, background and Finder layout.
        rw = work / 'installer-rw.dmg'
        run('hdiutil', 'convert', input_path, '-format', 'UDRW', '-o', rw)
        size = sum(p.stat().st_size for p in app.rglob('*') if p.is_file() and not p.is_symlink())
        minimum = int(run('hdiutil', 'resize', '-limits', rw).split()[0]) * 512
        target = max(minimum + 1024**3, size * 3 + 512 * 1024**2)
        run('hdiutil', 'resize', '-size', f'{(target + 1024**2 - 1) // 1024**2}m', rw)
        with mounted(rw, readonly=False) as mount:
            existing = list(mount.glob('*.app'))
            if len(existing) != 1 or existing[0].name != app.name:
                raise ValueError('Unexpected DMG application layout')
            shutil.rmtree(existing[0])
            run('ditto', '--noqtn', app, mount / app.name)
            run('codesign', '--verify', '--deep', '--strict', mount / app.name)
        run('hdiutil', 'convert', rw, '-format', 'UDZO', '-o', output)
    else:
        stage = work / 'dmg-root'
        stage.mkdir()
        run('ditto', '--noqtn', app, stage / app.name)
        (stage / 'Applications').symlink_to('/Applications')
        run('hdiutil', 'create', '-fs', 'HFS+', '-format', 'UDZO', '-volname', 'EduWork', '-srcfolder', stage, output)


def main():
    state = json.loads(Path('state.json').read_text())
    input_path = Path('input' + Path(state['source_asset_name']).suffix).resolve()
    out = Path('out'); out.mkdir(exist_ok=True)
    output = (out / f'EduWork-{state["version"]}-macos-arm64-electron-signed.dmg').resolve()
    with tempfile.TemporaryDirectory(prefix='eduwork-sign-') as directory:
        work = Path(directory)
        app = work / 'EduWork.app'
        if input_path.suffix == '.dmg':
            with mounted(input_path) as mount:
                apps = list(mount.glob('*.app'))
                if len(apps) != 1 or apps[0].is_symlink() or apps[0].name != 'EduWork.app':
                    raise ValueError('Expected one EduWork.app at DMG root')
                validate_app(apps[0])
                run('ditto', '--noqtn', apps[0], app)
        else:
            validate_zip(input_path)
            extracted = work / 'extracted'
            run('ditto', '-x', '-k', '--noqtn', input_path, extracted)
            apps = [p for p in extracted.rglob('*.app') if not any(q.suffix == '.app' for q in p.relative_to(extracted).parents)]
            if len(apps) != 1 or apps[0].name != 'EduWork.app':
                raise ValueError('Expected one top-level EduWork.app in ZIP')
            validate_app(apps[0])
            run('ditto', '--noqtn', apps[0], app)
        info = validate_app(app)
        if info['CFBundleShortVersionString'] != state['version'].split('-')[0]:
            raise ValueError('Application version differs from release version')
        with signing_identity() as identity:
            sign_app(app, identity, work)
            package(input_path, app, output, work)
            run('codesign', '--force', '--sign', identity, '--timestamp', output)
            run('codesign', '--verify', output)


if __name__ == '__main__':
    # Do not dump CalledProcessError.command: keychain commands contain passwords.
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f'External signing operation failed (exit {error.returncode}); see preceding tool diagnostics.') from None
