"""Reuse unchanged native batches while invalidating source context and rules."""
import json
import pytest
from types import SimpleNamespace as NS

import company_chapters
from company_reading import assess_questions


def test_unknown_batch_support_is_not_forwarded_to_cross_batch_review():
    fact={'fact':'样本公司已授予激励股份。','source_url':'https://original.test/grant',
          'evidence_id':'E-grant','scope':'target'}
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert role in {'公司本章问题覆盖核对','公司本章问题证据编号修复'}
        return json.dumps({'questions':[{'id':q['id'],'status':'answered',
            'support_ids':['P-not-in-this-batch'],'missing_fields':[],
            'reason':'模型引用了不存在的证明。'} for q in data['questions']]})
    result=assess_questions(4,'样本公司',[fact],invoke)
    assert result['errors']==['ValueError']
    assert result['batches'][0]['error']
    assert all(q['status']=='pending_review' for q in result['questions'])


def test_new_original_rechecks_global_coverage_without_resending_unchanged_batch_context(monkeypatch):
    sources=[];facts=[];calls=[]
    for i,character in enumerate(['甲','乙','丙']):
        text='样本公司披露'+character*9500+'。'
        url=f'https://original.test/{i}'
        sources.append(NS(id=i+1,title='样本公司原件'+str(i),url=url,source_type='filing',
            content='仅为董事会拟议方案，尚待批准。\n'+text+'\n原文附注。',summary='',source_metadata={'content_kind':'full_text'}))
        facts.append({'fact':text,'source_url':url,'evidence_id':'E'+str(i),'scope':'target'})
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(role)
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[p['id'] for p in data['proofs']],
            'missing_fields':['登记机关回执'],'reason':'已有公司披露，仍缺登记回执。'} for q in data['questions']],
            'follow_up_queries':[]})
    prior=assess_questions(2,'样本公司',facts[:2],invoke,originals=sources[:2])
    calls.clear()
    added=assess_questions(2,'样本公司',facts,invoke,previous=prior,originals=sources)
    assert calls.count('公司本章问题覆盖核对')==1
    assert any('跨批' in role for role in calls) and any('独立核对' in role for role in calls)
    calls.clear();sources[0].content=sources[0].content.replace('仅为董事会拟议方案，尚待批准。','董事会方案已经获得批准并实施。')
    changed=assess_questions(2,'样本公司',facts,invoke,previous=added,originals=sources)
    assert calls.count('公司本章问题覆盖核对')==1
    calls.clear();monkeypatch.setattr(company_chapters,'CONTRACT_SHA256','changed-review-requirements')
    assess_questions(2,'样本公司',facts,invoke,previous=changed,originals=sources)
    assert calls.count('公司本章问题覆盖核对')==3


def test_unknown_support_is_repaired_once_against_only_batch_proofs():
    fact={'fact':'企业已实施员工激励。','source_url':'https://example.test/a','evidence_id':'E-native','scope':'target'}
    calls=[]
    def invoke(role,prompt,trace):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        ids=['P-unknown'] if len(calls)==1 else [data['proofs'][0]['id']]
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':ids,
            'missing_fields':['最新执行记录'],'reason':'已有历史执行事实'} for q in data['questions']]})
    result=assess_questions(4,'任意企业',[fact],invoke)
    assert not result['errors']
    assert calls==['公司本章问题覆盖核对','公司本章问题证据编号修复']
    assert result['batches'][0]['response']['questions'][0]['support_ids'][0]!='P-unknown'


@pytest.mark.parametrize('malformed',[[],None,{'questions':[[]]},{'questions':[]}])
def test_malformed_question_batch_is_repaired_and_reused(malformed):
    fact={'fact':'企业披露2025年收入及研发进展。','source_url':'https://example.test/a','scope':'target'}
    calls=[]
    def invoke(role,prompt,trace):
        calls.append(role)
        if len(calls)==1:return json.dumps(malformed)
        assert role=='公司本章问题核对格式修复'
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[data['proofs'][0]['id']],
            'missing_fields':['独立核验'],'reason':'披露支持部分结论。'} for q in data['questions']]})
    result=assess_questions(6,'企业',[fact],invoke)
    assert not result['errors'] and calls==['公司本章问题覆盖核对','公司本章问题核对格式修复']
    def forbidden(*args):raise AssertionError('successful receipt must be reused')
    assert assess_questions(6,'企业',[fact],forbidden,previous=result)==result


def test_failed_question_format_repair_stays_pending_after_one_attempt():
    calls=[]
    def invoke(role,*args):calls.append(role);return '[]'
    result=assess_questions(6,'企业',[{'fact':'企业已交付产品。'}],invoke)
    assert calls==['公司本章问题覆盖核对','公司本章问题核对格式修复']
    assert result['errors']==['ValueError'] and not result['complete']
    assert all(q['status']=='pending_review' for q in result['questions'])


def test_only_exact_paid_groups_survive_and_stale_singletons_repack_losslessly(monkeypatch):
    monkeypatch.setattr('company_question_consolidation.consolidate_questions',lambda *a,**k:{})
    facts=[{'fact':f'企业已完成第{i}批产品交付。','source_url':'https://original.test/shared',
        'evidence_id':'E-shared','scope':'target'} for i in range(6)]
    original=NS(id=1,title='企业披露',url='https://original.test/shared',source_type='filing',
        content='原披露。'+''.join(f['fact'] for f in facts),summary='',source_metadata={'content_kind':'full_text'})
    calls=[]
    def invoke(role,prompt,trace):
        assert role=='公司本章问题覆盖核对'
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(data)
        return json.dumps({'questions':[{'id':q['id'],'status':'partial',
            'support_ids':[p['id'] for p in data['proofs']],'missing_fields':['验收记录'],
            'reason':'交付已有披露，验收仍待核验。'} for q in data['questions']]})
    receipts=[b for f in facts for b in assess_questions(6,'企业',[f],invoke,originals=[original])['batches']]
    calls.clear()
    unchanged=assess_questions(6,'企业',facts,invoke,previous={'batches':receipts},originals=[original])
    assert not calls and len(unchanged['batches'])==6
    original.content+='补充说明：尚待客户完成验收。'
    changed=assess_questions(6,'企业',facts,invoke,previous={'batches':receipts},originals=[original])
    assert len(calls)==1 and len(changed['batches'])==1
    assert {p['text'] for p in calls[0]['proofs']}=={f['fact'] for f in facts}
    assert not changed['errors']
