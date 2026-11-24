"""Production defaults, retained audit receipts, and material acceptance gates."""
import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from company_chapters import chapters,outline,chapter_section_formats,chapter_visual_policy
from report_agents.company_review_policy import acceptance_profile,semantic_review_enabled
from report_agents.company_semantic_review import build_package,policy,package_errors
from report_agents.company_bounded_editorial import bounded_review
from test_company_semantic_review import passing
from test_company_incremental_tasks import evidence,fake_synthesis


@pytest.fixture(autouse=True)
def production_defaults(monkeypatch):
    monkeypatch.delenv('COMPANY_EDITORIAL_REVIEW_MODE',raising=False)
    monkeypatch.delenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE',raising=False)


def audit(data,issue=None):
    return {'sources':[{'information_id':r['information_id'],'status':'block' if issue else 'pass',
        'paragraph_ids':[p['paragraph_id'] for p in data['body']],
        'reason':'已核对当前正文和完整原文'} for r in data['sources']],
        'issues':[issue] if issue else []}


def overall(data,status='resolved'):
    result=passing({**data,'sources':[{'information_id':s['information_id']}
        for s in data['completed_source_audits']]})
    result['source_issue_decisions']=[{'issue_id':i['issue_id'],'status':status,
        'reason':'按完整原文确认其实质影响','material_impact':'core_conclusion',
        'impact_reason':'正文主要结论与原文相反，影响本章主要判断'} for i in data['source_issues']]
    return result


@pytest.mark.parametrize('spec',chapters(),ids=lambda c:str(c['chapter']))
def test_defaults_follow_real_chapter_roles(spec):
    rows=evidence('通用企业',spec['chapter'],1)
    research=spec['mode']=='research'
    assert semantic_review_enabled(rows)==research
    assert acceptance_profile(spec['chapter'])==('practical_core' if research else 'strict_core')
    if research:assert not semantic_review_enabled(rows+evidence('通用企业',12,1))


def test_explicit_profile_cannot_turn_summary_into_research(monkeypatch):
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','practical_core')
    assert all(acceptance_profile(n)=='strict_core' for n in (1,11,12,13,14))
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    assert all(acceptance_profile(n)=='strict_core' for n in range(2,11))


@pytest.mark.parametrize('number',[1,11,12,13,14,99])
def test_local_relevance_entry_excludes_nonresearch_chapters(number):
    from report_agents.company_information_usage import build_information_usage
    with pytest.raises(ValueError,match='invalid_current_research_chapter'):
        build_information_usage({'statements':[]},{},outline(),'通用企业',
            lambda *a:pytest.fail('summary must not invoke research routing'),current_chapter=number)


@pytest.mark.parametrize('number',range(2,11))
def test_small_body_audits_native_sources_then_practical_acceptance_and_replays(number):
    rows=evidence('另一家企业',number,1);calls=[]
    def invoke(role,prompt):
        calls.append(role)
        data=json.loads(prompt.rsplit('资料：',1)[1])
        if role=='公司核心事实写作':
            result={'findings':[{'finding_id':'P','text':rows[0]['text'],'information_ids':['S000']}]}
        elif role=='公司章节原文保留审稿':result=audit(data)
        else:
            assert role=='公司整章实用验收'
            result=overall(data)
        return json.dumps(result)
    package=build_package({'records':rows},'另一家企业',invoke)
    assert package['batches'][0]['accepted'],package['batches'][0]['errors']
    assert calls==['公司核心事实写作','公司章节原文保留审稿','公司整章实用验收']
    assert not package_errors(package,rows,'另一家企业')
    assert build_package({'records':rows},'另一家企业',lambda *a:pytest.fail('repeated model call'),package)==package


@pytest.mark.parametrize('number',[6,7,8,9,10])
def test_public_entry_uses_same_synthesis_and_acceptance_without_env(number):
    from report_agents.company_editorial import build_editorial_package
    rows=evidence('后续研究公司',number,1);calls=[]
    def invoke(role,prompt):
        calls.append(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':result=audit(data)
        elif role=='公司整章实用验收':result=overall(data)
        else:
            result=fake_synthesis(role,prompt)
            if role=='公司章节整合写作':
                result['findings']=[{**result['findings'][0],'finding_id':f'P{index}','aspect':aspect}
                    for index,aspect in enumerate(data['contract']['sections'])]
                for finding in result['findings']:
                    if data['contract'].get('section_formats',{}).get(finding['aspect'])=='table':
                        finding['text']='| 指标 | 原文依据 |\n| --- | --- |\n| 已知事实 | '+finding['text']+' |'
        return json.dumps(result)
    result=build_editorial_package({'records':rows},outline(),'后续研究公司',invoke)
    assert result['batches'][0]['accepted'],result['batches'][0]['errors']
    assert '公司章节证据整合' in calls and '公司章节整合写作' in calls
    assert calls[-1]=='公司整章实用验收'
    assert result['chapter_summary']['content_role']=='chapter_summary'
    if number==6:
        from report_agents.company_chapter_synthesis import has_markdown_table
        assert all(any(f['aspect']==aspect and has_markdown_table(f['text']) for f in result['findings'])
            for aspect in chapter_section_formats(6,outline()[5][1]))


@pytest.mark.parametrize('kind',['wrong_subject','wrong_amount','unsupported_claim','false_gap'])
def test_material_errors_and_important_omissions_still_block(kind):
    rows=evidence('核查企业',9,1)
    findings=[{'finding_id':'P','text':'有实质错误的核心结论。','information_ids':['S000']}]
    issue={'issue_id':'X','kind':kind,'paragraph_ids':['P'],'source_ids':['S000'],
        'source_meaning':'原文有明确主体、金额与执行事实','output_meaning':'正文核心结论与原文不符',
        'reason':'影响报告主要判断，必须纠正'}
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        return audit(data,issue) if role=='公司章节原文保留审稿' else overall(data,'block')
    result=bounded_review(rows,findings,'核查企业',call,{},lambda:None)
    assert result['issues'][0]['kind']==kind
    assert result['paragraphs'][0]['status']=='block' and result['sources'][0]['status']=='block'


def test_default_matches_experimental_profile_and_retains_source_leaf_receipts(monkeypatch):
    rows=evidence('核查企业',6,1)
    findings=[{'finding_id':'P','text':rows[0]['text'],'information_ids':['S000']}]
    tasks={};calls=[]
    def call(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':return audit(data)
        if role=='公司章节一致性审稿':return {'issues':[],'advice':[]}
        if role=='公司整章实用验收':return overall(data)
        return passing(data)
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    leaf=policy(scoped=False);strict=policy(chapter=6)
    bounded_review(rows,findings,'核查企业',call,tasks,lambda:None)
    calls.clear();monkeypatch.delenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE')
    result=bounded_review(rows,findings,'核查企业',call,json.loads(json.dumps(tasks)),lambda:None)
    assert not result['issues'] and calls==['公司整章实用验收']
    assert policy(scoped=False)==leaf and policy(chapter=6)!=strict
    default=policy(chapter=6)
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','practical_core')
    assert policy(chapter=6)==default


def test_old_incomplete_strict_review_cannot_bypass_default_practical_acceptance(monkeypatch):
    from company_flow_state import CompanyFlowLimit
    rows=evidence('核查企业',6,1);saved=[]
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')
    def interrupted(role,prompt):
        if role=='公司核心事实写作':
            return json.dumps({'findings':[{'finding_id':'P','text':rows[0]['text'],'information_ids':['S000']}]})
        if role=='公司核心事实审稿缺项补核':raise CompanyFlowLimit('pause_missing_check')
        result=passing(json.loads(prompt.rsplit('资料：\n',1)[1]));result['critical_checks']=[]
        return json.dumps(result)
    with pytest.raises(CompanyFlowLimit):
        build_package({'records':rows},'核查企业',interrupted,receipt_callback=lambda b:saved.append(deepcopy(b)))
    assert any(h.get('invalid_review') for h in saved[-1]['semantic_history'])
    monkeypatch.delenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE');calls=[]
    def resumed(role,prompt):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        return json.dumps(audit(data) if role=='公司章节原文保留审稿' else overall(data))
    result=build_package({'records':rows},'核查企业',resumed,{'batches':[saved[-1]]})
    assert result['batches'][0]['accepted']
    assert calls==['公司章节原文保留审稿','公司整章实用验收']


def test_default_chapter_scopes_do_not_mutate_process_env():
    import os
    before=dict(os.environ)
    with ThreadPoolExecutor(max_workers=3) as pool:
        result=list(pool.map(lambda n:(n,acceptance_profile(n)),[2,6,12,9,11]*4))
    assert all(profile==('practical_core' if n in (2,6,9) else 'strict_core') for n,profile in result)
    assert dict(os.environ)==before


def test_unaddressed_source_issue_cannot_be_accepted():
    rows=evidence('核查企业',6,1)
    findings=[{'finding_id':'P','text':'正文主张。','information_ids':['S000']}]
    issue={'issue_id':'X','kind':'wrong_amount','paragraph_ids':['P'],'source_ids':['S000'],
        'source_meaning':'关键金额100万元','output_meaning':'关键金额1000万元','reason':'核心金额错误'}
    def call(role,prompt):
        data=json.JSONDecoder().raw_decode(prompt.rsplit('资料：\n',1)[1])[0]
        if role=='公司章节原文保留审稿':return audit(data,issue)
        # Even a model-supplied body pass cannot waive an unaddressed source error.
        result=overall(data);result.pop('source_issue_decisions')
        return result
    with pytest.raises(ValueError,match='incomplete_source_issue_decisions'):
        bounded_review(rows,findings,'核查企业',call,{},lambda:None)


def test_fixed_chapter_two_table_and_chapter_three_timeline_contracts():
    assert chapter_section_formats(2,['主体工商信息表','股权与治理信息表'])=={
        '主体工商信息表':'table','股权与治理信息表':'table'}
    assert chapter_visual_policy(3)=='dual_track_history_timeline'
