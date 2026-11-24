from copy import deepcopy
from types import SimpleNamespace as NS
import json

import pytest

from company_chapter_retrieval import question_searches, balanced_candidates, rank_read_facts
from company_progressive_reading import chapter_fact_candidates, recalled_fact_candidates, plan_reading
from company_reading_reuse import validated_read_facts


def test_original_financial_question_survives_layout_heading_and_has_complementary_queries():
    q={'id':'Q06-1','question':'甲公司各业务的收入、利润、现金流对应什么会计期间、币种、合并范围及调整口径？'}
    searches=dict(question_searches(6,[q]))
    assert searches[q['id']]==q['question']
    assert 'Q06-1#核心业务板块运营进展核验表' not in searches
    assert any('报告分部' in query for query in searches.values())
    assert any('内部抵销' in query for query in searches.values())
    assert not question_searches(6,[{**q,'status':'answered'}],expanded=True)


def test_operating_question_covers_release_research_and_delivery_separately():
    searches=dict(question_searches(6,[{'id':'q','question':'甲公司研发、销售、生产、产能与产品交付处于什么实际状态？'}]))
    assert {'q','q#研发进展','q#产品上线','q#服务交付'}<=set(searches)
    assert '上线' in searches['q#产品上线'] and 'SLA' in searches['q#服务交付']


def test_balanced_candidates_preserve_complementary_queries_and_deduplicate():
    assert balanced_candidates({'financial':['a','b','c','d'],'launch':['d','e','a']},5)==['a','d','b','e','c']


def test_reading_frontier_reserves_complementary_questions_before_deeper_hits():
    from company_progressive_reading import candidate_queue
    ranked={'financial':['a','b','c'],'launch':['d','e','a']}
    assert candidate_queue(ranked,[],dict.fromkeys('abcde'))[0]==['a','d','b','e','c']


def test_planner_sees_more_than_three_ranges_from_the_same_original(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    nodes=[{'node_id':f'N{i}','source_id':'1','source_title':'原文','original_sha256':'hash',
        'start':i*10,'end':i*10+10,'pages':[i+1],'headings':[],'opening_locator':'证据',
        'question_locators':[],'window_ids':[f'W{i}'],'chars':10} for i in range(24)]
    monkeypatch.setattr('company_progressive_reading.directory',lambda *_:nodes)
    monkeypatch.setattr('company_chapter_retrieval.rank_questions',lambda *a,**kw:({'q':[n['node_id'] for n in nodes]},{}))
    seen=[]
    def planner(role,prompt,trace=None,**kwargs):
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);seen.extend(packet['directory'])
        return json.dumps({'read_node_ids':['N18'],'initial_search_required':False,'queries':[],'platform_routes':[]})
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',planner)
    result=plan_reading(f,6)
    assert len(seen)==24 and 'N18' in result['selected_node_ids']
    assert result['selected_chars']<=result['read_char_budget']


def test_reserved_reading_node_is_visible_before_planner_decides(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    nodes=[{'node_id':f'N{i}','source_id':'1','source_title':'原文','original_sha256':'hash',
        'start':i*10,'end':i*10+10,'pages':[i+1],'headings':[],'opening_locator':'证据',
        'question_locators':[],'window_ids':[f'W{i}'],'chars':10} for i in range(100)]
    monkeypatch.setattr('company_progressive_reading.directory',lambda *_:nodes)
    monkeypatch.setattr('company_chapter_retrieval.rank_questions',lambda *a,**kw:({'q':[n['node_id'] for n in nodes]},{}))
    monkeypatch.setattr('company_chapter_retrieval.required_nodes',lambda *a,**kw:(['N80'],[]))
    seen=[]
    def planner(role,prompt,trace=None,**kwargs):
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);seen.extend(packet['directory'])
        return json.dumps({'read_node_ids':[],'initial_search_required':False,'queries':[],'platform_routes':[]})
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',planner)
    result=plan_reading(f,6)
    assert len(seen)==56 and any(n['node_id']=='N80' for n in seen)
    assert 'N80' in result['selected_node_ids']


def native_fixture():
    from test_company_native_reuse import fixture
    prepared,prior=fixture()
    body='原文'*10
    return prepared,prior,{'1':body}


@pytest.mark.parametrize('bad',['target','date','source_hash','window_hash','failed','context','unacknowledged','text','offset'])
def test_fact_recall_rejects_invalid_original_or_receipt(bad):
    prepared,prior,originals=native_fixture();target=prior['target_name'];date=prior['research_as_of']
    st=prior['source_review']['1']
    if bad=='target':target='其他主体'
    if bad=='date':date='old'
    if bad=='source_hash':prepared[0][0]['original_sha256']='changed'
    if bad=='window_hash':prepared[0][0]['windows'][0]['native_window_sha256']='changed'
    if bad=='failed':st['review_decisions'][0]['error']='failure'
    if bad=='context':st['review_decisions'][0]['disposition']='needs_context'
    if bad=='unacknowledged':st['coverage_ranges']=[]
    if bad=='text':prior['facts'][0]['fact']='错误'*5
    if bad=='offset':prior['facts'][0]['original_offset']=-1
    assert not validated_read_facts(prepared,[prior],target,date,originals)


def test_valid_prior_fact_can_enter_unselected_current_range_once():
    prepared,prior,originals=native_fixture();before=deepcopy(prior)
    facts=validated_read_facts(prepared,[prior,prior],prior['target_name'],prior['research_as_of'],originals)
    assert len(facts)==1 and prior==before
    current={'target_name':prior['target_name'],'research_as_of':prior['research_as_of'],
        'facts':[],'source_review':{'1':{'original_sha256':'body','coverage_ranges':[]}}}
    admitted=chapter_fact_candidates(current,6,recalled=facts)
    assert len(admitted)==1 and admitted[0]['chapter']==4
    assert admitted[0]['_prior_admission_coverage']['reviewed_windows']==1
    current['facts']=facts;current['source_review']['1']['coverage_ranges']=[[0,20]]
    assert len(chapter_fact_candidates(current,6,recalled=facts))==1


def test_cross_chapter_fact_reaches_usage_without_new_original_read(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source,FINANCE
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source(text=FINANCE)])
    f.read(5,assess=False)
    prior=deepcopy(f.reads[5]);current=deepcopy(prior)
    current['source_review']['1']['coverage_ranges']=[]
    current['source_review']['1']['review_decisions']=[]
    current['source_review']['1']['reviewed_windows']=0
    current['reading_manifest']=[];current['facts']=[]
    f.reads[6]=current
    f.sections[6]='## 6. 业务运营分析'
    before=sum('公司全量原文分析' in role for role,_ in calls)
    recalled=recalled_fact_candidates(f,6)
    assert any(FINANCE in fact['fact'] for fact in recalled)
    f.write(6)
    assert any(FINANCE in row['text'] for row in f.usages[6]['records'])
    assert sum('公司全量原文分析' in role for role,_ in calls)==before
    assert f.reads[5]==prior


def neighbor_receipt_fixture():
    from company_flow_state import fingerprint
    quote='新品正式上线。';tail='客户服务安排另行披露。';body=quote+tail
    window={'window_id':'W1','start':len(quote),'end':len(body),'quote':tail,
        'native_window_sha256':fingerprint(['body',len(quote),len(body),tail]),
        'context_quote':body,'context_start':0,'context_end':len(body),
        'native_context_sha256':fingerprint(['body',0,len(body),body])}
    doc={'source_id':'1','source_url':'https://example.com/report','source_title':'甲公司年报',
        'original_sha256':'body','evidence_id':'E1','source_kind':'issuer_disclosure',
        'subject_binding':{'issuer_document':True},'model_reading_mode':'native','windows':[window]}
    fact={'statement_id':'F1','source_id':'1','evidence_id':'E1','original_sha256':'body',
        'original_offset':0,'original_end':len(quote),'fact':quote,'chapter':5}
    review={'target_name':'甲公司','research_as_of':'2026-01-01','facts':[fact],
        'source_review':{'1':{'original_sha256':'body'}},'reading_manifest':[]}
    receipt={'input_sha256':'saved-request','error':'','window_manifest':[
        {'evidence_id':'E1','window_id':'W1','start':len(quote),'end':len(body)}],
        'response':{'decisions':[{'evidence_id':'E1','disposition':'accepted'}],
            'window_reviews':[{'evidence_id':'E1','window_id':'W1','disposition':'accepted'}],
            'facts':[{'evidence_id':'E1','window_id':'W1','quote':quote,'chapter':5,
                'scope':'target','relevance':'产品发布进展'}]}}
    return ([doc],{},[]),review,{'1':body},receipt


def test_saved_neighbor_context_fact_survives_replaced_plan_manifest():
    prepared,review,originals,receipt=neighbor_receipt_fixture()
    before=deepcopy((review,receipt))
    facts=validated_read_facts(prepared,[review],'甲公司','2026-01-01',originals,[receipt])
    assert len(facts)==1 and facts[0]['fact']=='新品正式上线。'
    assert facts[0]['original_offset']<prepared[0][0]['windows'][0]['start']
    assert (review,receipt)==before


@pytest.mark.parametrize('bad',['failed','unacknowledged','source_rejected','manifest','window_hash',
    'context_hash','source_hash','changed_subject','quote','target','date'])
def test_saved_proposal_recall_still_requires_native_binding_and_acknowledgement(bad):
    prepared,review,originals,receipt=neighbor_receipt_fixture()
    doc=prepared[0][0];window=doc['windows'][0];target='甲公司';date='2026-01-01'
    if bad=='failed':receipt['error']='failed'
    if bad=='unacknowledged':receipt['response']['window_reviews']=[]
    if bad=='source_rejected':receipt['response']['decisions'][0]['disposition']='no_relevant_claim'
    if bad=='manifest':receipt['window_manifest'][0]['start']+=1
    if bad=='window_hash':window['native_window_sha256']='changed'
    if bad=='context_hash':window['native_context_sha256']='changed'
    if bad=='source_hash':doc['original_sha256']='changed'
    if bad=='changed_subject':doc['subject_binding']['entity_validation']='rejected'
    if bad=='quote':receipt['response']['facts'][0]['quote']='不存在的内容'
    if bad=='target':target='乙公司'
    if bad=='date':date='old'
    assert not validated_read_facts(prepared,[review],target,date,originals,[receipt])
