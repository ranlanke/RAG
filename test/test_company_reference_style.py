import pytest
from types import SimpleNamespace
from company_chapters import CHAPTER_REFERENCE_RULE,prompt_contract
from test_company_chapter_synthesis import fixture
from report_agents.company_chapter_synthesis import synthesize


@pytest.mark.parametrize('number',[2,3,6,12,14])
def test_chapters_share_citation_style_without_disabling_central_index(number):
    assert CHAPTER_REFERENCE_RULE in prompt_contract(number,'任意公司')
    assert CHAPTER_REFERENCE_RULE not in prompt_contract(13,'任意公司')


def test_standalone_synthesis_uses_the_same_citation_rule():
    rows,contract,plan,draft=fixture();calls=[]
    def call(role,prompt):
        calls.append(role)
        if '写作' in role:assert CHAPTER_REFERENCE_RULE in prompt
        return plan if '证据整合' in role else draft
    synthesize(rows,'海岳设备有限公司',contract,call)
    assert len(calls)==2


def test_same_source_is_indexed_once_and_links_stay_out_of_chapter_body():
    from research_workflow import ResearchWorkflow
    workflow=ResearchWorkflow.__new__(ResearchWorkflow)
    url='https://source.test/issuer-original.pdf';target='样本公司'
    eid=workflow._stable_evidence_id(url,'公告')
    fact={'source_url':url,'source_title':'公告','fact':target+'披露登记信息。',
        'evidence_id':eid,'evidence_ids':[eid],'evidence_scope':'target_direct','chapter':2}
    p=SimpleNamespace(target_name=target,target_type='company',sources=[],company_source_review_package={'facts':[fact]})
    chapter2=f'## 2. 工商\n\n正文。[{eid}]\n\n### 本章小结\n\n已确定事实。[{eid}]'
    chapter3=f'## 3. 历史\n\n正文。[{eid}]\n\n### 本章小结\n\n已确定事实。[{eid}]'
    report=chapter2+'\n\n'+chapter3+'\n\n## 13. 信息来源\n\n## 14. 后续研究点'
    result=workflow._append_report_evidence_index(p,report,'{"facts":[]}')
    body,index=result.split('## 13.',1)
    assert body.rstrip()==(chapter2+'\n\n'+chapter3) and url not in body
    assert index.count('| '+eid+' |')==1 and url in index
