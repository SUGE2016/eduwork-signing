"""Release discovery and durable notarization state; never executes upstream scripts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime, timezone

ALLOWED_SOURCES = {'ECNU/EduWork', 'SUGE2016/EduWork'}
ASSET = re.compile(r'^EduWork-(\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*)-macos-arm64-electron\.(dmg|zip)$')


def run(*args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def api(endpoint, method='GET', fields=None):
    cmd = ['gh', 'api', endpoint, '--method', method]
    for key, value in (fields or {}).items():
        cmd += ['-f' if key == 'make_latest' else '-F', f'{key}={value}']
    return json.loads(run(*cmd) or '{}')


def repo():
    return os.environ['GITHUB_REPOSITORY']


def source():
    value = os.environ.get('SOURCE_REPO') or 'ECNU/EduWork'
    if value not in ALLOWED_SOURCES:
        raise ValueError('Source repository is not allowlisted')
    return value


def output(key, value):
    if '\n' in str(value) or '\r' in str(value):
        raise ValueError('Multiline output rejected')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        stream.write(f'{key}={value}\n')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def select_asset(release):
    if release['draft']:
        return None
    candidates = [a for a in release['assets'] if ASSET.fullmatch(a['name']) and a['state'] == 'uploaded']
    # Fail closed if a release unexpectedly has multiple versions for one format.
    for extension in ('.dmg', '.zip'):
        matches = [a for a in candidates if a['name'].endswith(extension)]
        if len(matches) > 1:
            raise ValueError('Ambiguous macOS release assets')
        if matches:
            return matches[0]
    return None


def download(repository, asset, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('wb') as stream:
        subprocess.run(['gh', 'api', f'repos/{repository}/releases/assets/{int(asset["id"])}',
                        '-H', 'Accept: application/octet-stream'], stdout=stream, check=True)
    expected = asset.get('digest')
    if expected and expected != 'sha256:' + sha256(destination):
        raise ValueError('GitHub asset digest mismatch')


def load_state(release):
    matches = [a for a in release['assets'] if a['name'] == 'state.json']
    if len(matches) != 1:
        raise ValueError('Incomplete signing draft: state.json missing; inspect before retrying')
    download(repo(), matches[0], 'state.json')
    state = json.loads(Path('state.json').read_text())
    if state['schema'] != 1 or state['release_id'] != release['id'] or state['source_repo'] not in ALLOWED_SOURCES:
        raise ValueError('Invalid signing state')
    return state


def save_state(state):
    Path('state.json').write_text(json.dumps(state, indent=2) + '\n')
    run('gh', 'release', 'upload', state['tag'], 'state.json', '--repo', repo(), '--clobber')


def discover():
    own = api(f'repos/{repo()}/releases?per_page=100')
    pending = [r for r in own if r['draft'] and r['tag_name'].startswith('signed-')]
    if pending:
        # Resume one outstanding submission before accepting another.
        release = min(pending, key=lambda r: r['id'])
        state = load_state(release)
        if state['phase'] == 'failed':
            output('task', 'none')
            output('release_id', '')
            output('source_release_id', '')
            print('Paused: failed draft needs owner intervention; see its logs.')
            return
        output('task', 'resume')
        output('release_id', release['id'])
        output('source_release_id', '')
        return
    requested = os.environ.get('SOURCE_TAG', '')
    releases = api(f'repos/{source()}/releases?per_page=100')
    if requested:
        releases = [r for r in releases if r['tag_name'] == requested]
        if not releases:
            raise ValueError('Requested published source release not found')
    else:
        floor = os.environ.get('SIGN_FROM') or api(f'repos/{repo()}')['created_at']
        releases = [r for r in releases if r['published_at'] and r['published_at'] >= floor]
    for release in sorted(releases, key=lambda r: r['published_at']):
        tag = f'signed-{release["id"]}'
        if any(r['tag_name'] == tag for r in own):
            continue
        asset = select_asset(release)
        if not asset:
            continue
        output('task', 'new')
        output('release_id', '')
        output('source_release_id', release['id'])
        print(json.dumps({'source': source(), 'tag': release['tag_name'], 'asset': asset['name'], 'asset_id': asset['id']}))
        return
    output('task', 'none')
    output('release_id', '')
    output('source_release_id', '')
    print('No new published macOS release; nothing to sign.')


def prepare(release_id):
    release = api(f'repos/{source()}/releases/{int(release_id)}')
    asset = select_asset(release)
    if not asset:
        raise ValueError('Source no longer has an eligible published asset')
    tag = release['tag_name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', tag):
        raise ValueError('Unsupported source tag')
    # Resolve the current tag to a full commit, not a mutable branch name.
    commit = api(f'repos/{source()}/commits/{tag}')['sha']
    version = ASSET.fullmatch(asset['name']).group(1)
    if tag not in {version, 'v' + version, 'macos-v' + version, 'desktop-v' + version}:
        raise ValueError('Tag and binary version do not agree')
    download(source(), asset, 'input' + Path(asset['name']).suffix)
    state = {'schema': 1, 'source_repo': source(), 'source_release_id': release['id'],
             'source_tag': tag, 'source_commit': commit, 'source_asset_id': asset['id'],
             'source_asset_name': asset['name'], 'source_sha256': sha256('input' + Path(asset['name']).suffix),
             'source_published_at': release['published_at'], 'version': version,
             'prerelease': release['prerelease'], 'tag': f'signed-{release["id"]}',
             'phase': 'prepared', 'signer_commit': os.environ['GITHUB_SHA']}
    Path('state.json').write_text(json.dumps(state, indent=2) + '\n')


def stage():
    state = json.loads(Path('state.json').read_text())
    dmg = Path('out') / f'EduWork-{state["version"]}-macos-arm64-electron-signed.dmg'
    state['signed_name'] = dmg.name
    state['signed_sha256'] = sha256(dmg)
    state['team_id'] = os.environ['APPLE_TEAM_ID']
    state['notary_name'] = f'eduwork-{state["source_asset_id"]}-{state["signed_sha256"][:16]}.dmg'
    state['phase'] = 'awaiting-notary'
    state['submission_id'] = None
    state['prepared_at'] = datetime.now(timezone.utc).isoformat()
    body = (f'Signed macOS distribution of {state["source_repo"]} {state["source_tag"]}.\n\n'
            f'Source: https://github.com/{state["source_repo"]}/releases/tag/{state["source_tag"]}\n\n'
            'Publication requires Apple notarization and isolated validation. See delivery.json for provenance and checks.\n')
    release = api(f'repos/{repo()}/releases', 'POST', {'tag_name': state['tag'], 'name': f'EduWork {state["version"]} · signed macOS',
                  'body': body, 'draft': 'true', 'prerelease': str(state['prerelease']).lower(), 'target_commitish': os.environ['GITHUB_SHA']})
    state['release_id'] = release['id']
    save_state(state)
    run('gh', 'release', 'upload', state['tag'], dmg, '--repo', repo())
    output('release_id', release['id'])


def resume(release_id):
    release = api(f'repos/{repo()}/releases/{int(release_id)}')
    if not release['draft']:
        raise ValueError('Signing target is not a draft')
    state = load_state(release)
    asset = next(a for a in release['assets'] if a['name'] == state['signed_name'])
    dmg = Path('out') / state['signed_name']
    download(repo(), asset, dmg)
    if sha256(dmg) != state['signed_sha256']:
        raise ValueError('Stored signed DMG hash mismatch')
    if not state.get('submission_id'):
        history = json.loads(run('xcrun', 'notarytool', 'history', '--keychain-profile', 'ci-notary', '--output-format', 'json'))
        found = [h for h in history['history'] if h['name'] == state['notary_name']]
        if len(found) > 1:
            raise ValueError('Ambiguous submission history; owner intervention required')
        if found:
            state['submission_id'] = found[0]['id']
        else:
            # Unique name permits recovery if submission succeeds but state persistence fails.
            import shutil
            submission = Path(os.environ.get('RUNNER_TEMP', '.')) / state['notary_name']
            shutil.copyfile(dmg, submission)
            result = json.loads(run('xcrun', 'notarytool', 'submit', submission,
                                    '--keychain-profile', 'ci-notary', '--output-format', 'json'))
            state['submission_id'] = result['id']
        save_state(state)
    info = json.loads(run('xcrun', 'notarytool', 'info', state['submission_id'], '--keychain-profile', 'ci-notary', '--output-format', 'json'))
    if info['status'] == 'In Progress':
        output('ready', 'false')
        print('Apple is still processing; saved submission will be checked on the next run.')
        return
    run('xcrun', 'notarytool', 'log', state['submission_id'], '--keychain-profile', 'ci-notary', 'notary-log.json')
    run('gh', 'release', 'upload', state['tag'], 'notary-log.json', '--repo', repo(), '--clobber')
    if info['status'] != 'Accepted':
        state['phase'] = 'failed'
        save_state(state)
        raise ValueError('Apple notarization rejected: inspect draft notary-log.json')
    run('xcrun', 'stapler', 'staple', dmg)
    run('xcrun', 'stapler', 'validate', dmg)
    state['phase'] = 'ready'
    state['signed_sha256'] = sha256(dmg)
    run('gh', 'release', 'upload', state['tag'], dmg, '--repo', repo(), '--clobber')
    save_state(state)
    output('ready', 'true')
    output('release_id', state['release_id'])


def fetch_ready(release_id):
    release = api(f'repos/{repo()}/releases/{int(release_id)}')
    state = load_state(release)
    if state['phase'] != 'ready' or not release['draft']:
        raise ValueError('Not ready for verification')
    asset = next(a for a in release['assets'] if a['name'] == state['signed_name'])
    download(repo(), asset, Path('out') / state['signed_name'])
    if sha256(Path('out') / state['signed_name']) != state['signed_sha256']:
        raise ValueError('Verification input hash mismatch')


def publish(release_id):
    release = api(f'repos/{repo()}/releases/{int(release_id)}')
    state = load_state(release)
    report = json.loads(Path('verification.json').read_text())
    if state['phase'] != 'ready' or report.get('passed') is not True or report['sha256'] != state['signed_sha256'] or report['release_id'] != state['release_id']:
        raise ValueError('Verification result does not match this exact file')
    delivery = {k: v for k, v in state.items() if k not in {'phase', 'notary_name'}}
    delivery['verification'] = report
    Path('delivery.json').write_text(json.dumps(delivery, indent=2) + '\n')
    Path('SHA256SUMS').write_text(f'{state["signed_sha256"]}  {state["signed_name"]}\n')
    run('gh', 'release', 'upload', state['tag'], 'delivery.json', 'SHA256SUMS', 'verification.json', '--repo', repo(), '--clobber')
    # Compare source asset identity and tag again before publishing.
    current = api(f'repos/{state["source_repo"]}/releases/{state["source_release_id"]}')
    if current['draft'] or current['tag_name'] != state['source_tag'] or not any(a['id'] == state['source_asset_id'] for a in current['assets']):
        raise ValueError('Source release changed; refusing publication')
    if api(f'repos/{state["source_repo"]}/commits/{state["source_tag"]}')['sha'] != state['source_commit']:
        raise ValueError('Source tag moved; refusing publication')
    api(f'repos/{repo()}/releases/{state["release_id"]}', 'PATCH', {'draft': 'false', 'make_latest': 'false'})
    print(f'https://github.com/{repo()}/releases/tag/{state["tag"]}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['discover', 'prepare', 'stage', 'resume', 'fetch-ready', 'publish'])
    parser.add_argument('release_id', nargs='?')
    args = parser.parse_args()
    functions = {'discover': discover, 'prepare': prepare, 'stage': stage, 'resume': resume, 'fetch-ready': fetch_ready, 'publish': publish}
    if args.command in {'discover', 'stage'}:
        functions[args.command]()
    else:
        functions[args.command](args.release_id)
