import io
import json
import zipfile

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app import config
from app.config import ROOT, META
from app.errors import BusinessError
from app.archive import serialize_recovery
from app.evidence import FIELD_LABELS
from app.seals import (BUNDLE_SCHEMA, get_recovery, get_seal, list_recoveries,
                       list_seals, recover, seal_result, verify_bundle)

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _db():
    from app.main import DB_PATH
    return DB_PATH


def _labels():
    """表格表头标签：沿用全站字段标签，并补齐恢复台自身字段。"""
    labels = dict(META.get('labels') or {})
    labels.update({key: label for key, label, _ in FIELD_LABELS})
    labels.update({'package_id': '封存包标识', 'copy_id': '恢复副本标识', 'run_id': '原批次',
                   'seal': '原件封签', 'rule_version': '规则版本', 'created_at': '封存时间',
                   'attempts': '尝试次数', 'updated_at': '更新时间', 'status': '状态',
                   'verdict': '裁决', 'archive_status': '封存状态'})
    return labels


def _bundle_zip(bundle):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('seal-bundle.json', json.dumps(bundle, ensure_ascii=False, indent=2))
        archive.writestr('README.txt', '封存舱单包，含原采信快照与封存证据。导入时请核对 package_id 与封签。\n')
    return buffer.getvalue()


def _parse_import(body, content_type):
    """接受封存包 zip 或直接 JSON；损坏或非封存包一律拒绝，避免把重算结果当原件导入。"""
    if 'zip' in (content_type or '') or body[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                text = archive.read('seal-bundle.json')
        except (zipfile.BadZipFile, KeyError):
            raise BusinessError('BAD_SEAL_BUNDLE', '压缩包内缺少 seal-bundle.json')
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise BusinessError('BAD_SEAL_BUNDLE', '封存包内容不是有效 JSON')
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise BusinessError('BAD_SEAL_BUNDLE', '封存包内容不是有效 JSON')
    return data.get('bundle', data) if isinstance(data, dict) else data


@router.post('/api/runs/{run_id}/seal')
def create_seal(run_id: str):
    from app import store
    return seal_result(_db(), store.get(_db(), run_id))


@router.get('/api/seals')
def seals():
    return [{**row, 'integrity': get_seal(_db(), row['package_id'])['integrity']} for row in list_seals(_db())]


@router.get('/api/seals/{package_id}')
def seal(package_id: str):
    return get_seal(_db(), package_id)


@router.get('/api/seals/{package_id}/bundle')
def seal_bundle(package_id: str):
    package = get_seal(_db(), package_id)
    return Response(_bundle_zip(package['bundle']), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{package_id}-seal-bundle.zip"'})


@router.post('/api/seals/import')
async def import_seal(request: Request):
    from app import store
    body = await request.body()
    if not body:
        raise BusinessError('BAD_SEAL_BUNDLE', '封存包为空')
    bundle = _parse_import(body, request.headers.get('content-type', ''))
    required = {'package_id', 'schema', 'manifest', 'evidence', 'input_snapshot'}
    if not isinstance(bundle, dict) or not required.issubset(bundle) or bundle.get('schema') != BUNDLE_SCHEMA:
        raise BusinessError('BAD_SEAL_BUNDLE', '不是受支持的封存包格式')
    integrity = verify_bundle(bundle)
    if not integrity['intact'] or not integrity['seal_ok']:
        raise BusinessError('SEAL_UNVERIFIED', '封存包完整性或封签校验未通过，拒绝导入')
    manifest = bundle['manifest']
    with store.connect(_db()) as con:
        con.execute('INSERT OR IGNORE INTO seals(package_id,run_id,seal,rule_version,bundle,created_at) VALUES(?,?,?,?,?,?)',
                    (bundle['package_id'], manifest.get('run_id'), bundle['evidence'].get('seal'),
                     manifest.get('rule_version') or 'unknown', json.dumps(bundle, ensure_ascii=False),
                     bundle.get('sealed_at') or manifest.get('generated_at') or ''))
    return get_seal(_db(), bundle['package_id'])


@router.post('/api/recover/{package_id}')
def create_recovery(package_id: str):
    copy = recover(_db(), package_id)
    return JSONResponse(copy, status_code=200 if copy['status'] == '可用' else 409)


@router.get('/api/recover/{copy_id}')
def read_recovery(copy_id: str):
    return get_recovery(_db(), copy_id)


@router.get('/api/recover/{copy_id}/export')
def export_recovery(copy_id: str):
    copy = get_recovery(_db(), copy_id)
    return Response(serialize_recovery(copy), media_type='application/zip',
                    headers={'Content-Disposition': f'attachment; filename="{copy_id}-recovery-copy.zip"'})


def _shell_state(package_id, package):
    """恢复页沿用三栏版式，KPI 取原件封存证据，不取按现规则重算的结果。"""
    evidence = package['bundle']['evidence']
    manifest = package['bundle']['manifest']
    counts = evidence.get('counts') or {}
    return {
        'manifest': {'run_id': package['run_id'], 'sha256': manifest.get('sha256')},
        'source': manifest.get('source') or 'sealed',
        'graph': {'roots': evidence.get('roots') or [], 'children': {}},
        'balance': {'pieces': evidence.get('pieces'), 'net_kg': evidence.get('net_kg'),
                    'gross_kg': evidence.get('gross_kg'), 'input': counts.get('input'),
                    'accepted': evidence.get('accepted'), 'quarantined': evidence.get('quarantined')},
        'archive': {'blocked': evidence.get('archive_blocked'), 'status': evidence.get('archive_status')},
    }


@router.get('/recover')
def recovery_page(request: Request):
    package_id = request.query_params.get('package_id')
    copy_id = request.query_params.get('copy_id')
    copy = get_recovery(_db(), copy_id) if copy_id else None
    if copy and not package_id:
        package_id = copy['package_id']
    if not package_id:
        packages = list_seals(_db())
        current = list_recoveries(_db())
        return templates.TemplateResponse(request=request, name='recover.html', context={
            'meta': META, 'key': 'recover', 'state': None, 'package': None, 'copy': None,
            'packages': packages, 'recoveries': current, 'labels': FIELD_LABELS, 'field_labels': _labels()})
    package = get_seal(_db(), package_id)
    if copy is None:
        copy = next((c for c in list_recoveries(_db()) if c['package_id'] == package_id), None)
        copy = get_recovery(_db(), copy['copy_id']) if copy else None
    current = None
    try:
        from app import store
        current = store.get(_db(), package['run_id'])
    except BusinessError:
        current = None
    state = _shell_state(package_id, package)
    return templates.TemplateResponse(request=request, name='recover.html', context={
        'meta': META, 'key': 'recover', 'state': state, 'package': package, 'copy': copy,
        'current': current, 'current_rule_version': config.RULE_VERSION,
        'labels': FIELD_LABELS, 'field_labels': _labels()})
