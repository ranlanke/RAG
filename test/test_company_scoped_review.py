"""Scope failures must not become invented source bindings or false blockers."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from report_agents.company_scoped_review import (
    ReviewScopeIncomplete, claim_units, validate_scoped_review,
    merge_scoped_checks, scoped_paragraph_review,
)
from report_agents.company_task_batches import checked_call


def sample(target='任意公司'):
    finding={'finding_id':'F','text':target+'获得行业奖项。评级为AA。','information_ids':['S1','S2']}
    batches=[[{'information_id':'S1','text':target+'获得行业奖项。','context':{},'provenance':[]}],
             [{'information_id':'S2','text':'评级为AA。','context':{},'provenance':[]}]]
    return finding,batches,claim_units(finding)


def check(claim, status, sources=(), **extra):
    return {'claim_id':claim['claim_id'],'kind':'direct_fact','status':status,
            'source_ids':list(sources),'reason':'已核对本批具体证据',**extra}


def test_other_batch_absence_does_not_override_supported_claim():
    finding,batches,claims=sample()
    answers=[{'checks':[check(claims[0],'supported',['S1']),check(claims[1],'not_assessed')]},
             {'checks':[check(claims[0],'not_assessed'),check(claims[1],'supported',['S2'])]}]
    result=merge_scoped_checks(finding,claims,batches,answers)
    assert result['paragraphs'][0]['status']=='pass' and not result['issues']
    with pytest.raises(ReviewScopeIncomplete,match='incomplete_batches'):
        merge_scoped_checks(finding,claims,batches,answers[:1])
    answers[1]['checks'][1]=check(claims[1],'not_assessed')
    with pytest.raises(ReviewScopeIncomplete,match='unresolved_claims'):
        merge_scoped_checks(finding,claims,batches,answers)


def test_last_batch_contradiction_survives_other_batch_pass():
    finding,batches,claims=sample()
    answers=[{'checks':[check(c,'supported',['S1']) for c in claims]},
             {'checks':[check(claims[0],'not_assessed'),check(claims[1],'contradicted',['S2'],
                 issue_kind='wrong_time',source_meaning='该评级已失效')]}]
    result=merge_scoped_checks(finding,claims,batches,answers)
    assert result['paragraphs'][0]['status']=='block'
    assert result['issues'][0]['source_ids']==['S2']


@pytest.mark.parametrize('target',['制造企业甲','软件企业乙','物流企业丙'])
def test_scoped_tasks_resume_after_interruption_without_repeating_completed_batch(target):
    finding,batches,claims=sample(target);tasks={};calls=[]
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);ids=[r['information_id'] for r in data['sources']]
        calls.append(ids)
        if len(calls)==2:raise RuntimeError('simulated crash')
        selected=0 if ids==['S1'] else 1
        return {'checks':[check(c,'supported' if i==selected else 'not_assessed',ids if i==selected else [])
                          for i,c in enumerate(claims)]}
    with pytest.raises(RuntimeError,match='simulated crash'):
        scoped_paragraph_review(finding,batches,target,[],[],call,tasks,lambda:None)
    restored=json.loads(json.dumps(tasks,sort_keys=True))
    result=scoped_paragraph_review(finding,batches,target,[],[],call,restored,lambda:None)
    assert calls==[['S1'],['S2'],['S2']] and result['paragraphs'][0]['status']=='pass'
    assert scoped_paragraph_review(finding,batches,target,[],[],
        lambda *a:pytest.fail('repeated successful task'),restored,lambda:None)==result
    # Only the changed source batch is invalidated.
    changed=deepcopy(batches);changed[1][0]['text']='更新后的评级仍为AA。'
    scoped_paragraph_review(finding,changed,target,[],[],call,restored,lambda:None)
    assert calls[-1]==['S2'] and len(calls)==4


def test_cross_batch_calculation_gets_one_complete_resolution_and_is_cached():
    finding,batches,_=sample();finding['text']='两项金额合计为30万元。';claims=claim_units(finding)
    tasks={};calls=[]
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(role)
        if role=='公司局部证据主张核验':return {'checks':[check(claims[0],'not_assessed')]}
        assert len(data['sources'])==2 and len(data['batch_checks'])==2
        return {'checks':[check(claims[0],'supported',['S1','S2'])]}
    result=scoped_paragraph_review(finding,batches,'企业',[],[],call,tasks,lambda:None)
    assert calls==['公司局部证据主张核验']*2+['公司跨批主张复核']
    assert scoped_paragraph_review(finding,batches,'企业',[],[],
        lambda *a:pytest.fail('repeated resolution'),tasks,lambda:None)==result


def test_unbound_or_partial_absence_is_not_repaired_by_inventing_sources():
    _,_,claims=sample();tasks={};calls=[]
    def call(role,prompt):
        calls.append(role)
        return {'checks':[check(claims[0],'contradicted',[],issue_kind='unsupported_claim',source_meaning='本批没看到')]}
    for _ in range(2):
        with pytest.raises(ReviewScopeIncomplete,match='unbound_claim'):
            checked_call('review','partial scope',call,tasks,lambda:None,
                         lambda a:validate_scoped_review(a,claims[:1],{'S1'}),policy='test')
    assert calls==['review']
    with pytest.raises(ReviewScopeIncomplete,match='partial_absence'):
        validate_scoped_review({'checks':[check(claims[0],'contradicted',['S1'],
            issue_kind='unsupported_claim',source_meaning='本批没看到')]},claims[:1],{'S1'})


def test_real_msci_failure_receipt_cannot_be_repaired_into_a_bound_false_verdict():
    proof=json.loads((Path(__file__).parent/'fixtures'/'chapter_review_cross_batch_failure.json').read_text(encoding='utf-8'))
    assert proof['completed_native_audit'][0]['status']=='pass'
    assert 'AA' in proof['fact']
    answer={'issues':proof['failing_issue']};calls=[];tasks={}
    def validate(a):
        if not a['issues'][0]['source_ids']:raise ValueError('unbound_material_issue')
    def call(*args):calls.append(args);return deepcopy(answer)
    for _ in range(2):
        with pytest.raises(ValueError,match='unbound_material_issue'):
            checked_call('review','actual failure',call,tasks,lambda:None,validate,policy='test')
    assert len(calls)==1 and 'repair_answer' not in next(iter(tasks.values()))


def test_claims_preserve_numbers_and_every_nonempty_text_unit():
    text='公司收入1.25亿元；同比增长3.5%。\n另一条事项。'
    claims=claim_units({'finding_id':'F','text':text})
    assert '1.25' in claims[0]['text'] and len({c['claim_id'] for c in claims})==len(claims)
    assert ''.join(c['text'] for c in claims).replace('\n','')==text.replace('\n','')


@pytest.mark.parametrize('role',['公司局部证据主张核验','公司跨批主张复核'])
@pytest.mark.parametrize('number',[2,3,4,5,9])
def test_new_review_roles_use_independent_context_and_final_size_guard(role,number):
    from types import SimpleNamespace
    from report_agents.company_chapter_agent import CompanyChapterAgent
    from report_agents.company_transport import CompanyModelUnavailable
    agent=CompanyChapterAgent(number,SimpleNamespace(target_name='泛化企业'),lambda *a,**k:None)
    seen=[]
    def invoke(role,prompt,trace=None):seen.append(prompt);return '{}'
    agent.invoke(invoke,role,'范围明确的核验任务')
    assert seen[0].startswith('当前是独立核对或局部编辑任务。')
    with pytest.raises(CompanyModelUnavailable):agent.invoke(invoke,role,'x'*60000)
    assert len(seen)==1
