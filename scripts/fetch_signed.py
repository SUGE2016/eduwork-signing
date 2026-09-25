"""Run in upstream CI to collect a completed delivery, without signing credentials."""
import argparse
import json
from pathlib import Path
import time

from service import api, download, sha256


def collect(signer, upstream, source_id, directory):
    delivery_release = api(f'repos/{signer}/releases/tags/signed-{source_id}')
    if delivery_release['draft']:
        raise ValueError('Signing delivery is not published')
    assets = {a['name']: a for a in delivery_release['assets']}
    directory.mkdir(parents=True, exist_ok=True)
    download(signer, assets['delivery.json'], directory / 'delivery.json')
    receipt = json.loads((directory / 'delivery.json').read_text())
    source = api(f'repos/{upstream}/releases/{source_id}')
    if receipt['source_repo'] != upstream or receipt['source_release_id'] != source_id or receipt['source_tag'] != source['tag_name']:
        raise ValueError('Delivery belongs to another source release')
    if api(f'repos/{upstream}/commits/{source["tag_name"]}')['sha'] != receipt['source_commit']:
        raise ValueError('Source commit mismatch')
    if not any(a['id'] == receipt['source_asset_id'] for a in source['assets']):
        raise ValueError('Source asset was replaced')
    if receipt['verification'].get('passed') is not True or receipt['verification']['sha256'] != receipt['signed_sha256']:
        raise ValueError('Delivery not verified')
    name = receipt['signed_name']
    if Path(name).name != name or not name.endswith('.dmg'):
        raise ValueError('Invalid signed filename')
    download(signer, assets[name], directory / name)
    if sha256(directory / name) != receipt['signed_sha256']:
        raise ValueError('Final DMG digest mismatch')
    print(directory / name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--signer', default='SUGE2016/eduwork-signing')
    parser.add_argument('--upstream', default='ECNU/EduWork')
    parser.add_argument('--release-id', type=int, required=True)
    parser.add_argument('--output', type=Path, default=Path('signed'))
    args = parser.parse_args()
    collect(args.signer, args.upstream, args.release_id, args.output)
