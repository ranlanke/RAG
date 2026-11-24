from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from company_reading import ReadingIndex
from report_agents.company_corpus import VERSION, _batches, build_corpus_review

TARGET='示例制造股份有限公司'
ALIAS='示例制造'


def project(body=None):
    s=NS(id=1,project_id=42,url='https://original.example/1',title=TARGET,
         source_type='web',content=body or TARGET+'披露2025年度营业收入12亿元，数据采用合并报表口径。',
         summary='',object_key='',source_metadata={'content_kind':'full_text'})
    return NS(id=42,target_name=TARGET,sources=[s],company_research_intake={'as_of_date':'2026-11-05'})


def prepared(p,aliases):
    value=ReadingIndex().select(p,6)['prepared']
    for doc in value[0]:doc['subject_binding']['disclosed_aliases']=aliases
    return value


def model(calls):
    def invoke(role,prompt,trace=None):
        calls.append(role)
        docs=json.loads(prompt.rsplit('资料：\n',1)[1])['originals']
        return json.dumps({'decisions':[{'evidence_id':d['evidence_id'],'disposition':'accepted'} for d in docs],
            'facts':[{'evidence_id':d['evidence_id'],'window_id':w['window_id'],'chapter':6,
                      'scope':'group','relevance':'披露本期营业收入及报表范围。'} for d in docs for w in d['windows']],
            'window_reviews':[{'evidence_id':d['evidence_id'],'window_id':w['window_id'],
                              'disposition':'accepted','reason':'核对完整原文。','missing_fields':[]}
                              for d in docs for w in d['windows']]})
    return invoke


def read(p,inputs,calls):
    return build_corpus_review(p,{'facts':[]},{},model(calls),lambda *a,**kw:None,prepared=inputs)


def test_new_source_alias_with_no_new_mentions_reuses_native_receipt_and_current_literals():
    p=project();calls=[]
    old_inputs=prepared(p,[TARGET]);first=read(p,old_inputs,calls)
    p.company_corpus_analysis_cache=first
    # A new source disclosed this abbreviation, but in this unchanged input
    # it occurs only inside the already supplied complete legal name.
    current=prepared(p,[TARGET,ALIAS]);calls.clear()
    second=read(p,current,calls)
    assert calls==[] and second['diagnostic']['cached_review_batches']==1
    fact=second['facts'][0]
    assert fact['subject_binding']['disclosed_aliases']==[TARGET,ALIAS]
    assert fact['evidence_quote']==p.sources[0].content[fact['original_offset']:fact['original_end']]
    # Reuse never skips the current hash guard.
    damaged=deepcopy(current);damaged[0][0]['windows'][0]['native_window_sha256']='wrong'
    calls.clear();third=read(p,damaged,calls)
    assert calls and not third['facts']
    assert third['diagnostic']['all_candidate_windows_reviewed'] is False
    # A stored proposal is not accepted just because its input key matched.
    first['review_receipts'][0]['response']['facts'][0]['scope']='invalid_scope'
    calls.clear();fourth=read(p,current,calls)
    assert not calls and not fourth['facts']
    assert fourth['rejected_selections'][0]['reason']=='unbound_scope'


def test_legacy_exact_hash_migrates_only_the_redundant_alias_delta():
    p=project();calls=[]
    aliases=[TARGET,'示例制造股份']
    old_inputs=prepared(p,aliases);first=read(p,old_inputs,calls)
    batch=list(_batches(old_inputs[0]))[0]
    legacy_hash=hashlib.sha256((VERSION+'\n'+TARGET+'\n2026-11-05\n'+json.dumps(batch,ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
    first['review_receipts'][0]['input_sha256']=legacy_hash
    p.company_corpus_analysis_cache=first;calls.clear()
    second=read(p,prepared(p,aliases+[ALIAS]),calls)
    assert not calls and second['facts'] and second['diagnostic']['cached_review_batches']==1


@pytest.mark.parametrize('change',['body_elsewhere','subject','cutoff','source_url','context'])
def test_meaningful_native_or_subject_changes_still_invalidate(change):
    body=TARGET+'披露2025年度营业收入12亿元。'+ALIAS+'提供软件服务。'
    p=project(body);calls=[]
    aliases=[TARGET,ALIAS] if change=='alias_removed' else [TARGET]
    first=read(p,prepared(p,aliases),calls);p.company_corpus_analysis_cache=first
    new_aliases=[TARGET] if change=='alias_removed' else [TARGET,ALIAS] if change=='new_mention' else aliases
    if change=='body_elsewhere':p.sources[0].content+='另页披露相关条件及风险。'
    if change=='subject':p.sources[0].source_metadata['entity_validation']='related_entity_context'
    if change=='cutoff':p.company_research_intake['as_of_date']='2026-10-01'
    if change=='source_url':p.sources[0].url='https://other.example/2'
    current=prepared(p,new_aliases)
    if change=='context':current[0][0]['windows'][0]['context_quote']+='新增主体说明'
    calls.clear();read(p,current,calls)
    assert calls, change


def test_alias_enrichment_reuses_extraction_but_changes_semantic_review_identity():
    from report_agents.company_corpus_writer import collect_statements
    from report_agents.company_information_usage import information_records
    p=project(TARGET+'披露2025年度营业收入12亿元。'+ALIAS+'提供软件服务。');calls=[]
    first=read(p,prepared(p,[TARGET]),calls)
    old_records=information_records(collect_statements(p,first))
    p.company_corpus_analysis_cache=first;calls.clear()
    current=read(p,prepared(p,[TARGET,ALIAS]),calls)
    new_records=information_records(collect_statements(p,current))
    assert not calls and current['facts']
    assert old_records and new_records
    assert {r['information_id'] for r in old_records}.isdisjoint(r['information_id'] for r in new_records)
    assert new_records[0]['context']['subject_binding']['disclosed_aliases']==[TARGET,ALIAS]


@pytest.mark.parametrize('independent_mention',[False,True])
def test_new_alias_rechecks_previously_unselected_native_window_when_needed(independent_mention):
    body=TARGET+'披露软件服务情况。'+(ALIAS+'提供软件服务。' if independent_mention else '')
    p=project(body);inputs=prepared(p,[TARGET])
    def no_selection(role,prompt,trace=None):
        docs=json.loads(prompt.rsplit('资料：\n',1)[1])['originals']
        return json.dumps({'decisions':[{'evidence_id':d['evidence_id'],'disposition':'no_relevant_claim'} for d in docs],
            'facts':[],'window_reviews':[{'evidence_id':d['evidence_id'],'window_id':w['window_id'],
            'disposition':'no_relevant_claim','reason':'初次阅读未选入。','missing_fields':[]} for d in docs for w in d['windows']]})
    first=build_corpus_review(p,{'facts':[]},{},no_selection,lambda *a,**kw:None,prepared=inputs)
    p.company_corpus_analysis_cache=first;calls=[]
    second=read(p,prepared(p,[TARGET,ALIAS]),calls)
    if independent_mention:
        assert calls and second['facts'] and second['diagnostic']['all_candidate_windows_reviewed']
        assert second['review_receipts'][0]['response']['alias_refresh_receipt']['error']==''
        assert all(p.sources[0].content[r['original_offset']:r['original_end']]==r['fact'] for r in second['facts'])
        p.company_corpus_analysis_cache=second;calls.clear()
        third=read(p,prepared(p,[TARGET,ALIAS]),calls)
        assert not calls and third['facts']==second['facts']
    else:
        assert not calls and not second['facts']


def test_new_source_does_not_hide_its_new_original_behind_cached_old_source():
    p=project();calls=[]
    first=read(p,prepared(p,[TARGET]),calls);p.company_corpus_analysis_cache=first
    p.sources.append(NS(**{**vars(p.sources[0]),'id':2,'url':'https://original.example/2',
                          'content':TARGET+'披露新增业务事项，明确投资项目仍处于计划阶段。'}))
    calls.clear();second=read(p,prepared(p,[TARGET,ALIAS]),calls)
    assert calls and {f['source_id'] for f in second['facts']}=={'1','2'}


def test_failed_alias_primary_recovers_from_bound_window_read_and_survives_resume():
    from report_agents.company_transport import CompanyModelUnavailable
    p=project(TARGET+'披露软件服务。'+ALIAS+'提供相关服务。')
    initial=prepared(p,[TARGET]);docs=initial[0]
    def empty(role,prompt,trace=None):
        return json.dumps({'decisions':[{'evidence_id':d['evidence_id'],'disposition':'no_relevant_claim'} for d in docs],
            'facts':[],'window_reviews':[{'evidence_id':d['evidence_id'],'window_id':w['window_id'],
                'disposition':'no_relevant_claim','missing_fields':[]} for d in docs for w in d['windows']]})
    first=build_corpus_review(p,{'facts':[]},{},empty,lambda *a,**kw:None,prepared=initial)
    p.company_corpus_analysis_cache=first;calls=[]
    def recover(role,prompt,trace=None):
        calls.append(role)
        if role=='公司全量原文分析 Agent':
            raise CompanyModelUnavailable(diagnostic={'category':'output_truncated','retryable':False})
        assert role=='公司未确认窗口补读'
        current=json.loads(prompt.rsplit('资料：\n',1)[1])['originals']
        return json.dumps({'facts':[],'window_reviews':[{'evidence_id':d['evidence_id'],'window_id':w['window_id'],
            'disposition':'no_relevant_claim','missing_fields':[]} for d in current for w in d['windows']]})
    current=prepared(p,[TARGET,ALIAS])
    second=build_corpus_review(p,{'facts':[]},{},recover,lambda *a,**kw:None,prepared=current)
    receipt=second['review_receipts'][0]
    assert receipt['error']=='' and not second['facts'] and second['diagnostic']['all_candidate_windows_reviewed']
    assert receipt['response']['alias_refresh_receipt']['error']=='provider_unavailable'
    assert receipt['response']['alias_refresh_receipt']['recovered_by']=='bound_window_review'
    assert calls==['公司全量原文分析 Agent','公司未确认窗口补读']
    # A pre-fix checkpoint carries the same fully recovered response plus a
    # stale outer error. Revalidate this exact input, never repeat the primary.
    legacy=deepcopy(second);legacy['review_receipts'][0]['error']='provider_unavailable'
    legacy['review_receipts'][0]['response']['alias_refresh_receipt'].pop('recovered_by')
    p.company_corpus_analysis_cache=legacy;calls.clear()
    third=read(p,prepared(p,[TARGET,ALIAS]),calls)
    assert not calls and third['review_receipts'][0]['error']==''
    assert third['diagnostic']['all_candidate_windows_reviewed']
    changed=prepared(p,[TARGET,ALIAS]);changed[0][0]['windows'][0]['context_quote']+='不同的相邻原文。'
    calls.clear();read(p,changed,calls)
    assert calls  # The same recovery receipt cannot authorize changed input.


@pytest.mark.parametrize('failure',['missing_window','unknown_window','format_pending','window_error',
    'selection_pending','context_deferred','wrong_input_hash'])
def test_incomplete_window_recovery_cannot_clear_alias_failure(failure):
    from report_agents.company_corpus_cache import alias_refresh_recovered
    result={'alias_refresh_receipt':{'error':'provider_unavailable','windows':[['E-ABCDEF','W1']]},
        'window_review_receipt':{'input_sha256':'a'*64,'format_pending':False,'raw_response':'actual returned text',
            'response':{'window_reviews':[{'evidence_id':'E-ABCDEF','window_id':'W1','disposition':'no_relevant_claim'}]}},
        'pending_window_reviews':[],'pending_selection_repairs':[]}
    assert alias_refresh_recovered(result)
    if failure=='missing_window':result['pending_window_reviews']=[{'evidence_id':'E-ABCDEF','window_id':'W1'}]
    elif failure=='unknown_window':result['window_review_receipt']['response']['window_reviews'][0]['window_id']='foreign'
    elif failure=='format_pending':result['window_review_receipt']['format_pending']=True
    elif failure=='window_error':result['window_review_error']={'category':'connection'}
    elif failure=='selection_pending':result['pending_selection_repairs']=[{'reason':'unbound_scope'}]
    elif failure=='context_deferred':result['unresolved_context']=[{'stop_reason':'context_call_deferred'}]
    else:result['window_review_receipt']['input_sha256']='invalid'
    assert not alias_refresh_recovered(result)
