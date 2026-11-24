from copy import deepcopy
import json
import pytest
from report_agents.company_review_evidence import review_evidence, bind_reviewed_sources
from report_agents.company_scoped_review import scoped_paragraph_review, claim_units
from report_agents.company_task_batches import size


def row(i, text, context='', **extra):
    return {'information_id': i, 'text': text, 'evidence_ids': ['E-' + i],
            'context': {'context_quote': context, 'period': '2024', 'subject_basis': '某公司'},
            'provenance': [{'source_id': i, 'source_title': '年度披露', 'source_url': 'https://example.test/' + i}], **extra}


@pytest.mark.parametrize('company,claim', [('制造公司甲', '获得质量奖'), ('运输公司乙', '客户满意度93%'), ('医药公司丙', '认证被撤销')])
def test_recovery_is_verbatim_locatable_bounded_and_company_independent(company, claim):
    records = [row('I1', '其他事实', '背景。' * 800 + company + claim + '。仅涉及2024年。'),
               row('I2', company + claim + '。')]
    original = deepcopy(records)
    recovered = review_evidence(company + claim, records)
    assert size(recovered) <= 22000 and len(recovered) <= 6
    assert any(claim in r['quote'] for r in recovered)
    lookup = {r['information_id']: r for r in records}
    for item in recovered:
        value = lookup[item['information_id']]
        for key in item['field'].split('.'):
            value = value[key]
        assert value[item['start']:item['end']] == item['quote']
        assert item['context']['period'] == '2024'
    assert records == original


def test_same_topic_contradiction_and_different_source_are_retained():
    rows = [row('I1', '甲公司认证有效。'), row('I2', '甲公司认证已被撤销，不再有效。')]
    recovered = review_evidence('甲公司认证有效。', rows)
    assert {r['information_id'] for r in recovered} == {'I1', 'I2'}


def test_compound_claim_retrieves_each_indicator_instead_of_only_first_topic():
    rows = [row('I' + str(i), '核心技术收入占比99%以上。') for i in range(12)]
    rows += [row('R1', '研发投入强度约35%。'), row('R2', '研发人员占比64%。')]
    recovered = review_evidence('核心技术收入占比99%以上，研发投入强度约35%，研发人员占比64%。', rows)
    assert {'R1', 'R2'} <= {r['information_id'] for r in recovered}


def test_original_page_text_is_recoverable_and_tiny_budget_is_not_exceeded():
    rows = [row('I1', '无关', original='unused')]
    rows[0]['context']['original_page_context'] = {'text': '原页转录：认证已撤销。', 'physical_page': 3}
    assert any(r['field'] == 'context.original_page_context.text' for r in review_evidence('认证撤销', rows))
    assert review_evidence('认证撤销', rows, limit=10) == []


@pytest.mark.parametrize('decision', ['supported', 'contradicted'])
def test_missing_binding_is_resolved_by_model_and_cached_without_repeating_batches(decision):
    rows = [row('I1', '收入增长。'), row('I2', '相关产品信息。'), row('I3', '公司获得质量奖。')]
    finding = {'finding_id': 'F', 'text': '公司获得质量奖。', 'information_ids': ['I1', 'I2']}
    claim = claim_units(finding)[0]; tasks = {}; calls = []
    def invoke(role, prompt):
        calls.append(role)
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        check = {'claim_id': claim['claim_id'], 'kind': 'direct_fact', 'reason': '已核验', 'source_ids': [], 'status': 'not_assessed'}
        if role == '公司跨批主张复核':
            assert any(r['information_id'] == 'I3' and '质量奖' in r['quote'] for r in data['recovered_originals'])
            check.update(status=decision, source_ids=['I3'])
            if decision == 'contradicted':
                check.update(issue_kind='wrong_subject', source_meaning='获得者为另一主体')
        return {'checks': [check]}
    result = scoped_paragraph_review(finding, [[rows[0]], [rows[1]]], '任意公司', [], [], invoke, tasks, lambda: None, evidence_rows=rows)
    assert len(calls) == 3 and result['paragraphs'][0]['status'] == ('pass' if decision == 'supported' else 'block')
    assert scoped_paragraph_review(finding, [[rows[0]], [rows[1]]], '任意公司', [], [],
        lambda *a: pytest.fail('paid replay'), tasks, lambda: None, evidence_rows=rows) == result
    bind_reviewed_sources([finding], result)
    assert finding['information_ids'] == ['I1', 'I2', 'I3'] and finding['text'] == '公司获得质量奖。'


@pytest.mark.parametrize('fact_size', [17000, 34000])
def test_recovery_uses_remaining_request_budget_without_clipping_original_facts(monkeypatch, fact_size):
    import report_agents.company_review_evidence as recovery
    rows = [row('I1', 'A' * fact_size), row('I2', 'B' * fact_size)]
    finding = {'finding_id': 'F', 'text': '公司获得质量奖。', 'information_ids': ['I1', 'I2']}
    claim = claim_units(finding)[0]
    monkeypatch.setattr(recovery, 'review_evidence', lambda *a: [
        {'information_id': 'I3', 'quote': '原文' * 2000, 'evidence_ids': ['E-3']} for _ in range(4)])
    def invoke(role, prompt):
        assert size(prompt) <= 54000
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        resolved = role == '公司跨批主张复核'
        if resolved:
            assert [r['text'] for r in data['sources']] == ([r['text'] for r in rows] if fact_size == 17000 else [])
            assert 0 < len(data['recovered_originals']) <= 4
            if fact_size == 17000:
                assert len(data['recovered_originals']) < 4
        return {'checks': [{'claim_id': claim['claim_id'], 'kind': 'direct_fact', 'reason': '核验依据',
                            'status': 'supported' if resolved else 'not_assessed',
                            'source_ids': ['I3'] if resolved else []}]}
    result = scoped_paragraph_review(finding, [[rows[0]], [rows[1]]], '任意公司', [], [],
        invoke, {}, lambda: None, evidence_rows=rows + [row('I3', '原文')])
    assert result['paragraphs'][0]['status'] == 'pass'


@pytest.mark.parametrize('final_status', ['supported', 'contradicted', 'not_assessed'])
def test_partial_year_conflict_requires_actual_model_resolution(final_status):
    rows = [row('I1', '2021年用户为5亿。'), row('I2', '2026年用户为7亿。')]
    finding = {'finding_id': 'F', 'text': '2026年用户为7亿。', 'information_ids': ['I1', 'I2']}
    claim = claim_units(finding)[0]; calls = []
    def invoke(role, prompt):
        calls.append(role); data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        if role == '公司跨批主张复核':
            assert any(c['status'] == 'contradicted' for c in data['batch_checks'])
            assert '不同主体、指标口径或年份' in prompt
            status = final_status; sources = ['I1', 'I2']
        else:
            sources = [s['information_id'] for s in data['sources']]
            status = 'contradicted' if sources == ['I1'] else 'supported'
        return {'checks': [{'claim_id': claim['claim_id'], 'kind': 'direct_fact', 'status': status,
            'source_ids': sources, 'reason': '结合期间核验', 'issue_kind': 'wrong_time', 'source_meaning': '2021与2026为不同期间'}]}
    result = scoped_paragraph_review(finding, [[rows[0]], [rows[1]]], '任意公司', [], [],
        invoke, {}, lambda: None, evidence_rows=rows)
    assert calls == ['公司局部证据主张核验'] * 2 + ['公司跨批主张复核']
    assert result['paragraphs'][0]['status'] == ('pass' if final_status == 'supported' else 'block')


def test_oversized_summary_reviews_body_and_located_originals(monkeypatch):
    import report_agents.company_bounded_editorial as bounded
    from test_company_semantic_review import passing
    original_batches = bounded.stable_batches
    def batches(rows, key, packet, *args, **kwargs):
        if packet.__name__ == 'paragraph_prompt':
            raise ValueError('native_task_record_exceeds_input_budget:I1')
        return original_batches(rows, key, packet, *args, **kwargs)
    monkeypatch.setattr(bounded, 'stable_batches', batches)
    rows = [row('I1', '公司获得质量奖。')]
    findings = [{'finding_id': 'F', 'text': '公司获得质量奖。', 'information_ids': ['I1']},
                {'finding_id': 'S', 'text': '公司获得质量奖。', 'information_ids': ['I1'], 'content_role': 'chapter_summary'}]
    def invoke(role, prompt):
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        if role == '公司章节原文保留审稿':
            return {'sources': [{'information_id': 'I1', 'status': 'pass', 'paragraph_ids': ['F', 'S'], 'reason': '原文支持'}], 'issues': []}
        if role == '公司局部证据主张核验':
            return {'checks': [{'claim_id': c['claim_id'], 'status': 'supported', 'kind': 'direct_fact', 'source_ids': ['I1'], 'reason': '原文支持'} for c in data['claims']]}
        if role == '公司章节一致性审稿':return {'issues': [], 'advice': []}
        assert role == '公司核心事实逐段审稿'
        assert size(prompt) <= 54000 and data['read_only_context'][0]['paragraph_id'] == 'F'
        assert data['sources'][0]['text'] == rows[0]['text']
        return passing(data)
    result = bounded.bounded_review(rows, findings, '任意公司', invoke, {}, lambda: None)
    assert result['summary_delivery_checks'][0]['contains_evidence_gap'] is False


def test_final_unverified_claim_requests_revision_instead_of_passing_or_looping():
    rows = [row('I1', '仅披露业务收入。')]
    finding = {'finding_id': 'F', 'text': '治理体系达到行业最优水平。', 'information_ids': ['I1']}
    def invoke(role, prompt):
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        return {'checks': [{'claim_id': c['claim_id'], 'kind': 'conclusion', 'status': 'not_assessed',
                            'source_ids': [], 'reason': '未能证实该比较性评价'} for c in data['claims']]}
    result = scoped_paragraph_review(finding, [rows], '任意公司', [], [], invoke, {}, lambda: None, evidence_rows=rows)
    assert result['paragraphs'][0]['status'] == 'block'
    assert result['issues'][0]['verification_status'] == 'not_assessed'
    assert result['issues'][0]['source_reference_scope'] == 'reviewed_scope'
    assert '不等于原文不存在' in result['issues'][0]['reason']


def test_bounded_repair_edits_only_requested_paragraphs_and_reuses_paid_results(monkeypatch):
    from report_agents.company_bounded_editorial import bounded_repair
    rows = [row('I1', '公司2024年收入增长。'),row('I2','公司2024年获得质量奖。')]
    import report_agents.company_review_evidence as recovery
    locate=recovery.review_evidence
    monkeypatch.setattr(recovery,'review_evidence',lambda claim,rows,**kw:locate(claim,rows[:1],**kw))
    findings = [{'finding_id': 'F1', 'text': '公司2024年收入增长且业内最优，并获得质量奖。', 'information_ids': ['I1','I2']},
                {'finding_id': 'F2', 'text': '保留其他段落。', 'information_ids': ['I1']}]
    issues = [{'paragraph_ids': ['F1'], 'source_ids': ['I1'], 'source_meaning': '原文仅称收入增长',
               'output_meaning': '业内最优', 'reason': '比较无依据', 'kind': 'unsupported_claim'}]
    tasks = {}; before = deepcopy(findings)
    def invoke(role, prompt):
        assert size(prompt) <= 54000
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        assert data['paragraph']['finding_id'] == 'F1'
        assert all(r['information_id']!='I2' for r in data['sources'])
        return {'replacements': [{'paragraph_id': 'F1', 'text': '公司2024年收入增长，并获得质量奖。', 'information_ids': ['I1','I2']}],
                'remove_paragraph_ids': [], 'additions': []}
    result = bounded_repair(rows, findings, issues, '任意公司', {}, invoke, tasks, lambda: None)
    assert len(result['replacements']) == 1 and findings == before
    assert bounded_repair(rows, findings, issues, '任意公司', {},
        lambda *a: pytest.fail('paid replay'), tasks, lambda: None) == result


def test_summary_repair_cites_revised_body_instead_of_removed_body_references():
    from report_agents.company_bounded_editorial import bounded_repair
    rows=[row('OLD','公司收入增长。'),row('NEW','公司收入增长。')]
    findings=[{'finding_id':'F','text':'公司收入增长。','information_ids':['OLD']},
              {'finding_id':'S','text':'公司收入增长，但仍待核。','information_ids':['OLD'],'content_role':'chapter_summary'}]
    issues=[{'paragraph_ids':['F'],'source_ids':['NEW'],'source_meaning':'使用新版正文来源'},
            {'paragraph_ids':['S'],'source_ids':['OLD'],'source_meaning':'小结不列缺口'}]
    def invoke(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);fid=data['paragraph']['finding_id']
        if fid=='S':assert data['read_only_body'][0]['information_ids']==['NEW']
        return {'replacements':[{'paragraph_id':fid,'text':'公司收入增长。','information_ids':['NEW']}],
                'remove_paragraph_ids':[],'additions':[]}
    result=bounded_repair(rows,findings,issues,'任意公司',{},invoke,{},lambda:None)
    assert result['replacements'][-1]['information_ids']==['NEW']


def test_disputed_amount_recall_keeps_both_accounting_scopes():
    rows=[row('M'+str(i),'母公司现金流量净额7,355,600.96元。') for i in range(15)]
    rows.append(row('GROUP','合并现金流量净额642,693,273.51元。'))
    found=review_evidence('正文经营现金流净额642,693,273.51元，但反证称7,355,600.96元。',rows)
    assert found[0]['information_id']=='GROUP'
    assert any('7,355,600.96' in w['quote'] for w in found)


def test_large_summary_repair_keeps_full_revised_body_without_duplicate_originals():
    from report_agents.company_bounded_editorial import bounded_repair
    rows=[row('I','公司收入100万元。'*500)]
    body='公司收入100万元。'*1400
    findings=[{'finding_id':'F','text':body,'information_ids':['I']},
        {'finding_id':'S','text':'公司收入100万元，但资料待核。','information_ids':['I'],'content_role':'chapter_summary'}]
    issues=[{'paragraph_ids':['S'],'source_ids':['I'],'source_meaning':'小结不列缺口'}]
    def invoke(role,prompt):
        assert size(prompt)<=54000
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert data['read_only_body'][0]['text']==body and data['sources']==[]
        return {'replacements':[{'paragraph_id':'S','text':'公司收入100万元。','information_ids':['I']}],
            'remove_paragraph_ids':[],'additions':[]}
    assert bounded_repair(rows,findings,issues,'任意公司',{},invoke,{},lambda:None)['replacements']


def test_citation_enrichment_refreshes_summary_but_prose_changes_do_not():
    from report_agents.company_review_evidence import refresh_summary_bindings
    from report_agents.company_chapter_synthesis import body_key
    old=[{'finding_id':'F','aspect':'财务','text':'公司收入100万元。','information_ids':['I1']}]
    current=deepcopy(old)
    current[0]['information_ids'].append('I2')
    current.append({'finding_id':'S','text':'公司收入100万元。','content_role':'chapter_summary',
        'information_ids':['I1'],'summary_of_sha256':body_key(old)})
    refresh_summary_bindings(current,old)
    assert current[-1]['summary_of_sha256']==body_key(current)
    changed=deepcopy(current);changed[0]['text']='公司收入200万元。'
    refresh_summary_bindings(changed,current)
    assert changed[-1]['summary_of_sha256']!=body_key(changed)
