"""Reconcile question coverage across reading batches before planning searches.

A batch can establish a field; it cannot establish that other batches lack it.
All batch results enter a bounded merge tree. Models choose complementary
support, while original proofs and every merge receipt remain recoverable.
"""
from copy import deepcopy
import json
import os
import re
from pathlib import Path

from company_flow_state import fingerprint


STATUSES={'answered','partial','missing','conflict','not_applicable'}


def _support_bindings(values,proofs):
    """Recover formatting changes only when exactly one supplied ID matches."""
    aliases={}
    def key(value):
        match=re.fullmatch(r'P-([0-9a-f]+)',value.strip(),re.I) if isinstance(value,str) else None
        return (match[1].lower().lstrip('0') or '0') if match else None
    for known in proofs:
        normalized=key(known)
        if normalized is not None:aliases.setdefault(normalized,[]).append(known)
    bound=[];corrections=[]
    for value in values:
        canonical=value
        if isinstance(value,str) and value not in proofs:
            candidates=aliases.get(key(value),[])
            if len(candidates)==1:
                canonical=candidates[0]
                corrections.append({'proposed_id':value,'bound_id':canonical,
                    'basis':'unique_known_hex_identifier_format'})
        if canonical not in bound:bound.append(canonical)
    return bound,corrections


def _checked(response,questions,proofs,target):
    rows=response.get('questions')
    expected={q['id'] for q in questions}
    if (not isinstance(rows,list) or len(rows)!=len(expected) or
            {r.get('id') for r in rows if isinstance(r,dict)}!=expected):
        raise ValueError('incomplete_cross_batch_question_coverage')
    by_id={q['id']:q for q in questions};result=[]
    for row in rows:
        support,corrections=_support_bindings(row.get('support_ids') or [],proofs);status=row.get('status')
        if status not in STATUSES or any(i not in proofs for i in support):
            raise ValueError('unbound_cross_batch_question_support')
        if status in {'answered','partial','not_applicable'} and not support:
            raise ValueError('unsupported_cross_batch_question_status')
        if status=='conflict' and len(support)<2:raise ValueError('unbound_cross_batch_conflict')
        if not str(row.get('reason') or '').strip():raise ValueError('missing_cross_batch_question_reason')
        if not isinstance(row.get('missing_fields',[]),list):raise ValueError('invalid_cross_batch_missing_fields')
        result.append({**by_id[row['id']],**deepcopy(row),'support_ids':support,
            'support_id_normalizations':corrections,'review_origin':'model_review'})
    unresolved={q['id'] for q in result if q['status'] not in {'answered','not_applicable'}}
    queries=[]
    for row in response.get('follow_up_queries',[]):
        if not isinstance(row,dict) or row.get('question_id') not in expected:
            raise ValueError('invalid_cross_batch_query_question')
        query=str(row.get('query') or '').strip()
        if row['question_id'] not in unresolved or not query:continue
        if target not in query:query=target+' '+query
        if query not in [q['query'] for q in queries]:queries.append({**deepcopy(row),'query':query})
    return {'questions':result,'follow_up_queries':queries,
        'local_search_terms':list(dict.fromkeys(str(t).strip() for t in response.get('local_search_terms',[]) if str(t).strip()))}


def consolidate_questions(target,questions,proofs,batches,invoke,*,request_scope=None,previous=None,receipt_callback=None):
    """No top-N query cap and no private-material keyword blacklist."""
    previous=previous or {}
    legacy_order=bool(previous.get('receipts')) and not previous.get('batch_order')
    batches=[b for b in batches if not b.get('error') and b.get('response')]
    def batch_id(b):return b.get('input_sha256') or fingerprint(b['response'])
    # Checkpoint dictionaries may reorder receipts; that is not new evidence.
    batch_order=previous.get('batch_order') or ([batch_id(b) for b in batches] if legacy_order else [])
    proof_order=previous.get('proof_order') or ([p['id'] for p in proofs] if legacy_order else [])
    ranks={key:i for i,key in enumerate(batch_order)}
    batches=sorted(batches,key=lambda b:(ranks.get(batch_id(b),len(ranks)),batch_id(b)))
    ranks={key:i for i,key in enumerate(proof_order)}
    proofs=sorted(proofs,key=lambda p:(ranks.get(p['id'],len(ranks)),p['id']))
    batch_order=[batch_id(b) for b in batches];proof_order=[p['id'] for p in proofs]
    by_id={p['id']:p for p in proofs}
    nodes=[deepcopy(b['response']) for b in batches]
    if len(nodes)<2:return None
    initial_count=len(nodes)
    from report_agents.company_task_batches import size,TASK_BYTES
    cached={r['input_sha256']:r for r in (previous or {}).get('receipts',[]) if r.get('response') and not r.get('error')}
    receipts=list(deepcopy(cached).values())
    def remember(receipt):
        receipts[:]=[r for r in receipts if r['input_sha256']!=receipt['input_sha256']]+[receipt]
        if receipt_callback:receipt_callback({'receipts':deepcopy(receipts),'batch_order':batch_order,'proof_order':proof_order})
    rules=('把分批阅读的必答问题判断合成为本章统一结论，并据此制定补采计划。'
        '每批只看到局部资料：某批没看到字段，不等于任务里没找到。先用其他批次的已核原文补足，'
        '不能把已有统一社会信用代码、股东信息等反复列为缺失，也不能把局部missing按票数合并。'
        '逐个问题保留各字段适用主体、期间、口径和实际支持；不同资本数值、不同期间持股比例需解释时保留真实缺口，'
        '不把不同时点数值自动当错误，不从历史披露推出研究截至日状态。'
        '已有原文可直接说明或复算的变化原因，应记为已知解释；若仍需独立确认，明确只是核验边界，'
        '不把“等待公司书面确认”改写成原因从未找到，也不为此重复搜索同义公开材料。'
        'support_ids选能互补地支持全部结论的原文，重复同义证据可以合并；所有原始判断和未选证明仍保留在台账。'
        '只有经过跨批核对仍缺的具体公开原件才形成follow_up_queries。将重复检索意图合并，保留不同字段、时点或相反证据的必要查询。'
        '不设查询条数上限，也不因某个关键词排除查询。需要公司提供的合同、股东名册或最新状态证明等，'
        '写明next_step向谁取得什么，不把索取内部文件本身当公开网页搜索。'
        '不要假设某日或某期报告一定公开存在，不要求公开资料证明未披露的当日内部状态。'
        '没有可解决缺口的公开搜索时可返回空查询，明确保留未解决的问题，不能视为无风险或已完成。'
        '返回JSON {questions:[{id,status:answered/partial/missing/conflict/not_applicable,support_ids:[],missing_fields:[],reason,next_step}],'
        'follow_up_queries:[{question_id,query,reason}],local_search_terms:[]}。每个必答问题都要返回，support_ids只用本次资料的proofs。\n资料：\n')
    compact_merge=False
    def packet(group):
        ids={i for n in group for q in n.get('questions',[]) for i in q.get('support_ids',[]) if i in by_id}
        result={'target':target,'questions':questions,'request_scope':request_scope or {},
            'batch_results':group,'proofs':[p for p in proofs if p['id'] in ids],
            'view_scope':'已读各批问题判断及其原文支持；原始批次保留，不是新事实来源。'}
        if compact_merge or size(result)>TASK_BYTES-size(rules)-5000:
            # These originals were independently checked in the leaf batches.
            # Merge their complete judgments, not another ever-growing copy of
            # all originals. Keep every ID and explicitly disclose visibility.
            result['proofs']=[{**{k:v for k,v in p.items() if k!='text'},
                'native_text_sha256':fingerprint(p.get('text','')),
                'visible_scope':'原文在已保存的分批核验中；本次仅提供核验结论和定位'} for p in result['proofs']]
            result['view_scope']='只合并batch_results已有的字段结论、差异及缺口，不新增事实、计算或推论；原文编号全部保留，原文不在本次重发。既有判断不能支持的问题仍保留缺口。'
            if compact_merge:
                # IDs resolve to the complete leaf evidence ledger. Keep scope
                # and period here; repeated URLs/hashes cannot prevent merging.
                result['proofs']=[{k:v for k,v in p.items() if k in {'id','scope','period'}} for p in result['proofs']]
        return result
    def call(role,prompt,data,input_nodes):
        from company_chapters import CONTRACT_SHA256
        legacy_key=fingerprint(['question-consolidation-v2',
            CONTRACT_SHA256,role,prompt,os.getenv('MODEL'),os.getenv('BASE_URL')])
        prefix,marker,payload=prompt.rpartition('资料：\n')
        if marker:prompt=prefix+marker+json.dumps(json.loads(payload),ensure_ascii=False,sort_keys=True)
        key=fingerprint(['question-consolidation-v2',
            CONTRACT_SHA256,role,prompt,os.getenv('MODEL'),os.getenv('BASE_URL')])
        receipt={'input_sha256':key,'role':role,'input_nodes':input_nodes,'proof_ids':[p['id'] for p in data['proofs']]}
        try:
            if size(prompt)>TASK_BYTES:raise ValueError('question_consolidation_exceeds_input_budget')
            saved=cached.get(key) or cached.get(legacy_key)
            if saved:
                # Preserve legacy response ordering while migrating its key.
                if saved.get('raw_response'):
                    receipt['raw_response']=saved['raw_response']
                    answer=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',saved['raw_response'].strip()))
                else:answer=deepcopy(saved['response'])
            else:
                receipt['raw_response']=str(invoke(role,prompt,None))
                answer=json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',receipt['raw_response'].strip()))
            receipt['response']=answer
            value=_checked(answer,questions,{p['id']:p for p in data['proofs']},target)
            receipt['checked_response']=deepcopy(value)
        except Exception as exc:
            receipt['error']=type(exc).__name__;remember(receipt)
            raise
        remember(receipt)
        return value
    while True:
        groups=[];group=[]
        for node in nodes:
            proposal=group+[node]
            if group and size(packet(proposal))>TASK_BYTES-size(rules)-5000:
                groups.append(group);group=[node]
            else:group=proposal
        if group:groups.append(group)
        if len(groups)==len(nodes):
            if not compact_merge:
                compact_merge=True
                continue
            raise ValueError('question_consolidation_needs_narrower_evidence')
        merged=[]
        for group in groups:
            if len(group)==1:
                merged.append(group[0]);continue
            data=packet(group);prompt=rules+json.dumps(data,ensure_ascii=False)
            value=call('公司本章问题跨批综合核对',prompt,data,len(group))
            merged.append(value)
        if len(merged)==1:
            review_data={**data,'proposed_chapter_result':merged[0]}
            result=call('公司本章补采计划独立核对',rules.replace('把分批阅读的必答问题判断合成为本章统一结论，并据此制定补采计划。',
                '独立核对本章跨批结论与补采计划，并返回修正后的完整结果。'
                '逐项核查：已找到的字段是否又被列为缺失、已可复算的变化是否又被声称原因不明、'
                '公司披露与独立验证是否区分、内部取证是否被当成公开搜索、必要的不同字段与相反证据查询是否误删。')+
                json.dumps(review_data,ensure_ascii=False),review_data,1)
            result['consolidation']={'receipts':receipts,'initial_batches':initial_count,
                'batch_order':batch_order,'proof_order':proof_order}
            return result
        nodes=merged
