from types import SimpleNamespace as NS

from company_chapter_retrieval import rank_questions,required_nodes
from company_progressive_reading import directory
from research_store import ResearchObjectStore


def _prepared(body='2025 年限制性股票激励计划分两期授予，向激励对象实际授予限制性股票。'):
    docs=[]
    for sid,title,text in [('7','2025 年年度报告',body),
                           ('70','2026 年股权激励计划草案','拟授予限制性股票，尚需审议通过。')]:
        docs.append({'source_id':sid,'source_title':title,'original_sha256':'hash-'+sid+'-'+str(len(text)),
            'windows':[{'window_id':'W1','start':0,'end':len(text),'quote':text,
                'native_window_sha256':'window-'+sid+'-'+str(len(text)),'physical_page':64 if sid=='7' else 8}]})
    return docs,{},[]


def test_actual_grant_and_proposed_plan_are_separate_required_originals(monkeypatch):
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','false')
    prepared=_prepared();nodes=directory(prepared,('激励',))
    ranked,receipt=rank_questions(NS(store=None),4,nodes,prepared,
        [{'id':'Q04-2','question':'激励、应变与组织创新有何具体执行证据？'}])
    required,_=required_nodes(ranked,nodes,36000)
    assert {n['source_id'] for n in nodes if n['node_id'] in required}=={'7','70'}
    assert receipt['vector_status']=='disabled'


def test_embedding_cache_is_project_and_original_scoped(tmp_path,monkeypatch):
    import embedding
    class FakeEmbedder:
        calls=[]
        def get_embeddings(self,texts):
            self.calls.extend(texts)
            return [[float(len(t)),1.0] for t in texts]
    fake=FakeEmbedder()
    monkeypatch.setattr(embedding.embedding_service,'get_local_embeddings',fake.get_embeddings)
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','true')
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path
    prepared=_prepared();nodes=directory(prepared,('激励',))
    question=[{'id':'Q04-2','question':'激励、应变与组织创新有何具体执行证据？'}]
    def run(project_id,corpus):
        flow=NS(store=store,journal=NS(prefix=f'projects/{project_id}/company_chapter_flow'))
        return rank_questions(flow,4,directory(corpus,('激励',)),corpus,question)[1]
    first=run(1,prepared)
    assert first['vector_status']=='ready' and first['new_embeddings']==2
    second=run(1,prepared)
    assert second['new_embeddings']==0
    assert run(2,prepared)['new_embeddings']==2
    changed=_prepared(body='更新后的原文：2025 年实际授予限制性股票。')
    assert run(1,changed)['new_embeddings']==1
    assert len(list(tmp_path.rglob('*.npz')))==10


def test_question_first_hit_is_not_replaced_by_source_diversity():
    nodes=[{'node_id':'annual-governance','source_id':'7','chars':100},
        {'node_id':'annual-grant','source_id':'7','chars':100},
        {'node_id':'draft-plan','source_id':'70','chars':100}]
    ranked={'governance':['annual-governance'],
        'grant':['annual-grant','draft-plan']}
    required,_=required_nodes(ranked,nodes,200,per_question=1)
    assert required==['annual-governance','annual-grant']


def test_bm25_finds_chapter_three_variation_without_other_project_sources(monkeypatch):
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','false')
    target='北京金山办公软件股份有限公司'
    local='报告期完成增资，办理工商登记手续并换发营业执照。'
    foreign='历史工商变更与业务发展历程。'
    prepared=[{'source_id':sid,'source_title':title,'original_sha256':'hash-'+sid,
        'windows':[{'window_id':'W1','start':0,'end':len(body),'quote':body,
            'native_window_sha256':'window-'+sid,'physical_page':1}]}
        for sid,title,body in [('1',target+'工商变更公告',local),
            ('2','其他公司历史工商变更',foreign)]]
    nodes=directory((prepared,{},[]))
    project=NS(target_name=target,sources=[NS(id=1,title=target+'工商变更公告',source_metadata={})])
    flow=NS(store=None,project=project,journal=NS(prefix='projects/1/company_chapter_flow'))
    ranked,receipt=rank_questions(flow,3,nodes,(prepared,{},[]),[
        {'id':'Q03-1','question':target+'有哪些工商、业务与资本发展节点？'}])
    selected={node['node_id']:node['source_id'] for node in nodes}
    assert ranked['Q03-1#历史工商变更']
    assert {selected[nid] for pool in ranked.values() for nid in pool}=={'1'}
    assert receipt['indexed_windows']==1
    assert receipt['bm25_scope']=='current_project_originals'


def test_bm25_index_reuses_only_unchanged_project_originals(monkeypatch):
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','false')
    prepared=_prepared();nodes=directory(prepared,('激励',))
    flow=NS(store=None,journal=NS(prefix='projects/1/company_chapter_flow'))
    question=[{'id':'Q04-2','question':'激励、应变与组织创新有何具体执行证据？'}]
    rank_questions(flow,4,nodes,prepared,question)
    first=flow._chapter_bm25_cache
    rank_questions(flow,4,nodes,prepared,question)
    assert flow._chapter_bm25_cache is first
    changed=_prepared(body='更新后的原文：2025 年实际授予限制性股票。')
    rank_questions(flow,4,directory(changed,('激励',)),changed,question)
    assert flow._chapter_bm25_cache is not first


def test_semantic_candidate_can_surface_differently_worded_original(tmp_path,monkeypatch):
    import embedding
    def vectors(texts):
        return [[1.,0.] if '执照更新' in text or '历史工商变更' in text else [0.,1.]
            for text in texts]
    monkeypatch.setattr(embedding.embedding_service,'get_local_embeddings',vectors)
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','true')
    target='北京金山办公软件股份有限公司'
    docs=[]
    for sid,body in [('1','完成执照更新，注册登记事项已经核准。'),
                     ('2','研发团队开展新产品试点。')]:
        docs.append({'source_id':sid,'source_title':target+'公告',
            'original_sha256':'hash-'+sid,'windows':[{'window_id':'W1',
                'start':0,'end':len(body),'quote':body,
                'native_window_sha256':'window-'+sid,'physical_page':1}]})
    prepared=(docs,{},[]);nodes=directory(prepared)
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path
    project=NS(target_name=target,sources=[NS(id=sid,title=target+'公告',source_metadata={})
        for sid in (1,2)])
    flow=NS(store=store,project=project,journal=NS(prefix='projects/1/company_chapter_flow'))
    ranked,receipt=rank_questions(flow,3,nodes,prepared,[
        {'id':'Q03-1','question':target+'有哪些工商、业务与资本发展节点？'}])
    by_id={node['node_id']:node['source_id'] for node in nodes}
    assert receipt['vector_status']=='ready'
    assert by_id[ranked['Q03-1#历史工商变更'][0]]=='1'


def test_vector_never_embeds_another_projects_original(tmp_path,monkeypatch):
    import embedding
    observed=[]
    def vectors(texts):
        observed.extend(texts)
        return [[1.,0.] for _ in texts]
    monkeypatch.setattr(embedding.embedding_service,'get_local_embeddings',vectors)
    monkeypatch.setenv('COMPANY_CHAPTER_VECTOR_ENABLED','true')
    target='北京金山办公软件股份有限公司'
    docs=[]
    for sid,body in [('1','本项目工商登记已更新。'),('2','其他项目的内部资料。')]:
        docs.append({'source_id':sid,'source_title':target+'公告',
            'original_sha256':'hash-'+sid,'windows':[{'window_id':'W1',
                'start':0,'end':len(body),'quote':body,
                'native_window_sha256':'window-'+sid,'physical_page':1}]})
    prepared=(docs,{},[])
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path
    project=NS(target_name=target,sources=[NS(id=1,title=target+'公告',source_metadata={})])
    flow=NS(store=store,project=project,journal=NS(prefix='projects/1/company_chapter_flow'))
    _,receipt=rank_questions(flow,3,directory(prepared),prepared,[
        {'id':'Q03-1','question':target+'有哪些工商、业务与资本发展节点？'}])
    assert receipt['indexed_windows']==1
    assert receipt['new_embeddings']==1
    assert '其他项目的内部资料。' not in observed
