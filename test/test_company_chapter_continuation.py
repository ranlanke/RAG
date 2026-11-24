import pytest
from scripts.manual.company_bounded_chapter_run import continuation_intake


def test_continuation_keeps_project_scope_without_changing_selected_chapter():
    current={'target_name':'甲公司','focus_modules':['company.ch03'],'as_of_date':'2026-11-12','disabled_platforms':'tyc,xiniu'}
    saved={'target_name':'甲公司','focus_modules':['company.ch02'],'as_of_date':'2026-11-05','flow_version':'v1'}
    result=continuation_intake(current,saved,'甲公司')
    assert result['focus_modules']==['company.ch02'] and result['as_of_date']=='2026-11-05'
    assert result['disabled_platforms']=='tyc,xiniu' and 'flow_version' not in result
    assert current['focus_modules']==['company.ch03']


@pytest.mark.parametrize('saved',[None,{}, {'target_name':'乙公司'}])
def test_continuation_rejects_missing_scope_or_different_company(saved):
    with pytest.raises(ValueError,match='continuation_target_mismatch'):
        continuation_intake({'target_name':'甲公司'},saved,'甲公司')


def test_fresh_narrative_preserves_evidence_and_other_chapter():
    from types import SimpleNamespace as NS
    from scripts.manual.company_bounded_chapter_run import regenerate_narrative
    evidence={'records':[{'information_id':'S1'}]};reading={'facts':['原文']};calls=[]
    f=NS(editorials={2:{'accepted':True},3:{'accepted':True}},sections={2:'旧正文',3:'其他章'},
         usages={2:evidence},reads={2:reading},checkpoint=lambda *a:calls.append(a),
         journal=NS(data={'chapters':{'2':{'round_closed':True,'draft_complete':True}},
             'partial_editorials':{'2':[{'findings':['人工稿']}],'3':['保留']},'draft_previews':{'2':'旧稿'}}))
    regenerate_narrative(f,2)
    assert 2 not in f.editorials and f.editorials[3]['accepted']
    assert '旧正文' not in f.sections[2] and f.sections[3]=='其他章'
    assert f.usages[2] is evidence and f.reads[2] is reading
    assert f.journal.data['chapters']['2']['round_closed'] and f.journal.data['chapters']['2']['status']=='pending_writing'
    assert '2' not in f.journal.data['partial_editorials'] and f.journal.data['partial_editorials']['3']==['保留']
    assert calls==[('fresh_narrative_requested',2)]
