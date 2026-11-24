"""Missing material must stay visible without masquerading as unfinished execution."""
from copy import deepcopy
import json
import pytest

from company_chapters import outline
from report_agents.company_corpus import build_corpus_review
from report_agents.company_delivery_contract import assess_delivery,disclosed_question_gaps,fill_evidence_gaps
from report_agents.company_reading_status import classify_context_gap,pending_native_reading
from test_company_chapter_flow import flow,source,TARGET
from test_company_evidence_recovery import candidates


def test_successful_context_read_keeps_material_gap_without_blocking_clear_content():
    p,selection=candidates('平台仅显示案件数量，未提供案号和判决书。')
    doc=selection['prepared'][0][0];window=doc['windows'][0]
    def invoke(*args):
        return json.dumps({'decisions':[{'evidence_id':doc['evidence_id'],'disposition':'needs_context'}],
            'facts':[],'window_reviews':[{'evidence_id':doc['evidence_id'],'window_id':window['window_id'],
                'disposition':'needs_context','missing_fields':['具体案号和判决书']}]})
    result=build_corpus_review(p,{'facts':[]},{9:'司法'},invoke,lambda *a,**k:None,prepared=selection['prepared'])
    state=result['source_review']['1'];gap=state['structural_gaps'][0]
    assert result['diagnostic']['all_candidate_windows_reviewed'] is True
    assert gap['missing_fields']==['具体案号和判决书'] and gap['gap_kind']=='material_unavailable'
    assert gap['context_review_receipt_sha256'] and not pending_native_reading(state)
    assert result['diagnostic']['disclosed_material_gaps']==1


@pytest.mark.parametrize('failure',['transport','missing_acknowledgement','ocr_failed','no_source_verdict'])
def test_unfinished_or_failed_context_is_still_a_reading_failure(failure):
    from report_agents.company_reading_status import EXHAUSTED
    gap={'evidence_id':'E-123456','window_id':'W1','missing_fields':['案号'],'stop_reason':EXHAUSTED}
    response={'decisions':[{'evidence_id':'E-123456','disposition':'needs_context'}],
        'window_reviews':[{'evidence_id':'E-123456','window_id':'W1','disposition':'needs_context','missing_fields':['案号']}]}
    packet={'context_recovery':[{'request':gap,'response':response}],
        'context_expansions':[{'evidence_id':'E-123456','window_id':'W1'}]}
    if failure=='transport':gap.update(stop_reason='context_call_deferred',error={'category':'connection'})
    elif failure=='missing_acknowledgement':response['window_reviews']=[]
    elif failure=='ocr_failed':packet['context_expansions'][0]['original_page_context']={'status':'failed'}
    else:response['decisions']=[]
    classified=classify_context_gap(gap,packet)
    assert classified['gap_kind']=='pending_reading'
    assert pending_native_reading({'structural_gaps':[classified]})
    assert pending_native_reading({'unread_ranges':[[0,10]],'structural_gaps':[]})


@pytest.fixture
def bounded_chapter(tmp_path,monkeypatch):
    f,_,_=flow(tmp_path,monkeypatch,[source()]);f.run_chapter(7)
    result=f.coverage[7];result['complete']=False
    result['questions'][0].update(status='partial',missing_fields=['现任管理层关联关系的公司书面确认'],
        reason='任免公告支持任职信息，但未披露完整关联关系。',
        next_step='向公司取得现任管理层签署的关联关系声明。',review_origin='model_review')
    for row in result['questions']:row.setdefault('reason','原文支持任职事实。')
    result['follow_up_queries']=[]
    f.collect(7,gap=True);f.merge()
    sections=fill_evidence_gaps(f.sections,{},outline(),project=f.project)
    return f,result,'\n\n'.join(sections.values())


def test_reviewed_partial_answer_can_deliver_with_exact_gap_and_actual_collection(bounded_chapter):
    f,result,report=bounded_chapter
    assert disclosed_question_gaps(f.project,7,result,report)
    contract=assess_delivery(f.project,report)
    assert 7 in contract['disclosed_material_gap_chapters']
    assert '第7章必答问题尚未全部核对完成' not in contract['errors']
    assert result['complete'] is False and result['questions'][0]['status']=='partial'
    assert '向公司取得现任管理层签署的关联关系声明。' in report
    assert '第2章缺少回答核心问题的目标主体证据' in contract['errors']


@pytest.mark.parametrize('change',['model_error','pending_origin','fields','action','support','question_id',
    'unsettled_search','failed_search','removed_disclosure','changed_disclosure','changed_collection_status'])
def test_gap_label_cannot_hide_execution_or_disclosure_failure(bounded_chapter,change):
    f,original,report=bounded_chapter;result=deepcopy(original);row=result['questions'][0]
    if change=='model_error':result['errors']=['APIConnectionError']
    elif change=='pending_origin':row['review_origin']='program_pending'
    elif change=='fields':row['missing_fields']=[]
    elif change=='action':row['next_step']=''
    elif change=='support':row['support_ids']=['P-foreign']
    elif change=='question_id':row['id']='Q-other'
    elif change=='unsettled_search':f.journal.data['chapters']['7']['gap_search_done']=False
    elif change=='failed_search':f.journal.data['chapters']['7']['gap_search_done_status']='retryable_transport_failure'
    elif change=='removed_disclosure':report=report.replace('<!-- company-chapter-gaps:start -->','')
    elif change=='changed_disclosure':report=report.replace('现任管理层关联关系的公司书面确认','无需补采')
    else:f.journal.data['chapters']['7']['search_receipts'][-1]['status']='failed'
    assert not disclosed_question_gaps(f.project,7,result,report)


def test_optional_empty_chapter_needs_specific_gaps_but_core_evidence_remains_required(bounded_chapter):
    f,result,report=bounded_chapter
    f.project.company_information_usage_package['records']=[]
    contract=assess_delivery(f.project,report)
    assert '第7章缺少回答核心问题的目标主体证据' not in contract['errors']
    assert '第2章缺少回答核心问题的目标主体证据' in contract['errors']
    assert '第6章缺少回答核心问题的目标主体证据' in contract['errors']
    f.project.company_research_intake['required_chapters']=[7]
    assert '第7章缺少回答核心问题的目标主体证据' in assess_delivery(f.project,report)['errors']


def test_specific_disclosed_material_gap_does_not_reappear_as_generic_degraded_gate(bounded_chapter):
    from report_agents.company_delivery_contract import diagnostic_requires_review,report_hash
    f,_,report=bounded_chapter
    f.project.company_editorial_package['findings']=[]
    f.project.company_management_diagnostic={'degraded':True,'errors':['第七章尚无可核对的管理团队记录']}
    f.project.company_delivery_contract={'status':'passed','requirements':{'required_chapters':[7]},
        'disclosed_material_gap_chapters':[7]}
    f.project.company_document_review={'status':'passed','report_sha256':report_hash(report)}
    assert not diagnostic_requires_review(f.project,'management',report)
    f.project.company_management_diagnostic['errors']=['模型审稿调用失败']
    assert diagnostic_requires_review(f.project,'management',report)


def test_cached_no_relevant_context_verdict_restores_material_limit_only_with_native_binding():
    from report_agents.company_reading_status import completed_reading_state,EXHAUSTED
    from company_flow_state import fingerprint
    p,selection=candidates('仅有竞品数量，没有竞品名单。')
    doc=selection['prepared'][0][0];w=doc['windows'][0]
    gap={'evidence_id':doc['evidence_id'],'window_id':w['window_id'],
         'missing_fields':['具体竞品名单'],'stop_reason':EXHAUSTED,'gap_kind':'pending_reading'}
    page={'status':'unavailable','reason':'no_original_page'}
    response={'context_recovery':[{'request':gap,'response':{
        'decisions':[{'evidence_id':doc['evidence_id'],'disposition':'no_relevant_claim'}],
        'window_reviews':[{**gap,'disposition':'needs_context'}]}}],
        'context_expansions':[{**gap,'start':w['start'],'end':w['end'],
            'native_window_sha256':fingerprint([doc['original_sha256'],w['start'],w['end'],w['quote']]),
            'original_page_context':page}]}
    state={'evidence_id':doc['evidence_id'],'original_sha256':doc['original_sha256'],'structural_gaps':[gap]}
    manifest=[{**w,'source_id':doc['source_id'],'evidence_id':doc['evidence_id']}]
    receipts=[{'response':response}]
    result=completed_reading_state(state,manifest,receipts,p,lambda *a:page)
    assert not pending_native_reading(result)
    assert result['structural_gaps'][0]['missing_fields']==['具体竞品名单']
    assert state['structural_gaps'][0]['gap_kind']=='pending_reading'
    stale=deepcopy(receipts);stale[0]['response']['context_expansions'][0]['native_window_sha256']='changed'
    assert pending_native_reading(completed_reading_state(state,manifest,stale,p,lambda *a:page))
