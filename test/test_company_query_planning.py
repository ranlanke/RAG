import json
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from company_flow_state import CompanyFlowLimit
from company_query_planning import ensure_query_plan, cached_searches, validate_extensions


def fixture(target='甲公司', question='甲公司生产、产能和交付状态？'):
    calls = []; saves = []
    def invoke(role, prompt, trace):
        packet = json.loads(prompt.rsplit('资料：\n', 1)[1]); calls.append(packet)
        return json.dumps({'extensions': [extension(packet['questions'][0]['id'])]}, ensure_ascii=False)
    flow = NS(project=NS(id=1, target_name=target), intake={'as_of_date': '2026-11-05'},
        coverage={}, journal=NS(data={}, save=lambda: saves.append(True)), chapter_invoke=lambda _: invoke)
    return flow, [{'id': 'Q-1', 'question': question}], calls


def extension(qid='Q-1', **changes):
    return dict(question_id=qid, requirement='交付', query='交付 验收 状态',
        expected_evidence='实际交付时间和验收记录', background_ids=[], gap_field='') | changes


@pytest.mark.parametrize('target,number,question,requirement,query', [
    ('甲制造', 6, '甲制造生产、产能和交付状态？', '产能', '产线 产能 利用率'),
    ('乙软件', 6, '乙软件研发和产品交付状态？', '产品交付', '产品交付 部署 上线 验收'),
    ('丙工程', 6, '丙工程项目交付状态？', '项目交付', '项目交付 竣工 验收'),
    ('丁零售', 4, '丁零售股权激励的执行情况？', '股权激励', '股权激励 实际授予 归属'),
    ('戊服务', 9, '戊服务诉讼和行政处罚情况？', '行政处罚', '行政处罚 决定书 整改'),
])
def test_same_contract_across_companies_and_chapters(target, number, question, requirement, query):
    f, qs, calls = fixture(target, question)
    def model(role, prompt, trace):
        calls.append(json.loads(prompt.rsplit('资料：\n', 1)[1]))
        return json.dumps({'extensions': [extension(requirement=requirement, query=query)]})
    f.chapter_invoke = lambda _: model
    ensure_query_plan(f, number, qs, [])
    searches = cached_searches(f, number, qs, [])
    assert searches[0] == ('Q-1', question)
    assert any('#plan-' in qid and value == query for qid, value in searches)
    assert calls[0]['target'] == target and calls[0]['background'] == []


def test_cache_survives_restart_and_new_read_facts_but_scope_changes_replan():
    f, qs, calls = fixture()
    first = ensure_query_plan(f, 6, qs, [])
    f.journal.data = json.loads(json.dumps(f.journal.data))
    new_fact = {'source_id': '1', 'original_sha256': 'h', 'original_offset': 0, 'fact': '交付完成'}
    assert ensure_query_plan(f, 6, qs, [new_fact]) == first
    assert len(calls) == 1
    f.intake['as_of_date'] = '2026-11-06'
    ensure_query_plan(f, 6, qs, [])
    assert len(calls) == 2
    f.project.target_name = '另一个公司'
    assert all('#plan-' not in qid for qid, _ in cached_searches(f, 6, qs, []))
    ensure_query_plan(f, 6, qs, [])
    assert len(calls) == 3


def test_changed_background_receipt_invalidates_expansions_without_using_stale_context():
    f, qs, calls = fixture()
    fact = {'source_id': '1', 'original_sha256': 'h', 'original_offset': 0, 'fact': '交付完成'}
    entry = ensure_query_plan(f, 6, qs, [fact])
    assert entry['background']
    assert all('#plan-' not in qid for qid, _ in cached_searches(f, 6, qs, []))
    ensure_query_plan(f, 6, qs, [])
    assert len(calls) == 2 and calls[-1]['background'] == []


def test_validated_background_survives_transient_candidate_removal_but_not_source_change():
    import hashlib
    from test_company_chapter_flow import source
    f,qs,calls=fixture()
    f.project.sources=[source(1,text='交付完成')]
    fact={'source_id':'1','original_sha256':hashlib.sha256('交付完成'.encode()).hexdigest(),
        'original_offset':0,'fact':'交付完成'}
    first=ensure_query_plan(f,6,qs,[fact])
    assert ensure_query_plan(f,6,qs,[])==first and len(calls)==1
    assert any('#plan-' in qid for qid,_ in cached_searches(f,6,qs,[]))
    f.project.sources=[source(1,text='原文已更新')]
    assert all('#plan-' not in qid for qid,_ in cached_searches(f,6,qs,[]))
    ensure_query_plan(f,6,qs,[])
    assert len(calls)==2


def test_invalid_shape_falls_back_once_but_budget_and_pause_propagate():
    f, qs, calls = fixture()
    def invalid(*args): calls.append(1); return '{}'
    f.chapter_invoke = lambda _: invalid
    assert ensure_query_plan(f, 6, qs, [])['base']['status'] == 'fallback'
    ensure_query_plan(f, 6, qs, [])
    assert len(calls) == 1 and all('#plan-' not in qid for qid, _ in cached_searches(f, 6, qs, []))
    from research_workflow import WorkflowPaused
    for error in [CompanyFlowLimit('cap'), WorkflowPaused('pause'), RuntimeError('bug')]:
        f.journal.data = {}
        def fail(*args): raise error
        f.chapter_invoke = lambda _: fail
        with pytest.raises(type(error)): ensure_query_plan(f, 6, qs, [])
        assert not f.journal.data['chapter_query_plans']


def test_validator_rejects_wrong_question_requirement_refs_and_caps_fairly():
    qs = [{'id': 'a', 'question': '交付情况？'}, {'id': 'b', 'question': '交付情况？'}]
    rows = [extension('bad'), extension('a', requirement='不存在'),
            extension('a', background_ids=['invented'])]
    rows += [extension(qid, query=f'交付 验收 {i}') for qid in ['a', 'b'] for i in range(20)]
    selected, rejected = validate_extensions({'extensions': rows}, qs, [])
    assert len(rejected) == 3 and len(selected) == 8
    assert [r['question_id'] for r in selected] == ['a', 'b'] * 4


def test_only_new_explicit_gap_is_planned_once():
    f, qs, calls = fixture()
    ensure_query_plan(f, 6, qs, [])
    f.coverage[6] = {'questions': [{'id': 'Q-1', 'status': 'partial', 'missing_fields': ['验收日期']}]}
    def model(role, prompt, trace):
        packet = json.loads(prompt.rsplit('资料：\n', 1)[1]); calls.append(packet)
        return json.dumps({'extensions': [extension(query='验收 日期', gap_field='验收日期')]})
    f.chapter_invoke = lambda _: model
    ensure_query_plan(f, 6, qs, [], expanded=True)
    ensure_query_plan(f, 6, qs, [], expanded=True)
    assert len(calls) == 2 and calls[-1]['phase'] == 'gap'
    assert sum('#plan-' in qid for qid, _ in cached_searches(f, 6, qs, [])) == 2


def test_wrong_expansions_cannot_crowd_original_reading_or_display(tmp_path, monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source
    from company_progressive_reading import plan_reading
    f, _, _ = progressive_flow(tmp_path, monkeypatch, [source()])
    monkeypatch.setenv('COMPANY_CHAPTER_QUERY_PLANNING_ENABLED', 'true')
    nodes = [dict(node_id=f'N{i}', source_id='1', source_title='原文', original_sha256='hash',
        start=i*3000, end=(i+1)*3000, pages=[i+1], headings=[], opening_locator='证据',
        question_locators=[], window_ids=[f'W{i}'], chars=3000) for i in range(24)]
    monkeypatch.setattr('company_progressive_reading.directory', lambda *_: nodes)
    ranked = {'Q06-1': ['N0', 'N1', 'N2'], 'Q06-2': ['N3', 'N4', 'N5']}
    ranked.update({f'Q06-2#plan-{i}': [f'N{i+6}'] for i in range(8)})
    monkeypatch.setattr('company_chapter_retrieval.rank_questions', lambda *a, **kw: (ranked, {}))
    packets = []
    def model(role, prompt, trace=None, **kwargs):
        p = json.loads(prompt.rsplit('资料：\n', 1)[1]); packets.append((role, p))
        if '检索查询规划' in role: return json.dumps({'extensions': []})
        return json.dumps({'read_node_ids': [f'N{i}' for i in range(6, 14)],
            'initial_search_required': False, 'queries': [], 'platform_routes': []})
    monkeypatch.setattr(f.workflow, '_invoke_company_report_editor', model)
    result = plan_reading(f, 6)
    assert set(f'N{i}' for i in range(6)) <= set(result['selected_node_ids'])
    assert result['selected_chars'] <= 36000
    assert set(result['original_reserved_node_ids']) <= {n['node_id'] for n in packets[-1][1]['directory']}
    before = len(packets)
    assert plan_reading(f, 6) == result and len(packets) == before
    # A later pass has more zero-cost reused windows. It must not regenerate
    # the initial plan merely because mandatory allocation would now differ.
    monkeypatch.setattr('company_chapter_retrieval.required_nodes',lambda *a,**kw:(['N23'],[]))
    assert plan_reading(f,6)==result and len(packets)==before


def test_unknown_node_repair_uses_only_visible_directory_and_resumes_after_limit(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    from test_company_chapter_flow import source
    from company_progressive_reading import plan_reading
    f,_,_=progressive_flow(tmp_path,monkeypatch,[source()])
    nodes=[dict(node_id=f'N{i}',source_id='1',source_title='原文'*50,original_sha256='hash',
        start=i*10,end=i*10+10,pages=[i+1],headings=[],opening_locator='证据'*80,
        question_locators=[],window_ids=[f'W{i}'],chars=10) for i in range(600)]
    monkeypatch.setattr('company_progressive_reading.directory',lambda *_:nodes)
    monkeypatch.setattr('company_chapter_retrieval.rank_questions',lambda *a,**kw:({'q':[n['node_id'] for n in nodes]},{}))
    repairs=[];planning=[]
    def model(role,prompt,trace=None,**kwargs):
        if '编号格式修复' in role:
            p=json.loads(prompt.rsplit('资料：\n',1)[1]);repairs.append(p)
            assert len(p['directory'])==56 and len(prompt.encode())<54000
            if len(repairs)==1:raise CompanyFlowLimit('before_dispatch')
            return json.dumps({'read_node_ids':[p['directory'][0]['node_id']]})
        planning.append(role)
        return json.dumps({'read_node_ids':['invented'],'initial_search_required':False,'queries':[],'platform_routes':[]})
    monkeypatch.setattr(f.workflow,'_invoke_company_report_editor',model)
    with pytest.raises(CompanyFlowLimit):plan_reading(f,6)
    result=plan_reading(f,6)
    assert result['selected_node_ids'] and len(planning)==1 and len(repairs)==2
