import json
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from test_company_chapter_flow import flow, source, TARGET, FINANCE, fake_model
from company_progressive_reading import directory, plan_reading, plan_collection
from company_reading import assess_questions
from research_workflow import WorkflowPaused
from web_search import WebSearchProvider


def progressive_flow(tmp_path, monkeypatch, sources=()):
    f, calls, events = flow(tmp_path, monkeypatch, sources)
    f.progressive = True
    base = fake_model(calls)
    def model(role, prompt, trace=None, **kwargs):
        if '公司本章阅读取证规划' in role:
            p=json.loads(prompt.rsplit('资料：\n',1)[1])
            return json.dumps({'read_node_ids':[n['node_id'] for n in p['directory']],
                'initial_search_required':not p['directory'],'queries':[],'platform_routes':[], 'reason':'按本章问题选原文'})
        if '公司本章集中取证计划' in role:
            p=json.loads(prompt.rsplit('资料：\n',1)[1])
            return json.dumps({'queries':[{'question_id':p['questions'][0]['id'],'query':TARGET+' 最新登记原文',
                'reason':'补登记时点','expected_fields':['登记日期']}],'platform_routes':[]})
        if role.startswith(('公司本章问题覆盖核对','公司本章问题跨批综合核对','公司本章补采计划独立核对')):
            p=json.loads(prompt.rsplit('资料：\n',1)[1])
            return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[p['proofs'][0]['id']],
                'missing_fields':['最新登记日期'],'reason':'已有事实可写，最新日期尚未取得。',
                'next_step':'向登记机关取得登记档案。'} for q in p['questions']], 'follow_up_queries':[]})
        return base(role,prompt,trace,**kwargs)
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',model)
    return f,calls,events


def test_directory_exposes_question_match_beyond_opening():
    windows=[{'start':0,'end':100,'quote':'一般经营安排。'*20,'window_id':'W1','physical_page':1},
             {'start':100,'end':240,'quote':'其他事项。'*10+'员工激励实际授予已有记录。','window_id':'W2','physical_page':6}]
    node=directory(([{'source_id':'1','source_title':'年度行动方案','original_sha256':'hash',
                      'windows':windows}],{},[]),('激励',))[0]
    assert '激励' not in node['opening_locator']
    assert node['question_locators'][0]['term']=='激励'
    assert node['question_locators'][0]['page']==6
    assert '实际授予' in node['question_locators'][0]['excerpt']


def test_vector_creation_count_does_not_invalidate_same_reading_plan(tmp_path,monkeypatch):
    import embedding
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(1)])
    planner=f.workflow._invoke_company_report_editor
    planning_calls=[]
    def counted(role,prompt,trace=None,**kwargs):
        if '公司本章阅读取证规划' in role:planning_calls.append(role)
        return planner(role,prompt,trace,**kwargs)
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',counted)
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','true')
    monkeypatch.setattr(embedding.embedding_service,'get_embeddings',
        lambda texts:[[float(len(text)),1.0] for text in texts])
    first=plan_reading(f,4)
    second=plan_reading(f,4)
    assert first['input_sha256']==second['input_sha256']
    assert len(planning_calls)==1


def test_unanswered_question_reads_new_local_plan_before_closing_round(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(1),source(2,text=FINANCE+' 补充原文')])
    def planner(role,prompt,trace=None,**kwargs):
        packet=json.loads(prompt.rsplit('资料：\n',1)[1])
        sid='2' if packet['phase']=='supplement' else '1'
        return json.dumps({'read_node_ids':[n['node_id'] for n in packet['directory'] if n['source_id']==sid],
            'initial_search_required':False,'queries':[],'platform_routes':[],'reason':'按问题补读原文'})
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',planner)
    passes=[]
    def read(number,**kwargs):
        passes.append(kwargs.get('expanded',False))
        f.reads[number]={'source_review':{},'facts':[]}
        f.coverage[number]={'complete':False,'questions':[{'id':'Q06-1','status':'missing'}]}
    monkeypatch.setattr(f,'read',read)
    monkeypatch.setattr(f,'write',lambda number:f.sections.update({number:'## 6. 财务分析\n\n已保存正文。'}))
    monkeypatch.setattr(f,'persist_chapter_draft',lambda *a,**kw:None)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    # Program-required candidates may already cover the second original in
    # the first pass, without an extra paid supplement plan.
    assert passes==[False,False]  # final cached pass performs one coverage check
    assert f.progressive_selections[6]['mandatory_node_ids']
    assert f.journal.data['chapters']['6']['round_closed']


def test_unanswered_question_gets_read_budget_before_answered_question(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(1),source(2,text=FINANCE+' 补充原文')])
    f.coverage[6]={'questions':[{'id':'Q06-1','status':'answered'}, {'id':'Q06-2','status':'partial'}]}
    def planner(role,prompt,trace=None,**kwargs):
        packet=json.loads(prompt.rsplit('资料：\n',1)[1])
        nodes={n['source_id']:n for n in packet['directory']}
        return json.dumps({'read_node_ids':[nodes['1']['node_id'],nodes['2']['node_id']],
            'question_node_ids':{'Q06-1':[nodes['1']['node_id']], 'Q06-2':[nodes['2']['node_id']]},
            'initial_search_required':False,'queries':[],'platform_routes':[],'reason':'优先解决未答问题'})
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',planner)
    f.read_char_budget=next(n['chars'] for n in directory(f.reader.prepare(f.project)) if n['source_id']=='2')
    result=plan_reading(f,6,expanded=True)
    assert result['selected_node_ids']==[next(n['node_id'] for n in directory(f.reader.prepare(f.project)) if n['source_id']=='2')]
    assert result['budget_deferred_node_ids']


def test_selected_windows_finish_before_writing_even_when_coverage_says_answered(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    passes=[]
    def read(number,**kwargs):
        passes.append(kwargs)
        f.reads[number]={'source_review':{'1':{'review_windows':1,'reviewed_windows':int(len(passes)>=3)}},'facts':[]}
        f.coverage[number]={'complete':True,'questions':[]}
    monkeypatch.setattr(f,'read',read)
    monkeypatch.setattr(f,'write',lambda number:f.sections.update({number:'## 6. 财务分析\n\n已保存正文。'}))
    monkeypatch.setattr(f,'persist_chapter_draft',lambda *a,**kw:None)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert len(passes)==4
    assert all(p.get('assess') is False for p in passes[:3])
    assert passes[-1]=={'cached_only':True}
    assert f.journal.data['chapters']['6']['round_closed']


def test_fourth_selected_original_is_read_before_first_draft(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(i,text=FINANCE+f' 原文{i}') for i in range(1,5)])
    passes=[];wrote=[]
    def read(number,**kwargs):
        passes.append(kwargs)
        f.reads[number]={'source_review':{'4':{'review_windows':1,
            'reviewed_windows':int(len(passes)>=4)}},'facts':[]}
        f.coverage[number]={'complete':True,'questions':[]}
    monkeypatch.setattr(f,'read',read)
    monkeypatch.setattr(f,'write',lambda number:wrote.append(len(passes)) or
        f.sections.update({number:'## 6. 财务分析\n\n已保存正文。'}))
    monkeypatch.setattr(f,'persist_chapter_draft',lambda *a,**kw:None)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert len(f.progressive_selections[6]['window_ids_by_source'])==4
    assert wrote and wrote[0]==5
    assert all(p.get('assess') is False for p in passes[:4])
    assert passes[4]=={'cached_only':True}


def test_cached_receipts_do_not_trigger_draft_before_selected_reading(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(i,text=FINANCE+f' 原文{i}') for i in range(1,5)])
    f.journal.data['shared_read_receipts']={'saved':{'response':{},'input_sha256':'saved'}}
    passes=[];wrote=[]
    def read(number,**kwargs):
        passes.append(kwargs)
        f.reads[number]={'source_review':{'4':{'review_windows':1,
            'reviewed_windows':int(len(passes)>=4)}},'facts':[{'chapter':6}]}
        f.coverage[number]={'complete':True,'questions':[]}
    monkeypatch.setattr(f,'read',read)
    monkeypatch.setattr(f,'write',lambda number:wrote.append(len(passes)) or
        f.sections.update({number:'## 6. 财务分析\n\n已保存正文。'}))
    monkeypatch.setattr(f,'persist_chapter_draft',lambda *a,**kw:None)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert passes[0]=={'cached_only':True,'assess':False}
    assert wrote and wrote[0]>=4


def test_heading_only_writer_failure_does_not_start_gap_search_or_close_round(tmp_path,monkeypatch):
    f,_,events=progressive_flow(tmp_path,monkeypatch,[source()]);searches=[]
    monkeypatch.setattr(f,'write',lambda number:f.sections.update({number:'## 6. 本章标题\n\n'}))
    monkeypatch.setattr(f,'collect',lambda *a,**kw:searches.append(kw) or 0)
    f.run_chapter(6)
    record=f.journal.data['chapters']['6']
    assert record['status']=='pending_writing' and record['draft_saved'] is False
    assert not record.get('round_closed') and searches==[]
    assert not any(e.get('label')=='本章初稿已保存，开始一次集中补查' for e in events)


def test_completed_native_read_finishes_interrupted_coverage_before_writing(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.journal.data['shared_read_receipts']={'saved':{'response':{},'input_sha256':'saved'}}
    f.journal.data['partial_coverage']={'6':{'batches':[{'input_sha256':'done'}]}}
    f.coverage[6]={'questions':[{'id':'Q06-1','status':'pending_review'}],'errors':[],'complete':False}
    passes=[]
    def read(number,**kwargs):
        passes.append(kwargs)
        f.reads[number]={'source_review':{'1':{'review_windows':1,'reviewed_windows':1}},'facts':[{'chapter':6}]}
        if kwargs.get('assess',True):f.coverage[number]['complete']=True
    def write(number):
        assert f.coverage[number]['complete']
        f.sections[number]='## 6. 主营业务\n\n已核验正文。'
    monkeypatch.setattr(f,'read',read)
    monkeypatch.setattr(f,'write',write)
    monkeypatch.setattr(f,'persist_chapter_draft',lambda *a,**kw:None)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert passes==[{'cached_only':True,'assess':False},{'cached_only':True}]


def test_positive_new_sources_cannot_reopen_gap_round_and_draft_precedes_search(tmp_path,monkeypatch):
    f,model_calls,events=progressive_flow(tmp_path,monkeypatch,[source()]); searches=[]
    def collect(db,p,*args,**kw):
        assert f.journal.data['chapters']['6']['draft_saved']
        assert FINANCE in f.sections[6]
        searches.append({k:deepcopy(v) for k,v in kw['intake'].items() if not k.startswith('_company_query_cache') and not callable(v)})
        p.sources.append(source(len(p.sources)+1)); return 1
    monkeypatch.setattr(f.workflow,'collect_web_sources',collect)
    f.run_chapter(6)
    assert len(searches)==1 and searches[0]['_company_all_web_engines']
    assert all(q['company_all_web_engines'] for q in searches[0]['_company_chapter_query_items'])
    assert f.journal.data['chapters']['6']['round_closed']
    assert f.journal.data['chapters']['6']['gap_search_done_attempted']
    assert FINANCE in f.sections[6]
    before_replay=len(model_calls)
    f.run_chapter(6)
    assert len(searches)==1 and len(model_calls)==before_replay
    f.project.sources.append(source(99)); f.run_chapter(6)
    assert len(searches)==1  # Changed sources refresh writing, not the round allowance.


def test_failed_search_is_attempted_once_and_recorded_not_infinite_retry(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()]); calls=[]
    def fail(*args,**kwargs):calls.append(1);raise ConnectionError('offline')
    monkeypatch.setattr(f.workflow,'collect_web_sources',fail)
    with pytest.raises(ConnectionError):f.run_chapter(6)
    assert FINANCE in f.sections[6]
    f.run_chapter(6)
    assert len(calls)==1 and f.journal.data['chapters']['6']['round_closed']
    assert f.journal.data['chapters']['6']['search_receipts'][0]['status']=='interrupted'


def test_reading_budget_keeps_full_native_directory_and_deferred_ranges(tmp_path,monkeypatch):
    s=source();s.content+='\n\n'+('\n\n'.join('与当前问题无关的完整原文段落。'*100 for _ in range(40)))
    f,_,_=progressive_flow(tmp_path,monkeypatch,[s]); f.read_char_budget=3600
    p=plan_reading(f,6);f.progressive_selections[6]=p
    selected=f.selection(6)
    assert selected['diagnostic']['selected_chars']<=3600
    assert p['budget_deferred_node_ids'] and selected['diagnostic']['unread_ranges']
    assert len(directory(f.reader.prepare(f.project)))>1
    assert s.content.endswith('与当前问题无关的完整原文段落。'*100)


def test_unknown_directory_ids_are_rejected_without_claiming_material_useless(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',lambda *a,**kw:json.dumps(
        {'read_node_ids':['invented'],'initial_search_required':False,'platform_routes':[]}))
    plan=plan_reading(f,6)
    assert plan['response']['unresolved_node_ids']==['invented']
    assert 'invented' not in plan['selected_node_ids']
    assert plan['selected_node_ids'] and plan['mandatory_node_ids']
    assert plan['response']['node_selection_status']=='partial_fallback_to_verified_candidates'


def test_pause_propagates_without_becoming_missing_material():
    def paused(*a):raise WorkflowPaused('pause')
    with pytest.raises(WorkflowPaused):assess_questions(6,TARGET,[{'fact':FINANCE}],paused)


def test_redirected_pdf_html_is_not_successful_pdf(monkeypatch):
    response=NS(url='https://q.stock.sohu.com/',headers={'Content-Type':'text/html'},content=b'<html>navigation</html>',
        history=[NS(url='https://q.stock.sohu.com/newpdf/a.pdf',status_code=302,headers={'Location':'/'})],
        raise_for_status=lambda:None)
    monkeypatch.setattr('web_search.requests.get',lambda *a,**kw:response)
    result=WebSearchProvider()._fetch_url_once('https://q.stock.sohu.com/newpdf/a.pdf',12,30000,'ua','desktop',document_profile='company')
    assert result['status']=='invalid_pdf_response' and not result['content']
    assert result['error']=='pdf_url_returned_html' and not result['pdf_signature_verified']
    assert result['final_url']==response.url and result['redirect_chain'][0]['status_code']==302


def test_html_views_keep_table_cells_and_image_links_without_ocr():
    html='<article><table><tr><th>股东</th><th>比例</th></tr><tr><td>甲公司</td><td>51%</td></tr></table><img src="table.png" alt="股东表"></article>'
    result=WebSearchProvider._html_reading_views(html)
    assert result['raw_html']==html
    assert result['html_tables'][0]['rows'][1][1]['text']=='51%'
    assert result['image_links'][0]['status']=='retained_link_unread_no_ocr'


def test_model_selected_platform_routes_respect_disabled_providers(tmp_path,monkeypatch):
    from company_requirements import selected_platform_tools
    assert selected_platform_tools({'_company_platform_routes':['qcc_registration']},'qcc')=={'get_company_registration_info'}
    assert selected_platform_tools({'_company_platform_routes':['qcc_registration']},'tyc')==set()
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.disabled_platforms={'tyc','xiniu'}
    assert plan_collection(f,6)['queries'][0]['expected_fields']==['登记日期']


def test_all_engine_round_bypasses_single_provider_preference(tmp_path,monkeypatch):
    from research_workflow import ResearchWorkflow
    from search_results import SearchResults
    import research_workflow
    calls=[]
    monkeypatch.setenv('WEB_SEARCH_PROVIDER','tavily')
    provider=research_workflow.web_search_provider
    def multi(query,**kwargs):
        calls.append(query)
        return SearchResults([{'url':'https://original.test/one','title':'原文'}],
            audit={'engine_states':{'tavily':{'status':'returned'},'bocha':{'status':'returned'}}})
    monkeypatch.setattr(provider,'multi_engine_search',multi)
    monkeypatch.setattr(provider,'search',lambda *a,**k:pytest.fail('must not use single-provider override'))
    rows=ResearchWorkflow()._search_query_items_parallel(
        [{'query':TARGET+'登记','company_all_web_engines':True}],30,['tavily','bocha'])
    assert calls==[TARGET+'登记']
    assert len(rows)==1


def test_failed_coverage_is_pending_review_not_absent_material():
    def fail(*args):raise ConnectionError('disconnected')
    result=assess_questions(6,TARGET,[{'fact':FINANCE}],fail)
    assert result['errors']
    assert all(q['status']=='pending_review' and not q['missing_fields'] for q in result['questions'])


def test_cached_only_partial_recovery_can_write_before_coverage_calls(tmp_path,monkeypatch):
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.read(6)
    assert f.journal.data['shared_read_receipts']
    f.coverage.clear()
    calls.clear()
    f.read(6,cached_only=True,assess=False)
    assert f.coverage[6]['questions'][0]['status']=='pending_review'
    assert not any(role.startswith('公司本章问题覆盖核对') for role,_ in calls)
    f.write(6)
    assert FINANCE in f.sections[6]


def test_cold_read_pass_caps_new_batches_and_retains_the_unread_tail(tmp_path,monkeypatch):
    s=source(text='\n\n'.join(FINANCE+'\n'+('完整上下文原句。'*70) for _ in range(50)))
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[s]);f.read_char_budget=100000
    monkeypatch.setenv('COMPANY_CHAPTER_NEW_READ_BATCHES','2')
    f.read(6,assess=False)
    actual=[r for r,p in calls if r.startswith('公司全量原文分析')]
    assert len(actual)<=2
    review=f.reads[6]
    assert review['diagnostic']['review_batches']>2
    assert review['facts']
    assert any(s.get('unread_ranges') for s in review['source_review'].values())
    assert any(r.get('error')=='deferred_unread_batch' for r in review['review_receipts'])


def test_chapter_audit_does_not_reattach_entire_long_original():
    from report_agents.company_editorial_context import make_evidence_context
    s=source(text='原文完整背景。'*4000+FINANCE)
    row={'information_id':'I-known','text':FINANCE,'context':{'chapter_question_owner':6,'context_quote':FINANCE},
        'provenance':[{'source_id':1,'source_url':s.url,'original_offset':s.content.index(FINANCE)}]}
    scoped=make_evidence_context([row],[s])['sources'][0]
    assert scoped['visible_scope']=='bounded_native_excerpts'
    assert 'complete_original' not in scoped
    assert scoped['native_excerpts'][0]['native_context']==FINANCE
    assert s.content.endswith(FINANCE)


def test_same_directory_initial_plan_reuses_after_reading_progress(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    first=plan_reading(f,6)
    f.coverage[6]={'questions':[{'id':'Q06-1','status':'partial','reason':'新增阅读结果'}]}
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',lambda *a,**kw:pytest.fail('same initial directory must reuse navigation'))
    assert plan_reading(f,6)['input_sha256']==first['input_sha256']


def test_pending_preview_is_not_overwritten_by_heading_only_accepted_section(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    saved=[]
    f.db=NS(add=lambda *a:None,commit=lambda:None)
    def save(db,project,kind,title,text,meta):
        saved.append((text,meta));return NS(id=1)
    monkeypatch.setattr(f.workflow,'save_artifact',save)
    f.sections[6]='## 财务分析'
    f.journal.data['chapters']['6']={'status':'pending_review'}
    f.journal.data['draft_previews']={'6':{'text':FINANCE,'review_status':'pending_review'}}
    f.persist_chapter_draft(6)
    assert saved[0][0]==FINANCE
    assert saved[0][1]['narrative_review_status']=='pending_review'
    assert not saved[0][1]['full_report_accepted']
    assert f.sections[6]=='## 财务分析'


def test_restored_preview_uses_one_evidence_generation(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.read(6);f.write(6)
    saved=f.journal.data['partial_editorials']['6']
    current=deepcopy(saved)
    old=deepcopy(next(b for b in saved if b.get('findings')))
    old['draft_generation']='historical-input'
    old['input_sha256']='historical-batch'
    f.journal.data['partial_editorials']['6']=[old]+current
    f.restore_draft_preview(6)
    assert f.journal.data['draft_previews']['6']['text'].count(FINANCE)==1


def test_recovered_first_draft_goes_to_gap_without_second_initial_read(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.read(6)
    read=f.read;passes=[]
    def tracked(number,**kwargs):
        passes.append(kwargs.get('cached_only',False));return read(number,**kwargs)
    monkeypatch.setattr(f,'read',tracked)
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert passes==[True]
    assert FINANCE in f.sections[6]


def test_interrupted_writer_finishes_same_evidence_before_any_new_read(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.read(6);f.write(6)
    f.journal.data['chapters']['6']['status']='partial_draft'
    monkeypatch.setattr(f,'read',lambda *a,**kw:pytest.fail('finish persisted draft before adding evidence'))
    monkeypatch.setattr(f,'collect',lambda *a,**kw:0)
    f.run_chapter(6)
    assert FINANCE in f.sections[6]


def test_closed_pending_review_is_not_reused_as_completed_chapter(tmp_path,monkeypatch):
    from company_flow_state import fingerprint
    from company_progressive_reading import VERSION as RESEARCH_VERSION
    from report_agents.company_editorial import editorial_policy_fingerprint,record_payload
    from report_agents.company_evidence import source_body
    from report_agents.company_information_usage import routing_scope

    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6);f.write(6)
    record=f.journal.data['chapters']['6']
    record.update(status='pending_review',round_closed=True,research_policy=RESEARCH_VERSION,
        settled_sources_sha256=fingerprint([(str(s.id),fingerprint(source_body(s))) for s in f.project.sources]),
        settled_policy_sha256=fingerprint([RESEARCH_VERSION,routing_scope(f.project),editorial_policy_fingerprint()]),
        settled_dependency_sha256=fingerprint([record_payload(r) for r in f.routed_records(6)]))
    def reached(*args,**kwargs):raise RuntimeError('pending_review_must_continue')
    monkeypatch.setattr('company_progressive_reading.plan_reading',reached)
    with pytest.raises(RuntimeError,match='pending_review_must_continue'):f.run_chapter(6)


def test_cached_draft_reads_pending_selected_originals_before_gap_search(tmp_path,monkeypatch):
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6)
    calls.clear()
    original=f.read;passes=[]
    def read(number,**kwargs):
        cached=kwargs.get('cached_only',False);passes.append(cached)
        result=original(number,**kwargs)
        if len(passes)==1 or (not cached and passes.count(False)==1):
            state=next(st for st in f.reads[number]['source_review'].values() if st.get('review_windows'))
            state['reviewed_windows']=0
        return result
    def collect(*a,**kw):
        assert passes==[True,False,False,True]
        assert FINANCE in f.sections[6]
        return 0
    monkeypatch.setattr(f,'read',read);monkeypatch.setattr(f,'collect',collect)
    f.run_chapter(6)
    assert sum(role.startswith('公司本章问题覆盖核对') for role,_ in calls)<=1
    assert f.journal.data['chapters']['6']['selected_read_complete_sha256']
    f.run_chapter(6)
    assert passes==[True,False,False,True]


def test_failed_platform_receipt_is_saved_when_project_requirements_are_none(tmp_path,monkeypatch):
    f,_,_=progressive_flow(tmp_path,monkeypatch);f.project.company_research_requirements=None
    saved=[]
    db=NS(query=lambda *a:NS(filter=lambda *a:NS(first=lambda:None)),add=saved.append,commit=lambda:None)
    assert f.workflow._save_structured_source(db,f.project,'qcc','raw_receipt','平台失败回执',
        '{"error":"quota"}',{'target':TARGET,'tool':'test_tool','collection_failure':'quota'},
        intake={**f.intake,'target_type':'company'})
    assert saved[0].source_metadata['collection_failure']=='quota'
    assert saved[0].source_metadata['company_requirement_ids']==[]


def test_budget_exhaustion_during_collection_is_interrupted_not_finished(tmp_path,monkeypatch):
    from company_flow_state import CompanyFlowLimit
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    def exhausted(*args,**kwargs):
        f.journal.data['budget_stop_reason']='elapsed_seconds'
        raise CompanyFlowLimit('company_flow_time_budget_exhausted')
    monkeypatch.setattr(f.workflow,'collect_web_sources',exhausted)
    with pytest.raises(CompanyFlowLimit):f.run_chapter(6)
    state=f.journal.data['chapters']['6']
    assert state['gap_search_done_status']=='interrupted_budget'
    assert not state['gap_search_done_attempted']
    assert not any(state['search_receipts'][-1]['request_deltas'].values())
    assert state['search_receipts'][-1]['budget_stop_reason']=='elapsed_seconds'
    assert not state.get('round_closed')


def test_parallel_search_propagates_budget_before_provider_call(monkeypatch):
    from company_flow_state import CompanyFlowLimit
    from research_workflow import ResearchWorkflow,web_search_provider
    class Exhausted:
        def query(self,*args):raise CompanyFlowLimit('company_flow_time_budget_exhausted')
    calls=[]
    monkeypatch.setattr(web_search_provider,'search',lambda *a,**k:calls.append(1))
    with pytest.raises(CompanyFlowLimit):
        ResearchWorkflow()._search_query_items_parallel([{'query':'specific missing date'}],3,[],query_cache=Exhausted())
    assert not calls


def test_structured_collector_propagates_budget_without_false_transport_failure(monkeypatch):
    from types import SimpleNamespace
    from company_flow_state import CompanyFlowLimit
    from research_workflow import ResearchWorkflow
    w=ResearchWorkflow();calls=[]
    def exhausted(*a,**k):raise CompanyFlowLimit('company_flow_time_budget_exhausted')
    monkeypatch.setattr(w,'_collect_qcc_sources',exhausted)
    monkeypatch.setattr(w,'_collect_tyc_sources',lambda *a,**k:calls.append(1) or 0)
    with pytest.raises(CompanyFlowLimit):
        w.collect_structured_sources(None,SimpleNamespace(target_type='company',target_name='Example',title='Example',description='',industry=''),
            {'target_type':'company','target_name':'Example','_company_chapter_pass':True})
    assert not calls


def test_expired_budget_does_not_consume_collection_round(tmp_path,monkeypatch):
    from company_flow_state import CompanyFlowLimit
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    f.journal.started-=f.journal.limits['elapsed_seconds']+1
    with pytest.raises(CompanyFlowLimit):f.collect(6,gap=True)
    assert not f.journal.data['chapters']['6'].get('gap_search_done_attempted')
    assert f.journal.data['metrics']['search_queries']==0


def test_platform_budget_is_not_recorded_as_provider_transport_failure():
    from types import SimpleNamespace
    from company_flow_state import CompanyFlowLimit
    from company_platform import CompanyPlatformAudit
    class Exhausted:
        def platform(self,*args):raise CompanyFlowLimit('company_flow_time_budget_exhausted')
    project=SimpleNamespace(_company_flow_journal=Exhausted(),company_professional_platform_attempts=[])
    audit=CompanyPlatformAudit(project,'qcc','test-credential',{})
    with pytest.raises(CompanyFlowLimit):audit.call(None,'https://example.invalid',{},'registration',{})
    assert not project.company_professional_platform_attempts
    assert not audit.stop_reason


@pytest.mark.parametrize('error,status',[
    ('chapter_synthesis_failed:ValueError','pending_writing'),
    ('semantic_review_failed:ValueError','pending_review'),
])
def test_synthesis_failure_stops_before_expanding_reading(tmp_path,monkeypatch,error,status):
    f,calls,events=progressive_flow(tmp_path,monkeypatch,[source(1)])
    def failed_write(number):
        f.editorials[number]={'batches':[{'errors':[error]}]}
    monkeypatch.setattr(f,'write',failed_write)
    f.run_progressive_chapter(2)
    assert f.journal.data['chapters']['2']['status']==status
    assert not any(e.get('label')=='本章已有原文补读' for e in events)
    assert not any('公司本章集中取证计划' in role for role,_ in calls)


@pytest.mark.parametrize('number',[2,3,4,5,6,9])
def test_pending_synthesis_resumes_without_reopening_reading(tmp_path,monkeypatch,number):
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source(1)])
    f.read(number,assess=False)
    f.journal.data['chapters'].setdefault(str(number),{}).update(status='pending_writing',draft_saved=False)
    f.journal.data.setdefault('partial_editorials',{})[str(number)]=[{'input_sha256':'source-bound',
        'information_ids':[],'findings':[],'synthesis':{'plan':{'saved':'paid'}}}]
    f.journal.data['shared_read_receipts']={'saved':{'response':{}}}
    monkeypatch.setattr(f,'read',lambda *a,**k:pytest.fail('reopened reading before saved writer'))
    wrote=[]
    def fail(number):
        wrote.append(number)
        f.editorials[number]={'batches':[{'errors':['chapter_synthesis_failed:ValueError']}]}
    monkeypatch.setattr(f,'write',fail)
    f.run_progressive_chapter(number)
    assert wrote==[number]
    assert f.journal.data['chapters'][str(number)]['status']=='pending_writing'


def test_cached_read_does_not_rethrow_old_question_failure_before_retry(tmp_path,monkeypatch):
    f,calls,events=progressive_flow(tmp_path,monkeypatch,[source(1)])
    f.read(2,assess=False)
    f.coverage[2]={'questions':[{'id':'Q02-1','status':'pending_review'}],'errors':['ValueError']}
    f.read(2,cached_only=True,assess=False)
    assert f.coverage[2]['errors']==['ValueError']
    f.read(2,cached_only=True,assess=True)
    assert not f.coverage[2].get('errors')


def test_semantic_preview_restore_keeps_gap_aspects_and_summary_position(tmp_path,monkeypatch):
    from company_chapters import outline
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6);f.write(6)
    rows=[r for r in f.usages[6]['records'] if r['decision']['status']=='use'];assert rows
    aspects=outline()[5][1];iid=rows[0]['information_id']
    findings=[{'finding_id':'gap','aspect':aspects[0],'content_role':'evidence_gap',
        'text':'本次核验片段未提供该项直接评价资料。','information_ids':[]},
        {'finding_id':'body','aspect':aspects[1],'text':FINANCE,'information_ids':[iid]},
        {'finding_id':'summary','aspect':'本章小结','content_role':'chapter_summary','text':'据公司披露，已有相关业务数据。','information_ids':[iid]}]
    from report_agents.company_chapter_synthesis import body_key
    findings[-1]['summary_of_sha256']=body_key(findings[:-1])
    f.journal.data['partial_editorials']['6']=[{'input_sha256':'one-generation','draft_generation':'one',
        'review_mode':'company-core-semantic-v1','aspect':'核心事实语义审稿','chapter':6,
        'information_ids':[r['information_id'] for r in rows],'findings':findings}]
    f.restore_draft_preview(6)
    text=f.journal.data['draft_previews']['6']['text']
    assert '### '+aspects[0] in text and '本次核验片段未提供' in text
    assert '### '+aspects[1] in text
    assert text.index('### 本章小结')>text.index('### '+aspects[1])
