import json
from copy import deepcopy
from pathlib import Path
import pytest
from report_agents.company_review_references import normalize_review_references
from report_agents.company_scoped_review import validate_scoped_review, merge_scoped_checks
from report_agents.company_task_batches import checked_call


def rows():
    return [{'information_id':'I-1','evidence_ids':['E-doc']},
            {'information_id':'I-2','evidence_ids':['E-doc']},
            {'information_id':'I-3','evidence_ids':['E-other']}]


@pytest.mark.parametrize('field',['checks','critical_checks','paragraphs','issues'])
@pytest.mark.parametrize('status',['supported','contradicted','block','advice'])
def test_document_reference_keeps_verdict_and_expands_only_read_document_scope(field,status):
    original={field:[{'source_ids':['E-doc'],'status':status,'reason':'实际核验结论'}]}
    result=normalize_review_references(original,rows())
    item=result[field][0]
    assert item['source_ids']==['I-1','I-2']
    assert item['source_reference_scope']=='document' and item['document_source_ids']==['E-doc']
    assert item['status']==status and item['reason']==original[field][0]['reason']
    assert original[field][0]['source_ids']==['E-doc']
    assert normalize_review_references(result,rows())==result


def test_information_reference_is_unchanged_and_mixed_references_are_deduplicated():
    exact={'checks':[{'source_ids':['I-1'],'status':'supported'}]}
    assert normalize_review_references(exact,rows())==exact
    mixed=normalize_review_references({'checks':[{'source_ids':['I-1','E-doc','I-3']}]},rows())
    assert mixed['checks'][0]['source_ids']==['I-1','I-2','I-3']
    assert mixed['checks'][0]['original_source_ids']==['I-1','E-doc','I-3']


def test_real_summary_delivery_issue_uses_reviewed_paragraph_without_changing_block():
    from report_agents.company_semantic_review import validate_review
    raw=json.loads((Path(__file__).parent/'fixtures'/'chapter_review_summary_delivery_failure.json').read_text(encoding='utf-8'))
    original=deepcopy(raw)
    read_rows=[{'information_id':r['information_id']} for r in raw['sources']]
    findings=[{'finding_id':p['paragraph_id'],'content_role':'chapter_summary'} for p in raw['paragraphs']]
    tasks={}
    with pytest.raises(ValueError,match='unbound_material_issue'):
        checked_call('review','summary',lambda *a:deepcopy(raw),tasks,lambda:None,
                     lambda a:validate_review(a,read_rows,findings),policy='same')
    result=checked_call('review','summary',lambda *a:pytest.fail('paid re-review of unchanged delivery verdict'),tasks,lambda:None,
                       lambda a:validate_review(a,read_rows,findings),policy='same',
                       normalize=lambda a:normalize_review_references(a,read_rows))
    assert result['paragraphs'][0]['status']=='block'
    assert result['issues'][0]['source_reference_scope']=='reviewed_paragraph'
    assert result['issues'][0]['reason']==raw['issues'][0]['reason']
    assert next(c for c in result['critical_checks'] if c['kind']=='absence')['verification_target']=='summary_delivery'
    assert normalize_review_references(result,read_rows)==result and raw==original
    invalid=deepcopy(raw);invalid['issues'][0]['kind']='wrong_amount'
    with pytest.raises(ValueError,match='unbound_material_issue'):
        validate_review(normalize_review_references(invalid,read_rows),read_rows,findings)


def test_consistency_only_structural_issue_uses_actual_input_paragraphs():
    raw={'issues':[{'kind':'substantial_repetition','paragraph_ids':['P1','P2'],'source_ids':[],
                    'reason':'两个段落完整重复同一局限','output_meaning':'重复','source_meaning':'正文结构'}]}
    body=[{'paragraph_id':'P1','source_ids':['I-1']},{'paragraph_id':'P2','source_ids':['I-2']}]
    result=normalize_review_references(raw,rows(),body)
    assert result['issues'][0]['source_ids']==['I-1','I-2']
    assert result['issues'][0]['paragraph_reference_ids']==['P1','P2']
    assert 'paragraphs' not in result and raw['issues'][0]['source_ids']==[]
    assert normalize_review_references(raw,rows(),body[:1])['issues'][0]['source_ids']==[]


def test_repair_document_scope_survives_body_fact_id_changes_without_inventing_exact_support():
    from report_agents.company_review_references import normalize_repair_references
    raw={'replacements':[{'paragraph_id':'S','text':'原正文','information_ids':['I-1','E-doc']}]}
    result=normalize_repair_references(raw,rows(),{'I-2'})
    item=result['replacements'][0]
    assert item['information_ids']==['I-2'] and item['text']=='原正文'
    assert item['source_reference_scope']=='document' and item['document_source_ids']==['E-doc']
    assert item['original_information_ids']==['I-1','E-doc']
    assert raw['replacements'][0]['information_ids']==['I-1','E-doc']
    assert normalize_repair_references(raw,rows(),{'I-3'})['replacements'][0]['information_ids']==['I-1','E-doc']


@pytest.mark.parametrize('reference',['E-unknown','E-other','I-3'])
def test_unknown_or_out_of_batch_reference_has_explicit_error(reference):
    with pytest.raises(ValueError,match='unknown_review_source_reference') as caught:
        normalize_review_references({'checks':[{'source_ids':[reference]}]},rows()[:2])
    details=json.loads(str(caught.value).split(':',1)[1])
    assert details['invalid']==[reference]
    assert details['allowed_information_ids']==['I-1','I-2'] and details['allowed_document_ids']==['E-doc']


def test_real_failed_receipt_is_reused_without_a_model_or_rewriting_its_verdict():
    proof=json.loads((Path(__file__).parent/'fixtures'/'chapter_review_document_reference_failure.json').read_text(encoding='utf-8'))
    original={'checks':proof['original_invalid_checks']}
    read_rows=proof['actual_source_aliases'];claims=[{'claim_id':c['claim_id']} for c in original['checks']]
    tasks={};saved=[]
    def validate(a):validate_scoped_review(a,claims,set(proof['allowed_information_ids']))
    with pytest.raises(ValueError):
        checked_call('review','unchanged task',lambda *a:deepcopy(original),tasks,
                     lambda:saved.append(True),validate,policy='same')
    raw=deepcopy(tasks)
    result=checked_call('review','unchanged task',lambda *a:pytest.fail('unnecessary paid repair'),
                        tasks,lambda:None,validate,policy='same',
                        normalize=lambda a:normalize_review_references(a,read_rows))
    assert set(result['checks'][0]['source_ids'])==set(proof['allowed_information_ids'])
    assert result['checks'][0]['status']==original['checks'][0]['status']
    assert result['checks'][0]['reason']==original['checks'][0]['reason']
    key=next(iter(tasks))
    assert tasks[key]['answer']==raw[key]['answer'] and tasks[key]['repair_answer']==raw[key]['repair_answer']
    assert 'validation_error' not in tasks[key]


def test_document_level_contradiction_remains_blocked_after_merge():
    finding={'finding_id':'F','text':'金额为一百万元。','information_ids':['I-1','I-2']}
    claims=[{'claim_id':'C','text':finding['text']}]
    answer={'checks':[{'claim_id':'C','kind':'direct_fact','status':'contradicted','source_ids':['E-doc'],
                       'reason':'金额错误','issue_kind':'wrong_amount','source_meaning':'原文为十万元'}]}
    normalized=normalize_review_references(answer,rows()[:2])
    result=merge_scoped_checks(finding,claims,[rows()[:2]],[normalized])
    assert result['paragraphs'][0]['status']=='block'
    assert result['issues'][0]['kind']=='wrong_amount'
    assert result['issues'][0]['document_source_ids']==['E-doc']
    assert result['critical_checks'][0]['source_reference_scope']=='document'


@pytest.mark.parametrize('missing_check',[False,True])
def test_full_review_and_targeted_check_accept_document_refs_without_extra_binding_calls(missing_check,monkeypatch):
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    from test_company_semantic_review import setup,passing
    from report_agents.company_semantic_review import build_package,package_errors
    read_rows,previous=setup();calls=[]
    def invoke(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司核心事实审稿缺项补核':
            p=data['missing_paragraphs'][0]
            return json.dumps({'critical_checks':[{'paragraph_id':p['paragraph_id'],'kind':'direct_fact',
                'claim':p['text'],'evidence_check':'本已读文档支持该主张','source_ids':['E-ABCDEF'],'status':'pass'}]})
        answer=passing(data)
        for key in ['paragraphs','critical_checks']:
            for record in answer[key]:record['source_ids']=['E-ABCDEF']
        if missing_check:answer['critical_checks'].pop()
        return json.dumps(answer)
    result=build_package({'records':read_rows},'甲公司',invoke,previous)
    assert result['batches'][0]['accepted'] and not package_errors(result,read_rows,'甲公司')
    assert len(calls)==(2 if missing_check else 1)
    assert all(c['source_reference_scope']=='document' for c in result['batches'][0]['semantic_review']['critical_checks'])
