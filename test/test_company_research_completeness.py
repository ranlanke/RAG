import json
from types import SimpleNamespace as NS

from company_chapters import outline, VERSION
from company_reading import assess_questions
from report_agents.company_research_completeness import (qualitative_assessment,
    relationship_evidence,render_relationship_graphs,graph_block)
from report_agents.company_delivery_contract import fill_evidence_gaps
from report_agents.company_corpus_writer import render_compact_chapters


def project():
    return NS(id=10,target_name='样本软件股份有限公司',sources=[],
        company_research_intake={'company_template_version':VERSION,'ai_search_enabled':True},
        company_editorial_package={'findings':[{'finding_id':'f6','chapter':6,
            'text':'公司披露2026年上半年收入同比增长8%，该披露未验证逐笔回款。',
            'evidence_ids':['E-ABCDEF']}]},company_assessment_package={'dimensions':[{'score':None}]},
        company_assessment_diagnostic={})


def test_no_local_facts_still_plans_chapter_question_search():
    result=assess_questions(7,'样本公司',[],lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert not result['complete']
    assert {q['question_id'] for q in result['follow_up_queries']}=={q['id'] for q in result['questions']}
    assert all('样本公司' in q['query'] for q in result['follow_up_queries'])


def test_unscored_assessment_keeps_supported_qualitative_judgment():
    p=project();calls=[]
    def invoke(role,prompt,trace):
        calls.append(role)
        if role=='公司定性综合评价编写':
            return json.dumps({'findings':[{'id':'a','text':'收入增长支持当期业务扩张判断，回款质量仍需逐笔凭证。','support_ids':['QF0']}]})
        return json.dumps({'findings':[{'id':'a','status':'supported','reason':'判断范围与原段一致。'}],'issues':[]})
    text=qualitative_assessment(p,'## 12. 综合评价',invoke,outline())
    assert '业务扩张判断' in text and '[E-ABCDEF]' in text
    assert '数值评分：未形成' in text
    assert len(calls)==2 and p.company_qualitative_assessment_package['batches'][0]['accepted']
    assert p.company_assessment_package['dimensions'][0]['score'] is None


def test_rejected_or_unbound_assessment_never_becomes_a_conclusion():
    for bad_support in (True,False):
        p=project()
        def invoke(role,prompt,trace):
            if role=='公司定性综合评价编写':return json.dumps({'findings':[{'id':'a','text':'整体无风险，可评90分。','support_ids':['unknown' if bad_support else 'QF0']}]})
            return json.dumps({'findings':[{'id':'a','status':'reject','reason':'原文无法支持。'}],'issues':['无评分依据']})
        text=qualitative_assessment(p,'## 12. 综合评价',invoke,outline())
        assert '整体无风险' not in text and '90分' not in text
        assert p.company_editorial_package['findings'][0]['text'] in text
        assert p.company_qualitative_assessment_package['errors']


def test_non_registry_common_partner_relationship_is_read_and_reviewed():
    p=project();quote='截至2026年6月30日，甲合伙企业与乙合伙企业的普通合伙人均为丙管理有限公司。'
    p.company_source_review_package={'facts':[{'fact':quote,'evidence_id':'E-ABCDEF','chapter':10}]}
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司关系图原文关系提取':return json.dumps({'edges':[
            {'id':name,'source':name,'target':'丙管理有限公司','relation':'普通合伙人为','period':'2026年6月30日',
             'kind':'relationship','support_id':data['records'][0]['id'],'quote':quote} for name in ('甲合伙企业','乙合伙企业')]})
        return json.dumps({'edges':[{'id':e['id'],'status':'supported','reason':'原文明示共同节点，未推断双方控制。'} for e in data['edges']]})
    p.company_relationship_package=relationship_evidence(p,invoke)
    assert len(p.company_relationship_package['edges'])==2
    saved={};store=NS(put_bytes=lambda k,b,m:saved.update({k:b}))
    block=render_relationship_graphs(p,store)
    assert '甲合伙企业' in block and '乙合伙企业' in block and '丙管理有限公司' in block
    assert '[E-ABCDEF]' in block and len(saved)==1
    assert '实际控制人' not in ''.join(v.decode() for v in saved.values())


def test_relationship_graphs_paginate_without_ten_node_drop_and_survive_repair():
    p=project();p.company_relationship_package={'edges':[
        {'source':'股东'+str(i),'target':p.target_name,'relation':'披露持股1.00%，非实控结论',
         'period':'2026年6月30日','kind':'equity','evidence_ids':['E-ABCDEF']} for i in range(19)]}
    saved={};block=render_relationship_graphs(p,NS(put_bytes=lambda k,b,m:saved.update({k:b})))
    assert len(saved)==5 and '股东18' in block
    assert sum(len(a['edges']) for a in p.company_relationship_package['assets'])==19
    original={2:'## 2. 工商\n\n'+block,6:'## 6. 经营'}
    rebuilt=render_compact_chapters(p,{'groups':[]},outline(),original)
    assert graph_block(rebuilt[2])==block


def test_sparse_chapter_shows_exact_gaps_and_real_failed_collection_without_filler():
    p=project();p.company_question_coverage={8:{'questions':[{'id':'Q8','status':'partial',
        'question':'核心研发人员流失情况如何？','missing_fields':['研发岗位离职人数与平均在册人数'],
        'next_step':'向公司取得2026年研发岗位离职台账和月均在册分母。'}]}}
    p._company_flow_journal=NS(data={'chapters':{'8':{'search_receipts':[{'phase':'initial',
        'queries':[{'query':'样本软件 2026 员工 离职 研发'}],'added_sources':0,'status':'failed'}]}}})
    sections={8:'## 8. 人员\n\n公司披露研发员工数量。[E-ABCDEF]'}
    result=fill_evidence_gaps(sections,{},outline(),project=p)
    assert '公司披露研发员工数量' in result[8]
    assert '研发岗位离职人数与平均在册人数' in result[8]
    assert '向公司取得2026年研发岗位离职台账' in result[8]
    assert '调用失败' in result[8] and '样本软件 2026 员工 离职 研发' in result[8]
    assert '整体风险较低' not in result[8]
    assert fill_evidence_gaps(result,{},outline(),project=p)==result


def test_graph_is_embedded_in_word_with_native_aspect_and_no_internal_markers(monkeypatch):
    from io import BytesIO
    from zipfile import ZipFile
    from xml.etree import ElementTree as ET
    import docx_exporter
    p=project();p.company_relationship_package={'edges':[{'source':'股东甲','target':p.target_name,
        'relation':'持股51.38%，不由此推定实控','period':'2026年6月30日',
        'kind':'equity','evidence_ids':['E-ABCDEF']}]}
    saved={};store=NS(put_bytes=lambda k,b,m:saved.update({k:b}),get_bytes=lambda k:saved[k])
    block=render_relationship_graphs(p,store)
    monkeypatch.setattr(docx_exporter,'research_store',store)
    from report_agents.company_delivery_contract import validate_docx
    report='\n\n'.join('## '+str(i)+'. '+title+ ('\n\n'+block if i==2 else
        '\n\n| 来源编号 | 原件 |\n| --- | --- |\n| E-ABCDEF | 测试原件 |' if i==13 else '')
        for i,(title,_) in enumerate(outline(),1))
    word=docx_exporter.markdown_to_docx_bytes(report,outline=outline(),document_kind='company_report')
    assert validate_docx(word,report,outline())['docx_integrity_status']=='passed'
    with ZipFile(BytesIO(word)) as z:
        xml=z.read('word/document.xml').decode();root=ET.fromstring(xml)
        assert 'company-governance-graphs' not in xml
        media=[n for n in z.namelist() if n.startswith('word/media/')]
        assert len(media)==1 and z.read(media[0])==next(iter(saved.values()))
        extent=root.find('.//{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent')
        svg=ET.fromstring(z.read(media[0]));ratio=float(svg.get('height'))/float(svg.get('width'))
        assert abs(int(extent.get('cy'))/int(extent.get('cx'))-ratio)<0.0001


def test_semantic_relation_graph_does_not_discard_platform_visual_evidence(monkeypatch):
    import research_workflow
    p=project();p.target_type='company'
    p.sources=[NS(title='企业关系原件',source_type='qcc',source_metadata={
        'provider':'qcc','images':[{'object_key':'projects/10/visual_evidence/platform.png'}]})]
    p.company_relationship_package={'edges':[{'source':'股东甲','target':p.target_name,
        'relation':'披露股东','period':'2026年6月30日','kind':'equity','evidence_ids':['E-ABCDEF']}]}
    monkeypatch.setattr(research_workflow.research_store,'put_bytes',lambda *a:None)
    w=research_workflow.ResearchWorkflow.__new__(research_workflow.ResearchWorkflow)
    monkeypatch.setattr(w,'_is_structured_professional_source',lambda s:True)
    monkeypatch.setattr(w,'_structured_source_is_safe',lambda s:True)
    text=w._append_professional_evidence_assets(p,'## 2. 工商\n\n## 3. 历史','{}')
    assert '股权结构分析示意图' in text and '(projects/10/visual_evidence/platform.png)' in text


def test_existing_structured_graphs_also_survive_body_repair_and_repeat_insertion():
    from research_workflow import ResearchWorkflow
    from report_agents.company_delivery_contract import split_sections
    w=ResearchWorkflow.__new__(ResearchWorkflow);p=project()
    report='## 2. 工商\n\n已核对原始登记记录。\n\n## 3. 历史'
    rendered=w._insert_company_governance_graphs(report,'projects/10/visual_evidence/old-equity.svg','')
    assert w._insert_company_governance_graphs(rendered,'projects/10/visual_evidence/old-equity.svg','')==rendered
    sections=render_compact_chapters(p,{'groups':[]},outline(),split_sections(rendered))
    assert '(projects/10/visual_evidence/old-equity.svg)' in sections[2]


def test_native_reading_groups_complete_windows_without_dropping_context_or_ids():
    from report_agents.company_corpus import _batches
    windows=[{'window_id':'W'+str(i),'quote':'原文'+str(i)+'。'*500,
        'context_quote':'相邻原文'*900,'start':i*501,'end':i*501+501,
        'native_window_sha256':'window-proof-'+str(i)} for i in range(24)]
    original={'evidence_id':'E-ABCDEF','source_id':'1','model_reading_mode':'native-test','windows':windows}
    batches=list(_batches([original]))
    retained=[w for batch in batches for doc in batch for w in doc['windows']]
    assert retained==windows
    assert len(batches)<len(windows)/2
    assert all(len(json.dumps(batch,ensure_ascii=False))<32000 for batch in batches)
    assert any(len(doc['windows'])>=4 for batch in batches for doc in batch)


def test_completeness_receipts_expire_with_chapter_review_policy_and_request_scope(monkeypatch):
    import company_chapters
    from report_agents.company_research_completeness import _invoke
    p=project();p._company_flow_journal=NS(data={},save=lambda:None);calls=[]
    def invoke(*a):calls.append(1);return '{"status":"checked"}'
    _invoke(p,invoke,'独立核对','相同输入');_invoke(p,invoke,'独立核对','相同输入')
    assert len(calls)==1
    monkeypatch.setattr(company_chapters,'CONTRACT_SHA256','changed-review-contract')
    _invoke(p,invoke,'独立核对','相同输入');assert len(calls)==2
    p.company_research_intake['special_requirements']='新增审稿要求：分别陈述披露与独立验证。'
    _invoke(p,invoke,'独立核对','相同输入');assert len(calls)==3


def test_question_checks_only_repeat_the_native_context_for_their_actual_batch():
    facts=[{'fact':str(i)+'注册资本原件字段与主体期间。'*90,'source_url':'https://example.test/'+str(i%3),
        'evidence_id':'E-ABCDEF'+str(i)} for i in range(43)]
    originals=[NS(id=i,url='https://example.test/'+str(i),content='原始正文'*20000,
        summary='',source_metadata={'content_kind':'full_text'}) for i in range(3)]
    calls=[];seen=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(len(prompt))
        if role in {'公司本章问题跨批综合核对','公司本章补采计划独立核对'}:
            return json.dumps({'questions':[{'id':q['id'],'status':'missing','support_ids':[],
                'missing_fields':['需要对应期间原件'],'reason':'各批原文均未回答该问题。'} for q in data['questions']]})
        ids={p['id'] for p in data['proofs']};seen.extend(ids)
        supplied={r['information_id'] for s in data['evidence_context']['sources'] for r in s['native_excerpts']}
        assert supplied==ids
        return json.dumps({'questions':[{'id':q['id'],'status':'missing','support_ids':[],
            'missing_fields':['需要对应期间原件'],'reason':'本批不能回答该问题。'} for q in data['questions']]})
    result=assess_questions(2,'样本公司',facts,invoke,originals=originals)
    assert not result.get('errors') and len(calls)>=3
    assert len(seen)==len(result['proofs'])==len(facts)


def test_question_coverage_rechecks_when_the_requested_cutoff_changes():
    calls=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(data['request_scope'])
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[data['proofs'][0]['id']],
            'missing_fields':['需要截至日适用的登记原件'],'reason':'该材料只支持原披露时点。'} for q in data['questions']]})
    facts=[{'fact':'公司2026年5月披露其登记地址。','source_url':'https://original.test'}]
    first=assess_questions(2,'样本公司',facts,invoke,research_scope={'as_of_date':'2026-06-30'})
    second=assess_questions(2,'样本公司',facts,invoke,previous=first,research_scope={'as_of_date':'2026-11-05'})
    assert len(calls)==2 and first['input_sha256']!=second['input_sha256']
    assess_questions(2,'样本公司',facts,invoke,previous=second,research_scope={'as_of_date':'2026-11-05'})
    assert len(calls)==2
