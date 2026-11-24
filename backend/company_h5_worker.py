"""One fenced adapter worker; generation remains in Pageo's durable queue."""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
import hashlib
import io
import logging
import re
from pathlib import Path
import uuid
import zipfile

import httpx
from fastapi import HTTPException
from sqlalchemy import or_, select, update

from database import SessionLocal
from models import User
from company_catalog import document_store
from company_h5_models import CompanyH5Job
from company_h5 import source_allowed, auto_save
from pageo_client import PageoClient, INSTRUCTION, MAX_RESULT

logger = logging.getLogger(__name__)


def claim():
    now = datetime.utcnow()
    with SessionLocal() as db:
        condition = (CompanyH5Job.state.in_(['queued', 'preparing', 'generating']),
            or_(CompanyH5Job.lease_until.is_(None), CompanyH5Job.lease_until < now))
        row = db.scalar(select(CompanyH5Job).where(*condition).order_by(CompanyH5Job.updated_at).limit(1))
        if not row:
            return None
        token = uuid.uuid4().hex
        changed = db.execute(update(CompanyH5Job).where(CompanyH5Job.id == row.id, *condition)
            .values(lease_token=token, lease_until=now+timedelta(minutes=5)))
        db.commit()
        return (row.id, token) if changed.rowcount == 1 else None


def persist(job_id, token, **values):
    with SessionLocal() as db:
        values['updated_at'] = datetime.utcnow()
        changed = db.execute(update(CompanyH5Job).where(CompanyH5Job.id == job_id,
            CompanyH5Job.lease_token == token).values(**values))
        if changed.rowcount != 1:
            raise RuntimeError('H5 worker lease changed')
        db.commit()


def finished(job_id, token, state, stage, error=''):
    persist(job_id, token, state=state, stage=stage, error=error, active_key=None,
            lease_token='', lease_until=None)


def safe_reason(value):
    text = str(value or '')
    text = re.sub(r'https?://\S+|(?:/app|/opt|[A-Z]:[\\/])\S*', '[内部位置]', text)
    text = re.sub(r'(?i)(?:bearer\s+|sk-)[a-z0-9_.-]+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+', '[已脱敏]', text)
    return text[:800]


def progress_values(remote, counts):
    info = remote.get('continuation') or {}
    return dict(counts or {}) | {'pending_chapters':remote.get('pending_chapters',{}), 'progress': {k: info.get(k, 0) for k in (
        'completed_chapters','drafted_chapters','planned_chapters','completed_reading','planned_reading','requests','active_seconds')}
        | {'phase': safe_reason(info.get('phase') or remote.get('progress')),
           'retry_blocked': info.get('next_action') in {'inspect_task_failure','inspect_no_progress'},
           'next': safe_reason(info.get('next'))}}


def validate_html(payload):
    from bs4 import BeautifulSoup
    dom = BeautifulSoup(payload, 'html.parser')
    if not dom.find('html') or dom.select('script[src]'):
        raise ValueError('Non-standalone H5')
    for item in dom.select('img[src],link[rel="stylesheet"][href]'):
        value = item.get('src', item.get('href', ''))
        if not value.startswith('data:'):
            raise ValueError('External H5 resource')
    for style in dom.find_all('style'):
        for _, value in re.findall(r'url\(\s*([\'\"]?)(.*?)\1\s*\)', style.get_text(), re.S | re.I):
            if not value.startswith(('data:', '#')):
                raise ValueError('External H5 style resource')


def validate_zip(payload, html):
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        if len(z.infolist()) > 2000 or sum(i.file_size for i in z.infolist()) > MAX_RESULT:
            raise ValueError('Oversized H5 archive')
        if z.read('index.html') != html:
            raise ValueError('HTML and ZIP differ')
        for name in z.namelist():
            if name.startswith(('/', '\\')) or '..' in name.replace('\\', '/').split('/') or ':' in name:
                raise ValueError('Unsafe H5 archive')
        if z.testzip():
            raise ValueError('Corrupt H5 archive')


async def advance(job_id, token):
    with SessionLocal() as db:
        row = db.get(CompanyH5Job, job_id)
        user = db.get(User, row.owner_id)
        from company_knowledge import has_access
        if not user or not has_access(user) or not source_allowed(db, row, user):
            finished(job_id, token, 'failed', '来源权限已改变', '当前账号或来源已不可访问，已停止适配任务。')
            return
        if row.expires_at <= datetime.utcnow():
            finished(job_id, token, 'expired', '临时材料已到期')
            return
        snapshot = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    async with PageoClient() as client:
        remote_id = snapshot['remote_id']
        if not remote_id:
            persist(job_id, token, state='preparing', stage='准备后台任务')
            remote = await client.request('POST', '/api/h5/reports', json={'template_id': 'research'})
            remote_id = remote['id']
            if not re.fullmatch('[a-zA-Z0-9-]{1,64}', remote_id):
                raise ValueError('Invalid Pageo report identifier')
            persist(job_id, token, remote_id=remote_id)
        path = '/api/h5/reports/' + remote_id
        remote = await client.request('GET', path)
        counts = progress_values(remote, snapshot['material_counts'])
        persist(job_id, token, material_counts=counts)
        state = remote['status']
        if (state=='ready' or remote.get('candidate_complete')) and (not snapshot['html_key'] or counts.get('candidate_key')!=remote.get('candidate_key') or state=='ready'):
            html=await client.request('GET',path+'/download')
            preview=await client.request('GET',path+'/preview')
            bundle=await client.request('GET',path+'/static.zip')
            if state!='ready':
                latest=await client.request('GET',path)
                if latest.get('candidate_key')!=remote.get('candidate_key'):
                    persist(job_id,token,state='generating',stage='草稿正在更新，稍后读取完整版本',lease_token='',lease_until=datetime.utcnow()+timedelta(seconds=5))
                    return
            validate_html(html);validate_html(preview);validate_zip(bundle,html)
            candidate_key=remote.get('candidate_key') or hashlib.sha256(html).hexdigest()
            prefix='h5/results/'+job_id+'/'+candidate_key[:24]+'/'
            store=document_store()
            for name,payload,mime in [('index.html',html,'text/html; charset=utf-8'),('preview.html',preview,'text/html; charset=utf-8'),('website.zip',bundle,'application/zip')]:
                store.put_bytes(prefix+name,payload,mime)
            counts['candidate_key']=candidate_key
            previous=(snapshot['material_counts'] or {}).get('candidate_key','')
            if previous!=candidate_key and re.fullmatch('[a-f0-9]{64}',previous):
                counts['candidate_history']=list(dict.fromkeys([*counts.get('candidate_history',[]),previous]))
            persist(job_id,token,material_counts=counts,html_key=prefix+'index.html',preview_key=prefix+'preview.html',zip_key=prefix+'website.zip')
        if snapshot['resume_requested']==2:
            persist(job_id,token,resume_requested=0)
            await client.request('POST',path+'/repair-chapter',json={'revision':remote['revision'],'chapter':counts['repair_chapter']})
            state='generating'
        if state in {'failed', 'paused'}:
            if not snapshot['resume_requested']:
                finished(job_id, token, state, counts['progress']['phase'] or '生成已暂停，进度保留',
                         safe_reason(remote.get('error')) or '后台已暂停，已完成进度保留。')
                return
            # Persist intent consumption before network dispatch; an uncertain response
            # is resolved by GET on the same report, never by a blind second purchase.
            persist(job_id, token, resume_requested=0)
            info=remote.get('continuation') or {}
            remaining=max(0,int(info.get('planned_chapters') or 0)-int(info.get('drafted_chapters') or 0))
            request_budget=min(24,max(12,remaining*2+4))
            await client.request('POST', path+'/continue', json={'revision': remote['revision'],
                'additional_stages': 1, 'max_requests': request_budget, 'max_seconds': 900 if request_budget>12 else 600})
            state = 'generating'
        if state == 'draft':
            if not remote.get('sources'):
                payload = document_store().get_bytes(snapshot['source_key'])
                if hashlib.sha256(payload).hexdigest() != snapshot['source_sha256']:
                    raise ValueError('Source fingerprint differs')
                remote = await client.request('POST', path+'/sources', files={'file': (snapshot['source_name'], payload)})
            counts = remote.get('sources', [])
            if len(counts) != 1:
                raise ValueError('Expected exactly one source')
            persist(job_id, token, material_counts={k: counts[0].get(k, 0) for k in ('pages', 'images', 'tables')})
            if remote.get('hallmark_theme') != snapshot['theme']:
                await client.request('PATCH', path+'/theme', json={'theme_slug': snapshot['theme']})
            await client.request('PATCH', path, json={'title': snapshot['title']})
            persist(job_id, token, remote_started=1, stage='提交完整阅读版制作')
            await client.request('POST', path+'/chat', json={'text': INSTRUCTION, 'template_id': 'research',
                'execution_profile': 'v9-editorial-v1'})
            state = 'generating'
        if state=='ready':
            finished(job_id, token, 'ready', '完整成果已生成，可预览、下载或保存');auto_save(job_id);return
        if state=='review_pending':
            finished(job_id,token,'review_pending','完整草稿已生成，待核对章节可单独优化');return
        if state == 'generating':
            persist(job_id, token, state='generating', stage=remote.get('progress', '后台制作中')[:160],
                    transport_failures=0, lease_token='', lease_until=datetime.utcnow()+timedelta(seconds=5))
        else:
            finished(job_id, token, 'failed', '后台状态需要检查', '未取得可交付成果，未重新提交生成。')


async def heartbeat(job_id, token):
    while True:
        await asyncio.sleep(20)
        await asyncio.to_thread(persist, job_id, token, lease_until=datetime.utcnow()+timedelta(minutes=5))


async def run_one():
    lease = claim()
    if not lease:
        return False
    job_id, token = lease
    pulse = asyncio.create_task(heartbeat(job_id, token))
    try:
        await asyncio.wait_for(advance(job_id, token), timeout=240)
    except (httpx.HTTPError, TimeoutError) as exc:
        with SessionLocal() as db:
            row = db.get(CompanyH5Job, job_id)
            count = row.transport_failures+1
        if count >= 3:
            finished(job_id, token, 'failed', '连接恢复已达次数上限', '后台暂不可连接，已保留同一任务，未盲目重复生成。')
        else:
            persist(job_id, token, transport_failures=count, lease_token='',
                lease_until=datetime.utcnow()+timedelta(seconds=5*count), stage='连接中断，等待有限恢复')
    except HTTPException as exc:
        finished(job_id, token, 'failed', '后台操作未完成', str(exc.detail))
    except Exception as exc:
        logger.warning('H5 adapter stopped job %s (%s)', job_id, type(exc).__name__)
        finished(job_id, token, 'failed', '成果检查未通过', '已停止并保留后台任务，请检查资源、格式或来源后重试。')
    finally:
        pulse.cancel()
        with suppress(asyncio.CancelledError):
            await pulse
    return True


async def worker_loop():
    # Run the adapter in its own thread/event loop; parsing and transfers cannot
    # block the existing RAG HTTP event loop. Pageo executes model work separately.
    tick = 0
    while True:
        try:
            if tick % 20 == 0:
                await asyncio.to_thread(expire_temporary)
                await asyncio.to_thread(lambda: asyncio.run(refresh_stopped()))
            await asyncio.to_thread(lambda: asyncio.run(run_one()))
        except Exception as exc:
            logger.warning('H5 adapter cycle stopped (%s)', type(exc).__name__)
        tick += 1
        await asyncio.sleep(3)


async def refresh_stopped():
    """Refresh bounded stopped jobs so a deployed fix changes real recovery state."""
    with SessionLocal() as db:
        rows = [(row.id, row.remote_id) for row in db.scalars(select(CompanyH5Job).where(
            CompanyH5Job.state.in_(['failed','paused','review_pending']), CompanyH5Job.remote_id != '')
            .order_by(CompanyH5Job.updated_at).limit(20))]
    if not rows:
        return
    async with PageoClient() as client:
        for job_id, remote_id in rows:
            remote = await client.request('GET', '/api/h5/reports/'+remote_id)
            if remote.get('status') not in {'failed','paused','review_pending'}:
                continue
            with SessionLocal() as db:
                row = db.get(CompanyH5Job, job_id)
                if row.state not in {'failed','paused','review_pending'}:
                    continue
                row.material_counts = progress_values(remote, row.material_counts)
                row.stage = row.material_counts['progress']['phase'][:160]
                row.error = safe_reason(remote.get('error'))
                if remote.get('candidate_complete') and remote.get('candidate_key')!=(row.material_counts or {}).get('candidate_key'):
                    row.state='generating';row.stage='同步完整草稿的新版本'
                    row.lease_token='';row.lease_until=None
                row.updated_at = datetime.utcnow()
                db.commit()


def expire_temporary():
    """Expire only this adapter's keys; saved and recycled artifacts keep resources."""
    from company_catalog import CompanyDocument
    now = datetime.utcnow()
    with SessionLocal() as db:
        pending = [row.id for row in db.scalars(select(CompanyH5Job).where(CompanyH5Job.state == 'ready',
            CompanyH5Job.source_kind == 'knowledge', CompanyH5Job.saved_document_id == ''))
            if not (row.material_counts or {}).get('save_error')]
    for job_id in pending:
        auto_save(job_id)
    with SessionLocal() as db:
        rows = db.scalars(select(CompanyH5Job).where(CompanyH5Job.expires_at < now,
            CompanyH5Job.state.in_(['ready', 'review_pending', 'failed', 'paused', 'expired']),
            or_(CompanyH5Job.lease_until.is_(None), CompanyH5Job.lease_until < now)))
        for row in rows:
            kept = bool(row.saved_document_id and db.get(CompanyDocument, row.saved_document_id))
            for version in (row.material_counts or {}).get('candidate_history',[]):
                if not re.fullmatch('[a-f0-9]{64}',version):raise ValueError('Unexpected H5 candidate history')
                for name in ('index.html','preview.html','website.zip'):
                    document_store().delete_object('h5/results/'+row.id+'/'+version[:24]+'/'+name)
            fields = ['source_key'] if kept else ['source_key', 'html_key', 'zip_key', 'preview_key']
            for field in fields:
                key = getattr(row, field)
                prefix = 'h5/temporary/'+row.id+'/' if field == 'source_key' else 'h5/results/'+row.id+'/'
                if key:
                    if not key.startswith(prefix) or '..' in Path(key).parts:
                        raise ValueError('Unexpected H5 retention key')
                    document_store().delete_object(key)
                    setattr(row, field, '')
            if not kept:
                row.state = 'expired'; row.stage = '临时成果已到期'; row.active_key = None
            db.commit()
