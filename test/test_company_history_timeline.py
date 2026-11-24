import json
from copy import deepcopy
from types import SimpleNamespace
from xml.etree import ElementTree
import pytest
from report_agents.company_history_timeline import POLICY, validate_timeline, timeline_finding, render_svg
from report_agents.company_semantic_review import build_package, package_errors, prepare_findings
from report_agents.company_chapter_synthesis import contract_for
from report_agents.company_editorial import digest_groups
from report_agents.company_corpus_writer import render_compact_chapters
from test_company_semantic_review import passing
from report_agents.company_history_timeline import date_window, ordered_timeline


@pytest.fixture(autouse=True)
def strict_review_path(monkeypatch):
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')


@pytest.mark.parametrize('target',['未上市制造企业','另一家服务公司'])
def test_unsorted_real_dates_share_axis_and_preserve_causal_ids(target):
    rows,data,outline,_,_=sample(target)
    dates=['2011年12月20日','2011年12月29日','2011年8月18日/8月24日','2010-12-31']
    data['events']=[{**deepcopy(data['events'][i%2]),'event_id':f'T{i}',
        'date_label':label} for i,label in enumerate(dates)]
    data['causal_links']=[{'from_event':'T3','to_event':'T0','reason':'原文明确因果','information_ids':['S1']}]
    original=deepcopy(data);ordered=ordered_timeline(data)
    assert [e['event_id'] for e in ordered['events']]==['T3','T2','T0','T1']
    assert ordered['causal_links']==original['causal_links'] and data==original
    assert ordered_timeline(ordered)==ordered
    finding=timeline_finding(data,rows,contract_for(3,outline,target))
    assert finding['timeline_data']==ordered
    svg=render_svg(data,rows);tree=ElementTree.fromstring(svg)
    labels={e.text:float(e.attrib['x']) for e in tree.findall('{*}text') if e.text in dates}
    assert labels[dates[3]]<labels[dates[2]]<labels[dates[0]]<labels[dates[1]]
    assert 'marker-end=' in svg


def test_partial_overlapping_unknown_dates_do_not_claim_strict_order():
    rows,data,_,_,_=sample('不同目标')
    dates=['时间不详','2022年5月','2020年','2022-05-08','2022年5月8日','2021—2022年']
    data['events']=[{**deepcopy(data['events'][i%2]),'event_id':f'T{i}',
        'date_label':label} for i,label in enumerate(dates)]
    ordered=ordered_timeline(data)
    assert ordered['events'][0]['sort_date']=='2020'
    assert ordered['events'][-1]['sort_date'] is None
    overlapping=[e for e in ordered['events'] if e.get('time_group')]
    assert len(overlapping)==4 and len({e['time_group'] for e in overlapping})==1
    svg=render_svg(data,rows)
    assert '组内先后未定' in svg and '时间待定位' in svg
    assert date_window('2022年5月')[2]=='2022-05'
    assert date_window('2020年2月29日')[2]=='2020-02-29'
    assert date_window('2021年2月29日') is None
    assert date_window('近日') is None


def test_model_sort_date_cannot_invent_precision_or_override_label():
    rows,data,_,_,_=sample('通用目标')
    data['events'][0]['sort_date']='2020-01-01'
    with pytest.raises(ValueError,match='sort_date_not_in_label'):validate_timeline(data,rows)
    data['events'][0]['sort_date']='2020'
    validate_timeline(data,rows)


def sample(target):
    aspects=['历史工商变更','业务发展历程与关键节点']
    rows=[{'information_id':'S1','text':target+'2020年设立，2022年完成产品升级。',
        'context':{},'provenance':[],'evidence_ids':['E-123456'],
        'decision':{'status':'use','chapter':3,'aspect':aspects[0]}}]
    data={'decision':'render','reason':'设立与业务调整均有证据','events':[
        {'event_id':'T1','track':'registry','date_label':'2020年','date_kind':'event','entity':target,
         'label':'设立','status':'完成','note':'','information_ids':['S1']},
        {'event_id':'T2','track':'business_capital','date_label':'2022年','date_kind':'event','entity':target,
         'label':'产品升级','status':'完成','note':'','information_ids':['S1']}], 'causal_links':[]}
    outline=[('摘要',[]),('工商',[]),('历史',aspects)]+[('其他',[]) for _ in range(10)]
    plan={'questions':[{'question_id':str(i),'aspect':a,'question':a,'source_ids':['S1'],
        'status':'supported','assessment':rows[0]['text']} for i,a in enumerate(aspects)],
        'source_handling':[{'information_id':'S1','disposition':'main','reason':'直接依据'}]}
    draft={'findings':[{'finding_id':'P'+str(i),'aspect':a,'text':rows[0]['text'],'information_ids':['S1']}
        for i,a in enumerate(aspects)],'chapter_summary':{'text':rows[0]['text'],'information_ids':['S1']},'history_timeline':data}
    return rows,data,outline,plan,draft


@pytest.mark.parametrize('target',['青禾餐饮有限公司','星川设备合伙企业'])
@pytest.mark.parametrize('decision',['render','omit'])
@pytest.mark.parametrize('profile',['strict_core','default'])
def test_generic_real_workflow_and_zero_call_replay(target,decision,profile,monkeypatch):
    if profile=='default':monkeypatch.delenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE')
    rows,data,outline,plan,draft=sample(target)
    if decision=='omit':data.update(decision='omit',reason='关键节点不足',events=[],causal_links=[])
    calls=[]
    def invoke(role,prompt):
        calls.append(role)
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '小结补写' in role:return json.dumps({'chapter_summary':draft['chapter_summary']},ensure_ascii=False)
        if '整合写作' in role:
            assert '不固定套用红筹' in prompt and 'history_timeline' in prompt
            return json.dumps(draft,ensure_ascii=False)
        packet=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':
            return json.dumps({'sources':[{'information_id':r['information_id'],'status':'pass',
                'paragraph_ids':[p['paragraph_id'] for p in packet['body']],'reason':'已核原文与时间线'}
                for r in packet['sources']],'issues':[]})
        if role=='公司整章实用验收':packet['sources']=rows
        answer=passing(packet)
        if role=='公司整章实用验收':answer['source_issue_decisions']=[]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},target,invoke,outline=outline)
    assert out['batches'][0]['accepted'] and not package_errors(out,rows,target)
    assert len(calls)==(5 if profile=='default' else 4)
    assert build_package({'records':rows},target,lambda *a:pytest.fail('unexpected API'),out,outline=outline)==out
    visuals=[f for f in out['findings'] if f.get('content_role')=='history_timeline']
    assert len(visuals)==(decision=='render')
    if visuals:
        import report_agents.company_history_timeline as module
        monkeypatch.setattr(module,'render_timeline',lambda project,data:'![双轨时间线](timeline.svg)')
        # Use production digest conversion and renderer, preserving the finding role.
        digest={'groups':[{'chapter':3,'aspect':f['aspect'],'summary':f['text'],
            'information_ids':f['information_ids'],'evidence_ids':f['evidence_ids'],
            **{k:f[k] for k in ('content_role','timeline_data') if k in f}} for f in out['findings']]}
        section=render_compact_chapters(SimpleNamespace(id=1,company_information_usage_package={'records':rows}),digest,outline,{})[3]
        assert section.count('![双轨时间线]')==1
        assert section.index('### 业务发展历程与关键节点')<section.index('![双轨时间线]')<section.index('### 本章小结')
        assert '### 历史工商变更' in section


def test_structure_and_visual_text_cannot_diverge():
    rows,data,outline,_,_=sample('未上市公司')
    svg=render_svg(data,rows);ElementTree.fromstring(svg)
    assert '未上市公司' in svg and 'E-123456' in svg and '状态：完成' in svg
    assert 'marker-end=' not in svg
    linked=deepcopy(data);linked['causal_links']=[{'from_event':'T1','to_event':'T2','reason':'明确因果含义','information_ids':['S1']}]
    assert 'marker-end=' in render_svg(linked,rows)
    for mutate in [lambda d:d['events'][0].update(information_ids=['MISSING']),
                   lambda d:d['events'][1].update(event_id='T1'),
                   lambda d:d.update(causal_links=[{'from_event':'T1','to_event':'UNKNOWN','reason':'因为','information_ids':['S1']}])]:
        bad=deepcopy(data);mutate(bad)
        with pytest.raises(ValueError):validate_timeline(bad,rows)
    finding=timeline_finding(data,rows,contract_for(3,outline,'未上市公司'))
    finding['text']+='偷改图文字'
    with pytest.raises(ValueError,match='data_changed'):prepare_findings([finding],rows,3,outline[2][1][1])


def test_causal_error_is_model_reviewed_and_repairs_update_data():
    rows,data,outline,plan,draft=sample('海岳设备')
    data['causal_links']=[{'from_event':'T1','to_event':'T2','reason':'设立导致升级','information_ids':['S1']}]
    calls=[];saved=[]
    def invoke(role,prompt):
        calls.append(role)
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '小结补写' in role:return json.dumps({'chapter_summary':draft['chapter_summary']},ensure_ascii=False)
        if '整合写作' in role:return json.dumps(draft,ensure_ascii=False)
        if '局部修改' in role:
            fixed=deepcopy(data);fixed['causal_links']=[]
            return json.dumps({'history_timeline':fixed,'replacements':[]},ensure_ascii=False)
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);answer=passing(packet)
        if '集中审稿' in role:
            fid='C3-TIMELINE'
            next(p for p in answer['paragraphs'] if p['paragraph_id']==fid)['status']='block'
            answer['issues']=[{'issue_id':'I1','kind':'unsupported_claim','paragraph_ids':[fid],
                'source_ids':['S1'],'source_meaning':'只明确先后','output_meaning':'声明因果','reason':'虚构因果'}]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},'海岳设备',invoke,outline=outline,receipt_callback=saved.append)
    assert out['batches'][0]['accepted'] and not package_errors(out,rows,'海岳设备')
    visual=next(f for f in out['findings'] if f.get('content_role')=='history_timeline')
    assert not visual['timeline_data']['causal_links'] and 'marker-end=' not in render_svg(visual['timeline_data'],rows)
    assert len(calls)==6
    latest=saved[-1]
    stale=next(b for b in saved if any(f.get('timeline_data',{}).get('causal_links') for f in b.get('findings',[])))
    def pass_only(role,prompt):
        assert '写作' not in role and '修改' not in role
        return json.dumps(passing(json.loads(prompt.rsplit('资料：\n',1)[1])),ensure_ascii=False)
    resumed=build_package({'records':rows},'海岳设备',pass_only,{'batches':[latest,stale,latest]},outline=outline)
    assert resumed['batches'][0]['accepted']
    assert not next(f for f in resumed['findings'] if f.get('content_role')=='history_timeline')['timeline_data']['causal_links']


def test_incomplete_review_reports_exact_missing_ids():
    from report_agents.company_semantic_review import validate_review, review_prompt
    rows,data,outline,_,draft=sample('测试公司')
    finding=timeline_finding(data,rows,contract_for(3,outline,'测试公司'))
    findings=prepare_findings([finding],rows,3,outline[2][1][1])
    packet=json.loads(review_prompt(rows,findings,'测试公司').rsplit('资料：\n',1)[1])
    assert packet['required_critical_check_paragraph_ids']==['C3-TIMELINE']
    answer=passing(packet);answer['critical_checks']=[]
    with pytest.raises(ValueError,match='missing=C3-TIMELINE'):validate_review(answer,rows,findings)


def test_budget_deferred_repair_does_not_consume_repair_round():
    from company_flow_state import CompanyFlowLimit
    rows,data,outline,plan,draft=sample('测试公司');saved=[]
    def invoke(role,prompt):
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '小结补写' in role:return json.dumps({'chapter_summary':draft['chapter_summary']},ensure_ascii=False)
        if '整合写作' in role:return json.dumps(draft,ensure_ascii=False)
        if '局部修改' in role:raise CompanyFlowLimit('company_flow_estimated_cost_budget_exhausted')
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);answer=passing(packet)
        answer['paragraphs'][0]['status']='block'
        answer['issues']=[{'issue_id':'I1','kind':'unsupported_claim','paragraph_ids':[answer['paragraphs'][0]['paragraph_id']],
            'source_ids':['S1'],'source_meaning':'原文','output_meaning':'待修内容','reason':'实质错误'}]
        return json.dumps(answer,ensure_ascii=False)
    with pytest.raises(CompanyFlowLimit):build_package({'records':rows},'测试公司',invoke,outline=outline,receipt_callback=saved.append)
    marker=next(h for h in saved[-1]['semantic_history'] if h.get('deferred_by_budget'))
    assert marker['repair'] is False and saved[-1]['errors']==['repair_deferred_budget']


def test_delivery_verifies_svg_bytes_and_reference_not_a_duplicate_text_dump(tmp_path,monkeypatch):
    from research_store import ResearchObjectStore
    from report_agents.company_history_timeline import render_timeline
    from report_agents.company_rendered_findings import finding_is_rendered
    rows,data,outline,_,_=sample('交付测试')
    finding=timeline_finding(data,rows,contract_for(3,outline,'交付测试'))
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path/'objects'
    monkeypatch.setattr('research_store.research_store',store)
    project=SimpleNamespace(id=1,company_information_usage_package={'records':rows})
    rendered=render_timeline(project,data)
    assert finding['text'] not in rendered
    assert finding_is_rendered(project,finding,rendered,rows)
    assert not finding_is_rendered(project,finding,'正文未插图',rows)
    import re
    key=re.search(r'\]\((projects/[^)]+)\)',rendered)[1]
    store.put_bytes(key,b'<svg>corrupted</svg>','image/svg+xml')
    assert not finding_is_rendered(project,finding,rendered,rows)
