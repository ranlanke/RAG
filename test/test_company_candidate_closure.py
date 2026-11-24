from types import SimpleNamespace as NS
from copy import deepcopy
import pytest

from company_progressive_reading import candidate_queue,candidate_receipts,chapter_fact_candidates
from report_agents.company_corpus import research_context


def test_frontier_third_hit_is_read_unless_explicitly_excluded():
    ranked={'q':['a','b','c','d']}
    queue,excluded=candidate_queue(ranked,[{'node_id':'b','status':'exclude','basis':'locator',
        'reason':'采购金额，不是客户评价'}],dict.fromkeys('abcd'))
    assert queue==['a','c'] and set(excluded)=={'b'}
    assert candidate_queue(ranked,[{'node_id':'c','status':'exclude'}],dict.fromkeys('abcd'))[0]==['a','b','c']


def test_selected_is_not_read_and_old_source_receipt_is_invalid():
    plan={'candidate_ledger':{'a':{'source_id':'1','original_sha256':'new',
        'window_ids':['W1','W2'],'status':'selected'}}}
    review={'source_review':{'1':{'original_sha256':'new','review_decisions':[
        {'window_ids':['W1']},{'window_ids':['W2'],'error':'failed'}]}}}
    assert candidate_receipts(plan,review)['a']['status']=='selected'
    review['source_review']['1']['review_decisions'][1].pop('error')
    assert candidate_receipts(plan,review)['a']['status']=='read'
    review['source_review']['1']['original_sha256']='old'
    assert candidate_receipts(plan,review)['a']['status']=='selected'


def test_reading_cache_context_is_chapter_specific():
    project=NS(company_research_intake={},company_current_reading_chapter=5)
    five=research_context(project)
    project.company_current_reading_chapter=4
    assert five!=research_context(project)
    assert five['current_chapter']['number']==5


def test_other_chapter_fact_in_read_range_is_admitted_without_whole_project_fanout():
    def fact(owner,start,digest='hash'):
        return {'chapter':owner,'source_id':'1','original_offset':start,'original_end':start+10,
            'original_sha256':digest}
    local= fact(5,300); reusable=fact(6,110); unrelated=fact(4,300); stale=fact(6,110,'old')
    review={'facts':[local,reusable,unrelated,stale],'source_review':{'1':{
        'original_sha256':'hash','coverage_ranges':[[100,150]]}}}
    assert chapter_fact_candidates(review,5)==[reusable]


def test_other_chapter_fact_reaches_current_relevance_judgment(tmp_path,monkeypatch):
    import json
    from test_company_chapter_flow import flow,source,FINANCE,TEAM
    f,calls,_=flow(tmp_path,monkeypatch,[source(text=FINANCE+'\n'+TEAM)])
    f.read(6)
    f.write(6)
    payloads=[json.loads(prompt.rsplit('信息：\n',1)[1]) for role,prompt in calls
              if role.split('｜')[0]=='公司信息逐项用途核对']
    assert any(TEAM in row['text'] for rows in payloads for row in rows)
    assert any(TEAM in row['text'] and row['decision']['status']=='defer' for row in f.usages[6]['records'])


@pytest.mark.parametrize('number',[2,3,4,5,6,7,8,9,10])
def test_plan_change_preserves_prior_adopted_native_fact(number):
    fact={'chapter':6,'source_id':'s','original_sha256':'hash','original_offset':10,
          'original_end':14,'fact':'原文证据'}
    review={'target_name':'任意企业','research_as_of':'2026-01-01','facts':[fact],
            'source_review':{'s':{'original_sha256':'hash','coverage_ranges':[]}}}
    usage={'target_name':review['target_name'],'current_chapter':number,
           'review_scope':{'as_of_date':review['research_as_of']},'records':[{
               'decision':{'status':'use'},'context':{'original_sha256':'hash',
               'source_reading_coverage':{'original_sha256':'hash','reviewed_windows':1,
                   'full_coverage_receipt_sha256':'receipt'}},
               'provenance':[{'source_id':'s','original_offset':10,'native_quote':'原文证据'}]}]}
    admitted=chapter_fact_candidates(review,number,usage)
    assert len(admitted)==1
    assert admitted[0]['fact']==fact['fact']
    assert admitted[0]['_prior_admission_coverage']==usage['records'][0]['context']['source_reading_coverage']
    # The next writing pass carries the receipt instead of the empty new plan.
    next_usage=deepcopy(usage)
    next_usage['records'][0]['context']['source_reading_coverage']=admitted[0]['_prior_admission_coverage']
    assert chapter_fact_candidates(review,number,next_usage)==admitted
    assert '_prior_admission_coverage' not in fact
    nested={**fact,'original_offset':12,'fact':'证据'}
    expanded={**review,'facts':[fact,nested]}
    assert chapter_fact_candidates(expanded,number,usage)==admitted
    # A nested fact can still enter through an explicit relevant recall; prior
    # adoption alone must not turn a saved draft into a growing new input.
    assert chapter_fact_candidates(expanded,number,usage,recalled=[nested])==admitted+[nested]
    wide=deepcopy(usage)
    wide['records'][0]['text']='原文证据'
    wide['records'][0]['provenance'][0]['native_quote']='此前语境原文证据后续说明'
    assert chapter_fact_candidates(expanded,number,wide)==admitted
    for field,value in [('target_name','其他企业'),('current_chapter',99),('review_scope',{'as_of_date':'old'})]:
        stale={**usage,field:value}
        assert chapter_fact_candidates(review,number,stale)==[]
    for path,value in [('hash','changed'),('offset',100),('quote','其他文字'),('unread',0)]:
        bad=deepcopy(usage)
        if path=='hash':bad['records'][0]['context']['original_sha256']=value
        if path=='offset':bad['records'][0]['provenance'][0]['original_offset']=value
        if path=='quote':bad['records'][0]['provenance'][0]['native_quote']=value
        if path=='unread':bad['records'][0]['context']['source_reading_coverage']['reviewed_windows']=value
        assert chapter_fact_candidates(review,number,bad)==[]


def test_compact_read_receipt_replay_is_idempotent_and_keeps_gaps():
    from company_progressive_reading import compact_reading_coverage
    raw={'original_sha256':'native','reviewed_windows':4,'unread_ranges':[[10,20]],
         'structural_gaps':[{'field':'date'}]}
    compact=compact_reading_coverage(raw)
    assert compact['unread_ranges_count']==compact['structural_gaps_count']==1
    assert compact_reading_coverage(compact)==compact
    assert compact_reading_coverage(compact_reading_coverage(compact))==compact
    assert compact_reading_coverage({**raw,'reviewed_windows':3})!=compact
