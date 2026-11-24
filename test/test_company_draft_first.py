import json

import pytest

from report_agents.company_editorial import build_editorial_package
from test_company_editorial import OUTLINE,record,invoke_for
from test_company_progressive_chapter import progressive_flow,source


def test_every_draft_is_saved_before_any_audit_and_resume_does_not_rewrite(monkeypatch):
    import report_agents.company_editorial as editorial
    monkeypatch.setattr(editorial,'editorial_batches',lambda rows:[((r['decision']['chapter'],r['decision']['aspect']),[r]) for r in rows])
    rows=[record('I-a'),record('I-b',text='公司已取得许可，尚未开始生产。')];calls=[];saved=[]
    invoke=invoke_for(rows,calls)
    draft=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,draft_only=True,receipt_callback=saved.append)
    assert len(calls)==2 and all('编写' in r for r in calls)
    assert len(saved)==2 and all(b['findings'] and not b['accepted'] for b in saved)
    calls.clear()
    final=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,previous=draft)
    assert len(calls)==2 and all('独立审稿' in r for r in calls)
    assert all(b['accepted'] for b in final['batches'])


def test_zero_audit_allowance_preserves_draft_and_never_marks_it_accepted(tmp_path,monkeypatch):
    monkeypatch.setenv('COMPANY_CHAPTER_DRAFT_FIRST_ENABLED','true')
    monkeypatch.setenv('COMPANY_CHAPTER_REVIEW_MAX_CALLS','0')
    flow,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);flow.read(6)
    model=flow.workflow._invoke_company_report_editor;calls=[]
    def invoke(role,prompt,*args,**kwargs):calls.append(role);return model(role,prompt,*args,**kwargs)
    monkeypatch.setattr(flow.workflow,'_invoke_company_report_editor',invoke)
    flow.write(6)
    state=flow.journal.data['chapters']['6']
    assert state['draft_complete'] and not state['unwritten_information_ids']
    assert state['status']=='pending_review' and not flow.editorials[6]['batches'][0]['accepted']
    assert not any('审稿' in r for r in calls)
    assert flow.journal.data['draft_previews']['6']['text']


def test_audit_allowance_counts_physical_retries_and_does_not_block_writing(tmp_path,monkeypatch):
    import report_agents.company_transport as transport
    monkeypatch.setenv('COMPANY_CHAPTER_REVIEW_MAX_CALLS','2')
    original=transport.bounded_company_invoke
    monkeypatch.setattr(transport,'bounded_company_invoke',lambda *a,**kw:original(*a,**kw,sleep=lambda _:None))
    flow,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);calls=[]
    def model(role,*args):
        calls.append(role)
        if '审稿' in role:raise ConnectionError('test only')
        return 'complete'
    flow.raw_invoke=model
    with pytest.raises(transport.CompanyModelUnavailable) as caught:flow.invoke('独立审稿｜company_chapter_06_agent','native')
    assert caught.value.diagnostic['category']=='review_deferred' and len(calls)==2
    assert flow.journal.data['metrics']['model_calls']==2
    assert flow.invoke('正文编写｜company_chapter_06_agent','native')=='complete'


def test_cache_only_replay_does_not_duplicate_partial_receipts(tmp_path,monkeypatch):
    monkeypatch.setenv('COMPANY_CHAPTER_DRAFT_FIRST_ENABLED','true')
    monkeypatch.setenv('COMPANY_CHAPTER_REVIEW_MAX_CALLS','0')
    flow,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);flow.read(6);flow.write(6)
    before=len(flow.journal.data['partial_editorials']['6']);calls=flow.journal.data['metrics']['model_calls']
    flow.journal.limits['model_calls']=calls
    flow.write(6)
    assert flow.journal.data['metrics']['model_calls']==calls
    assert len(flow.journal.data['partial_editorials']['6'])==before
    assert flow.journal.data['chapters']['6']['draft_complete']
