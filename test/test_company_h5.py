"""No network or model calls: actual adapter, storage, permissions and restart paths."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import io
import json
from types import SimpleNamespace
import uuid
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_company_workspace import setup
import company_knowledge as kb
import company_h5 as h5
import company_h5_worker as worker
from company_h5_models import CompanyH5Job, H5SourceVersion, observe_version
from company_catalog import CompanyDocument, CompanyProject, CompanyFolder, document_store, can_read
from models import User

HTML=b'<!doctype html><html><head><meta charset="utf-8"></head><body><h1>Complete report</h1><section id="chapter-1">Original facts and qualifiers</section><a href="#source-1">Source</a><section id="source-1">Original excerpt</section></body></html>'


class FakePageo:
    def __init__(self):
        self.reports={};self.calls=[];self.lost_chat=False;self.unavailable=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def request(self,method,path,**kwargs):
        self.calls.append((method,path,kwargs))
        if self.unavailable:raise httpx.ConnectError('controlled transport failure')
        if path.endswith('/themes'):return {'themes':[{'id':'cobalt','name':'Cobalt','palette':['#fff','#123','#00f']}]}
        if path=='/api/h5/reports' and method=='POST':
            rid=uuid.uuid4().hex;self.reports[rid]={'id':rid,'status':'draft','revision':0,'sources':[],'progress':'reading'};return dict(self.reports[rid])
        rid=path.split('/')[4];row=self.reports[rid]
        if path.endswith('/sources'):
            row['sources']=[{'pages':30,'images':4,'tables':3,'name':kwargs['files']['file'][0]}];return dict(row)
        if path.endswith('/theme'):row['hallmark_theme']=kwargs['json']['theme_slug'];return dict(row)
        if path.endswith(('/chat','/continue','/repair-chapter')):
            row['status']='generating'
            if self.lost_chat:self.lost_chat=False;raise httpx.ReadTimeout('controlled lost acknowledgement')
            return dict(row)
        if path.endswith(('/download','/preview')):return row.get('html',HTML)
        if path.endswith('/static.zip'):
            result=io.BytesIO()
            with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('index.html',row.get('html',HTML))
            return result.getvalue()
        if method=='PATCH':row.update(kwargs['json'])
        return dict(row)


@pytest.fixture
def system(setup,monkeypatch):
    monkeypatch.setenv('H5_WORKBENCH_ENABLED','1')
    monkeypatch.setenv('H5_PAGEO_URL','http://127.0.0.1:18806')
    monkeypatch.setenv('H5_PAGEO_SERVICE_KEY','isolated-test-service-key')
    for module in (h5,worker):monkeypatch.setattr(module,'SessionLocal',setup.db)
    engine=setup.db.kw['bind']
    for table in (H5SourceVersion.__table__,CompanyH5Job.__table__):table.create(engine,checkfirst=True)
    with setup.db() as db:
        db.add_all([User(id=i,username=name,password_hash='not-a-real-login',role='admin' if name=='admin' else 'user')
            for i,name in enumerate(('alice','bob','carol','admin'),1)])
        db.add(CompanyProject(id='project',index_id='index',name='Project',owner='alice',owner_user_id=1,
            visibility='members',confidentiality='internal',department='project',category='项目原件'))
        db.add(CompanyFolder(id='folder',project_id='project',name='Reports'))
        db.commit()
    # The inherited fixture's cloud settings use its own index ID.
    index=kb.settings()['index_id']
    with setup.db() as db:db.get(CompanyProject,'project').index_id=index;db.commit()
    app=FastAPI();app.include_router(kb.router);app.include_router(h5.router)
    app.dependency_overrides[kb.get_current_user]=lambda:setup.user
    fake=FakePageo()
    monkeypatch.setattr(worker,'PageoClient',lambda:fake)
    monkeypatch.setattr(h5,'PageoClient',lambda:fake)
    async def no_sync(*_):pass
    import company_workspace
    monkeypatch.setattr(company_workspace,'sync_catalog_in_background',lambda *_,**kwargs:None)
    setup.client=TestClient(app);setup.fake=fake
    return setup


def source(system,name='source.txt',payload=b'Original facts, 7 samples, only 2024.',visibility='members',readers=None):
    ident=uuid.uuid4().hex;key='originals/'+ident+'.txt';document_store().put_bytes(key,payload)
    with system.db() as db:
        db.add(CompanyDocument(id=ident,index_id=kb.settings()['index_id'],title=name,format='TXT',size=len(payload),
            object_key=key,sha256=hashlib.sha256(payload).hexdigest(),owner='alice',owner_user_id=1,
            state='ready',cloud_id='cloud-'+ident,knowledge_project_id='project',folder_id='folder',project='Project',
            visibility=visibility,readers=readers or [],confidentiality='internal'))
        db.commit()
    return ident


def create(system,source_id,key=None,theme='cobalt'):
    response=system.client.post('/company-knowledge/h5/jobs',json={'document_id':source_id,
        'request_key':key or uuid.uuid4().hex,'theme':theme})
    assert response.status_code==202,response.text
    return response.json()


def cycle(system,job_id,complete=False):
    with system.db() as db:
        row=db.get(CompanyH5Job,job_id)
        row.lease_until=None
        if complete:system.fake.reports[row.remote_id]['status']='ready'
        db.commit()
    assert asyncio.run(worker.run_one())


def done(system,job):
    cycle(system,job['id']);cycle(system,job['id'],True)
    result=system.client.get('/company-knowledge/h5/jobs/'+job['id'])
    assert result.status_code==200,result.text
    assert result.json()['state']=='ready',result.text
    return result.json()


def save(system,job_id,**values):
    return system.client.post('/company-knowledge/h5/jobs/'+job_id+'/save',json={'project_id':'project','folder_id':'folder',**values})


def test_knowledge_complete_loop_idempotency_offline_preview_save_reopen_and_download(system):
    ident=source(system);key=uuid.uuid4().hex
    first=create(system,ident,key);assert create(system,ident,key)['id']==first['id']
    assert create(system,ident)['id']==first['id']
    done(system,first)
    assert system.client.get('/company-knowledge/h5/jobs/'+first['id']).json()['saved_document_id']
    calls=len(system.fake.calls)
    response=save(system,first['id']);assert response.status_code==200,response.text
    saved=response.json()['saved_document_id'];assert saved
    assert save(system,first['id']).json()['saved_document_id']==saved
    assert len(system.fake.calls)==calls
    with system.db() as db:
        doc=db.get(CompanyDocument,saved)
        assert doc.state=='display' and doc.format=='H5' and doc.cloud_id is None
        assert doc.object_key.startswith('h5/results/')
        assert db.scalar(select(func.count()).select_from(CompanyDocument))==2
    assert system.scheduled==[]
    preview=system.client.get('/company-knowledge/h5/jobs/'+first['id']+'/preview').json()['html']
    assert 'connect-src \'none\'' in preview and 'Original excerpt' in preview
    assert 'isolated-test-service-key' not in preview
    assert system.client.get('/company-knowledge/h5/saved/'+saved).json()['id']==first['id']
    assert system.client.get('/company-knowledge/h5/jobs/'+first['id']+'/download').content==HTML
    snapshot=system.client.get('/company-knowledge/h5/jobs/'+first['id']+'/original')
    assert snapshot.status_code==200,snapshot.text
    assert b'Original facts' in snapshot.content and b'alice' in snapshot.content
    with zipfile.ZipFile(io.BytesIO(system.client.get('/company-knowledge/h5/jobs/'+first['id']+'/download?kind=zip').content)) as z:assert z.read('index.html')==HTML
    original=system.client.get('/company-knowledge/documents/'+saved+'/original?download=true')
    assert original.status_code==200 and original.content==HTML
    assert 'attachment' in original.headers['content-disposition']
    assert system.client.post('/company-knowledge/documents/'+saved+'/retry').status_code==409
    listed=system.client.get('/company-knowledge/documents?local_only=true').json()['documents']
    assert next(d for d in listed if d['id']==saved)['label']=='无需索引'


def test_local_upload_never_imports_original_and_does_not_broaden_in_shared_destination(system):
    response=system.client.post('/company-knowledge/h5/uploads',files={'file':('local.txt',b'local material')},data={'request_key':'local-1','theme':'cobalt'})
    assert response.status_code==202,response.text
    with system.db() as db:assert db.scalar(select(func.count()).select_from(CompanyDocument))==0
    job=done(system,response.json());saved=save(system,job['id']).json()['saved_document_id']
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']+'/original').content==b'local material'
    system.user.id=2;system.user.username='bob'
    for path in ('/jobs/'+job['id'], '/jobs/'+job['id']+'/preview','/jobs/'+job['id']+'/download','/jobs/'+job['id']+'/original','/saved/'+saved):
        assert system.client.get('/company-knowledge/h5'+path).status_code==404,path
    assert system.client.get('/company-knowledge/documents/'+saved+'/original').status_code==404
    assert system.client.get('/company-knowledge/h5/jobs').json()['items']==[]


def test_source_revocation_reaches_job_preview_download_and_saved_file(system):
    ident=source(system,visibility='restricted',readers=['bob'])
    job=done(system,create(system,ident));saved=save(system,job['id']).json()['saved_document_id']
    system.user.id=2;system.user.username='bob'
    assert system.client.get('/company-knowledge/h5/saved/'+saved).status_code==200
    with system.db() as db:db.get(CompanyDocument,ident).readers=[];db.commit()
    assert system.client.get('/company-knowledge/h5/saved/'+saved).status_code==404
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']+'/preview').status_code==404
    assert system.client.get('/company-knowledge/documents/'+saved+'/original').status_code==404


def test_rename_move_does_not_report_stale_but_new_version_after_move_does(system):
    ident=source(system);job=done(system,create(system,ident))
    with system.db() as db:
        doc=db.get(CompanyDocument,ident);doc.title='renamed.txt';doc.folder_id='folder';doc.updated_at=datetime.utcnow()
        db.commit()
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()['source_changed'] is False
    uploaded=system.client.post('/company-knowledge/uploads',files={'file':('renamed.txt',b'updated facts')},
        data={'project_id':'project','folder_id':'folder'})
    assert uploaded.status_code==202,uploaded.text
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()['source_changed'] is True
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']+'/download').content==HTML


def test_save_failure_does_not_generate_again_and_new_result_allocates_new_file_version(system):
    ident=source(system);first=done(system,create(system,ident));calls=len(system.fake.calls)
    # Already saved: an idempotent request returns the same artifact.
    assert save(system,first['id'],folder_id='not-a-folder').status_code==200
    assert len(system.fake.calls)==calls
    assert system.client.get('/company-knowledge/h5/jobs/'+first['id']).json()['state']=='ready'
    one=save(system,first['id']).json()['saved_document_id']
    second=done(system,create(system,ident));two=save(system,second['id']).json()['saved_document_id']
    with system.db() as db:assert db.get(CompanyDocument,two).version==db.get(CompanyDocument,one).version+1
    assert one!=two


def test_lost_start_acknowledgement_and_restart_do_not_purchase_generation_twice(system):
    job=create(system,source(system));system.fake.lost_chat=True
    cycle(system,job['id'])
    with system.db() as db:
        row=db.get(CompanyH5Job,job['id']);assert row.remote_id and row.remote_started
        row.lease_until=datetime.utcnow()-timedelta(seconds=1);db.commit()
    cycle(system,job['id'])
    assert sum(path.endswith('/chat') for _,path,_ in system.fake.calls)==1
    cycle(system,job['id'],True)
    assert system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()['state']=='ready'


def test_transport_recovery_stops_after_three_cycles_and_can_resume_same_remote_task(system):
    job=create(system,source(system));cycle(system,job['id'])
    system.fake.unavailable=True
    for _ in range(3):cycle(system,job['id'])
    row=system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()
    assert row['state']=='failed' and '次数上限' in row['stage']
    assert not asyncio.run(worker.run_one())
    system.fake.unavailable=False
    assert system.client.post('/company-knowledge/h5/jobs/'+job['id']+'/resume').status_code==202
    cycle(system,job['id'],True)
    assert sum(path.endswith('/chat') for _,path,_ in system.fake.calls)==1


def test_unreadable_sources_and_disabled_entry_fail_closed(system,monkeypatch):
    ident=source(system,visibility='restricted')
    system.user.id=2;system.user.username='bob'
    assert system.client.get('/company-knowledge/h5/sources').json()['items']==[]
    assert system.client.post('/company-knowledge/h5/jobs',json={'document_id':ident,'request_key':'blocked'}).status_code==404
    monkeypatch.setenv('H5_WORKBENCH_ENABLED','0')
    assert system.client.get('/company-knowledge/h5/jobs').status_code==404
    assert system.client.get('/company-knowledge/status').json()['h5_enabled'] is False


def test_export_guard_rejects_resource_loss_and_zip_disagreement():
    with pytest.raises(ValueError):worker.validate_html(b'<html><img src="/temporary/image.png"></html>')
    with pytest.raises(ValueError):
        result=io.BytesIO()
        with zipfile.ZipFile(result,'w') as z:z.writestr('index.html',b'other content')
        worker.validate_zip(result.getvalue(),HTML)


def test_expiry_preserves_saved_and_recycled_resources_and_never_indexes_restore(system):
    first=done(system,create(system,source(system)))
    saved=save(system,first['id']).json()['saved_document_id']
    assert system.client.delete('/company-knowledge/documents/'+saved).status_code==200
    recycle=system.client.get('/company-knowledge/recycle-bin').json()['items']
    item=next(row for row in recycle if row['title'].startswith('source'))
    with system.db() as db:
        row=db.get(CompanyH5Job,first['id']);row.expires_at=datetime.utcnow()-timedelta(days=1)
        source_key, html_key=row.source_key,row.html_key;db.commit()
    worker.expire_temporary()
    assert not document_store().exists(source_key)
    assert document_store().exists(html_key)
    restored=system.client.post('/company-knowledge/recycle-bin/'+item['id']+'/restore')
    assert restored.status_code==200,restored.text
    with system.db() as db:assert db.get(CompanyDocument,saved).state=='display'
    assert system.scheduled==[]
    assert system.client.get('/company-knowledge/h5/jobs/'+first['id']+'/download').content==HTML
    uploaded=system.client.post('/company-knowledge/h5/uploads',files={'file':('another.txt',b'local')},data={'request_key':'expire-local','theme':'cobalt'})
    second=done(system,uploaded.json())
    with system.db() as db:
        row=db.get(CompanyH5Job,second['id']);row.expires_at=datetime.utcnow()-timedelta(days=1)
        html_key=row.html_key;db.commit()
    worker.expire_temporary()
    assert not document_store().exists(html_key)
    assert system.client.post('/company-knowledge/h5/jobs/'+second['id']+'/resume').status_code in (409,410)


def test_saved_artifact_remains_excluded_from_qa_even_if_metadata_is_accidentally_changed(system):
    from company_workspace import select_scope
    job=done(system,create(system,source(system)))
    saved=save(system,job['id']).json()['saved_document_id']
    with system.db() as db:
        doc=db.get(CompanyDocument,saved);doc.state='ready';doc.cloud_id='must-not-query-this';db.commit()
    assert 'must-not-query-this' not in select_scope(kb.settings(),system.user,[],'accessible')
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as rejected:select_scope(kb.settings(),system.user,[saved],'accessible')
    assert '不参与问答' in rejected.value.detail


def test_generator_outage_does_not_break_completed_preview_download_or_save(system):
    row=done(system,create(system,source(system)))
    system.fake.unavailable=True
    assert system.client.get('/company-knowledge/h5/themes').status_code==503
    assert system.client.get('/company-knowledge/h5/jobs/'+row['id']+'/preview').status_code==200
    assert system.client.get('/company-knowledge/h5/jobs/'+row['id']+'/download').content==HTML
    assert save(system,row['id']).status_code==200


def test_auto_save_uses_source_snapshot_and_denied_write_keeps_result(system):
    ident=source(system)
    job=create(system,ident)
    with system.db() as db:
        db.get(CompanyDocument,ident).folder_id=''
        db.commit()
    row=done(system,job)
    with system.db() as db:assert db.get(CompanyDocument,row['saved_document_id']).folder_id=='folder'
    system.user.id=2;system.user.username='bob'
    other=done(system,create(system,ident))
    assert not other['saved_document_id'] and '未保存到知识库' in other['save_error']
    assert other['state']=='ready' and system.client.get('/company-knowledge/h5/jobs/'+other['id']+'/download').content==HTML
    calls=len(system.fake.calls)
    assert save(system,other['id']).status_code in (403,404)
    assert len(system.fake.calls)==calls


def test_failure_reason_progress_and_resume_use_same_remote_with_hard_limit(system):
    job=create(system,source(system));cycle(system,job['id'])
    with system.db() as db:rid=db.get(CompanyH5Job,job['id']).remote_id
    system.fake.reports[rid].update(status='failed',error='规划缺少图解机会 token=secret123 /app/data/private',
        continuation={'phase':'章节规划校验','requests':3,'completed_reading':1,'planned_reading':1,'drafted_chapters':0,'planned_chapters':1})
    for attempt in range(2):
        cycle(system,job['id'])
        row=system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()
        assert '规划缺少图解机会' in row['error'] and 'secret123' not in row['error'] and '/app/data' not in row['error']
        assert row['progress']['requests']==3 and row['progress']['completed_reading']==1
        assert system.client.post('/company-knowledge/h5/jobs/'+job['id']+'/resume').status_code==202
        cycle(system,job['id'])
        system.fake.reports[rid]['status']='failed'
    cycle(system,job['id'])
    row=system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()
    assert not row['can_resume'] and system.client.post('/company-knowledge/h5/jobs/'+job['id']+'/resume').status_code==409
    continues=[kwargs['json'] for _,path,kwargs in system.fake.calls if path.endswith('/continue')]
    assert len(continues)==2 and all(v['max_requests']==12 and v['max_seconds']==600 for v in continues)
    assert sum(path.endswith('/chat') for _,path,_ in system.fake.calls)==1


def test_saved_destination_ceiling_also_applies_to_creator_workbench(system):
    job=done(system,create(system,source(system)))
    assert job['saved_document_id']
    with system.db() as db:
        db.get(CompanyProject,'project').deleted_at=datetime.utcnow()
        db.commit()
    for suffix in ['', '/preview?full=true', '/download', '/original']:
        assert system.client.get('/company-knowledge/h5/jobs/'+job['id']+suffix).status_code==404
    assert system.client.get('/company-knowledge/h5/saved/'+job['saved_document_id']).status_code==404


@pytest.mark.parametrize('blocked',[False,True])
def test_stopped_progress_refreshes_after_fix_without_generation_or_state_change(system,blocked):
    job=create(system,source(system));cycle(system,job['id'])
    with system.db() as db:rid=db.get(CompanyH5Job,job['id']).remote_id
    system.fake.reports[rid].update(status='failed',error='上次规划失败',
        continuation={'phase':'章节规划校验','requests':8,'next_action':'inspect_task_failure',
            'completed_reading':3,'planned_reading':3,'planned_chapters':28})
    cycle(system,job['id'])
    assert not system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()['can_resume']
    system.fake.reports[rid]['continuation']['next_action']='inspect_task_failure' if blocked else 'continue_same_run'
    before=len(system.fake.calls)
    asyncio.run(worker.refresh_stopped())
    row=system.client.get('/company-knowledge/h5/jobs/'+job['id']).json()
    assert row['state']=='failed' and row['can_resume']==(not blocked)
    assert row['progress']['requests']==8 and row['progress']['completed_reading']==3
    assert all(method=='GET' for method,_,_ in system.fake.calls[before:])


def test_long_report_recovery_is_bounded_by_existing_native_limit_and_keeps_reading(system):
    job=create(system,source(system));cycle(system,job['id'])
    with system.db() as db:rid=db.get(CompanyH5Job,job['id']).remote_id
    system.fake.reports[rid].update(status='failed',continuation={'requests':6,
        'planned_chapters':16,'drafted_chapters':0,'completed_reading':3,'planned_reading':3})
    cycle(system,job['id'])
    assert system.client.post('/company-knowledge/h5/jobs/'+job['id']+'/resume').status_code==202
    cycle(system,job['id'])
    calls=[v for m,p,v in system.fake.calls if p.endswith('/continue')]
    assert len(calls)==1 and calls[0]['json']['max_requests']==24 and calls[0]['json']['max_seconds']==900
    assert sum(p.endswith('/chat') for _,p,_ in system.fake.calls)==1


def test_complete_pending_draft_is_readable_but_not_saved_and_repairs_only_selected_chapter(system):
    job=create(system,source(system));cycle(system,job['id'])
    with system.db() as db:rid=db.get(CompanyH5Job,job['id']).remote_id
    system.fake.reports[rid].update(status='generating',candidate_complete=True,candidate_key='version1',
        pending_chapters={'0':{'reason':'引用限定待核对'}})
    cycle(system,job['id'])
    base='/company-knowledge/h5/jobs/'+job['id']
    row=system.client.get(base).json()
    assert row['candidate_available'] and row['state']=='generating' and not row['saved_document_id']
    assert system.client.get(base+'/download').content==HTML
    assert system.client.get(base+'/preview').status_code==200
    assert save(system,job['id']).status_code==409
    assert system.client.post(base+'/repair-chapter',json={'chapter':0}).status_code==409
    for attempt in range(2):
        system.fake.reports[rid]['status']='review_pending';cycle(system,job['id'])
        assert system.client.post(base+'/repair-chapter',json={'chapter':1}).status_code==409
        assert system.client.post(base+'/repair-chapter',json={'chapter':0}).status_code==202
        cycle(system,job['id'])
    system.fake.reports[rid]['status']='review_pending';cycle(system,job['id'])
    assert system.client.post(base+'/repair-chapter',json={'chapter':0}).status_code==409
    repairs=[kw['json'] for method,path,kw in system.fake.calls if path.endswith('/repair-chapter')]
    assert len(repairs)==2 and all(v['chapter']==0 for v in repairs)
    assert sum(p.endswith('/chat') for _,p,_ in system.fake.calls)==1
    system.user.id=2;system.user.username='bob'
    assert system.client.post(base+'/repair-chapter',json={'chapter':0}).status_code in (403,404)


def test_stopped_candidate_refresh_copies_new_artifact_without_model_dispatch(system):
    job=create(system,source(system));cycle(system,job['id'])
    with system.db() as db:rid=db.get(CompanyH5Job,job['id']).remote_id
    system.fake.reports[rid].update(status='review_pending',candidate_complete=True,candidate_key='first',pending_chapters={'0':{'reason':'check'}})
    cycle(system,job['id'])
    base='/company-knowledge/h5/jobs/'+job['id']
    system.fake.reports[rid].update(candidate_key='updated',html=HTML.replace(b'Complete report',b'Updated chapter'))
    before=len(system.fake.calls)
    asyncio.run(worker.refresh_stopped())
    assert all(method=='GET' for method,_,_ in system.fake.calls[before:])
    assert system.client.get(base).json()['state']=='generating'
    cycle(system,job['id'])
    latest=system.client.get(base).json()
    assert latest['state']=='review_pending' and latest['candidate_key']=='updated' and not latest['saved_document_id']
    assert b'Updated chapter' in system.client.get(base+'/download').content
    assert not any(method=='POST' for method,_,_ in system.fake.calls[before:])
