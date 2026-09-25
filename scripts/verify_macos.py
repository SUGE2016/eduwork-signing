"""Run only on a fresh runner with no signing or Apple credentials."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request

from service import run, sha256
from sign_macos import mounted, validate_app


def verify():
    state = json.loads(Path('state.json').read_text())
    dmg = (Path('out') / state['signed_name']).resolve()
    if sha256(dmg) != state['signed_sha256']:
        raise ValueError('DMG hash mismatch')
    run('xcrun', 'stapler', 'validate', dmg)
    # Keep an Internet-origin marker for the Gatekeeper assessment.
    run('xattr', '-w', 'com.apple.quarantine', f'0081;{int(time.time()):x};EduWorkSigningCI;', dmg)
    run('spctl', '--assess', '--type', 'open', '--context', 'context:primary-signature', '--verbose=2', dmg)
    with tempfile.TemporaryDirectory(prefix='eduwork-verify-') as directory:
        work = Path(directory)
        with mounted(dmg) as mount:
            app = mount / 'EduWork.app'
            validate_app(app)
            run('codesign', '--verify', '--deep', '--strict', app)
            signature = subprocess.run(['codesign', '-dv', '--verbose=4', str(app)], capture_output=True, text=True, check=True).stderr
            if f'TeamIdentifier={state["team_id"]}' not in signature:
                raise ValueError('Unexpected signing team')
            run('spctl', '--assess', '--type', 'execute', '--verbose=2', app)
            # Separate runtime smoke from interactive first-open confirmation.
            installed = work / 'EduWork.app'
            run('ditto', '--noqtn', app, installed)
        info = plistlib.loads((installed / 'Contents/Info.plist').read_bytes())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        env = {k: os.environ[k] for k in ('HOME', 'PATH', 'TMPDIR', 'LANG') if k in os.environ}
        env['EDUWORK_DESKTOP_TEST_DATA_ROOT'] = str(work / 'data')
        env['EDUWORK_CONFIG_FILE'] = str(work / 'config.jsonc')
        executable = installed / 'Contents/MacOS' / info['CFBundleExecutable']
        with open('runtime.log', 'w') as log:
            child = subprocess.Popen([str(executable), '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}'],
                                     env=env, stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise ValueError('App exited before reaching main UI')
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=2) as response:
                            targets = json.load(response)
                        if any(t.get('type') == 'page' and t.get('url', '').startswith('dsh-app://app/') for t in targets):
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(1)
                else:
                    raise ValueError('Main application UI was not observed within 180 seconds')
                time.sleep(5)
                if child.poll() is not None:
                    raise ValueError('App crashed after reaching main UI')
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL); child.wait()
    Path('verification.json').write_text(json.dumps({'passed': True, 'release_id': state['release_id'],
        'sha256': state['signed_sha256'], 'stapler': 'passed', 'gatekeeper_dmg_and_app': 'accepted',
        'runtime': 'Observed main dsh-app UI on isolated data',
        'scope': 'Gatekeeper assessed with Internet marker on DMG; runtime smoke uses local non-quarantined copy. Interactive first-open dialog and full feature suite are not automated.'}, indent=2) + '\n')


if __name__ == '__main__':
    verify()
