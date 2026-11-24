import json
from copy import deepcopy
from types import SimpleNamespace
import pytest

from company_chapters import chapters,chapter_summary_required
from report_agents.company_chapter_synthesis import contract_for,input_key,summary_finding,validate_draft
from report_agents.company_semantic_review import (LEGACY_BODY_POLICY,VERSION,build_package,digest,
    package_errors,prepare_findings,review_prompt)
from report_agents.company_editorial import record_payload,digest_groups
from report_agents.company_corpus_writer import render_compact_chapters
from test_company_chapter_synthesis import fixture
from test_company_semantic_review import passing


def old_package(target='海岳设备有限公司'):
    rows,c,plan,draft=fixture(target)
    outline=[('摘要',[]),(c['title'],c['sections'])]
    contract=contract_for(2,outline,target);contract.pop('chapter_summary_required')
    findings=prepare_findings(draft['findings'],rows,2,c['sections'][0])
    review=passing(json.loads(review_prompt(rows,findings,target).rsplit('资料：\n',1)[1]))
    key=digest([VERSION,LEGACY_BODY_POLICY,target,[record_payload(r) for r in rows],findings])
    synthesis={'input_sha256':input_key(rows,target,contract),'contract':contract,'plan':plan,
        'draft':{'findings':draft['findings']},'attempts':{'plan':1,'draft':1}}
    b={'review_mode':VERSION,'accepted':True,'chapter':2,'aspect':c['sections'][0],
       'information_ids':['S1'],'findings':findings,'semantic_review':review,'input_sha256':key,
       'policy_sha256':LEGACY_BODY_POLICY,'records_sha256':digest([record_payload(r) for r in rows]),
       'synthesis':synthesis,'synthesis_applied':True,'semantic_history':[]}
    return rows,outline,{'review_mode':VERSION,'policy_sha256':LEGACY_BODY_POLICY,'input_sha256':key,'findings':findings,'batches':[b]}


def summary_model(calls):
    def invoke(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if '小结补写' in role:
            assert '不列待核事项' in prompt and data['body'][0]['text']
            return json.dumps({'chapter_summary':{'text':data['body'][0]['text'],'information_ids':['S1']}},ensure_ascii=False)
        assert '局部复审' in role
        assert len(data['paragraphs'])==1 and data['paragraphs'][0]['content_role']=='chapter_summary'
        assert data['read_only_context']
        return json.dumps(passing(data),ensure_ascii=False)
    return invoke


@pytest.mark.parametrize('target',['海岳设备有限公司','星川生物股份有限公司'])
def test_accepted_body_gets_summary_only_and_zero_call_replay(target):
    rows,outline,previous=old_package(target);calls=[]
    out=build_package({'records':rows},target,summary_model(calls),previous,outline=outline)
    assert out['batches'][0]['accepted'] and len(calls)==2 and not package_errors(out,rows,target)
    body=[f for f in out['findings'] if f.get('content_role')!='chapter_summary']
    assert body==previous['findings'] and out['chapter_summary']['text']==body[0]['text']
    again=build_package({'records':rows},target,lambda *a:pytest.fail('cache missed'),out,outline=outline)
    assert again==out


def test_summary_attempt_survives_resume_and_body_is_not_rewritten():
    rows,outline,previous=old_package();calls=[];receipts=[]
    draft=build_package({'records':rows},'海岳设备有限公司',summary_model(calls),previous,
        receipt_callback=receipts.append,draft_only=True,outline=outline)
    assert len(calls)==1 and draft['batches'][0]['chapter_summary'] and not draft['batches'][0]['accepted']
    done=build_package({'records':rows},'海岳设备有限公司',summary_model(calls),draft,outline=outline)
    assert len(calls)==2 and done['batches'][0]['accepted']


def test_pending_older_draft_does_not_hide_accepted_body():
    rows,outline,previous=old_package();calls=[]
    pending=deepcopy(previous['batches'][0]);pending['accepted']=False
    pending['findings'][0]['text']='尚未审稿的旧草稿。'
    pending['synthesis']={'contract':pending['synthesis']['contract'],'attempts':{'plan':0}}
    previous['batches'].append(pending)
    out=build_package({'records':rows},'海岳设备有限公司',summary_model(calls),previous,outline=outline)
    assert out['batches'][0]['accepted'] and len(calls)==2
    assert out['findings'][:-1]==previous['findings']


@pytest.mark.parametrize('role',['公司章节小结补写','公司核心事实局部复审','公司核心事实局部修改'])
def test_local_tasks_do_not_inherit_full_chapter_examples(role,monkeypatch):
    import report_agents.company_chapter_agent as module
    monkeypatch.setattr(module,'prompt_contract',lambda *a:pytest.fail('full chapter prompt leaked'))
    agent=module.CompanyChapterAgent(2,SimpleNamespace(target_name='测试公司'),lambda *a,**k:None)
    captured=[]
    agent.invoke(lambda role,prompt,*a:captured.append(prompt),role,'仅处理本次指定内容')
    assert '局部编辑任务' in captured[0] and '仅处理本次指定内容' in captured[0]


def test_summary_sources_and_body_dependency_are_checked():
    rows,c,plan,draft=fixture();body=draft['findings']
    with pytest.raises(ValueError,match='unbound_chapter_summary'):
        summary_finding({'text':'已知结论','information_ids':['new-id']},body,2)
    s=summary_finding(draft['chapter_summary'],body,2)
    changed=deepcopy(body);changed[0]['text']='正文内容发生变化。'
    with pytest.raises(ValueError,match='stale_chapter_summary'):
        validate_draft({'findings':changed+[s]},rows,c)
    with pytest.raises(ValueError,match='duplicate_chapter_summary'):
        validate_draft({'findings':body+[s,s]},rows,c)
    with pytest.raises(ValueError,match='missing_chapter_summary'):
        validate_draft({'findings':body},rows,c)


def test_render_summary_at_end_and_keep_legacy_draft_readable():
    rows,outline,previous=old_package();out=build_package({'records':rows},'海岳设备有限公司',summary_model([]),previous,outline=outline)
    project=SimpleNamespace(target_name='海岳设备有限公司',company_information_usage_package={'records':rows})
    old=render_compact_chapters(project,{'groups':digest_groups(previous)},outline,{2:''})[2]
    new=render_compact_chapters(project,{'groups':digest_groups(out)},outline,{2:''})[2]
    assert new.startswith(old) and new.count('### 本章小结')==1
    assert new.split('### 本章小结')[1].strip().startswith(out['chapter_summary']['text'])
    assert '[E-123456]' in new.split('### 本章小结')[1]


def test_summary_requirement_comes_from_chapter_configuration():
    for spec in chapters():
        assert chapter_summary_required(spec['chapter'])==(spec['mode']=='research')
    assert chapter_summary_required(2) and chapter_summary_required(6)
    assert not chapter_summary_required(1) and not chapter_summary_required(12)


def test_untrusted_or_changed_body_receipt_cannot_take_summary_only_shortcut():
    from report_agents.company_semantic_review import reusable_body_review
    rows,outline,previous=old_package();b=previous['batches'][0]
    assert reusable_body_review(b,rows,b['findings'],'海岳设备有限公司')
    assert not reusable_body_review(b,rows,b['findings'],'另一家公司')
    changed=deepcopy(b['findings']);changed[0]['text']='不同正文'
    assert not reusable_body_review(b,rows,changed,'海岳设备有限公司')
    bad=deepcopy(b);bad['policy_sha256']='untrusted'
    assert not reusable_body_review(bad,rows,b['findings'],'海岳设备有限公司')


def test_summary_invented_conclusion_is_repaired_without_rewriting_body():
    rows,outline,previous=old_package();calls=[];first=[True]
    def invoke(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1]) if '资料：\n' in prompt else json.loads(prompt.rsplit('资料：',1)[1])
        if '小结补写' in role:return json.dumps({'chapter_summary':{'text':'公司已完成独立审计。','information_ids':['S1']}},ensure_ascii=False)
        if '局部修改' in role:
            assert all(f.get('content_role')=='chapter_summary' for f in data['paragraphs'])
            return json.dumps({'replacements':[{'paragraph_id':data['paragraphs'][0]['finding_id'],'text':rows[0]['text'],'information_ids':['S1']}],'additions':[]},ensure_ascii=False)
        answer=passing(data)
        if first[0]:
            first[0]=False;p=answer['paragraphs'][0];p['status']='block'
            answer['issues']=[{'issue_id':'B1','kind':'unsupported_claim','paragraph_ids':[p['paragraph_id']],
                'source_ids':['S1'],'source_meaning':'正文仅确认登记资本','output_meaning':'小结新增独立审计结论','reason':'正文无此事实'}]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},'海岳设备有限公司',invoke,previous,outline=outline)
    assert out['batches'][0]['accepted'] and len(calls)==4
    assert [f for f in out['findings'] if f.get('content_role')!='chapter_summary']==previous['findings']
    assert out['chapter_summary']['text']==rows[0]['text']


@pytest.mark.parametrize('target',['青禾餐饮有限公司','星川医疗有限公司'])
def test_summary_gap_is_semantically_flagged_and_only_summary_is_repaired(target):
    rows,outline,previous=old_package(target);calls=[];reviewed=[False]
    def invoke(role,prompt):
        calls.append(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1]) if '资料：\n' in prompt else json.loads(prompt.rsplit('资料：',1)[1])
        if '小结补写' in role:
            return json.dumps({'chapter_summary':{'text':rows[0]['text']+'部分事项仍有待补足核验。','information_ids':['S1']}},ensure_ascii=False)
        if '局部修改' in role:
            assert all(f.get('content_role')=='chapter_summary' for f in data['paragraphs'])
            return json.dumps({'chapter_summary':{'text':rows[0]['text'],'information_ids':['S1']}},ensure_ascii=False)
        answer=passing(data)
        if not reviewed[0]:
            reviewed[0]=True;pid=data['paragraphs'][0]['paragraph_id']
            answer['summary_delivery_checks'][0]['contains_evidence_gap']=True
            answer['summary_delivery_checks'][0]['reason']='小结混入未确定事项'
            answer['paragraphs'][0]['status']='block'
            answer['issues']=[{'issue_id':'G1','kind':'missing_deliverable','paragraph_ids':[pid],
                'source_ids':['S1'],'source_meaning':'正文已确定资本，缺口可保留在正文','output_meaning':'小结仍列未确定事项','reason':'仅删除小结中的缺口'}]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},target,invoke,previous,outline=outline)
    assert out['batches'][0]['accepted'] and len(calls)==4
    assert out['findings'][:-1]==previous['findings']
    assert out['chapter_summary']['text']==rows[0]['text']
