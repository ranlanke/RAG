import json
from copy import deepcopy
import pytest
from report_agents.company_semantic_review import build_package,package_errors
from test_company_history_timeline import sample
from test_company_semantic_review import passing


def test_missing_source_record_names_exact_id_and_recovers_without_rewriting():
    rows,_,outline,plan,draft=sample('不同目标');calls=[]
    def invoke(role,prompt):
        calls.append(role)
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '整合写作' in role:return json.dumps(draft,ensure_ascii=False)
        if '审稿记录修复' in role:
            assert '"missing": ["S1"]' in prompt and 'required_source_ids' in prompt
            packet=json.loads(prompt.split('资料：\n',1)[1].split('\n上次记录结构错误：',1)[0])
        else:packet=json.loads(prompt.rsplit('资料：\n',1)[1])
        answer=passing(packet)
        if '集中审稿' in role:answer['sources']=[]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},'不同目标',invoke,outline=outline)
    assert out['batches'][0]['accepted'] and len(calls)==4
    assert sum('整合写作' in c for c in calls)==1
    assert any('incomplete_semantic_sources:' in h.get('record_error','') for h in out['batches'][0]['semantic_history'])


@pytest.mark.parametrize('recover',[True,False])
def test_invalid_model_repair_is_saved_retried_once_and_never_partially_applied(recover):
    rows,_,outline,plan,draft=sample('其他目标公司');original=deepcopy(draft);calls=[]
    bad={'replacements':[{'paragraph_id':'P0','text':'','information_ids':[]}]}
    def invoke(role,prompt):
        calls.append(role)
        if '证据整合' in role:return json.dumps(plan,ensure_ascii=False)
        if '整合写作' in role:return json.dumps(draft,ensure_ascii=False)
        if '局部修改记录修复' in role:
            assert 'invalid_semantic_paragraph:P0' in prompt and 'remove_paragraph_ids' in prompt
            answer={'replacements':[{'paragraph_id':'P0','text':rows[0]['text'],'information_ids':['S1']}]} if recover else bad
            return json.dumps(answer,ensure_ascii=False)
        if '局部修改' in role:return json.dumps(bad,ensure_ascii=False)
        packet=json.loads(prompt.rsplit('资料：\n',1)[1]);answer=passing(packet)
        if '集中审稿' in role:
            next(p for p in answer['paragraphs'] if p['paragraph_id']=='P0')['status']='block'
            answer['issues']=[{'issue_id':'I1','kind':'unsupported_claim','paragraph_ids':['P0'],
                'source_ids':['S1'],'source_meaning':'原文内容','output_meaning':'待修改内容','reason':'具体事实错误'}]
        return json.dumps(answer,ensure_ascii=False)
    out=build_package({'records':rows},'其他目标公司',invoke,outline=outline)
    assert draft==original
    assert out['batches'][0]['accepted'] is recover
    assert all(f['text'] and f['information_ids'] for f in out['batches'][0]['findings'])
    invalid=[h for h in out['batches'][0]['semantic_history'] if h.get('repair_response')==bad]
    assert invalid and invalid[0]['record_error'].startswith('invalid_semantic_paragraph:P0')
    assert len(calls)==(6 if recover else 5)
    if recover:assert not package_errors(out,rows,'其他目标公司')
