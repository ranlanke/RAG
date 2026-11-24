import json
from copy import deepcopy
import pytest
from report_agents.company_semantic_review import build_package,review_prompt,validate_review,package_errors,merge_missing_critical_checks
from report_agents.company_editorial import VERSION,record_payload,fingerprint


@pytest.fixture(autouse=True)
def strict_review_path(monkeypatch):
    # This suite verifies the stricter claim path; defaults have a separate suite.
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')

def setup():
    rows=[{'information_id':'S1','text':'甲公司注册资本100万元。','context':{},'decision':{'status':'use','chapter':2,'aspect':'工商'},'evidence_ids':['E-ABCDEF'],'provenance':[]},
          {'information_id':'S2','text':'甲公司承诺在承诺期间内不从事竞争业务。','context':{},'decision':{'status':'use','chapter':2,'aspect':'工商'},'evidence_ids':['E-ABCDEF'],'provenance':[]}]
    proposed=[{'finding_id':'p1','text':'甲公司注册资本100万元。','information_ids':['S1']},{'finding_id':'p2','text':'甲公司承诺不从事竞争业务。','information_ids':['S2']}]
    key=fingerprint({'version':VERSION,'target':'甲公司','chapter':2,'aspect':'工商','records':[record_payload(r) for r in rows],'feedback':[]})
    previous={'batches':[{'input_sha256':key,'chapter':2,'aspect':'工商','information_ids':['S1','S2'],'findings':proposed,'attempts':[{'proposal':{'findings':proposed}}]}]}
    return rows,previous

def passing(data):
    return {'paragraphs':[{'paragraph_id':p['paragraph_id'],'status':'pass','source_ids':p['source_ids'],'reason':'核心事实正确'} for p in data['paragraphs']],
            'critical_checks':[{'paragraph_id':p['paragraph_id'],'kind':'direct_fact','claim':p['text'],'evidence_check':'已按所引证据核对','source_ids':p['source_ids'],'status':'pass'} for p in data['paragraphs']],
            'sources':[{'information_id':r['information_id'],'status':'advice','paragraph_ids':[p['paragraph_id'] for p in data['paragraphs'] if r['information_id'] in p['source_ids']],'reason':'一般限定省略不影响核心事实'} for r in data['sources']], 'issues':[],'advice':['一般细节不要求补回'],
            'summary_delivery_checks':[{'paragraph_id':p['paragraph_id'],'contains_evidence_gap':False,'reason':'小结只概括已写内容'} for p in data['paragraphs'] if p.get('content_role')=='chapter_summary']}


def test_gap_without_citations_is_reviewed_against_all_sources_and_cannot_skip_review():
    from report_agents.company_semantic_review import prepare_findings
    rows,_=setup()
    findings=prepare_findings([{'finding_id':'G1','text':'本次材料不足以评价应变能力。',
        'information_ids':[],'content_role':'evidence_gap'}],rows,2,'应变能力')
    data=json.loads(review_prompt(rows,findings,'另一家企业').rsplit('资料：\n',1)[1])
    assert len(data['sources'])==2 and data['paragraphs'][0]['content_role']=='evidence_gap'
    good=passing(data);validate_review(good,rows,findings)
    missing=deepcopy(good);missing['paragraphs']=[]
    with pytest.raises(ValueError,match='incomplete_semantic_paragraphs'):validate_review(missing,rows,findings)
    bad=deepcopy(good);bad['paragraphs'][0]['status']='block';bad['critical_checks'][0]['status']='block'
    bad['issues']=[{'issue_id':'X1','kind':'false_gap','paragraph_ids':['G1'],'source_ids':['S1'],
        'source_meaning':'来源已有注册资本','output_meaning':'正文声称没有注册资本','reason':'已有事实被写成缺口'}]
    validate_review(bad,rows,findings)
    assert bad['issues'] and bad['paragraphs'][0]['status']=='block'
    del findings[0]['content_role']
    with pytest.raises(ValueError,match='unknown_semantic_reference'):validate_review(good,rows,findings)

def test_semantic_acceptance_uses_ids_not_literal_spans_and_replays_without_calls():
    rows,previous=setup();calls=[]
    def invoke(role,prompt):calls.append(role);return json.dumps(passing(json.loads(prompt.rsplit('资料：\n',1)[1])),ensure_ascii=False)
    out=build_package({'records':rows},'甲公司',invoke,previous)
    assert out['batches'][0]['accepted'] and len(calls)==1 and not package_errors(out,rows,'甲公司')
    assert build_package({'records':rows},'甲公司',lambda *a:pytest.fail('cache missed'),out)['batches'][0]['accepted']
    changed=deepcopy(out);changed['findings'][0]['text']='乙公司注册资本200万元。'
    assert package_errors(changed,rows,'甲公司')


def test_pending_repair_can_resume_across_format_policy_without_migrating_acceptance():
    from report_agents.company_semantic_review import pending_repair_review,prepare_findings,digest,VERSION as REVIEW_VERSION
    rows,_=setup()
    findings=prepare_findings([{'finding_id':'P','text':'甲公司注册资本200万元。','information_ids':['S1']}],rows,2,'工商')
    data=json.loads(review_prompt(rows,findings,'甲公司').rsplit('资料：\n',1)[1]);review=passing(data)
    review['paragraphs'][0]['status']='block'
    review['issues']=[{'issue_id':'B','kind':'wrong_amount','paragraph_ids':['P'],'source_ids':['S1'],
                       'source_meaning':'资本100万元','output_meaning':'资本200万元','reason':'金额错'}]
    saved=deepcopy(findings);saved[0]['evidence_ids']=[]
    records=[record_payload(r) for r in rows]
    receipt={'review_mode':REVIEW_VERSION,'findings':saved,'semantic_review':review,'policy_sha256':'old-format',
             'records_sha256':digest(records),'input_sha256':digest([REVIEW_VERSION,'old-format','甲公司',records,saved])}
    receipt['semantic_history']=[{'input_sha256':receipt['input_sha256'],'review':deepcopy(review)}]
    assert pending_repair_review([receipt],rows,findings,'甲公司')==review
    modified=deepcopy(findings);modified[0]['text']='甲公司注册资本100万元。'
    assert pending_repair_review([receipt],rows,modified,'甲公司') is None
    changed=deepcopy(rows);changed[0]['text']='新的原文'
    assert pending_repair_review([receipt],changed,findings,'甲公司') is None
    assert pending_repair_review([receipt],rows,findings,'乙公司') is None
    stale=deepcopy(receipt);stale['semantic_history'][0]['input_sha256']='review-of-previous-body'
    assert pending_repair_review([stale],rows,findings,'甲公司') is None
    receipt['semantic_review']['issues']=[]
    assert pending_repair_review([receipt],rows,findings,'甲公司') is None


def test_missing_critical_check_is_repaired_without_repeating_full_review():
    rows,previous=setup();calls=[]
    def invoke(role,prompt):
        calls.append(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司核心事实审稿缺项补核':
            paragraph=data['missing_paragraphs'][0]
            return json.dumps({'critical_checks':[{'paragraph_id':paragraph['paragraph_id'],
                'kind':'direct_fact','claim':paragraph['text'],'evidence_check':'已核对对应原文',
                'source_ids':paragraph['source_ids'],'status':'pass'}]},ensure_ascii=False)
        answer=passing(data);answer['critical_checks'].pop()
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},'甲公司',invoke,previous)
    assert out['batches'][0]['accepted']
    assert calls==['公司核心事实集中审稿','公司核心事实审稿缺项补核']
    broken=deepcopy(out['batches'][0]['semantic_review']);broken['critical_checks'].pop()
    with pytest.raises(ValueError,match='incomplete_targeted_critical_checks'):
        merge_missing_critical_checks(broken,{'critical_checks':[]},rows,out['findings'])

def test_block_requires_material_evidence_and_all_ids_must_be_reviewed():
    rows,_=setup();findings=[{'finding_id':'P1','text':'甲公司资本100万元','information_ids':['S1','S2']}]
    data=json.loads(review_prompt(rows,findings,'甲公司').rsplit('资料：\n',1)[1]);r=passing(data)
    r['paragraphs'][0]['status']='block'
    with pytest.raises(ValueError,match='block_without_material_issue'):validate_review(r,rows,findings)
    r=passing(data);r['paragraphs'][0]['source_ids']=['missing']
    with pytest.raises(ValueError,match='unknown_semantic_reference'):validate_review(r,rows,findings)
    r=passing(data);r['sources'].pop()
    with pytest.raises(ValueError,match='incomplete_semantic_sources'):validate_review(r,rows,findings)

def test_material_error_repairs_only_affected_paragraph_and_reviews_only_affected_scope():
    rows,previous=setup();calls=[];first=[True];badid=[None];original_other=[None]
    def invoke(role,prompt):
        calls.append(role)
        if '局部修改' in role:
            d=json.loads(prompt.rsplit('资料：',1)[1]);assert len(d['paragraphs'])==1
            return json.dumps({'replacements':[{'paragraph_id':badid[0],'text':'甲公司注册资本100万元。','information_ids':['S1']}],'additions':[]},ensure_ascii=False)
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);r=passing(data)
        if first[0]:
            first[0]=False;badid[0]=data['paragraphs'][0]['paragraph_id'];original_other[0]=data['paragraphs'][1]['text']
            r['paragraphs'][0]['status']='block';r['issues']=[{'issue_id':'B1','kind':'wrong_amount','paragraph_ids':[badid[0]],'source_ids':['S1'],'source_meaning':'资本100万元','output_meaning':'资本错误','reason':'金额错误影响判断'}]
        else:assert len(data['paragraphs'])==1 and data['paragraphs'][0]['paragraph_id']==badid[0] and len(data['sources'])==1
        return json.dumps(r,ensure_ascii=False)
    out=build_package({'records':rows},'甲公司',invoke,previous)
    assert out['batches'][0]['accepted'] and len(calls)==3
    assert out['findings'][1]['text']==original_other[0]
    assert sum(bool(h.get('repair')) for h in out['batches'][0]['semantic_history'])==1

def test_utf8_budget_is_reserved_before_request(monkeypatch,tmp_path):
    from company_flow_state import FlowJournal,CompanyFlowLimit
    monkeypatch.setenv('COMPANY_FLOW_RUN_ESTIMATED_YUAN','0.000001')
    j=FlowJournal(object(),1,{})
    with pytest.raises(CompanyFlowLimit,match='estimated_cost'):j.invoke(lambda *a:pytest.fail('request must not be sent'),'test','测试')
    assert j.data['metrics']['model_calls']==0


def test_provider_rates_enforce_budget_before_request(monkeypatch):
    from company_flow_state import FlowJournal,CompanyFlowLimit
    monkeypatch.setenv('COMPANY_FLOW_INPUT_YUAN_PER_MILLION','2')
    monkeypatch.setenv('COMPANY_FLOW_OUTPUT_YUAN_PER_MILLION','8')
    monkeypatch.setenv('COMPANY_RELAY_MAX_OUTPUT_TOKENS','2')
    monkeypatch.setenv('COMPANY_FLOW_RUN_ESTIMATED_YUAN','0.00002')
    j=FlowJournal(object(),1,{})
    with pytest.raises(CompanyFlowLimit,match='estimated_cost'):
        j.invoke(lambda *a:pytest.fail('must not spend beyond provider budget'),'test','测试')
    assert j.data['metrics']['model_calls']==0 and j.run_cost_reserved_yuan==0


@pytest.mark.parametrize('rate',['-1','nan','inf'])
def test_invalid_provider_rate_cannot_disable_budget(monkeypatch,rate):
    from company_flow_state import FlowJournal
    monkeypatch.setenv('COMPANY_FLOW_INPUT_YUAN_PER_MILLION',rate)
    j=FlowJournal(object(),1,{})
    with pytest.raises(ValueError,match='invalid_company_model_cost_rates'):
        j.invoke(lambda *a:pytest.fail('invalid price must not invoke model'),'test','测试')


def test_semantic_paragraphs_render_in_existing_aspects_without_dropping_text():
    from types import SimpleNamespace
    from company_chapters import outline
    from report_agents.company_editorial import digest_groups
    from report_agents.company_corpus_writer import render_compact_chapters
    rows,previous=setup();rows[0]['decision']['aspect']=outline()[1][1][0]
    project=SimpleNamespace(target_name='甲公司',company_information_usage_package={'records':rows})
    finding={'finding_id':'P1','text':'甲公司注册资本100万元。','information_ids':['S1'],'evidence_ids':['E-ABCDEF'],'statement_ids':[],'chapter':2,'aspect':'核心事实语义审稿'}
    groups=digest_groups({'findings':[finding]})
    text=render_compact_chapters(project,{'groups':groups},outline(),{2:'old draft'})[2]
    assert finding['text'] in text and '### '+outline()[1][1][0] in text


def test_chapter_owner_is_selected_from_records_not_first_agent():
    rows,previous=setup();called=[]
    class Agent:
        def __init__(self,n):self.n=n
        def invoke(self,invoke,role,prompt):
            called.append(self.n)
            return json.dumps(passing(json.loads(prompt.rsplit('资料：\n',1)[1])),ensure_ascii=False)
    out=build_package({'records':rows},'甲公司',lambda *a:None,previous,chapter_agents={1:Agent(1),2:Agent(2)})
    assert out['batches'][0]['accepted'] and called==[2] and out['batches'][0]['chapter']==2


def test_two_repair_limit_survives_resume_and_advice_never_starts_repair():
    rows,previous=setup()
    def invoke(role,prompt):
        if '局部修改' in role:
            data=json.loads(prompt.rsplit('资料：',1)[1]);f=data['paragraphs'][0]
            return json.dumps({'replacements':[{'paragraph_id':f['finding_id'],'text':f['text'],'information_ids':f['information_ids']}],'additions':[]},ensure_ascii=False)
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);answer=passing(data);pid=data['paragraphs'][0]['paragraph_id'];answer['paragraphs'][0]['status']='block'
        answer['issues']=[{'issue_id':'B1','kind':'wrong_amount','paragraph_ids':[pid],'source_ids':['S1'],'source_meaning':'100万元','output_meaning':'错误金额','reason':'实质金额错误'}]
        return json.dumps(answer,ensure_ascii=False)
    first=build_package({'records':rows},'甲公司',invoke,previous)
    assert not first['batches'][0]['accepted'] and sum(bool(h.get('repair')) for h in first['batches'][0]['semantic_history'])==2
    calls=[]
    again=build_package({'records':rows},'甲公司',lambda *a:calls.append(a) or pytest.fail('repair budget reset'),first)
    assert not again['batches'][0]['accepted'] and not calls


@pytest.mark.parametrize('practical',[False,True])
def test_false_gap_local_revision_resumes_without_reauditing_unchanged_chapter(monkeypatch,practical):
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','practical_core' if practical else 'strict_core')
    local_role='公司整章实用验收' if practical else '公司核心事实局部复审'
    from company_flow_state import CompanyFlowLimit
    rows,previous=setup();saved=[];seen=[]
    def first(role,prompt):
        seen.append(role)
        if role==local_role and (not practical or '据披露，甲公司注册资本' in prompt):raise CompanyFlowLimit('test_pause_after_edit')
        if role=='公司核心事实局部修改':
            data=json.loads(prompt.rsplit('资料：',1)[1]);p=data['paragraphs'][0]
            return json.dumps({'replacements':[{'paragraph_id':p['finding_id'],'text':'据披露，甲公司注册资本100万元。','information_ids':['S1']}],'additions':[]})
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':
            return json.dumps({'sources':[{'information_id':r['information_id'],'status':'pass',
                'paragraph_ids':[],'reason':'已核原文'} for r in data['sources']],'issues':[]})
        if practical:data['sources']=[r for r in rows if r['information_id'] in {s['information_id'] for s in data['completed_source_audits']}]
        answer=passing(data);pid=data['paragraphs'][0]['paragraph_id']
        if practical:answer['source_issue_decisions']=[]
        answer['paragraphs'][0]['status']='block'
        answer['issues']=[{'issue_id':'G','kind':'false_gap','paragraph_ids':[pid],'source_ids':['S1'],
                           'source_meaning':'原文披露资本','output_meaning':'正文表述有误','reason':'必须更正',
                           'material_impact':'key_evidence_omission','impact_reason':'工商核验主要问题要求的注册资本漏用，主要问题未回答'}]
        return json.dumps(answer)
    with pytest.raises(CompanyFlowLimit,match='test_pause_after_edit'):
        build_package({'records':rows},'甲公司',first,previous,receipt_callback=lambda b:saved.append(deepcopy(b)))
    pending=json.loads(json.dumps(saved[-1],sort_keys=True))
    assert pending['pending_local_review'] and '据披露' in pending['findings'][0]['text']
    resumed=[]
    def finish(role,prompt):
        resumed.append(role);assert role==local_role
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);assert len(data['paragraphs'])==1
        if practical:data['sources']=[r for r in rows if r['information_id'] in {s['information_id'] for s in data['completed_source_audits']}]
        answer=passing(data)
        if practical:answer['source_issue_decisions']=[{'issue_id':i['issue_id'],'status':'resolved','reason':'更正后核心事实符合已核原文'} for i in data['source_issues']]
        return json.dumps(answer)
    result=build_package({'records':rows},'甲公司',finish,{'batches':[pending]})
    assert result['batches'][0]['accepted'] and resumed==[local_role], (resumed,result['batches'][0]['semantic_history'][-2:])
    assert 'pending_local_review' not in result['batches'][0]


def test_unchanged_summary_retains_document_reference_after_local_body_reference_changes():
    from report_agents.company_semantic_review import apply_semantic_repair
    from report_agents.company_chapter_synthesis import body_key
    rows,_=setup();rows[1]={**rows[0],'information_id':'S2'}
    findings=[{'finding_id':'F','aspect':'工商','text':'资本100万元。','information_ids':['S1']},
              {'finding_id':'S','text':'资本100万元。','information_ids':['S1'],'content_role':'chapter_summary'}]
    findings[-1]['summary_of_sha256']=body_key(findings[:-1])
    repair={'replacements':[{'paragraph_id':'F','text':'据披露，资本100万元。','information_ids':['S2']}]}
    synthesis={'contract':{'sections':['工商'],'chapter_summary_required':True}}
    result,receipt,_=apply_semantic_repair(repair,findings,rows,5,'工商',{'F'},synthesis)
    assert result[-1]['information_ids']==['S2'] and result[-1]['text']==findings[-1]['text']
    assert receipt['reference_normalizations'][0]['source_reference_scope']=='document'
    rows[1]['evidence_ids']=['E-other']
    with pytest.raises(ValueError,match='unbound_chapter_summary'):
        apply_semantic_repair(repair,findings,rows,5,'工商',{'F'},synthesis)
