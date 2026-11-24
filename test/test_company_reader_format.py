from copy import deepcopy
import pytest
from report_agents.company_reader_format import reader_text
from report_agents.company_semantic_review import prepare_findings,review_prompt,validate_review
from test_company_semantic_review import setup,passing
import json


def test_internal_tokens_are_formatting_only_and_real_evidence_and_facts_survive():
    text='据年报披露，甲公司资本100万元[I-12345678][I-abcdef0123456789]。[E-ABCDEF][Evidence Gap]待核事项仍留在正文。'
    assert reader_text(text)=='据年报披露，甲公司资本100万元。[E-ABCDEF]待核事项仍留在正文。'
    assert reader_text('[I-业务编号][E-123456] 型号I-12345678')=='[I-业务编号][E-123456] 型号I-12345678'
    rows,_=setup();proposal=[{'finding_id':'P1','text':text,'information_ids':['S1']}];raw=deepcopy(proposal)
    out=prepare_findings(proposal,rows,2,'工商')
    assert proposal==raw and out[0]['information_ids']==['S1'] and out[0]['evidence_ids']==['E-ABCDEF']
    assert out[0]['text']==reader_text(text)


def test_summary_check_cannot_be_omitted_or_ignored_and_qualifier_is_not_word_filtered():
    rows,_=setup();findings=[{'finding_id':'SUM','text':'据公司披露，部分款项尚未收回。',
        'information_ids':['S1'],'content_role':'chapter_summary'}]
    data=json.loads(review_prompt(rows,findings,'另一家公司').rsplit('资料：\n',1)[1]);answer=passing(data)
    # A described business fact is not automatically a research gap because of “尚未”.
    validate_review(answer,rows,findings)
    missing=deepcopy(answer);missing.pop('summary_delivery_checks')
    with pytest.raises(ValueError,match='incomplete_summary_delivery_checks'):validate_review(missing,rows,findings)
    answer['summary_delivery_checks'][0]['contains_evidence_gap']=True
    with pytest.raises(ValueError,match='summary_gap_requires_local_revision'):validate_review(answer,rows,findings)
