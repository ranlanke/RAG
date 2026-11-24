from copy import deepcopy
import pytest
from company_chapters import outline
from report_agents.company_chapter_synthesis import contract_for,validate_draft


def material(target):
    c={**contract_for(2,outline(),target),'chapter_summary_required':False}
    rows=[{'information_id':'S1','text':target+'现有材料'}]
    draft={'findings':[{'finding_id':str(i),'aspect':aspect,'text':'现有材料说明，保留核验边界。','information_ids':['S1']}
                       for i,aspect in enumerate(c['sections'])]}
    return rows,c,draft


@pytest.mark.parametrize('target',['青禾餐饮有限公司','北辰设备股份有限公司'])
def test_template_promised_tables_cannot_be_replaced_by_prose(target):
    rows,c,draft=material(target)
    with pytest.raises(ValueError,match='missing_required_table'):validate_draft(draft,rows,c)


def test_tables_are_layout_contract_not_company_specific_fact_matching():
    rows,c,draft=material('另一目标')
    for f in draft['findings'][:2]:f['text']='| 核查项目 | 现有证据 |\n| --- | --- |\n| 已知或缺口 | 如实列示 |'
    validate_draft(draft,rows,c)
    bad=deepcopy(draft);bad['findings'][0]['text']='```text\n'+bad['findings'][0]['text']+'\n```'
    with pytest.raises(ValueError,match='missing_required_table'):validate_draft(bad,rows,c)
