"""Project-scoped, source-bound retrieval for progressive chapter reading.

Native windows are the indexed children; reading nodes are their parent context.
The vector cache lives below the existing flow prefix and is removed with it.
"""
from collections import Counter, defaultdict
from io import BytesIO
import math
import os
import re

from company_flow_state import fingerprint


VERSION = 'company-chapter-retrieval-v4-question-preserving'
VECTOR_CACHE_VERSION = 'company-chapter-retrieval-v1'
ASPECT_TERMS={
    '激励':'股权激励 限制性股票 实际授予 员工持股 薪酬 考核',
    '决策':'董事会 审议 决议 授权',
    '执行':'实施 落实 实际执行',
}

# Additional queries preserve separate evidence needs; the original is always searched.
QUESTION_TOPICS=(
    (('收入','利润','现金流'), '经营财务', '营业收入 营业利润 净利润 经营活动现金流量净额'),
    (('各业务','分部','分部门'), '分部披露', '分部信息 报告分部 财务信息 利润 现金流'),
    (('会计期间','币种','口径'), '会计口径', '会计期间 币种 合并范围 调整口径 内部抵销 财务报表附注'),
    (('研发','技术成果'), '研发进展', '研发 在研项目 进展 阶段 投入 计划'),
    (('产品','业务发展'), '产品上线', '产品 上线 正式 发布 商用'),
    (('研发','经营计划','市场前景','增长预期'), '经营计划', '经营计划 未来 产品 市场 战略'),
    (('交付','服务保障'), '服务交付', '服务 响应 时效 SLA 验收 培训'),
    (('产能','生产'), '生产能力', '生产 产能 产量 利用率 服务器 负载'),
)


def question_searches(number, questions, *, expanded=False):
    from company_chapters import chapter
    searches=[]
    for q in questions:
        if expanded and q.get('status') in {'answered','not_applicable'}:continue
        searches.append((q['id'],q['question']))
        for triggers,label,query in QUESTION_TOPICS:
            if any(term in q['question'] for term in triggers):
                searches.append((q['id']+'#'+label,query))
        # Layout headings may add vocabulary, but generic labels cannot route a question.
        generic={'业务','核心','运营','核验','情况','说明','关键','数据','信息'}
        for section in chapter(number)['sections']:
            if not any(term in q['question'] for term in re.findall(r'[\u4e00-\u9fff]{2}',section)
                       if term not in generic):continue
            expansion=' '.join(words for term,words in ASPECT_TERMS.items() if term in section)
            if '激励' in section:
                searches.extend([(q['id']+'#'+section+'-授予','年度报告 报告期 激励对象 首次授予 预留授予 万股 实际授予'),
                    (q['id']+'#'+section+'-执行','激励 实际授予 已授予 归属 实施记录 年度报告')])
            searches.append((q['id']+'#'+section,(section+' '+expansion).strip()))
    return list(dict.fromkeys(searches))


def balanced_candidates(ranked, limit):
    """Reserve a slot per query before taking its next hit; deduplicate overlaps."""
    chosen=[];seen=set()
    for offset in range(max((len(pool) for pool in ranked.values()),default=0)):
        for pool in ranked.values():
            if offset>=len(pool) or pool[offset] in seen:continue
            seen.add(pool[offset]);chosen.append(pool[offset])
            if len(chosen)>=limit:return chosen
    return chosen


def _terms(value):
    text=str(value).casefold()
    return list(dict.fromkeys(re.findall(r'[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}',text)))


def _lexical(query, passage):
    terms=_terms(query)
    text=passage.casefold()
    return sum(min(3,text.count(term)) * (2 if len(term)>3 else 1) for term in terms)


def _bm25_tokens(value):
    """Local Chinese character/ngram terms plus whole ASCII words."""
    tokens=[]
    for part in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+',str(value).casefold()):
        if not '\u4e00' <= part[0] <= '\u9fff':
            tokens.append(part)
            continue
        tokens.extend(part)
        tokens.extend(part[i:i+2] for i in range(len(part)-1))
        tokens.extend(part[i:i+3] for i in range(len(part)-2))
    return tokens


def _project_bm25(flow, prepared, passages):
    """Build an in-memory index only from this project's prepared originals."""
    scope=getattr(getattr(flow,'journal',None),'prefix','')
    key=fingerprint([VERSION,scope,[(str(doc['source_id']),doc['original_sha256'],
        [(w['window_id'],w['native_window_sha256']) for w in doc['windows']])
        for doc in prepared[0]]])
    cached=getattr(flow,'_chapter_bm25_cache',None)
    if cached and cached[0]==key:return cached[1]
    state=_bm25_index(passages)
    flow._chapter_bm25_cache=(key,state)
    return state


def _bm25_index(passages):
    postings=defaultdict(list);lengths=[]
    for index,(_,_,_,text) in enumerate(passages):
        terms=Counter(_bm25_tokens(text));lengths.append(sum(terms.values()))
        for term,count in terms.items():postings[term].append((index,count))
    return postings,lengths,sum(lengths)/max(1,len(lengths))


def rank_read_facts(facts, number, questions, *, searches=None):
    """Recall validated native facts independently of the selected original nodes."""
    passages=[(str(i),str(f['source_id']),str(i),f['fact']) for i,f in enumerate(facts)]
    index=_bm25_index(passages)
    ranked={qid:_bm25_rank(query,passages,index,limit=8)
            for qid,query in (searches if searches is not None else question_searches(number,questions))}
    originals={qid:pool for qid,pool in ranked.items() if '#' not in qid}
    selected=list(dict.fromkeys(balanced_candidates(originals,16)+balanced_candidates(ranked,48)))[:48]
    return [{**facts[int(i)],'_retrieval_question_ids':[qid for qid,pool in ranked.items() if i in pool]}
            for i in selected]


def _bm25_rank(query, passages, index, *, allowed_sources=None, limit=40):
    postings,lengths,average=index
    count=len(passages);scores=defaultdict(float)
    for term in set(_bm25_tokens(query)):
        rows=postings.get(term,())
        if not rows:continue
        idf=math.log(1+(count-len(rows)+0.5)/(len(rows)+0.5))
        for position,frequency in rows:
            denominator=frequency+1.5*(0.25+0.75*lengths[position]/max(average,1))
            scores[position]+=idf*frequency*2.5/denominator
    nodes={}
    for position,score in scores.items():
        if allowed_sources is not None and passages[position][1] not in allowed_sources:continue
        nid=passages[position][0]
        nodes[nid]=max(score,nodes.get(nid,0))
    return sorted(nodes,key=lambda nid:(-nodes[nid],nid))[:limit]


def _vector_cache(flow, prepared, model_name):
    """Reuse embeddings by project/source/body/model; never embed discovery snippets."""
    import numpy as np
    from embedding import embedding_service

    rows=[]; vectors=[]; created=0
    prefix=flow.journal.prefix+'/retrieval'
    for doc in prepared[0]:
        windows=doc['windows']
        if not windows:continue
        digest=fingerprint([VECTOR_CACHE_VERSION,model_name,doc['source_id'],doc['original_sha256'],
            [(w['window_id'],w['native_window_sha256']) for w in windows]])
        key=prefix+'/'+digest+'.npz'
        try:
            payload=flow.store.get_bytes(key)
            with np.load(BytesIO(payload),allow_pickle=False) as cached:
                values=cached['vectors'].astype('float32')
            if values.shape[0]!=len(windows):raise ValueError('chapter_vector_cache_count_mismatch')
        except FileNotFoundError:
            # Long annual reports can take minutes on CPU. Persist each small
            # batch so an interrupted project build never repeats completed work.
            batches=[]
            for start in range(0,len(windows),16):
                group=windows[start:start+16]
                batch_key=prefix+'/'+digest+'-'+str(start)+'.npz'
                try:
                    payload=flow.store.get_bytes(batch_key)
                    with np.load(BytesIO(payload),allow_pickle=False) as cached:
                        batch=cached['vectors'].astype('float32')
                    if batch.shape[0]!=len(group):raise ValueError('chapter_vector_batch_count_mismatch')
                except FileNotFoundError:
                    batch=np.asarray(embedding_service.get_local_embeddings([w['quote'] for w in group]),
                        dtype='float32')
                    if batch.ndim!=2 or batch.shape[0]!=len(group):
                        raise ValueError('chapter_vector_batch_count_mismatch')
                    stream=BytesIO();np.savez_compressed(stream,vectors=batch.astype('float16'))
                    flow.store.put_bytes(batch_key,stream.getvalue())
                    created+=len(group)
                batches.append(batch)
            values=np.concatenate(batches)
            if values.ndim!=2 or values.shape[0]!=len(windows):raise ValueError('chapter_vector_count_mismatch')
            stream=BytesIO();np.savez_compressed(stream,vectors=values.astype('float16'))
            flow.store.put_bytes(key,stream.getvalue())
        rows.extend((str(doc['source_id']),w['window_id']) for w in windows)
        vectors.append(values)
    if not vectors:return rows,np.empty((0,0),dtype='float32'),created
    return rows,np.concatenate(vectors),created


def rank_questions(flow, number, nodes, prepared, questions, *, expanded=False, searches=None):
    """Return ranked parent nodes per question and an explicit vector receipt."""
    project=getattr(flow,'project',None)
    project_ids={str(source.id) for source in project.sources or []} if project is not None else None
    scoped_docs=[doc for doc in prepared[0] if project_ids is None or
        str(doc['source_id']) in project_ids]
    scoped_prepared=(scoped_docs,prepared[1],prepared[2])
    node_by_window={}
    for node in nodes:
        if project_ids is not None and str(node['source_id']) not in project_ids:continue
        for wid in node['window_ids']:
            node_by_window[(str(node['source_id']),wid)]=node['node_id']
    passages=[]
    for doc in scoped_docs:
        for window in doc['windows']:
            nid=node_by_window.get((str(doc['source_id']),window['window_id']))
            if nid:
                passages.append((nid,str(doc['source_id']),window['window_id'],
                    doc['source_title']+' '+window.get('heading','')+' '+window['quote']))
    target=getattr(project,'target_name','')
    if target:
        allowed_bm25_sources={str(source.id) for source in project.sources or []
            if target in (source.title or '') or target in (getattr(source,'content','') or '') or
            (source.source_metadata or {}).get('entity_validation')=='verified'}
    else:
        allowed_bm25_sources=None
    searches=question_searches(number,questions,expanded=expanded) if searches is None else searches
    lexical={};bm25={}
    bm25_index=_project_bm25(flow,scoped_prepared,passages)
    for qid,query in searches:
        scores=defaultdict(float)
        for nid,_,_,text in passages:
            scores[nid]=max(scores[nid],_lexical(query,text))
        lexical[qid]=sorted((nid for nid in scores if scores[nid]>0),
            key=lambda nid:(-scores[nid],nid))[:40]
        bm25[qid]=_bm25_rank(query,passages,bm25_index,
            allowed_sources=allowed_bm25_sources)
    semantic={};status='disabled';created=0
    if (os.getenv('COMPANY_CHAPTER_VECTOR_ENABLED','false').lower() not in {'false','0','off'}
            and passages and getattr(flow,'store',None) is not None):
        try:
            import numpy as np
            from embedding import embedding_service
            model=os.getenv('EMBEDDING_MODEL','BAAI/bge-m3')
            vector_docs=[doc for doc in scoped_docs if allowed_bm25_sources is None or
                str(doc['source_id']) in allowed_bm25_sources]
            rows,vectors,created=_vector_cache(flow,(vector_docs,prepared[1],prepared[2]),model)
            norms=np.linalg.norm(vectors,axis=1)
            query_vectors=np.asarray(embedding_service.get_local_embeddings([query for _,query in searches]),dtype='float32')
            if query_vectors.shape!=(len(searches),vectors.shape[1]):
                raise ValueError('chapter_query_vector_dimension_mismatch')
            for (qid,_),vector in zip(searches,query_vectors):
                if vector.shape!=(vectors.shape[1],):raise ValueError('chapter_query_vector_dimension_mismatch')
                similarity=(vectors@vector)/(np.maximum(norms,1e-9)*max(np.linalg.norm(vector),1e-9))
                ranked=sorted((i for i in range(len(rows)) if allowed_bm25_sources is None or
                    rows[i][0] in allowed_bm25_sources),
                    key=lambda i:(-float(similarity[i]),rows[i]))[:80]
                semantic[qid]=list(dict.fromkeys(node_by_window[rows[i]] for i in ranked
                    if rows[i] in node_by_window))[:40]
            status='ready'
        except Exception as exc:
            status='unavailable:'+type(exc).__name__
    ranked={}
    for qid,_ in searches:
        fused=defaultdict(float)
        for pool in (lexical.get(qid,[]),bm25.get(qid,[]),semantic.get(qid,[])):
            for rank,nid in enumerate(pool):fused[nid]+=1/(60+rank+1)
        merged=sorted(fused,key=lambda nid:(-fused[nid],nid))
        # Keep the strongest exact-text hit visible even when a dense model
        # favors a differently worded passage.
        ranked[qid]=list(dict.fromkeys(lexical.get(qid,[])[:1]+merged))[:24]
    return ranked,{'version':VERSION,'vector_status':status,'new_embeddings':created,
        'indexed_windows':len(passages),'bm25_scope':'current_project_originals',
        'question_candidate_counts':{qid:len(v) for qid,v in ranked.items()},
        'queries':[{'id':qid,'query':query} for qid,query in searches]}


def required_nodes(ranked, nodes, budget, *, per_question=2):
    """Reserve reading space fairly across questions before model suggestions."""
    known={n['node_id']:n for n in nodes}
    chosen=[];deferred=[];used=0
    for offset in range(per_question):
        for qid,pool in ranked.items():
            available=[nid for nid in pool if nid not in chosen and nid not in deferred]
            if not available:continue
            # Prefer a different original when the next candidate is comparable.
            sources={known[nid]['source_id'] for nid in chosen}
            different=next((nid for nid in available[:5] if known[nid]['source_id'] not in sources),None)
            nid=(different or available[0]) if offset else available[0]
            size=known[nid]['chars']
            if used+size<=budget:chosen.append(nid);used+=size
            else:deferred.append(nid)
    return chosen,deferred
