from copy import deepcopy
import json

import pytest

from report_agents.company_editorial import REVIEW_CRITERIA,bind_proposition_spans,validate_audit
from report_agents.company_editorial_context import make_evidence_context
from report_agents.company_relay_review import review_slices,run_sliced_review,validate_slice
from test_company_editorial import record


def fixture():
    rows=[record(text='公司成立于2011年。公司注册资本为100万元。股东甲持股60%。股东乙持股40%。')]
    findings=[{'finding_id':'p','text':rows[0]['text'],'information_ids':[rows[0]['information_id']]}]
    return rows,findings,review_slices(rows,findings,'公司',make_evidence_context(rows),max_propositions=2)


def response(plan,findings):
    return {'criteria':dict.fromkeys(REVIEW_CRITERIA,True),
        'findings':[{'finding_id':'p','status':'supported','reason':'主体与数值受到原文支持。'}],
        'information':[{'information_id':plan['scope'][0][0],'status':'retained','reason':'本组独立事实和条件完整保留。',
            'spans':[{'proposition_id':p,'finding_id':'p','output_excerpt':findings[0]['text']} for _,p in plan['scope']]}],
        'issues':[]}


def test_all_native_facts_are_partitioned_and_original_gate_still_applies():
    rows,findings,plans=fixture();assert len(plans)==2
    for plan in plans:
        body=json.loads(plan['prompt'].rsplit('资料：\n',1)[1])
        assert body['records'][0]['text']==rows[0]['text']
        assert len(body['records'][0]['propositions'])==2
    merged,_=run_sliced_review(plans,findings,lambda n,p:response(plans[n],findings))
    assert not validate_audit(rows,findings,bind_proposition_spans(rows,merged,findings))
    missing=deepcopy(merged);missing['information'][0]['spans'].pop()
    assert validate_audit(rows,findings,missing)


def test_completed_slices_survive_failure_and_resume_only_unfinished_scope():
    _,findings,plans=fixture();saved=[];calls=[]
    def interrupted(n,p):
        calls.append(n)
        if n==1:raise ConnectionError('test only')
        return response(plans[n],findings)
    with pytest.raises(ConnectionError):run_sliced_review(plans,findings,interrupted,save=lambda r:saved.append(r))
    assert len(saved[-1])==1
    resumed=[]
    merged,receipts=run_sliced_review(plans,findings,lambda n,p:resumed.append(n) or response(plans[n],findings),previous=saved[-1])
    assert resumed==[1] and len(receipts)==2 and merged['criteria']['semantic_retention']


def test_negative_review_is_never_overwritten_by_other_passing_slices():
    rows,findings,plans=fixture()
    def review(n,p):
        r=response(plans[n],findings)
        if n==1:r['criteria']['semantic_retention']=False;r['information'][0]['status']='lost';r['issues']=['持股比例遗漏。']
        return r
    merged,_=run_sliced_review(plans,findings,review)
    assert not merged['criteria']['semantic_retention'] and merged['issues']
    assert validate_audit(rows,findings,bind_proposition_spans(rows,merged,findings))


def test_unlocatable_output_and_unknown_source_propositions_require_new_review():
    _,findings,plans=fixture();r=response(plans[0],findings)
    r['information'][0]['spans'][0]['output_excerpt']='实际正文没有这句话。'
    with pytest.raises(ValueError):validate_slice(r,plans[0]['scope'],findings)
    r=response(plans[0],findings);r['information'][0]['spans'][0]['proposition_id']='unknown'
    with pytest.raises(ValueError):validate_slice(r,plans[0]['scope'],findings)


def test_real_editorial_package_resumes_saved_slices_without_rewriting_prose(monkeypatch):
    from report_agents.company_editorial import build_editorial_package
    from report_agents.company_transport import CompanyModelUnavailable
    from test_company_editorial import OUTLINE,proposal
    monkeypatch.setenv('COMPANY_RELAY_SLICED_REVIEW_ENABLED','true')
    monkeypatch.setenv('COMPANY_RELAY_REVIEW_PROPOSITIONS','2')
    rows,_,_=fixture();calls=[];fail=[True]
    def invoke(role,prompt,trace=None):
        calls.append(role)
        if role=='公司交付正文编写':return json.dumps(proposal(rows),ensure_ascii=False)
        if role.endswith(':1') and fail[0]:
            raise CompanyModelUnavailable(attempted=True,diagnostic={'category':'connection','attempts':1,'request_invoked':True})
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        scope=[(r['information_id'],p['proposition_id']) for r in data['records'] for p in r['propositions']]
        return json.dumps(response({'scope':scope},data['findings']),ensure_ascii=False).replace('"finding_id": "p"','"finding_id": "p0"')
    first=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1)
    assert not first['batches'][0]['accepted'] and len(first['batches'][0]['audit_slices'])==1
    fail[0]=False;calls.clear()
    second=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,previous=first)
    assert second['batches'][0]['accepted'] and calls==['公司交付分组独立审稿:1']


def test_locator_repair_changes_only_bad_locators_and_keeps_negative_verdicts():
    from report_agents.company_relay_review import repair_output_locators
    _,findings,plans=fixture();r=response(plans[0],findings)
    r['information'][0]['spans'][0]['output_excerpt']='改写过的正文'
    r['criteria']['factual_support']=False;r['issues']=['另有事实错误']
    original=deepcopy(r)
    def fix(prompt):
        return {'bindings':[{'information_id':plans[0]['scope'][0][0],
            'proposition_id':plans[0]['scope'][0][1],'span_index':0,'finding_id':'p',
            'output_excerpt':findings[0]['text'],'reason':'逐字复制正文用于定位'}],
            'criteria':dict.fromkeys(REVIEW_CRITERIA,True),'issues':[]}
    fixed,recorded=repair_output_locators(r,plans[0],findings,fix)
    assert r==original and fixed['criteria']==original['criteria'] and fixed['issues']==original['issues']
    assert fixed['information'][0]['spans'][1]==original['information'][0]['spans'][1]
    assert recorded['original_response']==original


def test_locator_repair_cannot_pass_missing_meaning_or_unknown_ids():
    from report_agents.company_relay_review import repair_output_locators
    _,findings,plans=fixture();r=response(plans[0],findings)
    r['information'][0]['spans'][0]['output_excerpt']='改写过的正文'
    binding={'information_id':plans[0]['scope'][0][0],'proposition_id':plans[0]['scope'][0][1],
        'span_index':0,'finding_id':'p','output_excerpt':None,'reason':'正文没有表达'}
    with pytest.raises(ValueError,match='unbound_review_slice_output'):
        repair_output_locators(r,plans[0],findings,lambda p:{'bindings':[binding]})
    with pytest.raises(ValueError,match='incomplete_review_locator_repair'):
        repair_output_locators(r,plans[0],findings,lambda p:{'bindings':[{**binding,'proposition_id':'unknown'}]})


def test_failed_slice_retains_response_and_specific_diagnostic():
    _,findings,plans=fixture();r=response(plans[0],findings);r['information']=[]
    with pytest.raises(ValueError) as caught:
        run_sliced_review(plans[:1],findings,lambda n,p:r)
    assert caught.value.company_review_diagnostic['code']=='incomplete_review_slice_information'
    assert caught.value.company_review_diagnostic['response']==r


def test_later_deferred_receipt_does_not_erase_completed_review_slices(monkeypatch):
    from report_agents.company_editorial import build_editorial_package
    from report_agents.company_transport import CompanyModelUnavailable
    from test_company_editorial import OUTLINE,proposal
    monkeypatch.setenv('COMPANY_RELAY_SLICED_REVIEW_ENABLED','true')
    monkeypatch.setenv('COMPANY_RELAY_REVIEW_PROPOSITIONS','2')
    rows,_,_=fixture();calls=[]
    def invoke(role,prompt,trace=None):
        calls.append(role)
        if role=='公司交付正文编写':return json.dumps(proposal(rows),ensure_ascii=False)
        if role.endswith(':1'):
            raise CompanyModelUnavailable(attempted=True,diagnostic={'category':'connection','attempts':1,'request_invoked':True})
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        scope=[(r['information_id'],p['proposition_id']) for r in data['records'] for p in r['propositions']]
        return json.dumps(response({'scope':scope},data['findings']),ensure_ascii=False).replace('"finding_id": "p"','"finding_id": "p0"')
    first=build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1)
    later=deepcopy(first['batches'][0]);later['audit_slices']=[]
    first['batches'][0]['audit_slices'][0]['input_sha256']='legacy-json-key-order-hash'
    previous={**first,'batches':first['batches']+[later]};calls.clear()
    build_editorial_package({'records':rows},OUTLINE,'公司',invoke,workers=1,previous=previous)
    assert calls and set(calls)=={'公司交付分组独立审稿:1'}


def test_review_hash_survives_sorted_checkpoint_roundtrip():
    rows,findings,plans=fixture()
    restored=json.loads(json.dumps({'rows':rows,'findings':findings},sort_keys=True))
    again=review_slices(restored['rows'],restored['findings'],'公司',make_evidence_context(restored['rows']),max_propositions=2)
    assert [p['input_sha256'] for p in plans]==[p['input_sha256'] for p in again]


def test_multiple_spans_for_one_proposition_are_repaired_individually():
    from report_agents.company_relay_review import repair_output_locators
    _,findings,plans=fixture();r=response(plans[0],findings)
    r['information'][0]['spans'].append(deepcopy(r['information'][0]['spans'][0]))
    r['information'][0]['spans'][0]['output_excerpt']='改写文本甲'
    r['information'][0]['spans'][2]['output_excerpt']='改写文本乙'
    def fix(prompt):
        return {'bindings':[{'information_id':plans[0]['scope'][0][0],'proposition_id':plans[0]['scope'][0][1],
            'span_index':i,'finding_id':'p','output_excerpt':findings[0]['text'],'reason':'正文连续原句'} for i in (0,2)]}
    fixed,_=repair_output_locators(r,plans[0],findings,fix)
    assert fixed['information'][0]['spans'][0]['output_excerpt']==findings[0]['text']
    assert fixed['information'][0]['spans'][2]['output_excerpt']==findings[0]['text']
