import json
from copy import deepcopy
from pathlib import Path
import pytest
from report_agents.company_review_references import normalize_review_references
from report_agents.company_scoped_review import validate_scoped_review, merge_scoped_checks, ReviewScopeIncomplete
from report_agents.company_task_batches import checked_call, digest, model_scope


ROWS = [{'information_id': 'I-1', 'evidence_ids': ['E-1']}]


@pytest.mark.parametrize('field', ['checks', 'critical_checks'])
@pytest.mark.parametrize('label,expected', [
    ('meta_observation', 'conclusion'), (' FACT ', 'direct_fact'),
    ('计算', 'calculation'), ('relationship', 'relationship'),
    ('资料整体观察', 'conclusion'), ('another_future_category', 'conclusion')])
def test_descriptive_labels_are_compatible_without_changing_judgment(field, label, expected):
    raw = {field: [{'kind': label, 'status': 'block', 'source_ids': ['I-1'],
                    'reason': '有明确反证', 'evidence_check': '原文金额为十万元'}]}
    before = deepcopy(raw)
    result = normalize_review_references(raw, ROWS)
    item = result[field][0]
    assert item['kind'] == expected
    assert {k: item[k] for k in raw[field][0] if k != 'kind'} == {
        k: raw[field][0][k] for k in raw[field][0] if k != 'kind'}
    if label != expected:
        assert item['original_kind'] == label
    assert raw == before and normalize_review_references(result, ROWS) == result


@pytest.mark.parametrize('status', ['supported', 'contradicted', 'not_assessed'])
def test_category_fallback_cannot_turn_a_contradiction_or_unknown_into_pass(status):
    claim = {'claim_id': 'C-1', 'text': '任意企业金额为一百万元。'}
    check = {'claim_id': 'C-1', 'kind': 'unknown_label', 'status': status,
             'source_ids': ['E-1'], 'reason': '依据原文核验',
             'issue_kind': 'wrong_amount', 'source_meaning': '原文为十万元'}
    answer = normalize_review_references({'checks': [check]}, ROWS)
    finding = {'finding_id': 'F-1', 'text': claim['text'], 'information_ids': ['I-1']}
    if status == 'not_assessed':
        with pytest.raises(ReviewScopeIncomplete, match='unresolved_claims'):
            merge_scoped_checks(finding, [claim], [ROWS], [answer])
    else:
        merged = merge_scoped_checks(finding, [claim], [ROWS], [answer])
        assert merged['paragraphs'][0]['status'] == ('block' if status == 'contradicted' else 'pass')
        assert merged['critical_checks'][0]['original_check_kinds'] == ['unknown_label']


@pytest.mark.parametrize('bad', [None, '', '   ', [], 3])
def test_missing_or_malformed_category_is_not_invented(bad):
    answer = normalize_review_references({'checks': [{'claim_id': 'C', 'kind': bad,
        'status': 'supported', 'source_ids': ['I-1'], 'reason': '已核验'}]}, ROWS)
    with pytest.raises((ValueError, TypeError)):
        validate_scoped_review(answer, [{'claim_id': 'C'}], {'I-1'})


@pytest.mark.parametrize('change', [
    {'status': 'partially_supported'}, {'status': None}, {'source_ids': []},
    {'source_ids': ['E-unknown']}, {'reason': ''},
    {'status': 'contradicted', 'issue_kind': 'invented_issue', 'source_meaning': ''}])
def test_substantive_checks_are_not_relaxed(change):
    check = {'claim_id': 'C', 'kind': 'meta_observation', 'status': 'supported',
             'source_ids': ['I-1'], 'reason': '已核验', **change}
    with pytest.raises(ValueError):
        answer = normalize_review_references({'checks': [check]}, ROWS)
        validate_scoped_review(answer, [{'claim_id': 'C'}], {'I-1'})


def test_unfamiliar_issue_category_keeps_the_block_and_original_label():
    raw = {'issues': [{'kind': 'time_and_value_mismatch', 'source_ids': ['I-1'], 'status': 'block'}]}
    normalized = normalize_review_references(raw, ROWS)
    assert normalized['issues'][0]['kind'] == 'material_contradiction'
    assert normalized['issues'][0]['original_issue_kind'] == 'time_and_value_mismatch'
    assert normalized['issues'][0]['status'] == 'block' and raw['issues'][0]['kind'] == 'time_and_value_mismatch'


def test_real_failed_repair_reuses_saved_answer_without_any_model_call():
    fixture = json.loads((Path(__file__).parent/'fixtures/chapter_review_kind_failure.json').read_text(encoding='utf-8'))
    receipt = deepcopy(fixture['receipt'])
    key = digest(['test-policy', model_scope(), 'review', 'same prompt'])
    tasks = {key: receipt}; before = deepcopy(receipt)
    result = checked_call('review', 'same prompt', lambda *a: pytest.fail('unnecessary paid call'),
        tasks, lambda: None,
        lambda a: validate_scoped_review(a, fixture['claims'], {r['information_id'] for r in fixture['rows']}),
        policy='test-policy', normalize=lambda a: normalize_review_references(a, fixture['rows']))
    assert tasks[key]['answer'] == before['answer']
    assert tasks[key]['repair_answer'] == before['repair_answer']
    assert 'validation_error' not in tasks[key]
    for original, normalized in zip(before['repair_answer']['checks'], result['checks']):
        assert normalized['status'] == original['status'] and normalized['reason'] == original['reason']
    assert result['checks'][-1]['original_kind'] == 'meta_observation'


@pytest.mark.parametrize('target,label', [('制造企业甲', 'meta_observation'), ('物流企业乙', '综合观察')])
def test_scoped_entry_and_cached_resume_support_other_targets(target, label):
    from test_company_scoped_review import sample, check
    from report_agents.company_scoped_review import scoped_paragraph_review
    finding, batches, claims = sample(target); tasks = {}; calls = []
    def invoke(role, prompt):
        calls.append(role)
        data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        ids = [r['information_id'] for r in data['sources']]
        selected = 0 if ids == ['S1'] else 1
        return {'checks': [check(c, 'supported' if i == selected else 'not_assessed',
            ids if i == selected else [], kind=label) for i, c in enumerate(claims)]}
    result = scoped_paragraph_review(finding, batches, target, [], [], invoke, tasks, lambda: None)
    assert result['paragraphs'][0]['status'] == 'pass' and len(calls) == 2
    assert scoped_paragraph_review(finding, batches, target, [], [],
        lambda *a: pytest.fail('repeated paid task'), tasks, lambda: None) == result


@pytest.mark.parametrize('missing_check', [False, True])
def test_complete_review_and_supplemental_check_use_the_same_compatibility(missing_check,monkeypatch):
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    from test_company_semantic_review import setup, passing
    from report_agents.company_semantic_review import build_package, package_errors
    rows, previous = setup(); calls = []
    def invoke(role, prompt):
        calls.append(role); data = json.loads(prompt.rsplit('资料：\n', 1)[1])
        if role == '公司核心事实审稿缺项补核':
            p = data['missing_paragraphs'][0]
            return json.dumps({'critical_checks': [{'paragraph_id': p['paragraph_id'], 'kind': '资料整体观察',
                'claim': p['text'], 'evidence_check': '已核验文档支持该主张', 'source_ids': ['E-ABCDEF'], 'status': 'pass'}]})
        answer = passing(data)
        for c in answer['critical_checks']:
            c['kind'] = 'meta_observation'
        if missing_check:
            answer['critical_checks'].pop()
        return json.dumps(answer)
    result = build_package({'records': rows}, '甲公司', invoke, previous)
    assert result['batches'][0]['accepted'] and not package_errors(result, rows, '甲公司')
    assert len(calls) == (2 if missing_check else 1)
    assert all(c['kind'] == 'conclusion' and c['original_kind']
               for c in result['batches'][0]['semantic_review']['critical_checks'])
