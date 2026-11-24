import json
import pytest
from report_agents.company_practical_review import practical_review


@pytest.mark.parametrize('status',['block','advice','resolved'])
@pytest.mark.parametrize('company',['制造企业甲','服务企业乙'])
def test_overall_acceptance_preserves_audits_and_explicit_verdicts(company,status):
    rows=[{'information_id':'I','text':company+'实际已有合作方评价。','context':{'subject_basis':'母公司现金流量表'},'evidence_ids':['E']}]
    findings=[{'finding_id':'P','text':company+'缺少评价，但合作方肯定其服务。','information_ids':['I']}]
    source={'information_id':'I','status':'block','paragraph_ids':['P'],'reason':'已有评价，缺口措辞不准确'}
    issue={'issue_id':'X','kind':'false_gap','paragraph_ids':['P'],'source_ids':['I'],
           'source_meaning':'已有评价','output_meaning':'缺少评价','reason':'缺口措辞'}
    calls=[];tasks={}
    def call(role,prompt):
        calls.append(role)
        assert '不做逐句细审' in prompt
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert data['issue_source_contexts']['I']['subject_basis']=='母公司现金流量表'
        assert data['completed_source_audits']==[{'information_id':'I','status':'block'}]
        verdict='block' if status=='block' else 'advice'
        assert company in json.dumps(data['issue_originals'],ensure_ascii=False)
        answer={'paragraphs':[{'paragraph_id':'P','status':verdict,'source_ids':['I'],'reason':'整体判断'}],
                'critical_checks':[{'paragraph_id':'P','status':verdict,'source_ids':['I'],'kind':'direct_fact',
                                    'claim':'合作方评价','evidence_check':'已核原文记录实际评价'}],
                'source_issue_decisions':[{'issue_id':'SRC-0','status':status,'reason':'整体实质性判断',
                    'material_impact':'key_evidence_omission','impact_reason':'主要问题所需的评价证据整段漏用，导致主要问题未回答'}],
                'issues':[],'advice':[],'summary_delivery_checks':[]}
        if status=='advice':
            answer['critical_checks']=[]
            answer['advice']=[{'reason':'一般文字建议'}]
        return answer
    result=practical_review(rows,findings,company,{},[source],[issue],call,tasks,lambda:None)
    assert bool(result['issues'])==(status=='block')
    assert result['sources'][0]['status']==('block' if status=='block' else 'advice')
    assert result['issue_crosschecks'][0]['original_issue']['reason']==issue['reason']
    if status=='advice':
        assert result['critical_checks'][0]['check_scope']=='paragraph_overall'
        assert result['critical_checks'][0]['status']=='advice'
        assert result['advice']==['一般文字建议']
    assert practical_review(rows,findings,company,{},[source],[issue],lambda *a:pytest.fail('repeated paid audit'),tasks,lambda:None)==result
    assert calls==['公司整章实用验收']


@pytest.mark.parametrize('origin',['source','overall'])
@pytest.mark.parametrize('source_text,draft_text,kind',[
    ('股份支付计入所有者权益的金额262,849,226.23元。',
     '股份支付间接体现研发团队稳定性或激励力度。','unsupported_claim'),
    ('本公司的记账本位币为人民币。','记账本位币人民币，全公司统一。','misleading_scope'),
])
def test_minor_details_never_trigger_repair_or_additional_calls(origin,source_text,draft_text,kind):
    rows=[{'information_id':'I','text':source_text,'context':{},'evidence_ids':['E']}]
    findings=[{'finding_id':'P','text':draft_text,'information_ids':['I']}]
    issue={'issue_id':'X','kind':kind,'paragraph_ids':['P'],'source_ids':['I'],
           'source_meaning':source_text,'output_meaning':draft_text,'reason':'措辞范围或轻微推断缺少直接依据'}
    source={'information_id':'I','status':'block' if origin=='source' else 'pass',
            'paragraph_ids':['P'],'reason':'原文核验记录'}
    calls=[];tasks={}
    def call(role,prompt):
        calls.append(role)
        assert '不要求修改，不为其追加核对' in prompt
        assert '研发团队稳定性' in prompt and '全公司统一' in prompt
        return {'paragraphs':[{'paragraph_id':'P','status':'block','source_ids':['I'],'reason':'局部细节意见'}],
            'critical_checks':[{'paragraph_id':'P','kind':'conclusion','claim':draft_text,
                'evidence_check':'局部表述不严密','source_ids':['I'],'status':'block'}],
            'source_issue_decisions':[{'issue_id':'SRC-0','status':'block','reason':issue['reason']}]
                if origin=='source' else [],
            'issues':[{**issue,'material_impact':'minor_detail','impact_reason':''}] if origin=='overall' else [],
            'advice':[],'summary_delivery_checks':[]}
    source_issues=[issue] if origin=='source' else []
    result=practical_review(rows,findings,'任意公司',{},[source],source_issues,call,tasks,lambda:None)
    assert result['issues']==[]
    assert all(r['status']!='block' for key in ('paragraphs','sources','critical_checks') for r in result[key])
    assert result['issue_crosschecks'][0]['decision']['status']=='advice'
    def forbidden(*args):pytest.fail('minor detail caused another paid call')
    assert practical_review(rows,findings,'任意公司',{},[source],source_issues,forbidden,tasks,lambda:None)==result
    assert calls==['公司整章实用验收']


@pytest.mark.parametrize('kind,impact',[
    ('wrong_subject',{}),('wrong_amount',{}),('wrong_time',{}),('negation_reversal',{}),
    ('unsupported_claim',{'material_impact':'core_conclusion','impact_reason':'主要结论把亏损写成盈利'}),
    ('false_gap',{'material_impact':'key_evidence_omission','impact_reason':'整段经营计划漏用导致主要问题未回答'}),
])
def test_material_errors_remain_blockers(kind,impact):
    from report_agents.company_practical_review import materiality_decision
    decision={'status':'block','reason':'实质错误',**impact}
    assert materiality_decision({'kind':kind},decision)==decision


def test_large_context_reuses_complete_read_facts_and_still_blocks_wrong_amount(monkeypatch):
    from copy import deepcopy
    from report_agents.company_bounded_editorial import bounded_review
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','practical_core')
    rows=[{'information_id':iid,'text':text,'context':{'context_quote':'相邻完整原文'*12000,
        'period':'2026年上半年','currency':'人民币','subject_basis':'目标公司'},'evidence_ids':[]}
        for iid,text in [('I-a','公司营业收入100万元。'),('I-b','公司提供办公软件服务。')]]
    findings=[{'finding_id':'P','text':'公司营业收入200万元，提供办公软件服务。',
        'information_ids':['I-a','I-b']}]
    original=deepcopy(rows);calls=[];tasks={}
    issue={'issue_id':'X','kind':'wrong_amount','paragraph_ids':['P'],'source_ids':['I-a'],
        'source_meaning':'营业收入100万元','output_meaning':'营业收入200万元','reason':'关键金额错误'}
    def call(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert len(prompt.encode())<=57000
        if role=='公司章节原文保留审稿':
            assert data['source_audit_scope']=='verified_read_facts'
            assert data['body'][0]['text']==findings[0]['text']
            assert {r['information_id'] for r in data['sources']}=={'I-a','I-b'}
            assert '相邻完整原文'*50 not in prompt
            for row in data['sources']:
                context=data['contexts'][row['context_id']]
                assert context['period']=='2026年上半年' and context['currency']=='人民币'
                text=row['text']
                if isinstance(text,dict):text=data['texts'][text['text_ref']]
                assert text==next(r['text'] for r in rows if r['information_id']==row['information_id'])
            return {'sources':[{'information_id':r['information_id'],'status':'block' if r['information_id']=='I-a' else 'pass',
                'paragraph_ids':['P'],'reason':'核对已读原文事实'} for r in data['sources']], 'issues':[issue]}
        assert role=='公司整章实用验收'
        assert all(r['source_audit_scope']=='verified_read_facts' for r in data['completed_source_audits'])
        return {'paragraphs':[{'paragraph_id':'P','source_ids':['I-a','I-b'],'status':'block','reason':'关键金额错误'}],
            'critical_checks':[{'paragraph_id':'P','kind':'direct_fact','claim':'营业收入','evidence_check':'100万元被写成200万元',
                'source_ids':['I-a'],'status':'block'}],
            'source_issue_decisions':[{'issue_id':'SRC-0','status':'block','reason':'关键金额错误'}],
            'issues':[],'advice':[],'summary_delivery_checks':[]}
    result=bounded_review(rows,findings,'任意公司',call,tasks,lambda:None,contract={'chapter':6})
    assert result['issues'][0]['kind']=='wrong_amount' and result['paragraphs'][0]['status']=='block'
    assert rows==original
    assert all(r['source_audit_scope']=='verified_read_facts' for r in result['sources'])
    assert len(calls)==2
    assert bounded_review(rows,findings,'任意公司',lambda *a:pytest.fail('repeated paid review'),tasks,
        lambda:None,contract={'chapter':6})==result
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    with pytest.raises(ValueError,match='native_task_record_exceeds_input_budget'):
        bounded_review(rows,findings,'任意公司',lambda *a:pytest.fail('strict path silently changed'),{},
            lambda:None,contract={'chapter':6})


def test_large_overall_packet_compacts_pass_statuses_without_dropping_objections():
    rows=[{'information_id':'I-'+str(i),'text':'原文明确收入100万元。','context':{},'evidence_ids':[]} for i in range(80)]
    findings=[{'finding_id':'P','text':'正文事实。'*3000,'information_ids':[r['information_id'] for r in rows]}]
    sources=[{'information_id':r['information_id'],'status':'block' if i==0 else 'pass',
        'paragraph_ids':['P'],'reason':'已核对','source_audit_scope':'verified_read_facts'} for i,r in enumerate(rows)]
    issue={'issue_id':'X','kind':'wrong_amount','paragraph_ids':['P'],'source_ids':['I-0'],
        'source_meaning':'100万元','output_meaning':'200万元','reason':'关键金额错误'}
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert len(prompt.encode())<=54000
        assert data['source_audit_summary']=={'total':80,'statuses':{'block':1,'pass':79}}
        assert [r['information_id'] for r in data['completed_source_audits']]==['I-0']
        assert data['source_issues'][0]['source_meaning']==issue['source_meaning']
        assert data['paragraphs'][0]['text']==findings[0]['text']
        return {'paragraphs':[{'paragraph_id':'P','status':'block','source_ids':['I-0'],'reason':'关键金额错误'}],
            'critical_checks':[{'paragraph_id':'P','status':'block','source_ids':['I-0'],'kind':'direct_fact',
                'claim':'收入','evidence_check':'关键金额不符'}],
            'source_issue_decisions':[{'issue_id':'SRC-0','status':'block','reason':'关键金额不符'}],
            'issues':[],'advice':[],'summary_delivery_checks':[]}
    result=practical_review(rows,findings,'任意公司',{},sources,[issue],call,{},lambda:None)
    assert len(result['sources'])==80 and result['issues'][0]['kind']=='wrong_amount'
    assert result['paragraphs'][0]['status']=='block'


def test_overall_empty_citations_use_explicit_paragraph_scope_without_paid_repair():
    rows=[{'information_id':'I','text':'原文收入100万元。','context':{},'evidence_ids':[]}]
    findings=[{'finding_id':'P','text':'收入100万元。','information_ids':['I']}]
    sources=[{'information_id':'I','status':'pass','paragraph_ids':['P'],'reason':'已核对'}]
    tasks={};calls=[]
    def call(role,prompt):
        calls.append(role)
        return {'paragraphs':[{'paragraph_id':'P','status':'pass','source_ids':[],'reason':'与已核原文一致'}],
            'critical_checks':[{'paragraph_id':'P','kind':'direct_fact','claim':'收入100万元',
                'evidence_check':'与已核原文一致','source_ids':[],'status':'pass'}],
            'source_issue_decisions':[],'issues':[],'advice':[],'summary_delivery_checks':[]}
    result=practical_review(rows,findings,'任意公司',{},sources,[],call,tasks,lambda:None)
    for field in ('paragraphs','critical_checks'):
        assert result[field][0]['source_ids']==['I']
        assert result[field][0]['source_reference_scope']=='reviewed_paragraph'
        assert result[field][0]['status']=='pass'
    assert next(iter(tasks.values()))['answer']['paragraphs'][0]['source_ids']==[]
    assert calls==['公司整章实用验收']


def test_native_audit_reuses_unchanged_prose_after_citation_enrichment(monkeypatch):
    from copy import deepcopy
    from report_agents.company_bounded_editorial import bounded_review
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','practical_core')
    rows=[{'information_id':i,'text':'公司收入100万元。','context':{},'evidence_ids':[]} for i in ['I-a','I-b']]
    baseline=[{'finding_id':'P','text':'公司收入100万元。','information_ids':['I-a']}]
    tasks={};calls=[]
    def call(role,prompt):
        calls.append(role)
        if role=='公司章节原文保留审稿':
            return {'sources':[{'information_id':r['information_id'],'paragraph_ids':['P'],'status':'pass','reason':'核对一致'} for r in rows],'issues':[]}
        return {'paragraphs':[{'paragraph_id':'P','source_ids':['I-a'],'status':'pass','reason':'核对一致'}],
            'critical_checks':[],'source_issue_decisions':[],'issues':[],'advice':[],'summary_delivery_checks':[]}
    bounded_review(rows,baseline,'任意公司',call,tasks,lambda:None,contract={'chapter':6})
    enriched=deepcopy(baseline);enriched[0]['information_ids'].append('I-b')
    bounded_review(rows,enriched,'任意公司',call,tasks,lambda:None,contract={'chapter':6},audit_baseline=baseline)
    assert calls.count('公司章节原文保留审稿')==1
    enriched[0]['text']='公司收入200万元。'
    bounded_review(rows,enriched,'任意公司',call,tasks,lambda:None,contract={'chapter':6},audit_baseline=baseline)
    assert calls.count('公司章节原文保留审稿')==2
