#!/usr/bin/env python3
"""Tidal-owned deployment and interim capture using native lifecycle commands.

Requires root on Linux. Installation stops at a read-only API. Explicit resume
requires a verified Storage Box capture and successful native recovery checks.
The later infrastructure deployment uses the same native commands and lock.
"""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid


SERVICES = ('tidal-api.service', 'tidal.service', 'tidal-kick.service')
TIMERS = ('tidal.timer', 'tidal-kick.timer')
RETIRED = ('tidal-scan.service', 'tidal-kicker.service', 'tidal-kicker.timer', 'tidal-deploy.service')
PRIVATE_FILES = ('server.env', 'server.yml', 'server-keystore.json')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, content, *, mode=0o600, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        os.fchmod(stream.fileno(), mode)
        if owner is not None:
            os.fchown(stream.fileno(), owner.pw_uid, owner.pw_gid)
        stream.write(content.encode() if isinstance(content, str) else content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def write_json(path, data):
    atomic(path, json.dumps(data, sort_keys=True) + '\n')


def command(args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, text=True,
        capture_output=True, timeout=kwargs.pop('timeout', 300), **kwargs).stdout


def unit_files(release, config, state, user):
    common = (f'User={user}\nGroup={user}\nWorkingDirectory={release}\n'
        f'EnvironmentFile={config}/server.env\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
        'Environment=PYTHONUNBUFFERED=1\nUMask=0077\nTimeoutStopSec=120\n')
    cli = f'{release}/.venv/bin/python -I -m tidal.cli'
    held = f'ConditionPathExists=!{state}/held\n'
    workers = held + f'ConditionPathExists=!{state}/workers-held\n'
    return {
        'tidal-api.service': '[Unit]\nDescription=Tidal read-only API\nAfter=network.target\n' + held +
            '\n[Service]\nType=simple\n' + common + f'ExecStart={cli} api serve --config {config}/server.yml\n'
            'Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n',
        'tidal.service': '[Unit]\nDescription=Tidal scanner and settlement cycle\nAfter=network-online.target\n' + workers +
            '\n[Service]\nType=oneshot\n' + common + f'ExecStart={cli} scan run --config {config}/server.yml --no-confirmation --auto-settle --auto-enable-tokens\n'
            'SuccessExitStatus=75\nTimeoutStartSec=45min\n',
        'tidal-kick.service': '[Unit]\nDescription=Tidal kick automation cycle\nAfter=network-online.target\n' + workers +
            '\n[Service]\nType=oneshot\n' + common +
            ''.join(f'ExecStart={cli} kick run --config {config}/server.yml --headless --source-type {source}\n'
                    for source in ('strategy', 'fee-burner')) + 'SuccessExitStatus=75\nTimeoutStartSec=12min\n',
        'tidal.timer': '[Unit]\nDescription=Tidal scanner every fifteen minutes\n' + workers +
            '\n[Timer]\nOnCalendar=*-*-* *:0/15:00 UTC\nPersistent=true\nUnit=tidal.service\n\n[Install]\nWantedBy=timers.target\n',
        'tidal-kick.timer': '[Unit]\nDescription=Tidal hourly kick cycle\n' + workers +
            '\n[Timer]\nOnBootSec=2min\nOnCalendar=hourly\nAccuracySec=30s\nPersistent=true\nUnit=tidal-kick.service\n\n[Install]\nWantedBy=timers.target\n',
    }


class Deployment:
    def __init__(self, *, user='wavey', home=None, state='/var/lib/electro/apps/tidal',
                 config='/etc/electro/apps/tidal', units='/etc/systemd/system',
                 lock='/var/lib/electro-backup/locks/application-tidal', releases=None):
        self.user = pwd.getpwnam(user)
        self.home = Path(home or (Path(self.user.pw_dir) / '.tidal'))
        self.state, self.config, self.units, self.lock = map(Path, (state, config, units, lock))
        self.releases = Path(releases or (Path(self.user.pw_dir) / 'tidal-releases'))
        self.database = self.home / 'server/tidal.db'

    @contextmanager
    def locked(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def native(self, release, args, *, configuration=None, environment=None, allow_blocked=False):
        config = Path(configuration or self.config)
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': self.user.pw_dir,
            'TIDAL_HOME': str(self.home), 'TIDAL_CONFIG': str(config / 'server.yml'),
            'TIDAL_ENV_FILE': str(config / 'server.env'), 'PYTHONDONTWRITEBYTECODE': '1', **(environment or {})}
        identity = {} if os.geteuid() == self.user.pw_uid else {
            'user': self.user.pw_uid, 'group': self.user.pw_gid, 'extra_groups': []}
        completed = subprocess.run([str(Path(release) / '.venv/bin/python'), '-I', '-m', 'tidal.cli',
            *map(str, args), '--json'], env=env, cwd=release, capture_output=True, text=True, timeout=2700, **identity)
        try:
            result = json.loads(completed.stdout)
            valid = result['interface_version'] == 1 and isinstance(result['data'], dict) and isinstance(result['blockers'], list)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid or completed.returncode not in (0, 1, 75):
            raise RuntimeError('Native Tidal command did not return a valid operational result')
        if not allow_blocked and (completed.returncode or result['blockers']):
            raise RuntimeError('Tidal remains held: ' + result['code'] + '; inspect the native command')
        return result

    def hold(self, release):
        # Persist conditions before stopping anything. A reboot at any following
        # instruction cannot restart an old writer or bypass the worker hold.
        atomic(self.state / 'held', '')
        atomic(self.state / 'workers-held', '')
        for name in (*SERVICES, *TIMERS, *RETIRED):
            condition = f'[Unit]\nConditionPathExists=!{self.state}/held\n'
            if name != 'tidal-api.service':
                condition += f'ConditionPathExists=!{self.state}/workers-held\n'
            atomic(self.units / (name + '.d') / '90-electro-hold.conf', condition, mode=0o644)
        command(['systemctl', 'daemon-reload'])
        for name in (*TIMERS, *RETIRED, *SERVICES):
            loaded = command(['systemctl', 'show', name, '-p', 'LoadState', '--value']).strip()
            if loaded != 'not-found':
                command(['systemctl', 'stop', name])
        command(['systemctl', 'disable', *TIMERS, 'tidal-kicker.timer'])
        self.native(release, ['hold'])

    def verify_no_old_process(self, repository):
        for process in Path('/proc').iterdir():
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                args = (process / 'cmdline').read_bytes().split(b'\0')
                # Restrict the check to this application's actual executable
                # names/modules, not SSH, shells or unrelated application args.
                running = any(Path(arg.decode(errors='replace')).name in ('tidal', 'tidal-server') for arg in args[:2])
                running |= b'tidal.cli' in args or b'tidal.server_cli' in args
                if running:
                    raise RuntimeError('An unmanaged Tidal process remains; stop it before migration')
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue

    def prepare_candidate(self, archive, checksum, configuration):
        archive, configuration = Path(archive).resolve(), Path(configuration).resolve()
        if not re.fullmatch('[0-9a-f]{64}', checksum) or digest(archive) != checksum:
            raise ValueError('Select the exact retained artifact checksum')
        release = self.releases / checksum
        release.mkdir(parents=True, exist_ok=True)
        self.releases.chmod(0o755)
        release.chmod(0o755)
        marker = release / '.source-complete'
        if marker.exists():
            if marker.read_text().strip() != checksum:
                raise ValueError('Existing release directory contains different source')
        else:
            if any(release.iterdir()):
                raise ValueError('Incomplete release extraction requires inspection; choose no replacement state')
            with tarfile.open(archive) as source:
                source.extractall(release, filter='data')
            atomic(marker, checksum + '\n', mode=0o644)
        command([release / '.python/bin/python3.12', release / 'scripts/prepare_release.py', release], timeout=900)
        # The retained archive contains no mutable state or secrets. The service
        # user needs traversal/read access; only root may change the runtime.
        for path in (release, *release.rglob('*')):
            if path.is_symlink():
                os.lchown(path, 0, 0)
            else:
                os.chown(path, 0, 0)
                path.chmod(0o755 if path.is_dir() else 0o644 | (path.stat().st_mode & 0o111))
        metadata = json.loads((release / 'electro-release.json').read_text())
        files = {name: (configuration / name).read_bytes() for name in PRIVATE_FILES}
        files.update({name: value.encode() for name, value in unit_files(release, self.config, self.state, self.user.pw_name).items()})
        identity = hashlib.sha256(json.dumps({name: hashlib.sha256(value).hexdigest() for name, value in files.items()}, sort_keys=True).encode()).hexdigest()
        candidate = self.state / 'candidates' / identity
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        saved = {key: metadata[key] for key in ('revision', 'source_sha256', 'requirements_sha256')}
        saved.update(archive_sha256=checksum, release_path=str(release))
        data = {'application': 'tidal', 'candidate': str(candidate), 'releases': {'application': saved},
            'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
        for name, value in files.items():
            existing = candidate / name
            if existing.exists() and digest(existing) != data['files'][name]:
                raise ValueError('Candidate changed after staging')
            atomic(existing, value)
        write_json(candidate / 'candidate.json', data)
        with tempfile.TemporaryDirectory(prefix='tidal-config-') as temporary:
            root = Path(temporary)
            shutil.chown(root, self.user.pw_uid, self.user.pw_gid)
            for name in PRIVATE_FILES:
                atomic(root / name, files[name], owner=self.user)
            report = self.native(release, ['check-config'], configuration=root,
                environment={'TXN_KEYSTORE_PATH': str(root / 'server-keystore.json')})['data']
        if report['database_path'] != str(self.database) or report['home_path'] != str(self.home):
            raise ValueError('Candidate selects a different database or application home')
        return candidate, data, report['signers']

    def protect(self, operation, repository):
        protected = operation / 'originals'
        if (protected / 'manifest.json').exists():
            manifest = json.loads((protected / 'manifest.json').read_text())
            if any(digest(protected / name) != value for name, value in manifest['files'].items()):
                raise ValueError('Protected original state changed')
            return protected
        protected.mkdir(mode=0o700, exist_ok=True)
        legacy = not (self.state / 'active.json').exists()
        if not legacy:
            record, old_release = self.check_active()
            with tempfile.TemporaryDirectory(prefix='tidal-original-') as temporary:
                shutil.chown(temporary, self.user.pw_uid, self.user.pw_gid)
                snapshot = Path(temporary) / 'tidal.db'
                self.native(old_release, ['db', 'snapshot', '--output', snapshot])
                atomic(protected / 'tidal.db', snapshot.read_bytes())
            write_json(protected / 'previous-release.json', record)
        outbox = self.home / 'server/action_outbox.db'
        for name, source in ((('tidal.db', self.database), ('action_outbox.db', outbox)) if legacy else ()):
            if not source.is_file():
                raise ValueError('First cutover requires both original database files')
            target = protected / name
            if target.exists():
                target.unlink()  # no manifest exists and no migration is allowed yet
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as old, closing(sqlite3.connect(target)) as new:
                old.backup(new)
                if new.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Original SQLite state is invalid')
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            target.chmod(0o600)
        # Preserve the original split setup as data. These directories are small
        # enough to retain whole, including encrypted keys and the old runtimes.
        with tarfile.open(protected / 'legacy-configuration.tar.gz', 'w:gz') as archive:
            for path in (self.home, Path(repository), Path(self.user.pw_dir) / '.local/share/uv/tools/tidal',
                         Path(self.user.pw_dir) / 'server-backup/backup.sh',
                         Path(self.user.pw_dir) / '.local/bin/tidal', Path(self.user.pw_dir) / '.local/bin/tidal-server'):
                if path.exists():
                    archive.add(path, arcname=str(path).lstrip('/'))
            for name in (*SERVICES, *TIMERS, *RETIRED):
                path = self.units / name
                if path.exists():
                    archive.add(path, arcname=str(path).lstrip('/'))
        for path in protected.iterdir():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
        manifest = {'legacy': legacy, 'captured_at': datetime.now(timezone.utc).isoformat(),
            'files': {path.name: digest(path) for path in protected.iterdir() if path.is_file()}}
        write_json(protected / 'manifest.json', manifest)
        return protected

    def publish_originals(self, protected):
        """Keep the pre-migration inputs on the existing mounted destination."""
        command(['mountpoint', '-q', '/mnt/storage-box'])
        destination = Path('/mnt/storage-box/backup/tidal-native')
        destination.mkdir(parents=True, exist_ok=True)
        checksum = digest(protected / 'manifest.json')
        local = protected.parent / ('originals-' + checksum + '.tar.gz')
        if not local.exists():
            with tarfile.open(local, 'x:gz') as bundle:
                bundle.add(protected, arcname='originals')
            with local.open('rb') as stream:
                os.fsync(stream.fileno())
        remote = destination / local.name
        if not remote.exists():
            temporary = remote.with_name('.' + remote.name + '.partial')
            shutil.copyfile(local, temporary)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            if digest(temporary) != digest(local):
                raise ValueError('Original-state Storage Box verification failed')
            temporary.replace(remote)
        if digest(remote) != digest(local):
            raise ValueError('Retained original-state archive changed')
        # A fresh destination read is required before changing the live DB.
        report = {'archive': str(remote), 'sha256': digest(remote), 'manifest_sha256': checksum}
        write_json(protected.parent / 'originals-storage-box.json', report)
        return report

    def install(self, archive, checksum, configuration, repository):
        candidate, data, signers = self.prepare_candidate(archive, checksum, configuration)
        release = Path(data['releases']['application']['release_path'])
        operation = self.state / 'operations' / candidate.name
        for previous in (self.state / 'operations').glob('*/journal.json'):
            if previous.parent != operation and json.loads(previous.read_text())['phase'] != 'complete':
                raise ValueError('Resume the unfinished cutover with its original candidate before selecting another release')
        operation.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal_path = operation / 'journal.json'
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {'phase': 'new', 'candidate': str(candidate)}
        if journal['phase'] == 'complete':
            self.check_active()
            return {'status': 'installed', 'workers_held': (self.state / 'workers-held').exists(), 'candidate': str(candidate)}
        write_json(journal_path, journal)
        self.hold(release)
        self.verify_no_old_process(repository)
        protected = self.protect(operation, repository)
        self.publish_originals(protected)
        journal['phase'] = 'protected'
        write_json(journal_path, journal)
        # Migrate the live working file, with separately retained immutable
        # originals. Retry resumes native migration; it never recopies old state.
        missing = []
        parent = self.config.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for path in reversed(missing):
            path.mkdir(mode=0o755)
            path.chmod(0o755)
        self.config.mkdir(exist_ok=True)
        self.config.chmod(0o750)
        os.chown(self.config, 0, self.user.pw_gid)
        for name in PRIVATE_FILES:
            atomic(self.config / name, (candidate / name).read_bytes(), owner=self.user)
        if json.loads((protected / 'manifest.json').read_text())['legacy']:
            with tempfile.TemporaryDirectory(prefix='tidal-import-') as temporary:
                original = Path(temporary)
                shutil.chown(original, self.user.pw_uid, self.user.pw_gid)
                for name in ('tidal.db', 'action_outbox.db'):
                    atomic(original / name, (protected / name).read_bytes(), owner=self.user)
                report = self.native(release, ['db', 'migrate', '--source-database', original / 'tidal.db', '--outbox', original / 'action_outbox.db'])
        else:
            report = self.native(release, ['db', 'migrate'])
        write_json(operation / 'migration.json', report)
        self.native(release, ['db', 'check'])
        for name in (*SERVICES, *TIMERS):
            atomic(self.units / name, (candidate / name).read_bytes(), mode=0o644)
        # Retired aliases are permanently masked; retain their original unit
        # bytes in the protected archive before removing the old entry points.
        for name in RETIRED:
            path = self.units / name
            path.unlink(missing_ok=True)
            path.symlink_to('/dev/null')
        command(['systemctl', 'daemon-reload'])
        record = {'candidate': str(candidate), 'releases': data['releases'], 'signers': signers,
            'configuration_files': [str(self.config / name) for name in PRIVATE_FILES] +
                [str(self.units / name) for name in (*SERVICES, *TIMERS)],
            'installed_at': datetime.now(timezone.utc).isoformat(), 'originals': str(protected)}
        write_json(self.state / 'active.json', record)
        self.install_entry_points(release, archive)
        self.native(release, ['hold'])
        (self.state / 'held').unlink()
        sync_directory(self.state)
        command(['systemctl', 'enable', 'tidal-api.service'])
        command(['systemctl', 'start', 'tidal-api.service'])
        journal['phase'] = 'complete'
        write_json(journal_path, journal)
        return {'status': 'installed', 'workers_held': True, 'candidate': str(candidate), 'originals': str(protected)}

    def install_entry_points(self, release, archive):
        # Old console script locations now use the selected native runtime.
        # Preserve their previous bytes/links in the original-state archive.
        launcher = f'#!/bin/bash\nset -euo pipefail\nexport TIDAL_HOME={self.home}\nexport TIDAL_CONFIG={self.config}/server.yml\nexport TIDAL_ENV_FILE={self.config}/server.env\nexec {release}/.venv/bin/python -I -m tidal.cli "$@"\n'
        for relative in ('.local/bin/tidal', '.local/bin/tidal-server', 'tidal/venv/bin/tidal', 'tidal/venv/bin/tidal-server',
                         '.local/share/uv/tools/tidal/bin/tidal', '.local/share/uv/tools/tidal/bin/tidal-server'):
            atomic(Path(self.user.pw_dir) / relative, launcher, mode=0o755)
        # Retain the existing application-owned pathname as a strict dispatcher.
        dispatcher = f'#!/bin/bash\nset -euo pipefail\nexec /usr/bin/python3 {release}/scripts/deploy_release.py "$@"\n'
        atomic(Path(self.user.pw_dir) / 'tidal/deploy-tidal.sh', dispatcher, mode=0o755)
        capture = f'#!/bin/bash\nset -euo pipefail\nexec /usr/bin/python3 {release}/scripts/deploy_release.py capture --archive {Path(archive).resolve()}\n'
        atomic('/usr/local/sbin/tidal-backup', capture, mode=0o755)
        from install_daily_capture import integrate
        daily = Path(self.user.pw_dir) / 'server-backup/backup.sh'
        original = daily.read_text()
        saved = self.state / 'original-daily-backup.sh'
        if not saved.exists():
            atomic(saved, original)
        atomic(daily, integrate(original), mode=0o755)

    def check_active(self):
        record = json.loads((self.state / 'active.json').read_text())
        candidate = Path(record['candidate'])
        data = json.loads((candidate / 'candidate.json').read_text())
        if record['releases'] != data['releases']:
            raise ValueError('Active release differs from its candidate')
        for name, expected in data['files'].items():
            target = (self.config if name in PRIVATE_FILES else self.units) / name
            if digest(candidate / name) != expected or digest(target) != expected:
                raise ValueError('Active configuration differs from retained inputs')
        return record, Path(record['releases']['application']['release_path'])

    def capture(self, archive, storage_mount, destination, output):
        command(['mountpoint', '-q', storage_mount])
        record, release = self.check_active()
        checksum = record['releases']['application']['archive_sha256']
        if digest(archive) != checksum:
            raise ValueError('Backup requires the exact installed offline artifact')
        destination, output = Path(destination), Path(output)
        if not destination.resolve().is_relative_to(Path(storage_mount).resolve()):
            raise ValueError('Capture destination must use the selected mounted Storage Box')
        destination.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        capture_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        with tempfile.TemporaryDirectory(prefix='.capture-', dir=output) as temporary:
            stage = Path(temporary)
            with tempfile.TemporaryDirectory(prefix='tidal-snapshot-') as scratch:
                shutil.chown(scratch, self.user.pw_uid, self.user.pw_gid)
                snapshot = Path(scratch) / 'tidal.db'
                native = self.native(release, ['db', 'snapshot', '--output', snapshot])
                shutil.copyfile(snapshot, stage / 'tidal.db')
                if digest(stage / 'tidal.db') != native['data']['sha256']:
                    raise ValueError('Native database snapshot checksum differs from captured bytes')
            for name in PRIVATE_FILES:
                shutil.copyfile(self.config / name, stage / name)
            for name in (*SERVICES, *TIMERS):
                shutil.copyfile(self.units / name, stage / name)
            shutil.copyfile(archive, stage / (checksum + '.tar.gz'))
            write_json(stage / 'release.json', record['releases']['application'])
            write_json(stage / 'native-database.json', native)
            manifest = {'format': 'tidal-capture-v1', 'captured_at': datetime.now(timezone.utc).isoformat(),
                'files': {path.name: digest(path) for path in stage.iterdir()}}
            write_json(stage / 'manifest.json', manifest)
            local = output / (capture_id + '.tar.gz')
            with tarfile.open(local, 'x:gz') as bundle:
                for path in sorted(stage.iterdir()):
                    bundle.add(path, arcname=path.name)
        with local.open('rb') as stream:
            os.fsync(stream.fileno())
        captured_sha = digest(local)
        remote = destination / local.name
        partial = remote.with_name('.' + remote.name + '.partial')
        shutil.copyfile(local, partial)
        with partial.open('rb') as stream:
            os.fsync(stream.fileno())
        if digest(partial) != captured_sha:
            raise ValueError('Storage Box capture verification failed')
        partial.replace(remote)
        result = {'capture': str(remote), 'local_capture': str(local), 'sha256': captured_sha,
            'release_sha256': checksum, 'database': native['data'], 'captured_at': manifest['captured_at']}
        write_json(self.state / 'latest-capture.json', result)
        return result

    def resume(self):
        record, release = self.check_active()
        capture = json.loads((self.state / 'latest-capture.json').read_text())
        if capture['release_sha256'] != record['releases']['application']['archive_sha256'] or digest(capture['capture']) != capture['sha256']:
            raise ValueError('Verify a matching Storage Box capture before enabling execution')
        current = self.native(release, ['db', 'check'])['data']
        if capture['database']['database_identity'] != current['database_identity']:
            raise ValueError('Verify a new capture after replacing the database')
        atomic(self.state / 'workers-held', '')
        for name in (*TIMERS, 'tidal.service', 'tidal-kick.service'):
            command(['systemctl', 'stop', name])
        command(['systemctl', 'disable', *TIMERS])
        self.native(release, ['hold'])
        self.native(release, ['reconcile'], allow_blocked=True)
        self.native(release, ['refresh', '--recovery'])
        self.native(release, ['resume'])
        (self.state / 'workers-held').unlink()
        sync_directory(self.state)
        try:
            command(['systemctl', 'start', 'tidal.service'], timeout=2800)
            command(['systemctl', 'start', 'tidal-kick.service'], timeout=900)
            status = self.native(release, ['status'], allow_blocked=True)
            if not status['data'].get('activated'):
                raise RuntimeError('Initial worker cycle invalidated activation')
            command(['systemctl', 'enable', '--now', *TIMERS])
            return {'status': 'resumed', 'native_status': status}
        except BaseException:
            atomic(self.state / 'workers-held', '')
            for name in (*TIMERS, 'tidal.service', 'tidal-kick.service'):
                command(['systemctl', 'stop', name])
            self.native(release, ['hold'])
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    install = sub.add_parser('install')
    install.add_argument('--archive', type=Path, required=True)
    install.add_argument('--sha256', required=True)
    install.add_argument('--configuration', type=Path, required=True)
    install.add_argument('--legacy-repository', type=Path, default=Path('/home/wavey/tidal'))
    capture = sub.add_parser('capture')
    capture.add_argument('--archive', type=Path, required=True)
    capture.add_argument('--storage-mount', type=Path, default=Path('/mnt/storage-box'))
    capture.add_argument('--destination', type=Path, default=Path('/mnt/storage-box/backup/tidal-native'))
    capture.add_argument('--output', type=Path, default=Path('/var/backups/tidal'))
    sub.add_parser('resume')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the application deployment entry point as root')
    os.umask(0o077)
    deployment = Deployment()
    with deployment.locked():
        if args.operation == 'install':
            result = deployment.install(args.archive, args.sha256, args.configuration, args.legacy_repository)
        elif args.operation == 'capture':
            result = deployment.capture(args.archive, args.storage_mount, args.destination, args.output)
        else:
            result = deployment.resume()
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
