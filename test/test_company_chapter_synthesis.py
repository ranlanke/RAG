import json
from copy import deepcopy
import pytest

from report_agents.company_chapter_synthesis import synthesize, validate_plan, contract_for
from report_agents.company_semantic_review import build_package, package_errors
from test_company_semantic_review import passing


@pytest.fixture(autouse=True)
def strict_review_path(monkeypatch):
    # Retain the original synthesis/strict-review contract assertions.
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')


def fixture(target='海岳设备有限公司', section='工商信息', number=2):
    rows=[{'information_id':'S1','text':target+'登记资本100万元。','context':{},'provenance':[],
           'evidence_ids':['E-123456'],'decision':{'status':'use','chapter':number,'aspect':section}}]
    contract={'chapter':number,'title':section,'sections':[section],'questions':[],'requirements':'','chapter_summary_required':True}
    plan={'questions':[{'question_id':'Q1','aspect':section,'question':'本节已有何种依据？','source_ids':['S1'],
        'status':'supported','assessment':'已取得登记字段，不能因另份材料无该字段认定缺失。'}],
        'source_handling':[{'information_id':'S1','disposition':'main','reason':'支持本节'}]}
    draft={'findings':[{'finding_id':'P1','aspect':section,'text':rows[0]['text'],'information_ids':['S1']}],
        'chapter_summary':{'text':rows[0]['text'],'information_ids':['S1']}}
    return rows,contract,plan,draft


@pytest.mark.parametrize('target,number', [('海岳设备有限公司',2),('远山软件公司',6),('新桥工程公司',9)])
def test_bounded_sections_keep_format_and_assessment_with_their_evidence(monkeypatch,target,number):
    from report_agents.company_bounded_editorial import bounded_draft
    from report_agents.company_chapter_synthesis import validate_draft
    import report_agents.company_bounded_editorial as bounded
    sections=['业务数据','交付记录']
    rows=[{'information_id':'S'+str(i),'text':text,'context':{},'provenance':[],
        'evidence_ids':[],'decision':{'status':'use','chapter':number,'aspect':sections[i//2]}}
        for i,text in enumerate(['会计期间为2025年。','收入100万元。','2026年完成交付。'])]
    contract={'chapter':number,'title':'业务核验','sections':sections,'questions':[],
        'requirements':'','chapter_summary_required':False,'section_formats':dict.fromkeys(sections,'table')}
    plan={'questions':[{'question_id':'Q'+str(i),'aspect':aspect,'question':'核验本节业务情况',
        'source_ids':[r['information_id'] for r in rows if r['decision']['aspect']==aspect],
        'status':'supported','assessment':('仅全量证据才能支持的跨材料结论' if i==0 else '2026年完成交付。')}
        for i,aspect in enumerate(sections)],'source_handling':[{'information_id':r['information_id'],
            'disposition':'main','reason':'本节依据'} for r in rows]}
    before=deepcopy(plan);state={'plan':plan};seen=[]
    monkeypatch.setattr(bounded,'stable_batches',lambda values,*a,**kw:[[v] for v in values])
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);seen.append((role,data))
        sub=data['contract'];assert len(sub['sections'])==1
        assert sub['section_formats']=={sub['sections'][0]:'table'}
        if 'partial_drafts' in data:
            return {'findings':[f for d in data['partial_drafts'] for f in d['findings']]}
        q=data['plan']['questions'][0]
        if sub['sections']==[sections[0]]:
            assert '仅全量证据才能支持的跨材料结论' not in prompt
            assert q['status']=='unread'
        else:assert q['assessment']=='2026年完成交付。'
        ids=data['output_id_registry']['allowed_information_ids']
        row=next(r for r in rows if r['information_id']==ids[0])
        return {'findings':[{'finding_id':'P1','aspect':sub['sections'][0],
            'text':'| 事项 | 依据 |\n| --- | --- |\n| 核验 | '+row['text']+' |','information_ids':ids}]}
    draft=bounded_draft(rows,target,contract,call,state,lambda:None)
    assert {i for f in draft['findings'] for i in f['information_ids']}=={'S0','S1','S2'}
    assert plan==before and contract['section_formats']==dict.fromkeys(sections,'table')
    validate_draft(draft,rows,contract)
    with pytest.raises(ValueError,match='incomplete_synthesis_draft'):
        validate_draft({'findings':[f for f in draft['findings'] if f['aspect']==sections[0]]},rows,contract)
    def forbidden(*args):pytest.fail('completed bounded tasks must be reused')
    assert bounded_draft(rows,target,contract,forbidden,state,lambda:None)==draft


def test_chapter_six_required_tables_survive_format_upgrade_without_rewriting():
    from company_chapters import outline
    from report_agents.company_chapter_synthesis import input_key,has_markdown_table
    actual=contract_for(6,outline(),'任意企业')
    assert actual['section_formats']=={'分部门业务运营数据核验表':'table','核心业务板块运营进展核验表':'table'}
    rows,old,plan,draft=fixture(section='分部门业务运营数据核验表',number=6)
    old['chapter_summary_required']=False
    table='| 指标 | 数值 |\n| --- | --- |\n| 收入 | '+('原文依据'*5000)+' |'
    expanded={'findings':[{**draft['findings'][0],'text':table}]}
    prior={'input_sha256':input_key(rows,'任意企业',old),'contract':old,'plan':plan,
        'draft':draft,'expanded_draft':expanded,'attempts':{},'bounded_tasks':{'saved':{}},
        'body_reduction_policy':'question-focused-v2'}
    before=deepcopy(prior);new={**old,'section_formats':{old['sections'][0]:'table'}}
    def forbidden(*a):pytest.fail('valid existing table must not be rewritten')
    restored=synthesize(rows,'任意企业',new,forbidden,prior)
    assert restored['draft']==expanded and has_markdown_table(restored['draft']['findings'][0]['text'])
    assert restored['restored_format_draft_from']==prior['input_sha256'] and prior==before
    assert synthesize(rows,'任意企业',new,forbidden,restored)==restored


@pytest.mark.parametrize('changed',['source','requirements','missing_table'])
def test_format_upgrade_never_bypasses_changed_evidence_or_invalid_draft(changed):
    from report_agents.company_chapter_synthesis import input_key
    rows,old,plan,draft=fixture(section='分部门业务运营数据核验表',number=6)
    old['chapter_summary_required']=False
    table={'findings':[{**draft['findings'][0],'text':'| 指标 | 数值 |\n| --- | --- |\n| 资本 | 100万元 |'}]}
    prior={'input_sha256':input_key(rows,'任意企业',old),'contract':old,'plan':plan,'draft':draft,
        'expanded_draft':deepcopy(table),'attempts':{}}
    new={**old,'section_formats':{old['sections'][0]:'table'}}
    if changed=='source':rows[0]['text']+='新的原件内容。'
    elif changed=='requirements':new['requirements']='新增核验要求'
    else:prior['expanded_draft']=draft
    calls=[]
    def call(role,prompt):calls.append(role);return plan if role=='公司章节证据整合' else table
    result=synthesize(rows,'任意企业',new,call,prior)
    assert calls==['公司章节证据整合','公司章节整合写作']
    assert not result.get('restored_format_draft_from')


def test_chapter_four_actual_grants_reach_writer_with_source_binding():
    # Literal excerpt from source 70, 2026 restricted-stock plan draft, p. 3.
    quote=('2025 年 6 月 4 日，公司向 198 名激励对象首次授予 279.10 万股第二类限制性股票；'
           '2025 年 12 月 29 日，公司向 47 名激励对象授予预留 66.90 万股第二类限制性股票。')
    row={'information_id':'I-actual-grants','text':quote,'context':{'source_id':'70'},
         'provenance':[{'source_id':'70','original_offset':1536,'source_url':
             'http://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2026/2026-4/2026-04-24/12163136.PDF'}],
         'evidence_ids':['E-8BCC90F1C140'],
         'decision':{'status':'use','chapter':4,'aspect':'内部激励'}}
    contract={'chapter':4,'title':'组织治理与运营管理','sections':['内部激励'],
        'questions':['激励、应变与组织创新有何具体执行证据？'],'requirements':'','chapter_summary_required':False}
    plan={'questions':[{'question_id':'Q1','aspect':'内部激励','question':contract['questions'][0],
        'source_ids':[row['information_id']],'status':'supported','assessment':'有实际授予记录。'}],
        'source_handling':[{'information_id':row['information_id'],'disposition':'main','reason':'实际执行结果'}]}
    draft={'findings':[{'finding_id':'P1','aspect':'内部激励','text':quote,
        'information_ids':[row['information_id']]}]}
    seen=[]
    def call(role,prompt):
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);seen.append((role,prompt,packet))
        return plan if role=='公司章节证据整合' else draft
    synthesize([row],'北京金山办公软件股份有限公司',contract,call)
    writer=next((prompt,packet) for role,prompt,packet in seen if role=='公司章节整合写作')
    source=writer[1]['sources'][0]
    text=source['text'] if isinstance(source['text'],str) else writer[1]['texts'][source['text']['text_ref']]
    assert all(token in text for token in ('198','279.10','47','66.90'))
    assert source['evidence_ids']==['E-8BCC90F1C140']
    assert writer[1]['provenance'][source['provenance_id']][0]['source_id']=='70'
    assert '已实施的授予、归属等结果须与拟实施计划分别表达' in writer[0]
    from report_agents.company_semantic_review import review_prompt
    assert '来源已有重要执行结果而正文只写拟实施计划' in review_prompt([row],draft['findings'],'北京金山办公软件股份有限公司')


def test_explicit_evidence_gap_does_not_require_a_fabricated_citation():
    from report_agents.company_chapter_synthesis import validate_draft
    rows,c,plan,draft=fixture()
    c['sections'].append('应变能力')
    gap={'finding_id':'G1','aspect':'应变能力','content_role':'evidence_gap',
         'text':'本次已核材料不足以评价应变能力。','information_ids':[]}
    draft['findings'].append(gap)
    assert validate_draft(draft,rows,c)[-1]==gap
    del gap['content_role']
    with pytest.raises(ValueError,match='unbound_synthesis_paragraph'):validate_draft(draft,rows,c)


@pytest.mark.parametrize('target,number,section,status',[
    ('海岳设备有限公司',2,'工商信息','supported'),
    ('星川生物股份有限公司',2,'控制关系','conflicting'),
    ('青禾餐饮合伙企业',2,'历史变更','not_obtained'),
    ('北辰物流有限公司',6,'供应链稳定性','unread'),
])
def test_different_targets_chapters_and_evidence_states(target,number,section,status):
    rows,c,plan,draft=fixture(target,section,number)
    plan['questions'][0]['status']=status
    calls=[];saved=[]
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);assert data['target']==target and data['contract']==c
        assert data['output_id_registry']=={'allowed_information_ids':['S1'],
            'required_question_slots':[{'question_id':'Q1','aspect':section}]}
        assert '金山' not in prompt and '51.38' not in prompt
        if '写作' in role:
            assert '本次仅交付小结' not in prompt
            assert '本次只写各小节正文' in prompt and '"chapter_summary":' not in prompt
            assert '资料不足段落列出实际检查过' in prompt
            assert '不能把部分窗口未见扩大为' in prompt
            assert '"content_role":"body或evidence_gap' in prompt
        calls.append(role)
        return plan if '证据整合' in role else draft
    state=synthesize(rows,target,c,call,save=saved.append)
    assert len(calls)==3 and state['plan']['questions'][0]['status']==status
    assert calls[-1]=='公司章节小结补写' and state['initial_writer_summary']==draft['chapter_summary']
    assert synthesize(rows,target,c,lambda *a:pytest.fail('cache miss'),state)==state
    changed_usage=deepcopy(rows);changed_usage[0]['uses']=[{'surface':'chapter_narrative'}]
    assert synthesize(changed_usage,target,c,lambda *a:pytest.fail('usage metadata invalidated evidence'),state)==state
    changed=deepcopy(rows);changed[0]['text']+='新资料'
    synthesize(changed,target,c,call,state)
    # Changed evidence rechecks the plan/body; identical resulting prose reuses
    # its summary task instead of charging for the same prompt again.
    assert len(calls)==5 and saved[0]['attempts']=={'plan':1}


def test_partial_plan_resumes_without_repeating_and_failures_are_bounded():
    rows,c,plan,draft=fixture();saved=[]
    def interrupted(role,prompt):
        if '写作' in role:raise RuntimeError('simulated disconnect')
        return plan
    with pytest.raises(RuntimeError):synthesize(rows,'海岳设备有限公司',c,interrupted,save=saved.append)
    calls=[]
    out=synthesize(rows,'海岳设备有限公司',c,lambda role,prompt:calls.append(role) or draft,saved[-1])
    assert len(calls)==2 and out['attempts']=={'plan':1,'draft':2,'summary':1}
    saved=[];calls=[]
    with pytest.raises(ValueError,match='attempts_exhausted'):
        synthesize(rows,'海岳设备有限公司',c,lambda *a:calls.append(a) or {},save=saved.append)
    assert len(calls)==2
    with pytest.raises(ValueError,match='attempts_exhausted'):
        synthesize(rows,'海岳设备有限公司',c,lambda *a:pytest.fail('budget reset'),saved[-1])


def test_plan_rejects_missing_sources_sections_and_cross_target_reuses():
    rows,c,plan,draft=fixture()
    bad=deepcopy(plan);bad['questions'][0]['source_ids']=['invented']
    with pytest.raises(ValueError,match='unbound'):validate_plan(bad,rows,c)
    bad=deepcopy(plan);bad['source_handling']=[]
    with pytest.raises(ValueError,match='incomplete_synthesis_sources'):validate_plan(bad,rows,c)
    state=synthesize(rows,'甲',c,lambda role,prompt:plan if '证据整合' in role else draft)
    calls=[]
    synthesize(rows,'乙',c,lambda role,prompt:calls.append(role) or (plan if '证据整合' in role else draft),state)
    assert len(calls)==3


def test_shared_editorial_path_synthesizes_reviews_and_zero_call_replays():
    from report_agents.company_editorial import build_editorial_package
    rows,c,plan,draft=fixture();outline=[('摘要',[]),(c['title'],c['sections'])];calls=[]
    def invoke(role,prompt):
        calls.append(role)
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '整合写作' in role or '小结补写' in role:return json.dumps(draft,ensure_ascii=False)
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);assert data['chapter_synthesis']['plan']==plan
        return json.dumps(passing(data),ensure_ascii=False)
    import os
    old=os.environ.get('COMPANY_EDITORIAL_REVIEW_MODE')
    os.environ['COMPANY_EDITORIAL_REVIEW_MODE']='semantic_core'
    try:
        out=build_editorial_package({'records':rows},outline,'海岳设备有限公司',invoke)
        assert out['batches'][0]['accepted'] and len(calls)==4 and out['findings'][0]['aspect']=='工商信息'
        assert not package_errors(out,rows,'海岳设备有限公司')
        again=build_editorial_package({'records':rows},outline,'海岳设备有限公司',lambda *a:pytest.fail('cache miss'),previous=out)
        assert again==out
        other=[('摘要',[]),('新模板',['新字段'])]
        changed=build_package({'records':rows},'海岳设备有限公司',invoke,out,outline=other)
        assert not changed['batches'][0]['accepted']
    finally:
        if old is None:os.environ.pop('COMPANY_EDITORIAL_REVIEW_MODE',None)
        else:os.environ['COMPANY_EDITORIAL_REVIEW_MODE']=old


def test_saved_draft_not_applied_at_interruption_is_not_mistaken_for_old_prose():
    rows,c,plan,draft=fixture();outline=[('摘要',[]),(c['title'],c['sections'])];receipts=[]
    class Stop(Exception):pass
    def invoke(role,prompt):
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '整合写作' in role or '小结补写' in role:return json.dumps(draft,ensure_ascii=False)
        return json.dumps(passing(json.loads(prompt.rsplit('资料：\n',1)[1])),ensure_ascii=False)
    def save(receipt):
        receipts.append(receipt)
        if receipt.get('synthesis',{}).get('draft'):raise Stop()
    with pytest.raises(Stop):
        build_package({'records':rows},'海岳设备有限公司',invoke,receipt_callback=save,outline=outline)
    partial={'batches':[receipts[-1]]}
    out=build_package({'records':rows},'海岳设备有限公司',invoke,partial,outline=outline)
    assert out['batches'][0]['accepted'] and out['findings'][0]['text']==draft['findings'][0]['text']


def test_evidence_packet_is_lossless_and_mermaid_citations_do_not_break_fence():
    from report_agents.company_chapter_synthesis import evidence_packet
    from report_agents.company_editorial import digest_groups
    rows,_,_,_=fixture();long='完整上下文及限制条件。'*40
    rows[0]['context']={'context_quote':long};rows[0]['provenance']=[{'native_quote':long}]
    packet=evidence_packet(rows)
    cid=packet['sources'][0]['context_id'];pid=packet['sources'][0]['provenance_id']
    cref=packet['contexts'][cid]['context_quote']['text_ref'];pref=packet['provenance'][pid][0]['native_quote']['text_ref']
    assert cref==pref and packet['texts'][cref]==long
    f={'finding_id':'P1','chapter':2,'aspect':'关系图','text':'```mermaid\ngraph TD\n A[公司]\n```',
        'information_ids':['S1'],'statement_ids':[],'evidence_ids':['E-123456']}
    summary=digest_groups({'findings':[f]})[0]['summary']
    assert '```\n\n[E-' in summary and '```[E-' not in summary


def test_unsent_budget_stop_does_not_consume_synthesis_attempt():
    from company_flow_state import CompanyFlowLimit
    rows,c,plan,draft=fixture();saved=[]
    def stopped(*args):raise CompanyFlowLimit('company_flow_estimated_cost_budget_exhausted')
    with pytest.raises(CompanyFlowLimit):synthesize(rows,'海岳设备有限公司',c,stopped,save=saved.append)
    assert saved[-1]['attempts']['plan']==0
    saved=[]
    def stopped_while_saving(state):
        saved.append(state)
        if state['attempts']['plan']==1:raise CompanyFlowLimit('stopped_before_request')
    with pytest.raises(CompanyFlowLimit):synthesize(rows,'海岳设备有限公司',c,lambda *a:pytest.fail('must not send'),save=stopped_while_saving)
    assert saved[-1]['attempts']['plan']==0


def test_review_discovered_support_is_cited_without_rewriting_reviewed_text():
    from types import SimpleNamespace
    from report_agents.company_editorial import digest_groups
    from report_agents.company_corpus_writer import render_compact_chapters
    rows,c,_,_=fixture();extra=deepcopy(rows[0]);extra.update(information_id='S2',evidence_ids=['E-ABCDEF'])
    f={'finding_id':'P1','chapter':2,'aspect':c['sections'][0],'text':'两个来源的资本口径需要核对。',
       'information_ids':['S1'],'statement_ids':[],'evidence_ids':['E-123456']}
    package={'findings':[f],'batches':[{'accepted':True,'review_mode':'company-core-semantic-v1','semantic_review':{
        'paragraphs':[],'critical_checks':[{'paragraph_id':'P1','source_ids':['S1','S2'],'status':'pass'}]}}]}
    groups=digest_groups(package);before=deepcopy(groups)
    project=SimpleNamespace(target_name='海岳设备有限公司',company_information_usage_package={'records':rows+[extra]})
    body=render_compact_chapters(project,{'groups':groups},[('摘要',[]),(c['title'],c['sections'])],{2:''})[2]
    assert f['text'] in body and '[E-123456]' in body and '[E-ABCDEF]' in body and groups==before


def test_no_usable_evidence_returns_empty_receipt_without_call_or_fake_review(monkeypatch):
    from report_agents.company_editorial import build_editorial_package
    monkeypatch.setenv('COMPANY_EDITORIAL_REVIEW_MODE','semantic_core')
    out=build_editorial_package({'records':[]},[('摘要',[]),('待核章节',['主体信息'])],'尚无资料的公司',
        lambda *a:pytest.fail('empty evidence must not invoke model'))
    assert out['batches']==[] and out['findings']==[] and out['diagnostic']['usable_information']==0


def test_missing_plan_decisions_are_completed_without_repeating_full_plan():
    from report_agents.company_chapter_synthesis import input_key
    rows,c,plan,draft=fixture();c['chapter_summary_required']=False
    rows=[{**deepcopy(rows[0]),'information_id':'S'+str(i)} for i in range(1,27)]
    previous={'input_sha256':input_key(rows,'任意企业',c),'contract':c,'attempts':{'plan':2},
        'failed_responses':[{'stage':'plan','error':'incomplete_synthesis_sources','answer':plan}]}
    calls=[];saved=[]
    def call(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert role!='公司章节证据整合'
        if role=='公司章节证据用途补全':
            batch=data['evidence']['sources'];assert len(batch)<=12
            return {'source_handling':[{'information_id':r['information_id'],'disposition':'background','reason':'补充证据'} for r in batch]}
        return {'findings':draft['findings']}
    result=synthesize(rows,'任意企业',c,call,previous,save=saved.append)
    assert len(result['plan']['source_handling'])==26
    assert result['plan']['questions']==plan['questions']
    assert calls.count('公司章节证据用途补全')==3
    assert result['attempts']['plan']==2
    assert synthesize(rows,'任意企业',c,lambda *a:pytest.fail('unchanged replay called model'),result)==result


def test_source_repair_budget_pause_does_not_spend_model_attempt():
    from report_agents.company_chapter_synthesis import complete_plan_sources
    from company_flow_state import CompanyFlowLimit
    rows,c,plan,_=fixture();plan['source_handling']=[];state={}
    def blocked(*args):raise CompanyFlowLimit('budget')
    with pytest.raises(CompanyFlowLimit):complete_plan_sources(plan,rows,c,blocked,state,lambda:None)
    assert all(r['attempts']==0 for r in state['source_handling_repairs'].values())
