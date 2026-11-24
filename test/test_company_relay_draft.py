import json

import pytest

from report_agents.company_editorial import build_editorial_package
from report_agents.company_editorial_context import make_evidence_context
from report_agents.company_relay_draft import draft_slices,run_sliced_draft
from report_agents.company_transport import CompanyModelUnavailable
from test_company_editorial import record,OUTLINE


def fixture():
    rows=[record(text='公司成立于2011年。公司注册资本为100万元。股东甲持股60%。股东乙持股40%。')]
    plans=draft_slices(rows,'公司',6,rows[0]['decision']['aspect'],make_evidence_context(rows),size=2)
    return rows,plans


def answer(prompt):
    data=json.loads(prompt.rsplit('材料：\n',1)[1])
    return {'findings':[{'finding_id':'p','text':''.join(p['text'] for p in row['propositions']),
        'information_ids':[row['information_id']]} for row in data['records']]}


def test_all_native_propositions_reach_small_tasks_without_losing_context():
    rows,plans=fixture();assert len(plans)==2
    for plan in plans:
        data=json.loads(plan['prompt'].rsplit('材料：\n',1)[1])
        assert data['records'][0]['text']==rows[0]['text']
    proposal,receipts=run_sliced_draft(plans,6,rows[0]['decision']['aspect'],lambda n,p:answer(p))
    assert ''.join(f['text'] for f in proposal['findings'])==rows[0]['text'] and len(receipts)==2


def test_failure_keeps_completed_pieces_and_resume_does_not_regenerate_them():
    rows,plans=fixture();saved=[]
    def fail(n,p):
        if n==1:raise ConnectionError('test only')
        return answer(p)
    with pytest.raises(ConnectionError):run_sliced_draft(plans,6,rows[0]['decision']['aspect'],fail,save=lambda r,p:saved.append((r,p)))
    assert saved[-1][1]['findings']
    calls=[]
    proposal,receipts=run_sliced_draft(plans,6,rows[0]['decision']['aspect'],lambda n,p:calls.append(n) or answer(p),previous=saved[-1][0])
    assert calls==[1] and len(receipts)==2 and len(proposal['findings'])==2


def test_partial_piece_is_not_mistaken_for_complete_record_on_editorial_resume(monkeypatch):
    monkeypatch.setenv('COMPANY_RELAY_SLICED_DRAFT_ENABLED','true')
    monkeypatch.setenv('COMPANY_RELAY_DRAFT_PROPOSITIONS','2')
    rows,_=fixture();calls=[];broken=[True]
    def invoke(role,prompt,trace=None):
        calls.append(role)
        if role.endswith(':1') and broken[0]:raise CompanyModelUnavailable(attempted=True,diagnostic={'category':'connection','request_invoked':True,'attempts':1})
        return json.dumps(answer(prompt),ensure_ascii=False)
    first=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,draft_only=True)
    batch=first['batches'][0]
    assert batch['findings'] and batch['draft_scope_complete'] is False and not batch['accepted']
    calls.clear();broken[0]=False
    second=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,draft_only=True,previous=first)
    assert calls==['公司交付分组正文编写:1']
    assert second['batches'][0]['draft_scope_complete'] and not second['batches'][0]['accepted']


def test_partial_state_clears_after_successful_whole_record_fallback(monkeypatch):
    from test_company_editorial import proposal
    monkeypatch.setenv('COMPANY_RELAY_SLICED_DRAFT_ENABLED','true')
    monkeypatch.setenv('COMPANY_RELAY_DRAFT_PROPOSITIONS','2')
    rows,_=fixture()
    def interrupted(role,prompt,trace=None):
        if role.endswith(':1'):raise CompanyModelUnavailable(attempted=True,diagnostic={'category':'connection','attempts':1})
        return json.dumps(answer(prompt),ensure_ascii=False)
    first=build_editorial_package({'records':rows},OUTLINE,'公司',interrupted,workers=1,draft_only=True)
    monkeypatch.setenv('COMPANY_RELAY_SLICED_DRAFT_ENABLED','false')
    second=build_editorial_package({'records':rows},OUTLINE,'公司',lambda *a:json.dumps(proposal(rows),ensure_ascii=False),
        workers=1,draft_only=True,previous=first)
    assert second['batches'][0]['draft_scope_complete'] and not second['batches'][0]['accepted']
