"""Deployment failures preserve originals and keep execution persistently held."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[2] / 'scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


deploy = module('deploy_release')
daily = module('install_daily_capture')


@pytest.fixture
def host(tmp_path, monkeypatch):
    user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(tmp_path / 'wavey'), pw_name='wavey')
    Path(user.pw_dir).mkdir()
    monkeypatch.setattr(deploy.pwd, 'getpwnam', lambda _: user)
    monkeypatch.setattr(deploy.os, 'chown', lambda *args: None)
    instance = deploy.Deployment(home=tmp_path / 'home', state=tmp_path / 'state', config=tmp_path / 'config',
        units=tmp_path / 'units', lock=tmp_path / 'locks/application-tidal', releases=tmp_path / 'releases')
    instance.database.parent.mkdir(parents=True)
    for filename, table in (('tidal.db', 'operations'), ('action_outbox.db', 'action_report_outbox')):
        with sqlite3.connect(instance.database.parent / filename) as database:
            database.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY, amount TEXT)')
            database.execute(f'INSERT INTO {table} VALUES (?, ?)', ('retained', '99999999999999999999999999999'))
    commands, native_calls = [], []
    def command(args, **kwargs):
        commands.append(list(map(str, args)))
        return 'LoadState=loaded\nActiveState=active\nMainPID=0\n' if 'show' in args else ''
    monkeypatch.setattr(deploy, 'command', command)
    monkeypatch.setattr(instance, 'verify_no_old_process', lambda _: None)
    monkeypatch.setattr(instance, 'install_entry_points', lambda *_: None)
    monkeypatch.setattr(instance, 'publish_originals', lambda _: native_calls.append(['originals-retrieved']))
    release = instance.releases / ('a' * 64)
    release.mkdir(parents=True)
    candidate = instance.state / 'candidates/first'
    candidate.mkdir(parents=True)
    contents = {name: 'private fixture' for name in deploy.PRIVATE_FILES}
    contents.update(deploy.unit_files(release, instance.config, instance.state, 'wavey'))
    data = {'application': 'tidal', 'candidate': str(candidate), 'releases': {'application': {
        'archive_sha256': 'a' * 64, 'release_path': str(release)}}, 'files': {}}
    for name, text in contents.items():
        (candidate / name).write_text(text)
        data['files'][name] = deploy.digest(candidate / name)
    deploy.write_json(candidate / 'candidate.json', data)
    monkeypatch.setattr(instance, 'prepare_candidate', lambda *_, **__: (
        candidate, json.loads((candidate / 'candidate.json').read_text()), {'scan': 'address', 'kick': 'address'}))
    def native(_release, args, **kwargs):
        args = list(map(str, args))
        native_calls.append(args)
        if args[:2] == ['db', 'migrate']:
            assert ['originals-retrieved'] in native_calls
            assert (instance.state / 'held').exists() and (instance.state / 'workers-held').exists()
            source = Path(args[args.index('--source-database') + 1])
            with sqlite3.connect(source) as original:
                assert original.execute('SELECT amount FROM operations WHERE id="retained"').fetchone()[0].endswith('99999')
            with sqlite3.connect(instance.database) as current:
                current.execute('INSERT OR IGNORE INTO operations VALUES ("imported", "1")')
            if instance.interrupt:
                raise InterruptedError('interrupted after native commit')
        if args[:2] == ['db', 'snapshot']:
            target = Path(args[args.index('--output') + 1])
            with sqlite3.connect(instance.database) as old, sqlite3.connect(target) as new:
                old.backup(new)
            return {'data': {'sha256': deploy.digest(target), 'database_identity': 'fixture', 'schema_revision': 'fixture-schema'}}
        if args[:2] == ['db', 'prepare-restore']:
            with sqlite3.connect(instance.database) as current:
                current.execute('INSERT OR IGNORE INTO operations VALUES ("prepared", "1")')
            if instance.interrupt:
                raise InterruptedError('interrupted after native restore preparation')
        return {'data': {'activated': True, 'database_identity': 'fixture', 'schema_revision': 'fixture-schema'},
            'blockers': [], 'interface_version': 1}
    monkeypatch.setattr(instance, 'native', native)
    instance.interrupt = False
    return SimpleNamespace(app=instance, commands=commands, calls=native_calls,
        candidate=candidate, release=release, repository=tmp_path / 'old-repository')


def install(host):
    return host.app.install('/fixture/archive', 'a' * 64, '/fixture/config', host.repository)


def test_first_cutover_protects_exact_originals_and_starts_only_the_api(host):
    result = install(host)
    protected = Path(result['originals'])
    with sqlite3.connect(protected / 'tidal.db') as database:
        assert database.execute('SELECT id FROM operations').fetchall() == [('retained',)]
    assert host.calls.index(['originals-retrieved']) < next(i for i, args in enumerate(host.calls) if args[:2] == ['db', 'migrate'])
    assert not (host.app.state / 'held').exists()
    assert (host.app.state / 'workers-held').exists()
    starts = [args[-1] for args in host.commands if args[:2] == ['systemctl', 'start']]
    assert starts == ['tidal-api.service']
    assert all((host.app.units / name).is_symlink() for name in deploy.RETIRED)
    assert all((host.app.units / name).readlink() == Path('/dev/null') for name in deploy.RETIRED)
    assert not any('ExecStartPre=' in (host.app.units / name).read_text() for name in deploy.SERVICES)
    assert all(host.app.config.joinpath(name).stat().st_mode & 0o777 == 0o600 for name in deploy.PRIVATE_FILES)


def test_interruption_keeps_holds_and_retry_never_recopies_old_database(host):
    host.app.interrupt = True
    with pytest.raises(InterruptedError):
        install(host)
    assert (host.app.state / 'held').exists() and (host.app.state / 'workers-held').exists()
    with sqlite3.connect(host.app.database) as database:
        database.execute('INSERT INTO operations VALUES ("new-identity", "2")')
    host.app.interrupt = False
    install(host)
    with sqlite3.connect(host.app.database) as database:
        assert {row[0] for row in database.execute('SELECT id FROM operations')} == {'retained', 'imported', 'new-identity'}
    calls = len(host.calls)
    install(host)
    assert len(host.calls) == calls  # completed retry neither migrates nor resumes


def test_original_tampering_refuses_retry_while_workers_remain_held(host):
    host.app.interrupt = True
    with pytest.raises(InterruptedError):
        install(host)
    protected = host.app.state / 'operations/first/originals/tidal.db'
    protected.write_bytes(b'corrupt')
    host.app.interrupt = False
    with pytest.raises(ValueError, match='original state changed'):
        install(host)
    assert (host.app.state / 'workers-held').exists()


def test_original_external_foundry_key_is_retained_and_checked(host, tmp_path):
    config = tmp_path / 'input'
    config.mkdir()
    key = tmp_path / 'external-foundry/tidal-prod'
    key.parent.mkdir()
    key.write_text('original encrypted key without public address')
    deploy.write_json(config / 'preflight.json', {'original_keystore': str(key),
        'original_keystore_sha256': deploy.digest(key), 'legacy_configurations': {}, 'legacy_secret_files': {}})
    operation = tmp_path / 'operation'
    operation.mkdir()
    protected = host.app.protect(operation, host.repository, config)
    with deploy.tarfile.open(protected / 'legacy-configuration.tar.gz') as archive:
        assert archive.extractfile(str(key).lstrip('/')).read() == key.read_bytes()
    key.write_text('different key')
    another = tmp_path / 'another-operation'
    another.mkdir()
    with pytest.raises(ValueError, match='key changed'):
        host.app.protect(another, host.repository, config)


@pytest.fixture
def saved_capture(host, tmp_path):
    install(host)
    archive = tmp_path / 'release.tar.gz'
    archive.write_bytes(b'fixture offline release')
    checksum = deploy.digest(archive)
    record = json.loads((host.app.state / 'active.json').read_text())
    record['releases']['application']['archive_sha256'] = checksum
    deploy.write_json(host.app.state / 'active.json', record)
    candidate = json.loads((host.candidate / 'candidate.json').read_text())
    candidate['releases'] = record['releases']
    deploy.write_json(host.candidate / 'candidate.json', candidate)
    (host.app.home / 'activation.json').write_text('must not restore')
    return host.app.capture(archive, tmp_path / 'mounted', tmp_path / 'mounted/captures', tmp_path / 'captures')


def test_daily_capture_is_complete_verified_and_never_contains_activation(host, saved_capture):
    result = saved_capture
    checksum = result['release_sha256']
    assert deploy.digest(result['capture']) == deploy.digest(result['local_capture']) == result['sha256']
    with deploy.tarfile.open(result['capture']) as bundle:
        names = set(bundle.getnames())
        assert names == {*deploy.PRIVATE_FILES, *deploy.SERVICES, *deploy.TIMERS,
            'tidal.db', checksum + '.tar.gz', 'native-database.json', 'release.json', 'manifest.json'}
        manifest = json.load(bundle.extractfile('manifest.json'))
        assert all(hashlib.sha256(bundle.extractfile(name).read()).hexdigest() == expected for name, expected in manifest['files'].items())


def test_local_capture_reuses_native_format_without_claiming_storage_success(host, saved_capture, tmp_path):
    archive = host.app.releases.parent / 'tidal-artifacts' / (saved_capture['release_sha256'] + '.tar.gz')
    archive.parent.mkdir()
    archive.write_bytes((tmp_path / 'release.tar.gz').read_bytes())
    previous = (host.app.state / 'latest-capture.json').read_bytes()
    host.commands.clear()
    output = tmp_path / 'staging/capture.tar'
    captured = host.app.capture(None, '/unavailable', '/unavailable/captures', output, local_only=True)
    assert not any(args[0] == 'mountpoint' for args in host.commands)
    assert (host.app.state / 'latest-capture.json').read_bytes() == previous
    assert output.read_bytes()[:2] != b'\x1f\x8b'
    with deploy.tarfile.open(output, 'r:') as bundle:
        assert json.load(bundle.extractfile('manifest.json'))['format'] == 'tidal-capture-v1'
        assert bundle.extractfile(archive.name).read() == archive.read_bytes()
    assert restore(host, captured)['status'] == 'restored'


def test_reconciliation_uses_native_procedure_and_holds_workers_without_stopping_api(host):
    install(host)
    host.commands.clear()
    result = host.app.reconcile()
    assert host.calls[-1] == ['reconcile']
    assert json.loads((host.app.state / 'reconciliation.json').read_text()) == result
    assert (host.app.state / 'workers-held').exists() and not (host.app.state / 'held').exists()
    assert ['systemctl', 'stop', 'tidal-api.service'] not in host.commands


def test_hold_on_replacement_host_disables_only_installed_timers(host, monkeypatch):
    def command(args, **kwargs):
        host.commands.append(list(map(str, args)))
        if 'show' in args:
            loaded = 'loaded' if args[2] == 'tidal.timer' else 'not-found'
            return f'LoadState={loaded}\nActiveState=inactive\nMainPID=0\n'
        return ''
    monkeypatch.setattr(deploy, 'command', command)
    host.app.hold(host.release)
    assert [args for args in host.commands if args[:2] == ['systemctl', 'disable']] == [
        ['systemctl', 'disable', 'tidal.timer']]
    assert (host.app.state / 'held').exists() and (host.app.state / 'workers-held').exists()


def restore(host, capture, *, overwrite=True):
    return host.app.restore(capture['capture'], capture['sha256'], overwrite=overwrite)


def test_restore_uses_saved_state_and_protects_displaced_state_without_migrating(host, saved_capture):
    with sqlite3.connect(host.app.database) as database:
        database.execute('INSERT INTO operations VALUES ("after-capture", "2")')
    host.calls.clear()
    host.commands.clear()
    result = restore(host, saved_capture)
    with sqlite3.connect(Path(result['previous_state']) / '0') as database:
        assert database.execute('SELECT id FROM operations WHERE id="after-capture"').fetchone()
    with sqlite3.connect(host.app.database) as database:
        assert {row[0] for row in database.execute('SELECT id FROM operations')} == {'retained', 'imported', 'prepared'}
    assert not any(args[:2] == ['db', 'migrate'] for args in host.calls)
    assert [args[-1] for args in host.commands if args[:2] == ['systemctl', 'start']] == ['tidal-api.service']
    assert result['workers_held'] and (host.app.state / 'workers-held').exists()
    assert json.loads((host.app.state / 'latest-capture.json').read_text())['origin'] == 'verified-restore'
    host.app.resume()  # The independently retrieved capture satisfies the existing gate.


def test_restore_retry_preserves_native_preparation_and_subsequent_state(host, saved_capture):
    host.app.interrupt = True
    with pytest.raises(InterruptedError, match='restore preparation'):
        restore(host, saved_capture)
    assert (host.app.state / 'held').exists() and (host.app.state / 'workers-held').exists()
    with sqlite3.connect(host.app.database) as database:
        database.execute('INSERT INTO operations VALUES ("after-preparation", "2")')
    for operation in (lambda: install(host), host.app.resume):
        with pytest.raises(ValueError, match='interrupted restore'):
            operation()
    host.app.interrupt = False
    restore(host, saved_capture)
    calls = len([args for args in host.calls if args[:2] == ['db', 'prepare-restore']])
    assert restore(host, saved_capture)['changed'] is False
    assert len([args for args in host.calls if args[:2] == ['db', 'prepare-restore']]) == calls
    with sqlite3.connect(host.app.database) as database:
        assert database.execute('SELECT id FROM operations WHERE id="after-preparation"').fetchone()


@pytest.mark.parametrize('existing', ['database', 'sidecar', 'empty'])
def test_restore_requires_explicit_overwrite_only_when_state_exists(host, saved_capture, existing):
    if existing != 'database':
        host.app.database.unlink()
    if existing == 'sidecar':
        Path(str(host.app.database) + '-wal').write_bytes(b'uncertain retained state')
    if existing == 'empty':
        assert restore(host, saved_capture, overwrite=False)['status'] == 'restored'
    else:
        with pytest.raises(ValueError, match='explicit restore overwrite'):
            restore(host, saved_capture, overwrite=False)


@pytest.mark.parametrize('problem', ['checksum', 'unknown-phase', 'unfinished-install', 'different-restore'])
def test_invalid_or_conflicting_restore_does_not_change_database(host, saved_capture, problem):
    before = deploy.digest(host.app.database)
    capture = dict(saved_capture)
    if problem == 'checksum':
        capture['sha256'] = '0' * 64
    elif problem == 'unfinished-install':
        deploy.write_json(host.app.state / 'operations/first/journal.json', {'phase': 'protected'})
    else:
        selected = capture['sha256'] if problem == 'unknown-phase' else '0' * 64
        deploy.write_json(host.app.state / 'recovery' / selected / 'restore.json',
            {'phase': 'unrecognized' if problem == 'unknown-phase' else 'installed'})
    with pytest.raises(ValueError):
        restore(host, capture)
    assert deploy.digest(host.app.database) == before


@pytest.mark.parametrize('problem', ['member-hash', 'schema', 'release', 'duplicate', 'link'])
def test_restore_rejects_inconsistent_capture_before_changing_live_state(host, saved_capture, tmp_path, problem):
    with deploy.tarfile.open(saved_capture['capture']) as original:
        contents = {member.name: original.extractfile(member).read() for member in original.getmembers()}
    manifest = json.loads(contents['manifest.json'])
    if problem == 'member-hash':
        contents['tidal.db'] = b'corrupt snapshot'
    elif problem in ('schema', 'release'):
        name = 'native-database.json' if problem == 'schema' else 'release.json'
        value = json.loads(contents[name])
        if problem == 'schema':
            value['data']['schema_revision'] = 'incompatible'
        else:
            value['revision'] = 'different'
        contents[name] = json.dumps(value).encode()
        manifest['files'][name] = hashlib.sha256(contents[name]).hexdigest()
        contents['manifest.json'] = json.dumps(manifest).encode()
    changed = tmp_path / 'changed.tar.gz'
    with deploy.tarfile.open(changed, 'w:gz') as bundle:
        for name, content in contents.items():
            member = deploy.tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        if problem in ('duplicate', 'link'):
            member = deploy.tarfile.TarInfo('tidal.db' if problem == 'duplicate' else 'indirect')
            if problem == 'link':
                member.type, member.linkname = deploy.tarfile.SYMTYPE, '/outside'
            bundle.addfile(member)
    before = deploy.digest(host.app.database)
    with pytest.raises(ValueError):
        host.app.restore(changed, deploy.digest(changed), overwrite=True)
    assert deploy.digest(host.app.database) == before


def test_replacement_host_does_not_need_the_retired_daily_mirror(host, monkeypatch):
    written = {}
    monkeypatch.setattr(deploy, 'atomic', lambda path, content, **_: written.update({str(path): content}))
    monkeypatch.setitem(sys.modules, 'install_daily_capture', daily)
    deploy.Deployment.install_entry_points(host.app, host.release, '/fixture/archive')
    assert '/usr/local/sbin/tidal-backup' in written
    assert not any(path.endswith('/server-backup/backup.sh') for path in written)


def test_resume_requires_matching_capture_before_any_native_activation(host):
    install(host)
    calls = list(host.calls)
    with pytest.raises(FileNotFoundError):
        host.app.resume()
    assert host.calls == calls
    assert (host.app.state / 'workers-held').exists()


@pytest.mark.parametrize('fail_cycle', [False, True])
def test_resume_checks_natively_then_runs_ordinary_cycles_and_reholds_on_failure(host, tmp_path, monkeypatch, fail_cycle):
    install(host)
    remote = tmp_path / 'remote-capture'
    remote.write_bytes(b'retrieved capture')
    deploy.write_json(host.app.state / 'latest-capture.json', {'release_sha256': 'a' * 64,
        'capture': str(remote), 'sha256': deploy.digest(remote), 'database': {'database_identity': 'fixture'}})
    command = deploy.command
    def run(args, **kwargs):
        if args == ['systemctl', 'start', 'tidal.service'] and fail_cycle:
            raise RuntimeError('fixture cycle failed')
        return command(args, **kwargs)
    monkeypatch.setattr(deploy, 'command', run)
    if fail_cycle:
        with pytest.raises(RuntimeError, match='cycle failed'):
            host.app.resume()
        assert (host.app.state / 'workers-held').exists()
        assert host.calls[-1] == ['hold']
        assert ['systemctl', 'stop', 'tidal-kick.service'] in host.commands
    else:
        assert host.app.resume()['status'] == 'resumed'
        assert not (host.app.state / 'workers-held').exists()
        assert host.calls[-4:] == [['reconcile'], ['refresh', '--recovery'], ['resume'], ['status']]
        assert ['systemctl', 'start', 'tidal.service'] in host.commands
        assert ['systemctl', 'start', 'tidal-kick.service'] in host.commands
        assert host.commands[-1] == ['systemctl', 'enable', '--now', *deploy.TIMERS]


def test_capture_from_another_database_cannot_authorize_resume(host, tmp_path):
    install(host)
    remote = tmp_path / 'remote-capture'
    remote.write_bytes(b'retrieved capture')
    deploy.write_json(host.app.state / 'latest-capture.json', {'release_sha256': 'a' * 64,
        'capture': str(remote), 'sha256': deploy.digest(remote), 'database': {'database_identity': 'other'}})
    with pytest.raises(ValueError, match='after replacing the database'):
        host.app.resume()
    assert ['resume'] not in host.calls


def test_one_outer_lock_serializes_capture_deploy_and_infrastructure(host):
    with host.app.locked():
        with pytest.raises(BlockingIOError):
            with host.app.locked():
                pytest.fail('second deployment obtained the shared lock')


def test_inactive_malformed_legacy_timer_does_not_prevent_stopping_live_api(host, monkeypatch):
    original_command = deploy.command
    def command(args, **kwargs):
        if args[:3] == ['systemctl', 'show', 'tidal-kicker.timer']:
            return 'LoadState=bad-setting\nActiveState=inactive\nMainPID=0\n'
        if args[:3] == ['systemctl', 'stop', 'tidal-kicker.timer']:
            pytest.fail('systemd refuses stop on this already inactive legacy unit')
        return original_command(args, **kwargs)
    monkeypatch.setattr(deploy, 'command', command)
    install(host)
    assert ['systemctl', 'stop', 'tidal-api.service'] in host.commands


@pytest.mark.parametrize('protected', [False, True])
def test_corrected_installer_can_only_supersede_before_any_protected_transition(host, protected):
    previous = host.app.state / 'operations/older'
    previous.mkdir(parents=True)
    deploy.write_json(previous / 'journal.json', {'phase': 'new', 'candidate': '/old'})
    if protected:
        deploy.write_json(previous / 'originals/manifest.json', {'files': {}})
        with pytest.raises(ValueError, match='unfinished cutover'):
            install(host)
    else:
        install(host)
        assert json.loads((previous / 'journal.json').read_text())['phase'] == 'superseded-before-protection'


def test_daily_route_update_is_idempotent_and_preserves_existing_jobs():
    source = '# existing migration archive\noverall_status=0\n# existing rsync\nrsync -avz --delete "$dir" "$DEST_DIR"\n'
    updated = daily.integrate(source)
    assert daily.integrate(updated) == updated
    assert updated.count('/usr/local/sbin/tidal-backup') == 1
    assert '# existing migration archive' in updated and '# existing rsync' in updated
    assert '--delete-excluded' not in updated
    assert "'/wavey/.tidal/***'" in updated
    assert updated.index('/usr/local/sbin/tidal-backup') < updated.index('rsync -avz')
    with pytest.raises(ValueError):
        daily.integrate('different backup engine\n')


def test_api_unit_removes_signing_environment_without_changing_worker_credentials():
    units = deploy.unit_files('/release', '/config', '/state', 'wavey')
    assert 'UnsetEnvironment=TXN_KEYSTORE_PATH TXN_KEYSTORE_PASSPHRASE' in units['tidal-api.service']
    assert 'UnsetEnvironment' not in units['tidal.service']
    assert 'UnsetEnvironment' not in units['tidal-kick.service']
