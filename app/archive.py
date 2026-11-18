import io, json, zipfile
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
    return buffer.getvalue()

def serialize_recovery(copy):
    """恢复副本单独可回读下载，文件名与内容都标明非原封件，避免被当作原件提交。"""
    if not copy or copy.get('status')!='可用' or not copy.get('copy'):
        raise BusinessError('COPY_UNAVAILABLE','恢复副本不可用，请保留旧封存件',409)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('recovery.json',json.dumps({k:copy[k] for k in ('schema','copy_id','package_id','label','status','source','source_rule_version','rule_version','integrity','seals','error')},ensure_ascii=False,indent=2))
        archive.writestr('comparison.json',json.dumps(copy['comparison'],ensure_ascii=False,indent=2))
        archive.writestr('assessment.json',json.dumps(copy['assessment'],ensure_ascii=False,indent=2))
        archive.writestr('copy-evidence.json',json.dumps({'counts':copy['copy']['summary']['counts'],'views':copy['copy']['views'],'manifest':copy['copy']['manifest']},ensure_ascii=False,indent=2))
    return buffer.getvalue()
