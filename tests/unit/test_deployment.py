"""Deployment failures preserve originals and keep execution persistently held."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
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
    monkeypatch.setattr(instance, 'prepare_candidate', lambda *_: (candidate, data, {'scan': 'address', 'kick': 'address'}))
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
            return {'data': {'sha256': deploy.digest(target), 'database_identity': 'fixture'}}
        return {'data': {'activated': True, 'database_identity': 'fixture'}, 'blockers': [], 'interface_version': 1}
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


def test_daily_capture_is_complete_verified_and_never_contains_activation(host, tmp_path):
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
    result = host.app.capture(archive, tmp_path / 'mounted', tmp_path / 'mounted/captures', tmp_path / 'captures')
    assert deploy.digest(result['capture']) == deploy.digest(result['local_capture']) == result['sha256']
    with deploy.tarfile.open(result['capture']) as bundle:
        names = set(bundle.getnames())
        assert names == {*deploy.PRIVATE_FILES, *deploy.SERVICES, *deploy.TIMERS,
            'tidal.db', checksum + '.tar.gz', 'native-database.json', 'release.json', 'manifest.json'}
        manifest = json.load(bundle.extractfile('manifest.json'))
        assert all(hashlib.sha256(bundle.extractfile(name).read()).hexdigest() == expected for name, expected in manifest['files'].items())


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
