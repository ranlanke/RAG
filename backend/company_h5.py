"""Small authenticated knowledge adapter around the frozen Pageo service."""
import hashlib
import re
import uuid
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import company_knowledge as kb
from company_catalog import CompanyDocument, CompanyProject, can_read, document_store, effective_acl, locked_scalar
from company_h5_models import CompanyH5Job, H5SourceVersion, observe_version
from database import SessionLocal


def latest_document_ids(documents):
    """Keep source selection independent of the host's version helper API."""
    latest, independent = {}, set()
    for doc in documents:
        if not doc.is_current:
            continue
        if doc.note_id or doc.research_project_id or not doc.sha256:
            independent.add(doc.id)
            continue
        key = (doc.index_id, doc.owner, doc.knowledge_project_id, doc.folder_id, doc.project, doc.title)
        if key not in latest or doc.version > latest[key].version:
            latest[key] = doc
    return independent | {doc.id for doc in latest.values()}
from models import User
from pageo_client import FORMATS, MAX_SOURCE, PageoClient, config, enabled


def require_enabled(user=Depends(kb.require_knowledge_user)):
    if not enabled():
        raise HTTPException(404, 'H5 工作台未启用。')
    return user


router = APIRouter(prefix='/company-knowledge/h5', tags=['H5 workbench'])


def cap_allows(acl, user):
    return (user.role == 'admin' or user.id == acl.get('owner_id') or user.username == acl.get('owner')
            or user.username in acl.get('readers', [])
            or (acl.get('confidentiality') == 'internal' and acl.get('visibility') == 'members'))


def source_rows(db, job):
    origin = db.get(CompanyDocument, job.source_id) if job.source_id else None
    latest = origin
    if job.source_family:
        latest = db.scalar(select(CompanyDocument).join(H5SourceVersion,
            H5SourceVersion.document_id == CompanyDocument.id).where(
                H5SourceVersion.family_id == job.source_family, CompanyDocument.state != 'deleted')
            .order_by(CompanyDocument.version.desc(), CompanyDocument.created_at.desc())) or origin
    return origin, latest


def source_allowed(db, job, user):
    if not cap_allows(job.source_acl, user):
        return False
    if job.source_kind != 'knowledge':
        return True
    origin, latest = source_rows(db, job)
    return all(doc is not None and doc.index_id == kb.settings()['index_id']
               and doc.state != 'deleted' and can_read(doc, user) for doc in (origin, latest))


def artifact_allowed(document, user):
    """An output cannot escape either its original or current source permissions."""
    with SessionLocal() as db:
        job = db.scalar(select(CompanyH5Job).where(CompanyH5Job.saved_document_id == document.id))
        if not job or not source_allowed(db, job, user):
            return False
        if document.knowledge_project_id:
            project = db.get(CompanyProject, document.knowledge_project_id)
            if not project or not can_read(project, user):
                return False
        return True


def find_job(db, job_id, user):
    job = db.get(CompanyH5Job, job_id)
    if not job or not source_allowed(db, job, user):
        raise HTTPException(404, '成果不存在或没有访问权限。')
    document = db.get(CompanyDocument, job.saved_document_id) if job.saved_document_id else None
    if document and not can_read(document, user):
        raise HTTPException(404, '成果不存在或没有访问权限。')
    if job.owner_id != user.id and user.role != 'admin':
        if not document or document.state == 'deleted' or not can_read(document, user):
            raise HTTPException(404, '成果不存在或没有访问权限。')
    return job


def source_changed(db, job):
    if job.source_kind != 'knowledge':
        return None
    origin, latest = source_rows(db, job)
    if not origin or origin.state == 'deleted' or not latest:
        return True
    digest = latest.sha256
    if not digest and latest.object_key:
        try:
            digest = hashlib.sha256(document_store().get_bytes(latest.object_key)).hexdigest()
        except FileNotFoundError:
            return True
    return digest != job.source_sha256 if digest else None


def public(db, job):
    document = db.get(CompanyDocument, job.saved_document_id) if job.saved_document_id else None
    saved = bool(document and document.state != 'deleted')
    progress = (job.material_counts or {}).get('progress', {})
    origin = db.get(CompanyDocument, job.source_id) if job.source_id else None
    return {key: getattr(job, key) for key in ('id', 'title', 'source_kind', 'source_id', 'source_name',
            'source_size', 'theme', 'state', 'stage', 'error', 'material_counts')} | {
        'created_at': job.created_at.isoformat() + 'Z', 'expires_at': job.expires_at.isoformat() + 'Z',
        'source_changed': source_changed(db, job), 'saved_document_id': job.saved_document_id if saved else '',
        'saved_project_id': document.knowledge_project_id if saved else '',
        'saved_folder_id': document.folder_id if saved else '',
        'saved_category': document.category if saved else '',
        'source_project_id': job.source_acl.get('project_id', origin.knowledge_project_id if origin else ''),
        'source_folder_id': job.source_acl.get('folder_id', origin.folder_id if origin else ''),
        'progress': progress, 'resume_count': (job.material_counts or {}).get('resume_count', 0),
        'save_error': (job.material_counts or {}).get('save_error', ''),
        'candidate_available': bool(job.html_key) and bool((job.material_counts or {}).get('candidate_key')),
        'candidate_key': (job.material_counts or {}).get('candidate_key',''),
        'pending_chapters': (job.material_counts or {}).get('pending_chapters',{}),
        'can_resume': job.state in {'failed', 'paused'} and bool(job.source_key) and job.expires_at > datetime.utcnow()
            and (job.material_counts or {}).get('resume_count', 0) < 2 and not progress.get('retry_blocked', False),
    }


@router.get('/themes')
async def themes(user=Depends(require_enabled)):
    try:
        async with PageoClient() as client:
            result = await client.request('GET', '/api/h5/themes')
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'H5 后台暂不可连接，请稍后重试；已生成成果仍可使用。') from None
    if not isinstance(result, dict) or not isinstance(result.get('themes'), list):
        raise HTTPException(503, 'H5 主题目录暂不可用，请联系管理员检查服务版本。')
    return {'themes': [({k: t[k] for k in ('id', 'name', 'palette', 'category', 'dark') if k in t}
                       | {'preview': '/h5-theme-previews/' + t['id'] + '.jpg',
                          'description': (t.get('composition') or {}).get('name', '')})
                       for t in result.get('themes', [])
                       if isinstance(t, dict) and re.fullmatch(r'[a-z0-9-]{1,64}', str(t.get('id', '')))]}


@router.get('/sources')
def sources(q: str = Query('', max_length=150), page: int = Query(1, ge=1),
            user=Depends(require_enabled)):
    with SessionLocal() as db:
        rows = db.scalars(select(CompanyDocument).where(CompanyDocument.index_id == kb.settings()['index_id'])
            .order_by(CompanyDocument.updated_at.desc())).all()
        current = latest_document_ids(rows)
        rows = [d for d in rows if d.id in current and d.state != 'deleted' and d.object_key
                and d.format.lower() in FORMATS and can_read(d, user) and (not q or q.lower() in d.title.lower())]
        return {'items': [{'id': d.id, 'title': d.title, 'format': d.format, 'size': d.size,
                          'project': d.project} for d in rows[(page-1)*20:page*20]], 'total': len(rows)}


def create_job(user, request_key, theme, name, payload, *, source_id=''):
    config()  # Fail before retaining materials when the private generator is unconfigured.
    if not 1 <= len(payload) <= MAX_SOURCE or Path(name).suffix.lower().lstrip('.') not in FORMATS:
        raise HTTPException(422, '请选择 40 MB 以内的 PDF、DOCX、TXT、Markdown 或图片文件。')
    if not request_key or len(request_key) > 80 or not theme or len(theme) > 64:
        raise HTTPException(422, '任务标识或主题无效。')
    digest = hashlib.sha256(payload).hexdigest()
    kind = 'knowledge' if source_id else 'local'
    identity = f'{user.id}:{kind}:{source_id}:{digest}:{theme}'
    active = hashlib.sha256(identity.encode()).hexdigest()
    with SessionLocal() as db:
        prior = db.scalar(select(CompanyH5Job).where(CompanyH5Job.owner_id == user.id,
            CompanyH5Job.request_key == request_key))
        if prior:
            if (prior.source_kind, prior.source_id, prior.source_sha256, prior.theme) != (kind, source_id, digest, theme):
                raise HTTPException(409, '任务标识已用于另一份材料或主题，请重新发起。')
            find_job(db, prior.id, user)
            return public(db, prior)
        pending = db.scalar(select(CompanyH5Job).where(CompanyH5Job.active_key == active))
        if pending:
            find_job(db, pending.id, user)
            return public(db, pending)
        from sqlalchemy import func
        if (db.scalar(select(func.count()).select_from(CompanyH5Job).where(
                CompanyH5Job.owner_id == user.id, CompanyH5Job.state.in_(['queued', 'preparing', 'generating']))) or 0) >= 2:
            raise HTTPException(429, '已有两份材料在制作，请先等待当前任务。')
        acl = {'owner_id': user.id, 'owner': user.username, 'visibility': 'restricted', 'readers': [], 'confidentiality': 'internal'}
        family, version, modified = '', 0, None
        if source_id:
            from company_workspace import find_doc
            doc = find_doc(db, source_id, user)
            if doc.format == 'H5' or not doc.object_key or (doc.sha256 and doc.sha256 != digest):
                raise HTTPException(409, '来源内容已改变或不可生成，请重新选择原件。')
            siblings = db.scalars(select(CompanyDocument).where(CompanyDocument.index_id == doc.index_id,
                CompanyDocument.owner == doc.owner, CompanyDocument.title == doc.title,
                CompanyDocument.knowledge_project_id == doc.knowledge_project_id,
                CompanyDocument.folder_id == doc.folder_id)).all()
            family = observe_version(db, doc, [s for s in siblings if s.id != doc.id])
            acl = effective_acl(doc) | {'owner_id': doc.owner_user_id, 'owner': doc.owner}
            acl.update(project_id=doc.knowledge_project_id, folder_id=doc.folder_id)
            version, modified = doc.version, doc.updated_at
        job_id = uuid.uuid4().hex
        key = f'h5/temporary/{job_id}/source{Path(name).suffix.lower()}'
        document_store().put_bytes(key, payload)
        job = CompanyH5Job(id=job_id, owner_id=user.id, owner=user.username, request_key=request_key,
            active_key=active, source_kind=kind, source_id=source_id, source_family=family, source_name=name[:255],
            source_sha256=digest, source_version=version, source_modified_at=modified, source_acl=acl,
            source_key=key, source_size=len(payload), theme=theme, title=Path(name).stem[:220] + ' · H5',
            expires_at=datetime.utcnow()+timedelta(days=7))
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            pending = db.scalar(select(CompanyH5Job).where(CompanyH5Job.active_key == active))
            if pending:
                return public(db, find_job(db, pending.id, user))
            raise HTTPException(409, '相同任务正在创建，请刷新记录。') from None
        return public(db, job)


class Generate(BaseModel):
    document_id: str = Field(min_length=1, max_length=64)
    request_key: str = Field(min_length=1, max_length=80)
    theme: str = Field(default='cobalt', pattern='^[a-z0-9-]{1,64}$')


@router.post('/jobs', status_code=202)
def generate(body: Generate, user=Depends(require_enabled)):
    from company_workspace import find_doc
    with SessionLocal() as db:
        doc = find_doc(db, body.document_id, user)
        if not doc.object_key:
            raise HTTPException(409, '这份资料尚无受控原件，暂不能生成。')
        if doc.size > MAX_SOURCE:
            raise HTTPException(413, '文件超过 H5 当前 40 MB 上限。')
        name, key = doc.title, doc.object_key
        if Path(name).suffix.lower().lstrip('.') != doc.format.lower():
            name += '.'+doc.format.lower()
    try:
        payload = document_store().get_bytes(key)
    except FileNotFoundError:
        raise HTTPException(409, '原件已缺失，请联系资料管理者。') from None
    return create_job(user, body.request_key, body.theme, name, payload, source_id=body.document_id)


@router.post('/uploads', status_code=202)
async def upload(file: UploadFile = File(...), request_key: str = Form(..., max_length=80),
                 theme: str = Form('cobalt', pattern='^[a-z0-9-]{1,64}$'), user=Depends(require_enabled)):
    import asyncio
    payload = await file.read(MAX_SOURCE+1)
    name = (file.filename or '材料.txt').replace('\\', '/').rsplit('/', 1)[-1]
    return await asyncio.to_thread(create_job, user, request_key, theme, name, payload)


@router.get('/jobs')
def jobs(page: int = Query(1, ge=1), user=Depends(require_enabled)):
    with SessionLocal() as db:
        rows = db.scalars(select(CompanyH5Job).where(CompanyH5Job.owner_id == user.id)
            .order_by(CompanyH5Job.created_at.desc())).all()
        visible = [job for job in rows if source_allowed(db, job, user)]
        return {'items': [public(db, job) for job in visible[(page-1)*20:page*20]], 'total': len(visible)}


@router.get('/jobs/{job_id}')
def job(job_id: str, user=Depends(require_enabled)):
    with SessionLocal() as db:
        return public(db, find_job(db, job_id, user))


@router.post('/jobs/{job_id}/resume', status_code=202)
def resume(job_id: str, user=Depends(require_enabled)):
    with SessionLocal() as db:
        row = find_job(db, job_id, user)
        row = locked_scalar(db, select(CompanyH5Job).where(CompanyH5Job.id == row.id))
        if row.owner_id != user.id or row.state not in {'paused', 'failed'}:
            raise HTTPException(409, '当前任务不需要恢复。')
        if not row.source_key or row.expires_at <= datetime.utcnow():
            raise HTTPException(410, '临时材料保留期已结束，请重新选择文件。')
        counts = dict(row.material_counts or {})
        if counts.get('resume_count', 0) >= 2 or counts.get('progress', {}).get('retry_blocked'):
            raise HTTPException(409, '恢复额度已用尽或需先检查原因，已完成进度保留。')
        counts['resume_count'] = counts.get('resume_count', 0) + 1
        row.material_counts = counts
        row.state = 'queued'; row.error = ''; row.stage = '等待从已保存进度继续'
        row.resume_requested = 1
        active = hashlib.sha256(f'{user.id}:{row.source_kind}:{row.source_id}:{row.source_sha256}:{row.theme}'.encode()).hexdigest()
        pending = db.scalar(select(CompanyH5Job).where(CompanyH5Job.active_key == active, CompanyH5Job.id != row.id))
        if pending:
            raise HTTPException(409, '相同来源和主题已有制作任务，请打开已有记录。')
        row.active_key = active
        row.lease_until = None; row.lease_token = ''; row.transport_failures = 0
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '相同任务正在恢复，请刷新记录。') from None
        return public(db, row)


class RepairChapter(BaseModel):
    chapter:int=Field(ge=0,le=39)


@router.post('/jobs/{job_id}/repair-chapter',status_code=202)
def repair_chapter(job_id:str,body:RepairChapter,user=Depends(require_enabled)):
    with SessionLocal() as db:
        row=find_job(db,job_id,user)
        row=locked_scalar(db,select(CompanyH5Job).where(CompanyH5Job.id==row.id))
        counts=dict(row.material_counts or {})
        if row.owner_id!=user.id:raise HTTPException(403,'仅制作人可以优化章节。')
        if row.state not in {'review_pending','failed','paused'} or not row.html_key or not counts.get('candidate_key'):
            raise HTTPException(409,'请等待完整草稿停止优化后，再选择章节。')
        if str(body.chapter) not in counts.get('pending_chapters',{}):raise HTTPException(409,'该章没有待核对问题。')
        if not row.source_key or row.expires_at<=datetime.utcnow():raise HTTPException(410,'原材料已到期，草稿保留。')
        attempts=dict(counts.get('chapter_repair_attempts',{}))
        if attempts.get(str(body.chapter),0)>=2:raise HTTPException(409,'本章两次优化已用尽，请按标注人工核对。')
        attempts[str(body.chapter)]=attempts.get(str(body.chapter),0)+1
        counts.update(chapter_repair_attempts=attempts,repair_chapter=body.chapter)
        active=hashlib.sha256(f'{user.id}:{row.source_kind}:{row.source_id}:{row.source_sha256}:{row.theme}'.encode()).hexdigest()
        if db.scalar(select(CompanyH5Job).where(CompanyH5Job.active_key==active,CompanyH5Job.id!=row.id)):
            raise HTTPException(409,'相同来源已有制作任务，请先等待完成。')
        row.material_counts=counts;row.resume_requested=2;row.state='queued';row.active_key=active
        row.stage=f'等待仅优化第{body.chapter+1}章';row.error='';row.lease_until=None;row.lease_token='';row.transport_failures=0
        try:db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'任务已更新，请刷新。') from None
        return public(db,row)


def result_bytes(db, job, kind):
    if job.state != 'ready' and not ((job.material_counts or {}).get('candidate_key') and job.html_key):
        raise HTTPException(409, '请等待完整成果生成。')
    key = job.zip_key if kind == 'zip' else job.html_key
    if not key:
        raise HTTPException(410, '临时成果已到期，请查看已保存成果或重新生成。')
    try:
        return document_store().get_bytes(key)
    except FileNotFoundError:
        raise HTTPException(410, '成果文件已缺失，请联系管理员。') from None


@router.get('/jobs/{job_id}/preview')
def preview(job_id: str, full: bool = False, user=Depends(require_enabled)):
    from h5_preview import isolate_html
    with SessionLocal() as db:
        row = find_job(db, job_id, user)
        result_bytes(db, row, 'html')
        html = document_store().get_bytes(row.html_key if full else (row.preview_key or row.html_key)).decode('utf-8')
    # This is JSON, rendered only inside an opaque-origin sandbox, never the app DOM.
    return {'html': isolate_html(html)}


@router.get('/jobs/{job_id}/original')
def original(job_id: str, user=Depends(require_enabled)):
    with SessionLocal() as db:
        row = find_job(db, job_id, user)
        key = row.source_key
        if not key and row.source_kind == 'knowledge':
            origin = db.get(CompanyDocument, row.source_id)
            key = origin.object_key if origin else ''
        if not key:
            raise HTTPException(410, '临时原件已到期。')
        try:
            payload = document_store().get_bytes(key)
        except FileNotFoundError:
            raise HTTPException(410, '生成时的原件已到期或缺失。') from None
        if hashlib.sha256(payload).hexdigest() != row.source_sha256:
            raise HTTPException(410, '生成时的原件已到期，当前来源已有更新，不能用新原件代替本次来源。')
        name = row.source_name
        if row.source_kind == 'knowledge':
            from company_watermark import ExportIdentity, WatermarkError, export_copy
            identity = ExportIdentity(user.username, user.id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), uuid.uuid4().hex)
            try:
                payload, _, name = export_copy(payload, Path(name).suffix.lstrip('.').upper(), name, identity)
            except WatermarkError as exc:
                raise HTTPException(exc.status, str(exc)) from None
    return Response(payload, media_type='application/octet-stream', headers={'Cache-Control': 'no-store',
        'Content-Disposition': "attachment; filename=source; filename*=UTF-8''"+quote(name, safe='')})


@router.get('/jobs/{job_id}/download')
def download(job_id: str, kind: str = Query('html', pattern='^(html|zip)$'), user=Depends(require_enabled)):
    with SessionLocal() as db:
        row = find_job(db, job_id, user)
        payload = result_bytes(db, row, kind)
        filename = row.title + '.' + kind
    return Response(payload, media_type='application/octet-stream', headers={'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff', 'Content-Disposition': "attachment; filename=report."+kind+"; filename*=UTF-8''"+quote(filename, safe='')})


class Save(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    folder_id: str = Field(default='', max_length=64)


@router.post('/jobs/{job_id}/save')
def save(job_id: str, body: Save, user=Depends(require_enabled)):
    return save_artifact(job_id, body, user)


def save_artifact(job_id, body, user):
    from company_projects import find_project, find_folder
    from company_audit import record
    with SessionLocal() as db:
        row = find_job(db, job_id, user)
        row = locked_scalar(db, select(CompanyH5Job).where(CompanyH5Job.id == row.id))
        if row.owner_id != user.id:
            raise HTTPException(403, '仅制作人可以保存成果。')
        if row.state!='ready':raise HTTPException(409,'待核对草稿可预览和下载；核对通过后才能保存正式成果。')
        if row.saved_document_id:
            existing = db.get(CompanyDocument, row.saved_document_id)
            if not existing or existing.state == 'deleted':
                raise HTTPException(409, '已保存成果位于回收站或已清理，请先在知识库恢复，或重新制作。')
            return public(db, row)
        payload = result_bytes(db, row, 'html')
        project = find_project(db, body.project_id, user, manage=True)
        find_folder(db, project.id, body.folder_id)
        acl = effective_acl(project)
        if row.source_acl.get('visibility') != 'members' or row.source_acl.get('confidentiality') != 'internal':
            acl = {'visibility': 'restricted', 'confidentiality': row.source_acl.get('confidentiality', 'internal'),
                   'readers': sorted(set(row.source_acl.get('readers', []) + [row.source_acl.get('owner', ''), row.owner]) - {''})}
        from sqlalchemy import func
        version = (db.scalar(select(func.max(CompanyDocument.version)).join(CompanyH5Job,
            CompanyH5Job.saved_document_id == CompanyDocument.id).where(CompanyDocument.index_id == project.index_id,
            CompanyDocument.owner == project.owner, CompanyDocument.knowledge_project_id == project.id,
            CompanyDocument.folder_id == body.folder_id, CompanyH5Job.title == row.title)) or 0) + 1
        # Existing knowledge dedup keys include title+content; explicit version names
        # preserve identical reruns without changing the original upload constraints.
        filename = row.title + (f' · V{version}' if version > 1 else '') + '.html'
        document = CompanyDocument(id=uuid.uuid4().hex, index_id=project.index_id, title=filename,
            format='H5', size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            object_key=row.html_key, owner=project.owner, owner_user_id=project.owner_user_id,
            uploaded_by=user.username, uploaded_by_user_id=user.id, modified_by=user.username,
            knowledge_project_id=project.id, folder_id=body.folder_id, inherits_project=1,
            project=project.name, department=project.department, industry=project.industry,
            category=project.category, tags=list(project.tags or []), review='参考资料',
            version=version, state='display', status_message='H5 展示成果，无需问答索引', parser='NONE',
            description='来源：'+row.source_name+'；展示成果不进入问答检索。', **acl)
        db.add(document); row.saved_document_id = document.id
        row.material_counts = {k:v for k,v in (row.material_counts or {}).items() if k != 'save_error'}
        db.flush(); record(db, user, 'document.h5_save', document)
        db.commit()
        return public(db, row)


def auto_save(job_id):
    """Save the existing result once; failure cannot re-enter generation."""
    with SessionLocal() as db:
        row = db.get(CompanyH5Job, job_id)
        if row.source_kind != 'knowledge' or row.saved_document_id:
            return
        origin = db.get(CompanyDocument, row.source_id)
        project_id = row.source_acl.get('project_id', origin.knowledge_project_id if origin else '')
        folder_id = row.source_acl.get('folder_id', origin.folder_id if origin else '')
        user = db.get(User, row.owner_id)
        db.expunge(user)
    try:
        save_artifact(job_id, Save(project_id=project_id, folder_id=folder_id), user)
    except Exception:
        with SessionLocal() as db:
            row = db.get(CompanyH5Job, job_id)
            row.material_counts = dict(row.material_counts or {}) | {
                'save_error': '未保存到知识库：来源目录不可写、已变更或暂不可用。完整成果已保留，请选择其他可写位置或下载；无需重新生成。'}
            db.commit()


@router.get('/saved/{document_id}')
def saved(document_id: str, user=Depends(require_enabled)):
    from company_workspace import find_doc
    with SessionLocal() as db:
        doc = find_doc(db, document_id, user)
        row = db.scalar(select(CompanyH5Job).where(CompanyH5Job.saved_document_id == doc.id))
        if not row:
            raise HTTPException(404, '成果不存在。')
        return public(db, find_job(db, row.id, user))
