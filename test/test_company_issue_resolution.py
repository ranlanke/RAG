import json
from copy import deepcopy
import pytest
from report_agents.company_issue_resolution import resolve_source_issues


@pytest.mark.parametrize('company',['甲公司','另一行业公司'])
@pytest.mark.parametrize('status',['resolved','advice','block'])
def test_crosscheck_keeps_real_verdict_and_reuses_original_receipt(company,status):
    rows=[{'information_id':'I1','text':company+'2022年用户超过100万。','evidence_ids':['E1']},
          {'information_id':'I2','text':company+'2024年用户超过100万。','evidence_ids':['E2']}]
    findings=[{'finding_id':'P','text':company+'截至2024年用户超过100万。','information_ids':['I2']}]
    issue={'issue_id':'X','kind':'wrong_time','paragraph_ids':['P'],'source_ids':['I1'],
           'source_meaning':'2022年已达成','output_meaning':'2024年超100万','reason':'年份不同'}
    checks=[{'paragraph_id':'P','status':'pass','source_ids':['I2'],'claim':'2024年超过100万','evidence_check':'另有后期原文'}]
    original=deepcopy(issue);tasks={}
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert data['issue']==original and data['completed_checks']==checks
        return {'issue_id':'X','status':status,'source_ids':['I1','I2'],'reason':'对照两个时点的实际记录判断'}
    retained,records=resolve_source_issues([issue],rows,findings,checks,company,call,tasks,lambda:None)
    assert retained==([issue] if status=='block' else [])
    assert records[0]['original_issue']==original and issue==original
    assert resolve_source_issues([issue],rows,findings,checks,company,
        lambda *a:pytest.fail('paid replay'),tasks,lambda:None)==(retained,records)


def test_crosscheck_no_issues_does_not_call_model():
    assert resolve_source_issues([],[],[],[],'任意公司',lambda *a:pytest.fail('unneeded call'),{},lambda:None)==([],[])


@pytest.mark.parametrize('status',['advice','block'])
@pytest.mark.parametrize('company',['制造企业甲','服务企业乙'])
def test_practical_review_preserves_raw_checks_and_material_blockers(status,company):
    from report_agents.company_issue_resolution import apply_issue_decisions
    from report_agents.company_semantic_review import validate_review
    rows=[{'information_id':'I','text':company+'合作方评价积极，满意度98%。','evidence_ids':['E']}]
    findings=[{'finding_id':'P','text':company+'缺少评价。公司披露合作方评价积极，满意度98%。','information_ids':['I']}]
    issue={'issue_id':'X','kind':'false_gap','paragraph_ids':['P'],'source_ids':['I'],
           'source_meaning':'存在实际评价','output_meaning':'缺少评价','reason':'缺口措辞与后文不一致'}
    review={'paragraphs':[{'paragraph_id':'P','status':'block','source_ids':['I'],'reason':'措辞不一致'}],
            'sources':[{'information_id':'I','status':'block','paragraph_ids':['P'],'reason':'已有评价'}],
            'critical_checks':[{'paragraph_id':'P','kind':'absence','status':'block','source_ids':['I'],
                                'claim':'缺少评价','evidence_check':'后文已含评价及满意度'}],
            'issues':[issue],'advice':[],'summary_delivery_checks':[]}
    def call(role,prompt):
        assert 'practical_core_v1' in prompt
        return {'issue_id':'X','status':status,'source_ids':['I'],
                'reason':'后文已呈现信息，一般措辞建议' if status=='advice' else '重要事实缺失影响主要判断'}
    retained,decisions=resolve_source_issues([issue],rows,findings,review['critical_checks'],company,call,{},lambda:None)
    review['issues']=retained
    apply_issue_decisions(review,decisions)
    validate_review(review,rows,findings)
    assert all(r['status']==status for key in ('sources','paragraphs','critical_checks') for r in review[key])
    assert review['issue_crosschecks'][0]['original_issue']==issue
    if status=='advice':
        assert review['critical_checks'][0]['original_status']=='block'
        assert review['critical_checks'][0]['evidence_check']=='后文已含评价及满意度'


def test_no_decision_cannot_clear_a_block():
    from report_agents.company_issue_resolution import apply_issue_decisions
    review={'issues':[],'sources':[],'critical_checks':[],
            'paragraphs':[{'paragraph_id':'P','status':'block'}]}
    apply_issue_decisions(review,[])
    assert review['paragraphs'][0]['status']=='block'
