"""Run interactively on the owner's Mac; secrets travel to gh via stdin only."""
import argparse
import base64
import getpass
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--p12', type=Path, required=True)
    parser.add_argument('--repo', default='SUGE2016/eduwork-signing')
    args = parser.parse_args()
    gh = shutil.which('gh') or str(Path.home() / '.local/bin/gh')
    data = args.p12.read_bytes()
    password = getpass.getpass('P12 export password (hidden): ')
    # Verify the password without printing a private key or placing it in argv.
    check = subprocess.run(['openssl', 'pkcs12', '-in', str(args.p12), '-passin', 'stdin', '-noout'],
                           input=password + '\n', text=True, capture_output=True)
    if check.returncode:
        raise SystemExit('P12 validation failed. Check export password and format; no secrets uploaded.')
    apple_id = input('Developer Apple ID email: ').strip()
    apple_password = getpass.getpass('Apple app-specific password (hidden): ')
    if not apple_id or not apple_password:
        raise SystemExit('Apple credentials required; no secrets uploaded.')
    secrets = {'MACOS_CERTIFICATE_P12_BASE64': base64.b64encode(data).decode(),
               'MACOS_CERTIFICATE_PASSWORD': password, 'APPLE_ID': apple_id,
               'APPLE_APP_SPECIFIC_PASSWORD': apple_password}
    for name, value in secrets.items():
        subprocess.run([gh, 'secret', 'set', name, '--env', 'signing', '--repo', args.repo],
                       input=value, text=True, check=True)
        print('Configured:', name)
    print('Secrets configured. Scheduling remains disabled until an end-to-end trial succeeds.')


if __name__ == '__main__':
    main()
