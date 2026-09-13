#!/usr/bin/env python3
"""Prepare one private configuration from the actual legacy Tidal installations.

One-time, read-only inspection of the old app; no DB, RPC or service operations.
Run with the new release's interpreter. Child stdout carries private settings
only to this process, never to the terminal or deployment journal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml


LEGACY_READ = r'''
import json, os
from pathlib import Path
from dotenv import dotenv_values
from tidal.config import load_client_settings, load_server_settings
from tidal.transaction_service.signer import TransactionSigner
root, home = Path(os.environ['LEGACY_REPOSITORY']), Path(os.environ['TIDAL_HOME'])
initial = dict(os.environ)
result = {}
for name, environment, loader, config in [
    ('scan', home / 'server/.env', load_server_settings, root / 'config/server.yaml'),
    ('kick', root / '.env', load_client_settings, None),
]:
    os.environ.clear()
    os.environ.update(initial)
    os.environ.update({key:value for key,value in dotenv_values(environment).items() if value is not None})
    settings = loader(config)
    key_path = Path(settings.txn_keystore_path).expanduser().resolve()
    signer = TransactionSigner(str(key_path), settings.txn_keystore_passphrase)
    result[name] = {'settings': settings.model_dump(mode='json'), 'signer': signer.address.lower(),
        'keystore_path': str(key_path), 'config_path': str(settings.resolved_config_path),
        'secret_path': str(settings.resolved_env_path)}
print(json.dumps(result))
'''

SECRET_FIELDS = {'rpc_url', 'token_price_agg_key', 'telegram_bot_token', 'telegram_admin_alert_chat_id',
                 'telegram_operations_alert_chat_id', 'txn_keystore_passphrase', 'tidal_api_key'}
PROFILE_FIELDS = {'txn_usd_threshold', 'txn_base_fee_cap_gwei', 'txn_require_curve_quote', 'txn_max_gas_limit'}


def copy_keystore(source, destination, verified_address):
    """Retain ciphertext and fill optional public metadata for silent inspection.

    Some original encrypted files omit `address`. Native legacy decryption has
    established this value. Never re-encrypt or change an existing declaration;
    the new native check-config validates the resulting copy.
    """
    content = Path(source).read_bytes()
    original = json.loads(content)
    if 'address' not in original:
        annotated = {**original, 'address': verified_address.lower().removeprefix('0x')}
        Path(destination).write_text(json.dumps(annotated, sort_keys=True) + '\n')
    else:
        Path(destination).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def prepare(legacy_python, repository, home, output, destination):
    from tidal.config import Settings
    repository, home, output, destination = [Path(path).expanduser().resolve() for path in (repository, home, output, destination)]
    if output.exists():
        raise ValueError('Choose a new private candidate directory; preserve earlier inputs')
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home.parent), 'TIDAL_HOME': str(home),
                   'LEGACY_REPOSITORY': str(repository), 'PYTHONDONTWRITEBYTECODE': '1'}
    child = subprocess.run([str(legacy_python), '-c', LEGACY_READ], cwd=repository, env=environment,
                           text=True, capture_output=True, timeout=120)
    if child.returncode:
        raise ValueError('Original configuration or encrypted key could not be read; inspect the original native setup')
    old = json.loads(child.stdout)
    if old['scan']['signer'] != old['kick']['signer']:
        raise ValueError('The two old Tidal installations use different identities; review before consolidation')
    scan, kick = old['scan']['settings'], old['kick']['settings']
    # Most decisions previously lived on the server. Any additional local
    # execution-policy difference needs explicit treatment, not a guessed merge.
    different = sorted(key for key in scan.keys() & kick.keys() if scan[key] != kick[key])
    extra_policy = [key for key in different if key.startswith('txn_') and key not in PROFILE_FIELDS |
                    {'txn_keystore_path', 'txn_keystore_passphrase', 'txn_data_freshness_limit_seconds'}]
    if extra_policy:
        public_differences = {key: {'scan': scan[key], 'kick': kick[key]} for key in extra_policy
                              if key in {'txn_data_freshness_limit_seconds', 'txn_max_gas_limit'}}
        raise ValueError('Additional legacy execution-profile differences require review: ' +
                         ', '.join(extra_policy) + '; ' + json.dumps(public_differences))
    policy = {key: value for key, value in scan.items() if key in Settings.model_fields and key not in SECRET_FIELDS}
    policy['kick'] = yaml.safe_load(Path(old['scan']['config_path']).read_text())['kick']
    policy.update(txn_keystore_path=str(destination / 'server-keystore.json'),
                  managed_signers={name: old['scan']['signer'] for name in ('scan', 'kick')},
                  execution_profiles={
                      'scan': {key: scan[key] for key in PROFILE_FIELDS},
                      # Legacy HTTP inspect/prepare used server threshold and
                      # freshness unless the CLI supplied an explicit override.
                      'strategy': {'txn_usd_threshold': scan['txn_usd_threshold'], 'txn_base_fee_cap_gwei': 1,
                                   'txn_require_curve_quote': True, 'txn_max_gas_limit': kick['txn_max_gas_limit']},
                      'fee_burner': {'txn_usd_threshold': 50, 'txn_base_fee_cap_gwei': 1,
                                     'txn_require_curve_quote': False, 'txn_max_gas_limit': kick['txn_max_gas_limit']},
                  })
    # Keep effective secrets in exactly one selected file. Old env spelling or
    # precedence is resolved by the old loader before selecting these values.
    secrets = {Settings.model_fields[key].alias or key.upper(): scan[key]
               for key in SECRET_FIELDS if key in Settings.model_fields and scan.get(key) is not None}
    if scan.get('tidal_api_key'):
        secrets['TIDAL_API_KEY'] = scan['tidal_api_key']
    secrets.update(TIDAL_HOME=str(home), DB_PATH=str(home / 'server/tidal.db'),
                   TIDAL_CONFIG=str(destination / 'server.yml'), TIDAL_ENV_FILE=str(destination / 'server.env'),
                   TXN_KEYSTORE_PATH=str(destination / 'server-keystore.json'))
    output.mkdir(mode=0o700, parents=True)
    (output / 'server.yml').write_text(yaml.safe_dump(policy, sort_keys=False))
    (output / 'server.env').write_text(''.join(key + '=' + json.dumps(str(value)) + '\n' for key, value in sorted(secrets.items())))
    source_key = Path(old['scan']['keystore_path'])
    original_key_sha256 = copy_keystore(source_key, output / 'server-keystore.json', old['scan']['signer'])
    for path in output.iterdir():
        path.chmod(0o600)
    checked = subprocess.run([sys.executable, '-I', '-m', 'tidal.cli', 'check-config', '--json'],
        env={**environment, 'TIDAL_CONFIG': str(output / 'server.yml'), 'TIDAL_ENV_FILE': str(output / 'server.env'),
             'TXN_KEYSTORE_PATH': str(output / 'server-keystore.json')}, cwd='/tmp', capture_output=True, text=True, timeout=120)
    if checked.returncode:
        raise ValueError('New native configuration check failed; preserve the private candidate for inspection')
    report = json.loads(checked.stdout)
    if report.get('interface_version') != 1 or report.get('code') != 'CONFIGURATION_VALID' or report['data']['signers'] != policy['managed_signers']:
        raise ValueError('Native configuration validation returned inconsistent signing evidence')
    result = {'status': 'prepared', 'candidate': str(output), 'signers': policy['managed_signers'],
              'execution_profiles': policy['execution_profiles'], 'legacy_different_fields': different,
              'legacy_configurations': {name: row['config_path'] for name, row in old.items()},
              'legacy_secret_files': {name: row['secret_path'] for name, row in old.items()},
              'original_keystore': str(source_key), 'original_keystore_sha256': original_key_sha256}
    (output / 'preflight.json').write_text(json.dumps(result, sort_keys=True) + '\n')
    (output / 'preflight.json').chmod(0o600)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-python', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path('/home/wavey/tidal'))
    parser.add_argument('--home', type=Path, default=Path('/home/wavey/.tidal'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--destination', type=Path, default=Path('/etc/electro/apps/tidal'))
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(prepare(args.legacy_python, args.repository, args.home, args.output, args.destination), sort_keys=True))
