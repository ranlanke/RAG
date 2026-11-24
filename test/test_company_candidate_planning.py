import json
from types import SimpleNamespace as NS

import pytest

from company_candidate_planning import select_originals
from company_chapters import VERSION
from company_flow_state import FlowJournal
from research_store import ResearchObjectStore


def flow(tmp_path,invoke):
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path/'objects'
    project=NS(id=1,target_name='样本股份有限公司',sources=[],
        company_research_intake={'company_template_version':VERSION,'focus_modules':['company.ch02']})
    journal=FlowJournal(store,1,{'target':project.target_name})
    project._company_flow_journal=journal
    return NS(project=project,journal=journal,reads={},coverage={},invoke=invoke,
        intake={'as_of_date':'2026-11-05'},event=lambda *a,**kw:None)


def batches(count):
    return [({'query':'样本股份有限公司 股东 原件','module':'company.ch02'},[
        {'url':'https://original.test/'+str(i),'title':'原件'+str(i),'summary':'候选的搜索定位摘要'+str(i),
         'content':'完整返回正文'+str(i)+'。'*90,'metadata':{'provider':'fixture','content_origin':'provider_raw_content'}}
        for i in range(count)])]


def test_many_candidates_are_kept_and_two_independent_choices_enter_reading(tmp_path):
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);rows=data['candidates']
        if role=='公司本章候选原件取用规划':return json.dumps({'selected':[{'candidate_id':r['candidate_id'],'reason':'当前股东原始披露。'} for r in rows if r['candidate_id']=='C0001']})
        return json.dumps({'additional':[{'candidate_id':r['candidate_id'],'reason':'补入与前项不同的控制关系原件。'} for r in rows if r['candidate_id']=='C0120']})
    f=flow(tmp_path,invoke);original=batches(120)
    selected=select_originals(f,2,original)
    assert [items[0]['url'] for _,items in selected]==['https://original.test/0','https://original.test/119']
    queue=f.journal.data['chapter_candidate_queues']['2']
    assert len(queue)==120
    assert sum(r['disposition']=='deferred_unread_candidate' for r in queue.values())==118
    assert all(f.journal.cached('candidate_originals',r['raw_key'])['item']['content'] for r in queue.values())


def test_selection_has_no_fixed_source_count_ceiling(tmp_path):
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        return json.dumps({'selected':[{'candidate_id':r['candidate_id'],'reason':'各自补齐一个不同字段。'} for r in data['candidates']]}) if '规划' in role else '{"additional":[]}'
    f=flow(tmp_path,invoke)
    assert len(select_originals(f,2,batches(25)))==25


def test_deferred_original_can_be_selected_in_a_later_gap_pass(tmp_path):
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if '核对' in role:return '{"additional":[]}'
        index=1 if data['phase']=='gap' else 0
        return json.dumps({'selected':[{'candidate_id':data['candidates'][index]['candidate_id'],'reason':'本轮需要这份原件。'}]})
    f=flow(tmp_path,invoke);first=select_originals(f,2,batches(3))
    item=first[0][1][0]
    f.project.sources=[NS(id=8,url=item['url'],title=item['title'],content=item['content'],summary='',source_metadata={'content_kind':'full_text'})]
    second=select_originals(f,2,[],gap=True)
    assert second[0][1][0]['url']=='https://original.test/2'
    assert len(f.journal.data['chapter_candidate_queues']['2'])==3


def test_unknown_model_id_is_recorded_without_discarding_valid_choices(tmp_path):
    def invoke(role,*a):
        return '{"selected":[{"candidate_id":"C0001"}]}' if '规划' in role else '{"additional":[{"candidate_id":"unbound"},{"candidate_id":"C0003"}]}'
    f=flow(tmp_path,invoke)
    chosen=select_originals(f,2,batches(3))
    assert [items[0]['url'] for _,items in chosen]==['https://original.test/0','https://original.test/2']
    assert len(f.journal.data['candidate_originals'])==3
    receipt=f.journal.data['candidate_selection_receipts']['2'][-1]
    assert receipt['status']=='finished'
    assert receipt['batches'][0]['unresolved_references'][0]['entry']['candidate_id']=='unbound'


def test_large_locator_set_is_read_in_full_by_both_roles_without_a_source_count_cap(tmp_path):
    original=batches(136)
    for i,item in enumerate(original[0][1]):item['summary']='完整候选摘要'+str(i)+'。'*2390+'结尾限制条件'+str(i)
    expected={f'C{i+1:04d}':item['summary'] for i,item in enumerate(original[0][1])}
    seen={'规划':{},'核对':{}};sizes=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);kind='规划' if '规划' in role else '核对'
        sizes.append(len(prompt))
        for row in data['candidates']:seen[kind][row['candidate_id']]=row['search_excerpt']
        key='selected' if kind=='规划' else 'additional'
        return json.dumps({key:[{'candidate_id':row['candidate_id'],'reason':'该候选补齐一个不同字段。'} for row in data['candidates']]})
    f=flow(tmp_path,invoke);selected=select_originals(f,2,original)
    assert seen=={'规划':expected,'核对':expected} and len(selected)==136
    assert max(sizes)<65000
    receipt=f.journal.data['candidate_selection_receipts']['2'][-1]
    assert len(receipt['batches'])>1 and all(r['status']=='finished' for r in receipt['batches'])
    assert len(receipt['selected_ids'])==136 and not receipt['deferred_ids']


def test_failed_later_batch_keeps_candidates_and_reuses_successful_batch_reviews(tmp_path):
    original=batches(60)
    for item in original[0][1]:item['summary']='完整摘要。'*600
    stopped=[False];first_group=[None]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);group=data['candidates'][0]['candidate_id']
        if first_group[0] is None:first_group[0]=group
        if group!=first_group[0] and not stopped[0]:
            stopped[0]=True;raise RuntimeError('temporary model outage')
        if stopped[0] and group==first_group[0]:pytest.fail('completed first batch must come from cache')
        key='selected' if '规划' in role else 'additional'
        return json.dumps({key:[{'candidate_id':r['candidate_id'],'reason':'互补原件。'} for r in data['candidates']]})
    f=flow(tmp_path,invoke)
    with pytest.raises(RuntimeError,match='temporary'):select_originals(f,2,original)
    receipt=f.journal.data['candidate_selection_receipts']['2'][-1]
    assert receipt['batches'][0]['status']=='finished' and receipt['batches'][-1]['status']=='failed'
    assert len(f.journal.data['candidate_originals'])==60
    assert len(select_originals(f,2,[]))==60
