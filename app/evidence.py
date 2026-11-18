import hashlib
import json

# 与封存页、恢复副本差异表共用同一组字段，避免口径分叉。
FIELD_LABELS = [
    ('pieces', '件数', lambda s: s.get('pieces')),
    ('net_kg', '净重 kg', lambda s: s.get('net_kg')),
    ('gross_kg', '毛重 kg', lambda s: s.get('gross_kg')),
    ('decl_pieces', '申报件数', lambda s: (s.get('declaration') or {}).get('pieces')),
    ('decl_net_kg', '申报净重 kg', lambda s: (s.get('declaration') or {}).get('net_kg')),
    ('decl_gross_kg', '申报毛重 kg', lambda s: (s.get('declaration') or {}).get('gross_kg')),
    ('input', '原始行', lambda s: (s.get('counts') or {}).get('input')),
    ('accepted', '采信行', lambda s: s.get('accepted')),
    ('quarantined', '隔离行', lambda s: s.get('quarantined')),
    ('graph_errors', '图错误', lambda s: len(s.get('graph_errors') or [])),
    ('weight_errors', '重量差异项', lambda s: sum(1 for row in (s.get('weights') or []) if row.get('code') != 'BALANCED')),
    ('hazards', '危险品阻断', lambda s: len(s.get('hazards') or [])),
    ('archive_status', '封存状态', lambda s: s.get('archive_status')),
    ('seal', '封签', lambda s: s.get('seal')),
]


def canonical(value):
    """稳定字节表示，用于包标识与副本摘要；封签复算不经过此函数。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def business_summary(state):
    balance = state['balance']
    return {
        'pieces': balance['pieces'],
        'net_kg': balance['net_kg'],
        'gross_kg': balance['gross_kg'],
        'accepted': balance['accepted'],
        'quarantined': balance['quarantined'],
        'counts': dict(state['counts']),
        'declaration': dict((state.get('context') or {}).get('declaration') or {}),
        'roots': list(state['graph']['roots']),
        'weights': list(state['weight_checks']),
        'hazards': list(state['hazard_issues']),
        'graph_errors': list(state['graph']['errors']),
        'records': [dict(row) for row in state['records']],
        'quarantine': [dict(row) for row in state['quarantine']],
        'archive_status': state['archive']['status'],
        'archive_blocked': state['archive']['blocked'],
        'seal': state['archive']['seal'],
    }


def seal_from(evidence):
    """复算封签。参数必须与 archive 规则封存时完全一致。"""
    return hashlib.sha256(json.dumps({
        'roots': evidence['roots'],
        'weights': evidence['weights'],
        'hazards': evidence['hazards'],
    }, sort_keys=True).encode()).hexdigest()


def comparison(original, recovered):
    rows = []
    for key, label, getter in FIELD_LABELS:
        before = getter(original)
        after = getter(recovered) if recovered is not None else None
        rows.append({'field': key, 'label': label, 'original': before, 'recovered': after, 'changed': before != after})
    return rows


ROW_FIELDS = ('pieces', 'net_kg', 'gross_kg', 'revision')


def row_comparison(original, recovered):
    """逐单号比对副本的件重，定位到具体提单，避免只说“有差异”。"""
    if not recovered:
        return []
    before = {row['id']: row for row in original.get('records') or []}
    after = {row['id']: row for row in recovered.get('records') or []}
    changes = []
    for node_id in sorted(set(before) | set(after)):
        old, new = before.get(node_id), after.get(node_id)
        if old is None or new is None:
            changes.append({'id': node_id, 'field': 'existence', 'label': '采信状态',
                            'original': '采信' if old else '未采信', 'recovered': '采信' if new else '未采信'})
            continue
        for field in ROW_FIELDS:
            if old.get(field) != new.get(field):
                changes.append({'id': node_id, 'field': field, 'label': {'pieces': '件数', 'net_kg': '净重 kg',
                                'gross_kg': '毛重 kg', 'revision': '版本'}[field],
                                'original': old.get(field), 'recovered': new.get(field)})
    return changes
