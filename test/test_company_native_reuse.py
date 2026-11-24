from copy import deepcopy
from types import SimpleNamespace as NS
import json
import pytest
from company_reading_reuse import split_reused,merge_reused
from report_agents.company_prompt_payload import shared_payload


def fixture(company='甲制造公司',chapter=4):
    windows=[{'window_id':f'W{i}','start':i*10,'end':i*10+10,'quote':'原文'*5,
              'native_window_sha256':f'bound{i}'} for i in range(2)]
    state={'original_sha256':'body','review_windows':2,'reviewed_windows':2,'accepted_claims':1,
           'review_decisions':[{'window_ids':['W0','W1'],'disposition':'accepted','error':''}],
           'coverage_ranges':[[0,20]],'structural_gaps':[]}
    fact={'statement_id':'F1','source_id':'1','original_sha256':'body','original_offset':0,
          'original_end':10,'fact':'原文'*5,'chapter':chapter}
    prior={'target_name':company,'research_as_of':'2026-01-01','research_context':{'current_chapter':{'number':chapter}},
           'source_review':{'1':state},'facts':[fact],
           'reading_manifest':[{'source_id':'1','window_id':w['window_id'],'start':w['start'],'end':w['end'],
                                'sha256':w['native_window_sha256']} for w in windows]}
    fresh=deepcopy(state);fresh.update(reviewed_windows=0,review_decisions=[],coverage_ranges=[])
    prepared=([{'source_id':'1','original_sha256':'body','windows':windows}],{'1':fresh},[])
    return prepared,prior


@pytest.mark.parametrize('company',['甲制造公司','乙医药公司','丙服务公司'])
@pytest.mark.parametrize('chapter',[2,3,4,5,6,7,8,9,10])
def test_company_and_chapter_independent_reuse(company,chapter):
    prepared,prior=fixture(company,chapter)
    before=deepcopy((prepared,prior))
    fresh,reused=split_reused(prepared,[prior],company,'2026-01-01')
    assert fresh[0]==[] and len(reused)==2
    out={'source_review':fresh[1],'facts':[],'diagnostic':{}}
    merge_reused(out,reused)
    assert out['source_review']['1']['reviewed_windows']==2
    assert out['facts']==prior['facts'] and out['diagnostic']['all_candidate_windows_reviewed']
    assert (prepared,prior)==before


@pytest.mark.parametrize('change',['target','date','metadata','body','error','gap','rejected','unacknowledged'])
def test_unsafe_reuse_is_rejected(change):
    prepared,prior=fixture();target='甲制造公司';date='2026-01-01'
    if change=='target':target='同名其他公司'
    if change=='date':date='2025-01-01'
    if change=='metadata':
        for w in prepared[0][0]['windows']:w['native_window_sha256']='changed'
    if change=='body':prepared[0][0]['original_sha256']='changed'
    if change=='error':prior['source_review']['1']['review_decisions'][0]['error']='failed'
    if change=='gap':prior['source_review']['1']['structural_gaps']=[{'reason':'missing_header'}]
    if change=='rejected':prior['source_review']['1']['validation_rejections']=['wrong_subject']
    if change=='unacknowledged':prior['source_review']['1']['coverage_ranges']=[]
    fresh,reused=split_reused(prepared,[prior],target,date)
    assert len(fresh[0][0]['windows'])==2 and not reused


def test_explicit_gap_rereads_only_its_window():
    prepared,prior=fixture()
    fresh,reused=split_reused(prepared,[prior],'甲制造公司','2026-01-01',force=[('1','W1')])
    assert [w['window_id'] for w in fresh[0][0]['windows']]==['W1']
    assert set(reused)=={('1','W0')}


@pytest.mark.parametrize('issue',['rejection','context_gap'])
def test_mixed_read_failure_does_not_invalidate_other_windows(issue):
    prepared,prior=fixture()
    prior['source_review']['1']['evidence_id']='E1'
    if issue=='rejection':
        prior['source_review']['1']['validation_rejections']=['no_affirmative_source_disposition']
        prior['rejected_selections']=[{'evidence_id':'E1','window_id':'W1',
            'reason':'no_affirmative_source_disposition'}]
    else:
        prior['source_review']['1']['structural_gaps']=[{'window_id':'W1','reason':'missing_header'}]
    fresh,reused=split_reused(prepared,[prior],'甲制造公司','2026-01-01')
    assert set(reused)=={('1','W0')}
    assert [w['window_id'] for w in fresh[0][0]['windows']]==['W1']
    # A deferred retry must preserve the successful window on the next pass.
    out={'target_name':prior['target_name'],'research_as_of':prior['research_as_of'],
         'source_review':fresh[1],'facts':[],'diagnostic':{},'reading_manifest':prior['reading_manifest']}
    merge_reused(out,reused)
    next_fresh,next_reused=split_reused(prepared,[out],prior['target_name'],prior['research_as_of'])
    assert set(next_reused)=={('1','W0')}
    assert [w['window_id'] for w in next_fresh[0][0]['windows']]==['W1']


def expand(value,texts):
    if isinstance(value,dict):
        if set(value)=={'text_ref'} and value['text_ref'] in texts:return texts[value['text_ref']]
        return {k:expand(v,texts) for k,v in value.items() if k!='shared_texts'}
    if isinstance(value,list):return [expand(v,texts) for v in value]
    return value


def test_exhausted_context_is_reused_but_gap_is_not_cleared():
    prepared,prior=fixture()
    st=prior['source_review']['1'];st['evidence_id']='E1'
    st['review_decisions'][0]['disposition']='needs_context'
    st['structural_gaps']=[{'window_id':'W1','missing_fields':['competitor feedback'],
        'gap_kind':'pending_reading','stop_reason':'available_native_context_exhausted_need_original_or_targeted_source'}]
    prior['review_receipts']=[{'error':'','response':{'window_reviews':[
        {'evidence_id':'E1','window_id':'W0','disposition':'no_relevant_claim'},
        {'evidence_id':'E1','window_id':'W1','disposition':'needs_context'}]}}]
    fresh,reused=split_reused(prepared,[prior],prior['target_name'],prior['research_as_of'])
    assert not fresh[0] and len(reused)==2
    out={'target_name':prior['target_name'],'research_as_of':prior['research_as_of'],
        'source_review':fresh[1],'facts':[],'diagnostic':{},'reading_manifest':prior['reading_manifest']}
    merge_reused(out,reused)
    assert out['source_review']['1']['structural_gaps']==st['structural_gaps']
    assert out['source_review']['1']['review_decisions'][0]['disposition']=='no_relevant_claim'
    assert out['diagnostic']['unresolved_structural_gaps']==1
    assert not split_reused(prepared,[out],prior['target_name'],prior['research_as_of'])[0][0]


def test_prompt_interning_preserves_every_character_and_relation():
    text='甲公司2025年收入为-1.20万元；非合并口径。\n'*80
    payload={'sources':[{'information_id':str(i),'text':text,'context':{'quote':text,'period':str(2024+i)}}
                        for i in range(10)],'target':'乙公司'}
    before=deepcopy(payload);packed=shared_payload(payload)
    assert expand(packed,packed['shared_texts'])==payload==before
    assert len(json.dumps(packed))<len(json.dumps(payload))*.25
    assert shared_payload({'text':'短文本'})=={'text':'短文本'}


def test_real_reader_reuses_after_chapter_switch(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source,FINANCE,TEAM
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source(text=FINANCE+'\n'+TEAM)])
    f.read(4,assess=False)
    before=sum('公司全量原文分析' in role for role,_ in calls)
    f.read(5,assess=False)
    assert f.reads[5]['diagnostic']['reused_native_windows']>0
    assert sum('公司全量原文分析' in role for role,_ in calls)==before
    count=len(calls)
    f.read(5,assess=False)
    assert len(calls)==count


def test_primary_response_survives_followup_budget_stop(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source,FINANCE
    from company_flow_state import CompanyFlowLimit
    from report_agents import company_reading_recovery as recovery
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source(text=FINANCE)])
    real=recovery.complete_window_reviews
    def stop(*a,**kw):raise CompanyFlowLimit('experiment_budget')
    monkeypatch.setattr(recovery,'complete_window_reviews',stop)
    with pytest.raises(CompanyFlowLimit):f.read(4,assess=False)
    before=sum('公司全量原文分析' in role for role,_ in calls)
    assert before==1
    monkeypatch.setattr(recovery,'complete_window_reviews',real)
    f.read(4,assess=False)
    assert sum('公司全量原文分析' in role for role,_ in calls)==before
    assert f.reads[4]['diagnostic']['all_candidate_windows_reviewed']


def test_initial_plan_does_not_accumulate_historical_windows(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source
    from company_progressive_reading import plan_reading
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    nodes=[{'node_id':f'N{i}','source_id':'1','source_title':'原文','original_sha256':'hash',
        'start':i*10,'end':i*10+10,'pages':[i+1],'headings':[],'opening_locator':'证据',
        'question_locators':[],'window_ids':[f'W{i}'],'chars':10} for i in range(100)]
    monkeypatch.setattr('company_progressive_reading.directory',lambda *_:nodes)
    monkeypatch.setattr('company_chapter_retrieval.rank_questions',lambda *a,**kw:({'Q05-1':[n['node_id'] for n in nodes]},{}))
    f.reads[5]={'source_review':{'1':{'original_sha256':'hash','review_decisions':[
        {'window_ids':[f'W{i}' for i in range(100)],'error':''}]}}}
    plan=plan_reading(f,5)
    assert plan['window_ids_by_source']=={'1':sorted(f'W{i}' for i in range(56))}
    assert plan['unselected_node_ids']==[f'N{i}' for i in range(56,100)]


def test_targeted_reread_is_durable_and_does_not_repeat(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source,FINANCE
    from company_progressive_reading import plan_reading
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source(text=FINANCE)])
    f.read(4,assess=False)
    plan=plan_reading(f,5)
    plan['input_sha256']='targeted-gap-plan'
    pairs=[(str(sid),wid) for sid,ids in plan['window_ids_by_source'].items() for wid in ids]
    plan.update(reread_windows=pairs[:1],reread_nodes=[{'node_id':plan['selected_node_ids'][0],
        'reason':'定位存在未提取字段','missing_evidence':'单位及期间'}])
    before=sum('公司全量原文分析' in role for role,_ in calls)
    f.read(5,reading_plan=plan,assess=False)
    assert sum('公司全量原文分析' in role for role,_ in calls)==before+1
    count=len(calls)
    f.read(5,reading_plan=plan,assess=False)
    assert len(calls)==count
    assert set(map(tuple,f.reads[5]['targeted_reread_windows']))==set(pairs[:1])
