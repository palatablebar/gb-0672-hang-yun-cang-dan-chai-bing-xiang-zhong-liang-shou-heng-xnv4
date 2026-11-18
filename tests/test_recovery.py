import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import app.modules.packaging.rules as packaging_rules
from app import config, main, seals, store
from app.engine import evaluate


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'recovery.sqlite'))
    return TestClient(main.app)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'recovery.sqlite')
    monkeypatch.setattr(main, 'DB_PATH', path)
    return path


@pytest.fixture
def sealed_clean(db):
    run = evaluate({'dataset': 'clean'})
    store.save(db, run)
    return seals.seal_result(db, store.get(db, run['manifest']['run_id']))


def _upgrade_pieces(monkeypatch):
    """模拟规则升级：主单件数被新规则改写，件重与封签随之变化。"""
    original_normalize = packaging_rules.normalize

    def upgraded(row, context):
        out = original_normalize(row, context)
        if not out['parent'].strip():
            out = {**out, 'pieces': out['pieces'] + 1}
        return out
    monkeypatch.setattr(packaging_rules, 'normalize', upgraded)


def test_clean_run_can_be_sealed_once(sealed_clean, db):
    run_id = sealed_clean['run_id']
    seals.seal_result(db, store.get(db, run_id))
    assert len(seals.list_seals(db, run_id)) == 1
    assert sealed_clean['bundle']['input_snapshot']['bytes_b64']
    assert sealed_clean['integrity']['intact'] and sealed_clean['integrity']['seal_ok']


def test_blocked_run_refuses_seal(db):
    run = evaluate({'dataset': 'dirty'})
    store.save(db, run)
    with pytest.raises(Exception) as error:
        seals.seal_result(db, store.get(db, run['manifest']['run_id']))
    assert getattr(error.value, 'code', '') == 'NOT_SEALABLE'
    assert seals.list_seals(db) == []


def test_recovery_same_rules_proves_all_results(sealed_clean, db):
    package = sealed_clean['package_id']
    copy = seals.recover(db, package)
    assert copy['status'] == '可用'
    assert copy['assessment']['decision'] == 'USE_SEALED_EVIDENCE'
    assert copy['assessment']['unprovable'] == []
    assert copy['seals']['copy_matches_original'] is True
    assert [row['field'] for row in copy['comparison'] if row['changed']] == []
    assert copy['row_comparison'] == []


def test_recovery_rule_upgrade_cannot_stand_in_for_original(sealed_clean, db, monkeypatch):
    monkeypatch.setattr(config, 'RULE_VERSION', '2026.11.18-r3')
    _upgrade_pieces(monkeypatch)

    copy = seals.recover(db, sealed_clean['package_id'])
    assert copy['status'] == '可用'
    assert copy['assessment']['decision'] == 'WAIT_ORIGINAL_ENV'
    changed = {row['field'] for row in copy['comparison'] if row['changed']}
    assert {'pieces', 'seal'} <= changed
    unprovable = {row['field'] for row in copy['assessment']['unprovable']}
    assert {'pieces', 'seal'} <= unprovable
    assert copy['seals']['copy_matches_original'] is False
    assert copy['source_rule_version'] == sealed_clean['rule_version']
    assert copy['rule_version'] == '2026.11.18-r3'
    assert any(row['field'] == 'pieces' for row in copy['row_comparison'])


def test_retry_does_not_create_second_usable_copy(sealed_clean, db):
    first = seals.recover(db, sealed_clean['package_id'])
    second = seals.recover(db, sealed_clean['package_id'])
    assert first['copy_id'] == second['copy_id']
    records = [r for r in seals.list_recoveries(db) if r['package_id'] == sealed_clean['package_id']]
    assert len(records) == 1
    assert records[0]['status'] == '可用'


def test_rerun_after_rule_upgrade_updates_same_copy(sealed_clean, db, monkeypatch):
    first = seals.recover(db, sealed_clean['package_id'])
    monkeypatch.setattr(config, 'RULE_VERSION', '2026.11.18-r3')
    second = seals.recover(db, sealed_clean['package_id'])
    assert first['copy_id'] == second['copy_id']
    assert second['rule_version'] == '2026.11.18-r3'
    records = [r for r in seals.list_recoveries(db) if r['package_id'] == sealed_clean['package_id']]
    assert len(records) == 1 and records[0]['status'] == '可用'


def test_failed_recovery_keeps_seal_and_current_result(sealed_clean, db, monkeypatch):
    package_id = sealed_clean['package_id']
    run_id = sealed_clean['run_id']
    before_run = store.get(db, run_id)

    def broken(*args, **kwargs):
        raise RuntimeError('current rules unavailable')
    monkeypatch.setattr(seals, 'evaluate', broken)

    failed = seals.recover(db, package_id)
    assert failed['status'] == '失败' and failed['error']['code'] == 'RECOVERY_FAILED'
    assert seals.get_seal(db, package_id)['integrity']['seal_ok'] is True
    assert store.get(db, run_id)['archive']['seal'] == before_run['archive']['seal']
    records = [r for r in seals.list_recoveries(db) if r['package_id'] == package_id]
    assert len(records) == 1 and records[0]['status'] == '失败'

    retry = seals.recover(db, package_id)
    records = [r for r in seals.list_recoveries(db) if r['package_id'] == package_id]
    assert len(records) == 1 and records[0]['status'] == '失败'
    assert retry['copy'] is None and retry['attempts'] == 2


def test_failed_retry_keeps_previous_usable_copy(sealed_clean, db, monkeypatch):
    first = seals.recover(db, sealed_clean['package_id'])
    monkeypatch.setattr(config, 'RULE_VERSION', '2026.11.18-r3')

    def broken(*args, **kwargs):
        raise RuntimeError('upgraded rules unavailable')
    monkeypatch.setattr(seals, 'evaluate', broken)

    result = seals.recover(db, sealed_clean['package_id'])
    assert result['status'] == '可用' and result['copy_id'] == first['copy_id']
    assert result['last_failure']['code'] == 'RECOVERY_FAILED'
    records = [r for r in seals.list_recoveries(db) if r['package_id'] == sealed_clean['package_id']]
    assert len(records) == 1 and records[0]['status'] == '可用'


def test_tampered_bundle_never_yields_copy(sealed_clean, db):
    package = seals.get_seal(db, sealed_clean['package_id'])
    bundle = package['bundle']
    bundle['evidence']['pieces'] += 1
    with store.connect(db) as con:
        con.execute('UPDATE seals SET bundle=? WHERE package_id=?',
                    (json.dumps(bundle, ensure_ascii=False), sealed_clean['package_id']))
    failed = seals.recover(db, sealed_clean['package_id'])
    assert failed['status'] == '失败' and failed['error']['code'] == 'SEAL_UNVERIFIED'
    assert failed['integrity']['intact'] is False


def test_bundle_export_import_roundtrip(client):
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    package = client.post('/api/runs/' + run['manifest']['run_id'] + '/seal').json()
    bundle_zip = client.get('/api/seals/' + package['package_id'] + '/bundle')
    assert bundle_zip.status_code == 200

    tampered = json.loads(zipfile.ZipFile(io.BytesIO(bundle_zip.content)).read('seal-bundle.json'))
    tampered['evidence']['gross_kg'] = '9999'
    payload = json.dumps({'bundle': tampered}).encode()
    rejected = client.post('/api/seals/import', content=payload, headers={'Content-Type': 'application/json'})
    assert rejected.status_code == 422
    assert rejected.json()['code'] == 'SEAL_UNVERIFIED'

    accepted = client.post('/api/seals/import', content=bundle_zip.content,
                           headers={'Content-Type': 'application/zip'})
    assert accepted.status_code == 200
    assert accepted.json()['package_id'] == package['package_id']


def test_recovery_api_and_readback_page(client):
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    package = client.post('/api/runs/' + run['manifest']['run_id'] + '/seal').json()
    copy = client.post('/api/recover/' + package['package_id'])
    assert copy.status_code == 200
    copy = copy.json()
    assert copy['copy_id'].startswith('rcp-')
    page = client.get('/recover?package_id=' + package['package_id'])
    assert page.status_code == 200
    body = page.text
    assert '原封签复算' in body
    assert '原件与副本差异' in body
    assert '由封存包' in body
    assert copy['copy_id'] in body
    exported = client.get('/api/recover/' + copy['copy_id'] + '/export')
    assert exported.status_code == 200
    assert 'comparison.json' in zipfile.ZipFile(io.BytesIO(exported.content)).namelist()
    again = client.post('/api/recover/' + package['package_id'])
    assert again.status_code == 200 and again.json()['copy_id'] == copy['copy_id']
    assert len([r for r in client.get('/api/seals').json() if r['package_id'] == package['package_id']]) == 1


def test_recovery_failure_is_conflict_and_page_stays_on_original(client, monkeypatch):
    run = client.post('/api/runs', json={'dataset': 'clean'}).json()
    package = client.post('/api/runs/' + run['manifest']['run_id'] + '/seal').json()

    def broken(*args, **kwargs):
        raise RuntimeError('current rules unavailable')
    monkeypatch.setattr(seals, 'evaluate', broken)
    response = client.post('/api/recover/' + package['package_id'])
    assert response.status_code == 409
    assert response.json()['status'] == '失败'
    page = client.get('/recover?package_id=' + package['package_id'])
    assert page.status_code == 200
    assert '旧封存件' in page.text
    assert client.get('/api/seals/' + package['package_id']).status_code == 200
    assert client.post('/api/recover/' + package['package_id']).status_code == 409
