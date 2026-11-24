from pathlib import Path

import pytest

from report_agents.company_editorial import editorial_policy_fingerprint,_legacy_policy_aliases,editorial_batches
from report_agents.company_transport import bounded_company_invoke
from test_company_progressive_chapter import progressive_flow,source


def test_connection_retry_uses_cooldown_instead_of_immediate_request():
    calls=[];waits=[]
    def invoke(*args):
        calls.append(1)
        if len(calls)<3:raise ConnectionError('upstream interrupted')
        return 'complete'
    assert bounded_company_invoke(invoke,retry_base_seconds=10,sleep=waits.append)('write','native')=='complete'
    assert waits==[10,20] and len(calls)==3


def test_pause_during_retry_prevents_another_physical_request(tmp_path,monkeypatch):
    import report_agents.company_transport as transport
    original=transport.bounded_company_invoke
    monkeypatch.setattr(transport,'bounded_company_invoke',lambda *a,**kw:original(*a,**kw,sleep=lambda _:None))
    flow,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    stopped=[False];calls=[]
    flow.pause_requested=lambda:stopped[0]
    def fail(*args):
        calls.append(1);stopped[0]=True;raise ConnectionError('test only')
    flow.raw_invoke=fail
    with pytest.raises(transport.CompanyModelUnavailable) as caught:flow.invoke('write','original evidence')
    assert caught.value.diagnostic['category']=='paused'
    assert len(calls)==1 and flow.journal.data['metrics']['model_calls']==1


@pytest.mark.parametrize('change,stable',[
    ('batching',True),('transport_timeout',True),('semantic_review',False)])
def test_semantic_policy_binds_rules_not_request_scheduling(monkeypatch,change,stable):
    before=editorial_policy_fingerprint();aliases=_legacy_policy_aliases();assert aliases
    read=Path.read_text
    def modified(path,*args,**kw):
        text=read(path,*args,**kw)
        if change=='batching' and path.name=='company_editorial.py':
            return text.replace("'COMPANY_EDITORIAL_BATCH_CHARS','6000'","'COMPANY_EDITORIAL_BATCH_CHARS','7000'")
        if change=='transport_timeout' and path.name=='research_workflow.py':
            return text.replace('COMPANY_REPORT_EDITOR_TIMEOUT_SECONDS','DIFFERENT_TIMEOUT_SETTING')
        if change=='semantic_review' and path.name=='company_editorial.py':
            return text.replace('def audit_prompt(','def changed_semantic_audit_prompt(')
        return text
    monkeypatch.setattr(Path,'read_text',modified)
    assert (editorial_policy_fingerprint()==before) is stable
    assert bool(_legacy_policy_aliases()) is stable


def test_known_legacy_audit_reuses_but_arbitrary_policy_does_not(tmp_path,monkeypatch):
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6);f.write(6)
    for batch in f.editorials[6]['batches']:batch['policy_sha256']=next(iter(_legacy_policy_aliases()))
    f.journal.data['partial_editorials']={};calls.clear();f.write(6)
    assert not any('正文编写' in role or '独立审稿' in role for role,p in calls)
    for batch in f.editorials[6]['batches']:batch['policy_sha256']='unrecognized_semantic_policy'
    f.journal.data['partial_editorials']={};calls.clear();f.write(6)
    assert any('独立审稿' in role for role,p in calls)


def test_new_small_batches_preserve_every_record(monkeypatch):
    rows=[{'information_id':f'I-{i}','text':'具体原文事实。'*150,'context':{'chapter_question_owner':2},
           'provenance':[],'evidence_ids':[],'statement_ids':[],
           'decision':{'chapter':2,'aspect':'工商信息'}} for i in range(12)]
    batches=editorial_batches(rows)
    assert len(batches)>1
    assert sorted(r['information_id'] for key,b in batches for r in b)==sorted(r['information_id'] for r in rows)
    assert all(len(b)<=4 for key,b in batches)


def test_progress_saves_in_caller_thread_before_independent_review_returns(tmp_path,monkeypatch):
    import threading
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6)
    ready=threading.Event();caller=threading.get_ident();saved_threads=[]
    persist=f.persist_chapter_draft;model=f.workflow._invoke_company_report_editor
    def save(*a,**kw):
        saved_threads.append(threading.get_ident());ready.set();return persist(*a,**kw)
    def review(role,prompt,trace=None,**kw):
        if role.startswith('公司交付正文独立审稿'):assert ready.wait(3),'draft must be persisted before review finishes'
        return model(role,prompt,trace,**kw)
    monkeypatch.setattr(f,'persist_chapter_draft',save)
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',review)
    f.write(6)
    assert saved_threads and all(t==caller for t in saved_threads)
    assert f.journal.data['partial_editorials']['6'][-1]['accepted']
