import hashlib
import json
from datetime import datetime, timezone

from app import config
from app.engine import evaluate
from app.errors import BusinessError
from app.evidence import business_summary, canonical, comparison, row_comparison, seal_from
from app.store import connect

BUNDLE_SCHEMA = 'seal-bundle-v1'
COPY_SCHEMA = 'recovery-copy-v1'
CRITICAL_FIELDS = ('pieces', 'net_kg', 'gross_kg', 'seal')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rule_version():
    return config.RULE_VERSION


def _package_id(manifest, evidence, snapshot):
    # 包标识只取内容，不含封存时间与生成耗时：同一份证据重复封存仍是同一个包。
    manifest = manifest or {}
    identity = {'schema': BUNDLE_SCHEMA,
                'manifest': {k: manifest.get(k) for k in ('run_id', 'sha256', 'rule_version', 'source')},
                'evidence': evidence, 'input_snapshot': snapshot}
    return 'pkg-' + hashlib.sha256(canonical(identity)).hexdigest()[:16]


def _copy_id(package_id):
    # 副本标识只由封存包决定：同一包无论重试或规则升级都只有一套可用副本。
    return 'rcp-' + hashlib.sha256(package_id.encode()).hexdigest()[:16]


def verify_bundle(bundle):
    """重算包标识并复算封签，任一不符即视为封存件不可采信。"""
    package_id = _package_id(bundle.get('manifest'), bundle.get('evidence'), bundle.get('input_snapshot'))
    evidence = bundle.get('evidence') or {}
    recomputed = seal_from(evidence) if evidence.get('roots') is not None else None
    seal_ok = bool(evidence.get('seal')) and recomputed == evidence.get('seal')
    return {'package_id': package_id, 'intact': package_id == bundle.get('package_id'),
            'seal_ok': seal_ok, 'seal_recomputed': recomputed}


def seal_result(path, result):
    """把当前可封存批次封成不可变包。同一结果重复封存只保留一个包。"""
    if not result.get('manifest') or not result.get('views') or 'input_snapshot' not in result:
        raise BusinessError('INCOMPLETE_RUN', '批次尚未完成，无法封存')
    if (result.get('archive') or {}).get('status') != '可封存':
        raise BusinessError('NOT_SEALABLE', '存在未处理阻断项，不能封存')
    summary = business_summary(result)
    if seal_from(summary) != summary['seal']:
        raise BusinessError('SEAL_BROKEN', '封签无法由封存证据复算')
    source_manifest = result['manifest']
    manifest = {k: source_manifest[k] for k in ('run_id', 'sha256', 'rule_version', 'source', 'generated_at')}
    body = {
        'schema': BUNDLE_SCHEMA,
        'manifest': manifest,
        'evidence': summary,
        'input_snapshot': result['input_snapshot'],
        'sealed_at': _now(),
    }
    package_id = _package_id(manifest, summary, result['input_snapshot'])
    bundle = {'package_id': package_id, **body}
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO seals(package_id,run_id,seal,rule_version,bundle,created_at) VALUES(?,?,?,?,?,?)',
                    (package_id, manifest['run_id'], summary['seal'], manifest['rule_version'],
                     json.dumps(bundle, ensure_ascii=False), bundle['sealed_at']))
    return get_seal(path, package_id)


def get_seal(path, package_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM seals WHERE package_id=?', (package_id,)).fetchone()
    if not row:
        raise BusinessError('SEAL_NOT_FOUND', '封存包不存在', 404)
    bundle = json.loads(row['bundle'])
    return {'package_id': row['package_id'], 'run_id': row['run_id'], 'seal': row['seal'],
            'rule_version': row['rule_version'], 'created_at': row['created_at'],
            'bundle': bundle, 'integrity': verify_bundle(bundle)}


def list_seals(path, run_id=None):
    with connect(path) as con:
        if run_id:
            rows = con.execute('SELECT * FROM seals WHERE run_id=? ORDER BY created_at DESC,rowid DESC', (run_id,)).fetchall()
        else:
            rows = con.execute('SELECT * FROM seals ORDER BY created_at DESC,rowid DESC').fetchall()
    return [{'package_id': r['package_id'], 'run_id': r['run_id'], 'seal': r['seal'],
             'rule_version': r['rule_version'], 'created_at': r['created_at']} for r in rows]


def seal_of_run(path, run_id):
    rows = list_seals(path, run_id)
    return get_seal(path, rows[0]['package_id']) if rows else None


def _assessment(package_version, integrity, differences, row_differences=()):
    """按“哪些业务结果仍能被证明”逐项裁决，而不是整份重算后冒充原件。"""
    provable, unprovable = [], []
    for row in differences:
        if row['changed']:
            unprovable.append({'field': row['field'], 'label': row['label'], 'original': row['original'],
                               'recovered': row['recovered'],
                               'basis': '原件值由原规则版本封存；副本按现规则重算，不能作为原封件的证明'})
        else:
            provable.append({'field': row['field'], 'label': row['label'], 'value': row['original'],
                             'basis': '原件与副本一致，仍可由封存证据复核'})
    changed_critical = [row['field'] for row in differences if row['changed'] and row['field'] in CRITICAL_FIELDS]
    if row_differences:
        # 逐单差异即使未汇总进总件重，也已说明副本不是原采信集合，须整单回原规则环境确认。
        for field in ('pieces', 'net_kg', 'gross_kg', 'existence'):
            if any(row['field'] == field for row in row_differences) and field not in changed_critical:
                changed_critical.append(field)
    same_rules = package_version == _rule_version()
    if not integrity['intact']:
        verdict = '封存包完整性校验失败，不能作为恢复来源；保留旧封存件，须回原规则环境重建'
        decision = 'REBUILD_ORIGINAL_ENV'
        basis = 'package_id 重算不符'
    elif not integrity['seal_ok']:
        verdict = '原封签复算不符，封存证据不可采信；保留旧封存件，须回原规则环境重建'
        decision = 'REBUILD_ORIGINAL_ENV'
        basis = '封签复算与原件不一致'
    elif changed_critical:
        verdict = '副本件重或封签已变，不能冒充原封件；受影响申报结果须等原规则环境重建，副本仅作差异复核'
        decision = 'WAIT_ORIGINAL_ENV'
        basis = '关键结果不可证明：' + '、'.join(changed_critical)
    elif same_rules:
        verdict = '原规则环境与现规则一致，业务结果均可由封存证据复核，无需重建整份舱单'
        decision = 'USE_SEALED_EVIDENCE'
        basis = '件重与封签均与原件一致'
    else:
        verdict = '件重与封签均未变，其余非关键字段变化不影响申报；可优先恢复封存证据并回读副本'
        decision = 'USE_SEALED_EVIDENCE'
        basis = '关键结果全一致，仅非关键字段随规则升级变化'
    return {'provable': provable, 'unprovable': unprovable, 'decision': decision, 'verdict': verdict, 'basis': basis,
            'original_environment_available': same_rules,
            'changed_critical': changed_critical}


def _attempts(path, package_id):
    with connect(path) as con:
        row = con.execute('SELECT attempts FROM recoveries WHERE package_id=?', (package_id,)).fetchone()
    return row['attempts'] if row else 0


def _record_failure(path, package_id, source, integrity, error):
    payload = {
        'schema': COPY_SCHEMA, 'copy_id': _copy_id(package_id), 'package_id': package_id,
        'status': '失败', 'source': source, 'integrity': integrity,
        'source_rule_version': source.get('rule_version'), 'rule_version': _rule_version(),
        'copy': None, 'seals': None, 'comparison': [], 'row_comparison': [], 'assessment': None,
        'error': error, 'created_at': _now(), 'attempt': {'status': '失败', 'error': error,
                                                          'rule_version': _rule_version(), 'at': _now()},
    }
    with connect(path) as con:
        existing = con.execute('SELECT * FROM recoveries WHERE package_id=?', (package_id,)).fetchone()
    if existing and existing['status'] == '可用':
        # 已有可用副本时不因一次失败而销毁它；失败只作为最近一次尝试附加在原副本上。
        kept = json.loads(existing['payload'])
        kept['attempt'] = payload['attempt']
        kept['last_failure'] = error
        with connect(path) as con:
            con.execute('UPDATE recoveries SET payload=?,attempts=attempts+1,updated_at=? WHERE package_id=?',
                        (json.dumps(kept, ensure_ascii=False), _now(), package_id))
        kept['attempts'] = _attempts(path, package_id)
        return kept
    _upsert(path, payload, '失败', package_id)
    payload['attempts'] = _attempts(path, package_id)
    return payload


def _upsert(path, payload, status, package_id):
    now = _now()
    # package_id 唯一：失败后重试只更新同一条记录，不会长出第二套可用副本。
    with connect(path) as con:
        con.execute('''INSERT INTO recoveries(copy_id,package_id,run_id,status,rule_version,payload,attempts,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(package_id) DO UPDATE SET
                         copy_id=excluded.copy_id,status=excluded.status,rule_version=excluded.rule_version,
                         payload=excluded.payload,attempts=recoveries.attempts+1,updated_at=excluded.updated_at''',
                    (payload['copy_id'], package_id, payload['source'].get('run_id'), status, payload['rule_version'],
                     json.dumps(payload, ensure_ascii=False), now, now))


def recover(path, package_id):
    """从封存包恢复可回读副本。同一封存包只保留一套副本；规则升级后重试会在原记录上更新。"""
    package = get_seal(path, package_id)
    with connect(path) as con:
        existing = con.execute('SELECT * FROM recoveries WHERE package_id=?', (package_id,)).fetchone()
    # 只有同一规则版本下已成功的副本才直接回读；规则升级后须按现规则重算，但仍占用同一条副本记录。
    if existing and existing['status'] == '可用' and existing['rule_version'] == _rule_version():
        return json.loads(existing['payload'])

    bundle = package['bundle']
    integrity = package['integrity']
    source = {
        'package_id': package_id,
        'run_id': package['run_id'],
        'source': (bundle.get('manifest') or {}).get('source'),
        'sha256': (bundle.get('manifest') or {}).get('sha256'),
        'rule_version': package['rule_version'],
        'sealed_at': bundle.get('sealed_at'),
    }
    if not integrity['intact'] or not integrity['seal_ok']:
        return _record_failure(path, package_id, source, integrity,
                               {'code': 'SEAL_UNVERIFIED', 'detail': '封存包完整性或封签校验未通过'})
    state = None
    try:
        state = evaluate({'dataset': source['source']}, snapshot=bundle['input_snapshot'])
    except BusinessError as error:
        return _record_failure(path, package_id, source, integrity, {'code': error.code, 'detail': error.message})
    except Exception as error:  # 当前规则无法重建时保留旧封存件，不改动原件
        return _record_failure(path, package_id, source, integrity,
                               {'code': 'RECOVERY_FAILED', 'detail': f'当前规则无法重建：{type(error).__name__}'})

    copy_summary = business_summary(state)
    differences = comparison(bundle['evidence'], copy_summary)
    row_differences = row_comparison(bundle['evidence'], copy_summary)
    assessment = _assessment(package['rule_version'], integrity, differences, row_differences)
    seals_view = {
        'original_seal': bundle['evidence'].get('seal'),
        'original_recomputed': integrity['seal_recomputed'],
        'original_ok': integrity['seal_ok'],
        'copy_seal': copy_summary['seal'],
        'copy_matches_original': copy_summary['seal'] == bundle['evidence'].get('seal'),
        'copy_run_id': state['manifest']['run_id'],
        'copy_rule_version': state['manifest']['rule_version'],
        'original_rule_version': package['rule_version'],
    }
    readback = {k: state[k] for k in ('records', 'quarantine', 'counts', 'graph', 'weight_checks',
                                     'hazard_issues', 'balance', 'archive', 'views', 'context', 'source')}
    readback['manifest'] = state['manifest']
    payload = {
        'schema': COPY_SCHEMA, 'copy_id': _copy_id(package_id), 'package_id': package_id,
        'status': '可用', 'label': '恢复副本（非原封件）', 'source': source, 'integrity': integrity,
        'source_rule_version': package['rule_version'], 'rule_version': _rule_version(),
        'copy': {'manifest': state['manifest'], 'summary': copy_summary, 'archive': state['archive'],
                 'views': state['views'], 'context': state['context'], 'readback': readback},
        'seals': seals_view, 'comparison': differences, 'row_comparison': row_differences,
        'assessment': assessment,
        'error': None, 'created_at': _now(),
        'attempt': {'status': '成功', 'error': None, 'rule_version': _rule_version(), 'at': _now()},
    }
    _upsert(path, payload, '可用', package_id)
    payload['attempts'] = _attempts(path, package_id)
    return payload


def get_recovery(path, copy_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM recoveries WHERE copy_id=?', (copy_id,)).fetchone()
    if not row:
        raise BusinessError('COPY_NOT_FOUND', '恢复副本不存在', 404)
    return json.loads(row['payload'])


def recovery_of_package(path, package_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM recoveries WHERE package_id=?', (package_id,)).fetchone()
    return json.loads(row['payload']) if row else None


def list_recoveries(path):
    with connect(path) as con:
        rows = con.execute('SELECT copy_id,package_id,run_id,status,rule_version,attempts,updated_at,payload '
                           'FROM recoveries ORDER BY updated_at DESC').fetchall()
    return [{'copy_id': r['copy_id'], 'package_id': r['package_id'], 'run_id': r['run_id'],
             'status': r['status'], 'rule_version': r['rule_version'], 'attempts': r['attempts'],
             'updated_at': r['updated_at'],
             'verdict': (json.loads(r['payload']).get('assessment') or {}).get('verdict')} for r in rows]
