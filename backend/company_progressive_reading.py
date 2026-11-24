"""Question-led, page-addressed reading. Unselected bytes remain explicitly unread.

Design references: PageIndex/jev-doc-search section/page navigation; Crawl4AI's
separate raw/fit views; Docling's page/cell provenance. No upstream code copied.
"""
from copy import deepcopy
import hashlib
import json
import os
from company_chapters import chapter
from company_flow_state import fingerprint
from company_reading import parse_json
from report_agents.company_evidence import source_body

VERSION = 'company-progressive-reading-v8-protected-query-planning'


def chapter_fact_candidates(review, number, previous_usage=None, *, recalled=()):
    """Chapter labels are hints; retrieved native ranges admit reusable facts."""
    previous_usage=previous_usage or {}
    retained=[]
    if (previous_usage.get('current_chapter')==number
            and previous_usage.get('target_name')==review.get('target_name')
            and previous_usage.get('review_scope',{}).get('as_of_date')==review.get('research_as_of')):
        for row in previous_usage.get('records',[]):
            context=row.get('context',{});coverage=context.get('source_reading_coverage',{})
            if (row.get('decision',{}).get('status')!='use' or not context.get('original_sha256')
                    or coverage.get('original_sha256')!=context['original_sha256']
                    or not coverage.get('reviewed_windows') or not coverage.get('full_coverage_receipt_sha256')):
                continue
            for proof in row.get('provenance',[]):
                start=proof.get('original_offset');native=proof.get('native_quote')
                # native_quote can be a whole context window while its offset
                # belongs to the extracted fact. Retain that exact admitted
                # fact, not the larger quotation anchored at the wrong offset.
                quote=row.get('text') or native
                if isinstance(native,str) and isinstance(quote,str) and quote not in native:continue
                if isinstance(start,int) and isinstance(quote,str) and quote:
                    retained.append((str(proof.get('source_id')),context['original_sha256'],start,quote,coverage))
    candidates=[]
    for fact in review.get('facts',[]):
        st=review.get('source_review',{}).get(str(fact.get('source_id')), {})
        start=fact.get('original_offset');end=fact.get('original_end')
        if end is None and isinstance(start,int):end=start+len(fact.get('fact',''))
        in_read_range=(bool(fact.get('original_sha256')) and isinstance(start,int) and isinstance(end,int)
            and st.get('original_sha256')==fact.get('original_sha256')
            and any(start<b and end>a for a,b in st.get('coverage_ranges',[])))
        # Changing a reading plan must not erase evidence already admitted for
        # this chapter. Bind it to the same original and exact native passage.
        # A retained long passage is not a new recall of every extracted fact
        # inside it: that silently grows the writer input on each resume.
        prior_coverage=next((coverage for sid,digest,left,quote,coverage in retained
            if isinstance(start,int) and isinstance(end,int)
            and st.get('original_sha256')==fact.get('original_sha256')
            and sid==str(fact.get('source_id')) and digest==fact.get('original_sha256')
            and left==start and end==left+len(quote)
            and quote==fact.get('fact')),None)
        if in_read_range:candidates.append(fact)
        elif prior_coverage:
            candidates.append({**fact,'_prior_admission_coverage':deepcopy(prior_coverage)})
    identities={(str(f.get('source_id')),f.get('original_sha256'),f.get('original_offset'),f.get('fact'))
                for f in candidates}
    for fact in recalled:
        identity=(str(fact.get('source_id')),fact.get('original_sha256'),fact.get('original_offset'),fact.get('fact'))
        st=review.get('source_review',{}).get(str(fact.get('source_id')), {})
        if identity not in identities and st.get('original_sha256')==fact.get('original_sha256'):
            candidates.append(deepcopy(fact));identities.add(identity)
    return candidates


def validated_project_facts(flow, prepared=None):
    from company_reading_reuse import validated_read_facts
    prepared=prepared or flow.reader.prepare(flow.project)
    originals={str(s.id):source_body(s)[0] for s in flow.project.sources or []}
    journal=getattr(flow.journal,'data',{})
    scope=journal.get('scope',{})
    receipts=(journal.get('shared_read_receipts',{}).values()
        if scope.get('target_name')==flow.project.target_name
        and scope.get('as_of_date','')==flow.intake.get('as_of_date','') else ())
    return validated_read_facts(prepared,flow.reads.values(),flow.project.target_name,
        flow.intake.get('as_of_date',''),originals,receipts)


def recalled_fact_candidates(flow, number, prepared=None):
    from company_chapter_retrieval import rank_read_facts
    from company_query_planning import cached_searches
    facts=validated_project_facts(flow,prepared)
    questions=[{'id':f'Q{number:02d}-{i+1}','question':q.format(target=flow.project.target_name)}
        for i,q in enumerate(chapter(number)['questions'])]
    searches=(cached_searches(flow,number,questions,facts) if query_planning_enabled() else None)
    return rank_read_facts(facts,number,questions,searches=searches)


def query_planning_enabled():
    return os.getenv('COMPANY_CHAPTER_QUERY_PLANNING_ENABLED','true').lower() not in {'false','0','off'}


def compact_reading_coverage(coverage):
    """Compact once; replay must preserve both receipt identity and gap counts."""
    result={k:v for k,v in coverage.items() if k not in {'unread_ranges','structural_gaps'}}
    for field in ('unread_ranges','structural_gaps'):
        result[field+'_count']=(len(coverage[field] or []) if field in coverage
            else coverage.get(field+'_count',0))
    result['full_coverage_receipt_sha256']=coverage.get('full_coverage_receipt_sha256') or fingerprint(coverage)
    return result


def candidate_queue(ranked, decisions, known):
    """One bounded frontier for both selection and acceptance; no implicit exclusion."""
    from company_chapter_retrieval import balanced_candidates
    frontier=balanced_candidates({qid:pool[:3] for qid,pool in ranked.items()},3*len(ranked))
    excluded={}
    for row in decisions:
        if (isinstance(row,dict) and row.get('node_id') in frontier
                and row.get('status')=='exclude' and isinstance(row.get('reason'),str)
                and row['reason'].strip() and row.get('basis') in {'locator','original'}):
            excluded[row['node_id']]=row
    return [nid for nid in frontier if nid in known and nid not in excluded],excluded


def candidate_receipts(plan, review):
    """Read completion binds to actual windows, never just selection membership."""
    result={}
    for nid,entry in plan.get('candidate_ledger',{}).items():
        row=deepcopy(entry)
        st=review.get('source_review',{}).get(str(row['source_id']),{})
        read={wid for receipt in st.get('review_decisions',[]) if not receipt.get('error')
              for wid in receipt.get('window_ids',[])}
        if (st.get('original_sha256')==row['original_sha256']
                and set(row['window_ids']).issubset(read)):
            row['status']='read'
        result[nid]=row
    return result


def directory(prepared, terms=()):
    nodes = []
    for doc in prepared[0]:
        group = []
        def append():
            if not group:
                return
            start, end = group[0]['start'], group[-1]['end']
            matching=[]; match_count=0
            for window in group:
                quote=window['quote']
                hits=[(quote.find(term),term) for term in terms if term in quote]
                match_count+=sum(quote.count(term) for term in terms)
                if hits:
                    position,term=min(hits)
                    matching.append((len(hits), {'term':term,'page':window.get('physical_page'),
                        'excerpt':quote[max(0,position-65):position+175]}))
            matching.sort(key=lambda row:-row[0])
            nodes.append({'node_id': 'N-' + fingerprint([doc['source_id'], doc['original_sha256'], start, end])[:20],
                'source_id': doc['source_id'], 'source_title': doc['source_title'],
                'original_sha256': doc['original_sha256'], 'start': start, 'end': end,
                'pages': list(dict.fromkeys(w.get('physical_page') for w in group if w.get('physical_page'))),
                'headings': list(dict.fromkeys(w.get('heading') for w in group if w.get('heading'))),
                'opening_locator': group[0]['quote'][:160],
                'question_locators':[row for _,row in matching[:3]],
                'question_match_count':match_count,
                'window_ids': [w['window_id'] for w in group], 'chars': sum(len(w['quote']) for w in group)})
        for window in doc['windows']:
            if group and window['end'] - group[0]['start'] > 4800:
                append(); group = []
            group.append(window)
        append()
    return nodes


def plan_reading(flow, number, *, expanded=False):
    """One semantic plan per changed directory/phase, not a whole-document read."""
    from company_requirements import ROUTES
    # Bound NEW extraction batches per pass, not the number of cached receipts
    # replayed. This lets cold runs save a first draft before all selected
    # pages finish, just as interrupted runs can use existing successful work.
    flow.project.company_new_native_batch_limit=max(1,int(os.getenv('COMPANY_CHAPTER_NEW_READ_BATCHES','2')))
    prepared = flow.reader.prepare(flow.project)
    terms=tuple(dict.fromkeys(term for phrase in chapter(number)['query_terms']
        for term in phrase.split() if len(term)>=2 and term not in {'公司','原文','执行'}))
    nodes = directory(prepared,terms)
    source_hashes={str(s.id):hashlib.sha256(source_body(s)[0].encode()).hexdigest()
                   for s in flow.project.sources or []}
    available = {key: {'provider': value[0], 'tool': value[1], 'aspect': value[3]}
                 for key, value in ROUTES.items() if value[0] not in flow.disabled_platforms}
    confirmed=[r for r in flow.journal.data.get('confirmed_original_requests',{}).values()
        if r['chapter']==number and r.get('status')=='confirmed' and source_hashes.get(r['source_id'])==r['original_sha256']]
    questions=[{'id': f'Q{number:02d}-{i+1}', 'question': q.format(target=flow.project.target_name)}
        for i,q in enumerate(chapter(number)['questions'])]
    status={q['id']:q.get('status') for q in flow.coverage.get(number,{}).get('questions',[])}
    from company_chapter_retrieval import rank_questions,required_nodes,balanced_candidates
    searches=None
    if query_planning_enabled():
        from company_query_planning import ensure_query_plan,cached_searches
        facts=validated_project_facts(flow,prepared)
        ensure_query_plan(flow,number,questions,facts,expanded=expanded)
        searches=cached_searches(flow,number,[{**q,'status':status.get(q['id'])} for q in questions],facts,expanded=expanded,include_gaps=expanded)
    from report_agents.company_task_batches import model_scope
    stable_key=fingerprint(['stable-initial-reading-v1',VERSION,flow.project.target_name,
        flow.intake.get('as_of_date',''),number,nodes,questions,searches,confirmed,available,
        flow.read_char_budget,model_scope()])
    cache=flow.journal.data.setdefault('progressive_reading_plans',{})
    initial_keys=flow.journal.data.setdefault('initial_reading_plan_keys',{})
    if not expanded and initial_keys.get(stable_key) in cache:
        return deepcopy(cache[initial_keys[stable_key]])
    ranked,retrieval=rank_questions(flow,number,nodes,prepared,
        [{**q,'status':status.get(q['id'])} for q in questions],expanded=expanded,searches=searches)
    from company_reading_reuse import reusable_windows
    reusable=reusable_windows(prepared,flow.reads.values(),flow.project.target_name,flow.intake.get('as_of_date',''))
    costs={n['node_id']:0 if all((str(n['source_id']),wid) in reusable for wid in n['window_ids'])
           else n['chars'] for n in nodes}
    mandatory,mandatory_deferred=required_nodes(ranked,[{**n,'chars':costs[n['node_id']]} for n in nodes],flow.read_char_budget)
    # Original requirements receive visible slots and the first half of reading
    # space before any model-generated expansions. Unused space remains shared.
    originals={qid:pool for qid,pool in ranked.items() if '#' not in qid}
    protected,_=required_nodes(originals,[{**n,'chars':costs[n['node_id']]} for n in nodes],
        flow.read_char_budget//2,per_question=3)
    original_visible=balanced_candidates(originals,16)
    baseline={qid:pool for qid,pool in ranked.items() if '#plan-' not in qid}
    baseline_mandatory,_=required_nodes(baseline,[{**n,'chars':costs[n['node_id']]} for n in nodes],flow.read_char_budget)
    baseline_visible=balanced_candidates(baseline,40)
    by_id={n['node_id']:n for n in nodes}
    frontier,_=candidate_queue(ranked,[],by_id)
    focus_ids=list(dict.fromkeys(protected+baseline_mandatory+original_visible+baseline_visible+mandatory+frontier+balanced_candidates(ranked,56)))[:56]
    focused=[by_id[nid] for nid in focus_ids]
    visible_nodes=focused
    # Independently recalled facts need not win a second original-node selection.
    facts=recalled_fact_candidates(flow,number,prepared)
    packet = {'target': flow.project.target_name, 'chapter': number,
        **({'confirmed_original_requests':confirmed} if confirmed else {}),
        'questions': questions,
        'phase': 'supplement' if expanded else 'initial',
        'sources':{n['source_id']:n['source_title'][:100] for n in visible_nodes},
        'directory': [{'node_id':n['node_id'],'source_id':n['source_id'],
            'pages':n['pages'],'headings':[h[:60] for h in n['headings'][:1]],
            'question_locators':[{**r,'excerpt':r['excerpt'][:100]} for r in n['question_locators'][:1]],
            **({'opening_locator':n['opening_locator'][:80]} if not n['question_locators'] else {}),
            'chars':n['chars']} for n in visible_nodes],
        'retrieval':{k:v for k,v in retrieval.items() if k!='new_embeddings'},
        'candidate_frontier':{qid:pool[:3] for qid,pool in ranked.items()},
        'already_read_node_ids':[nid for nid,cost in costs.items() if cost==0],
        'other_directory_node_count':len(nodes)-len(visible_nodes),
        'known_fact_locators': [{'source_id': f.get('source_id'), 'statement_id':f['statement_id'],
                                'question_ids':list(dict.fromkeys(qid.split('#',1)[0] for qid in f.get('_retrieval_question_ids',[]))),
                                'text_locator': f.get('fact', '')[:100]}
                                for f in facts],
        'coverage': flow.coverage.get(number, {}).get('questions', []),
        'available_platform_routes': available,
        'read_char_budget': flow.read_char_budget,
        'scope': '目录、开头和事实定位片段不是全文；不能据此认定未选原文无用或未披露。'}
    # Initial navigation stays valid for the same source directory and scope.
    # Completing a reading batch must not force another 100K directory request
    # just to recover the draft. Supplement plans still bind current findings.
    identity=packet if expanded else {**packet,'known_fact_locators':[],'coverage':[],'already_read_node_ids':[]}
    key = fingerprint([VERSION, identity, os.getenv('MODEL'), os.getenv('BASE_URL')])
    cache = flow.journal.data.setdefault('progressive_reading_plans', {})
    cached = cache.get(key)
    if cached:
        if not expanded:
            initial_keys[stable_key]=key;flow.journal.save()
        return deepcopy(cached)
    prompt = ('为本章制定有界阅读与取证计划。逐题检查directory中的question_locators和给出的目录，'
            '每个尚未有充分证据的问题都应优先选择最相关的原文范围；'
            '先利用共享原文，按目录选择能回答本章问题的范围；'
            '保留多个互补或相反证据路径，不要求当前章先读全库。按优先顺序返回read_node_ids，'
            '选择总量尽量不超过read_char_budget；其他范围记作未读，不认定无用。'
            'already_read_node_ids已有原文阅读回执，可跨章复用，不占新增阅读预算。先检查known_fact_locators。'
            '只有定位原文显示具体证据而已有事实未提取时，返回reread_nodes:[{node_id,reason,missing_evidence}]，'
            '明确遗漏什么以及哪段定位支持补提取；不能仅因换章节或本题未完整回答就重读全部旧范围。'
            '已有相关原文但未读，应先选原文，不因尚未阅读而搜索同一资料。'
            'confirmed_original_requests是模型已确认要读的已有原件：优先按其reason选择相关目录范围；'
            '已核范围可复用，只补尚未核对的相关范围，仍遵守read_char_budget，不要求通读整份。'
            'initial_search_required只在没有可供本章关键问题阅读的相关原文时为true。'
            'queries只查具体未答问题；每条带question_id、query、reason和expected_fields。'
            '需要公司提供的完整名册、合同和状态确认写为缺口，不要求网上一定取得。'
            '专业工具只从available_platform_routes选择，重复已有成功返回无需再取。'
            '按每个问题分别返回最需要阅读的node_id，优先放能提供互补事实的少量原文；'
            'read_node_ids也列出这些编号，作为兼容字段。'
            '对candidate_frontier中明确不支持本章问题的候选，可返回candidate_decisions:[{node_id,status:"exclude",basis:"locator",reason}]。'
            '只有给定定位内容足以确认不相关才排除；不确定就阅读。不要仅因来源重复、年代早或缺少直接评价排除其他有用事实。'
            '未排除的frontier候选由程序优先安排阅读；预算不足保留尚未核验，不宣称资料不存在。'
            '返回JSON {candidate_decisions:[],reread_nodes:[],question_node_ids:{问题ID:[node_id]},read_node_ids:[],initial_search_required:true/false,queries:[],platform_routes:[],reason}。'
            '\n资料：\n' + json.dumps(packet, ensure_ascii=False,separators=(',',':')))
    attempts=flow.journal.data.setdefault('reading_plan_attempts',{})
    answer=deepcopy(attempts[key]['response']) if key in attempts else parse_json(
        flow.chapter_invoke(number)('公司本章阅读取证规划', prompt, None))
    attempts.setdefault(key,{'response':deepcopy(answer),'chapter':number})
    flow.journal.save()
    known = {n['node_id']: n for n in nodes}
    ids = answer.get('read_node_ids')
    if not isinstance(ids,list) or any(not isinstance(i,str) or i not in known for i in ids):
        entry=attempts[key]
        repair_prompt=('修复上一计划引用了不存在的目录编号。只能返回给定候选目录中真实node_id，不能编造或通过近似匹配绑定。'
            '根据原计划意图确认对应条目；无法确认的保留unresolved_ids，不得宣称其内容无用。'
            '返回JSON {read_node_ids:[],unresolved_ids:[],reason}。\n资料：\n'+json.dumps({
                'previous_plan':answer,'directory':[{'node_id':n['node_id'],'source_id':n['source_id'],
                    'title':n['source_title'][:100],'pages':n['pages'],'opening_locator':n['opening_locator'][:80]}
                    for n in visible_nodes]},ensure_ascii=False,separators=(',',':')))
        repair_key=fingerprint(repair_prompt)
        if 'id_repair_response' in entry:
            ids=entry['id_repair_response'].get('read_node_ids')
        elif not entry.get('id_repair_attempted') or entry.get('id_repair_input_sha256')!=repair_key:
            from report_agents.company_task_batches import TASK_BYTES
            if len(repair_prompt.encode())>TASK_BYTES:raise ValueError('reading_id_repair_exceeds_input_budget')
            entry['id_repair_attempted']=True;entry['id_repair_input_sha256']=repair_key;flow.journal.save()
            from company_flow_state import CompanyFlowLimit
            from research_workflow import WorkflowPaused
            try:
                repair=parse_json(flow.chapter_invoke(number)('公司本章阅读节点编号格式修复',repair_prompt,None))
            except (CompanyFlowLimit,WorkflowPaused):
                entry.pop('id_repair_attempted',None);flow.journal.save();raise
            entry['id_repair_response']=repair;flow.journal.save();ids=repair.get('read_node_ids')
        answer['read_node_ids']=ids
    if not isinstance(ids, list) or any(not isinstance(i,str) or i not in known for i in ids):
        # A failed optional suggestion must not block program-owned retrieval.
        # Reject unknown IDs exactly; never guess which original was intended.
        supplied=ids if isinstance(ids,list) else []
        answer['unresolved_node_ids']=[i for i in supplied if isinstance(i,str) and i not in known]
        answer['node_selection_status']='partial_fallback_to_verified_candidates'
        ids=list(dict.fromkeys(i for i in supplied if isinstance(i,str) and i in known))
        answer['read_node_ids']=ids
    by_question=answer.get('question_node_ids')
    question_ids={q['id'] for q in packet['questions']}
    if (isinstance(by_question,dict) and set(by_question)<=question_ids and
            all(isinstance(rows,list) and all(isinstance(nid,str) and nid in known for nid in rows)
                for rows in by_question.values())):
        coverage={q['id']:q.get('status') for q in packet['coverage']}
        priority=sorted(by_question,key=lambda q:coverage.get(q) in {'answered','not_applicable'})
        ordered=[]
        for offset in range(max((len(rows) for rows in by_question.values()),default=0)):
            ordered.extend(by_question[q][offset] for q in priority if offset<len(by_question[q]))
        ids=list(dict.fromkeys(ordered+ids))
        answer['read_node_ids']=ids
    if type(answer.get('initial_search_required')) is not bool:
        raise ValueError('invalid_initial_search_decision')
    routes = answer.get('platform_routes', [])
    if isinstance(routes,list):
        normalized=[]
        for value in routes:
            supplied=value.get('route_id') or value.get('tool') if isinstance(value,dict) else value
            matches=[r for r,v in available.items() if supplied in {r,v['tool']}]
            normalized.append(matches[0] if len(matches)==1 else supplied)
        routes=normalized
        answer['platform_routes']=routes
    if not isinstance(routes, list) or any(r not in available for r in routes):
        raise ValueError('unknown_research_platform_route')
    decisions=answer.get('candidate_decisions',[])
    if not isinstance(decisions,list):raise ValueError('invalid_candidate_decisions')
    frontier,excluded=candidate_queue(ranked,decisions,known)
    reread=answer.get('reread_nodes',[])
    if not isinstance(reread,list) or any(not isinstance(r,dict) or r.get('node_id') not in known
            or not isinstance(r.get('reason'),str) or not r['reason'].strip()
            or not isinstance(r.get('missing_evidence'),str) or not r['missing_evidence'].strip() for r in reread):
        raise ValueError('invalid_targeted_reread')
    for r in reread:costs[r['node_id']]=known[r['node_id']]['chars']
    ids=list(dict.fromkeys(ids+[r['node_id'] for r in reread]))
    selected = []; deferred = []; used = 0
    for nid in dict.fromkeys(protected+baseline_mandatory+frontier+mandatory+ids):
        if nid in excluded:continue
        if used + costs[nid] > flow.read_char_budget:
            deferred.append(nid)
        else:
            selected.append(nid); used += costs[nid]
    windows = {}
    # Accumulate this chapter's selected ranges; never drop earlier evidence
    # when its supplement reads a different node.
    for n in nodes:
        if n['node_id'] in selected:
            windows.setdefault(n['source_id'], []).extend(n['window_ids'])
    if expanded:
        for sid,ids_before in flow.progressive_selections.get(number,{}).get('window_ids_by_source',{}).items():
            valid={w for n in nodes if str(n['source_id'])==str(sid) for w in n['window_ids']}
            windows.setdefault(sid,[]).extend(w for w in ids_before if w in valid)
    ledger={nid:{'source_id':known[nid]['source_id'],'original_sha256':known[nid]['original_sha256'],
        'window_ids':known[nid]['window_ids'],'status':'selected' if nid in selected else 'budget_deferred',
        'reason':'selected_for_reading' if nid in selected else 'reading_budget_limit'}
        for nid in dict.fromkeys(frontier+deferred+selected)}
    ledger.update({nid:{'source_id':known[nid]['source_id'],'original_sha256':known[nid]['original_sha256'],
        'window_ids':known[nid]['window_ids'],**row} for nid,row in excluded.items()})
    result = {'version': VERSION, 'input_sha256': key, 'response': answer,'candidate_ledger':ledger,
        'reread_nodes':[r for r in reread if r['node_id'] in selected],
        'reread_windows':[(str(known[r['node_id']]['source_id']),wid) for r in reread
            if r['node_id'] in selected for wid in known[r['node_id']]['window_ids']],
        'selected_node_ids': selected, 'budget_deferred_node_ids': deferred,
        'mandatory_node_ids':mandatory,'mandatory_budget_deferred_node_ids':mandatory_deferred,
        'original_reserved_node_ids':protected,'original_visible_node_ids':original_visible,
        'priority_windows':[(known[nid]['source_id'],wid) for nid in selected
            for wid in known[nid]['window_ids']],
        'question_ranked_node_ids':ranked,'retrieval':retrieval,
        'unread_relevant_node_ids':{qid:[nid for nid in pool if nid not in selected]
            for qid,pool in ranked.items()},
        'unselected_node_ids': [n['node_id'] for n in nodes if n['node_id'] not in selected],
        'window_ids_by_source': {sid: sorted(set(ids)) for sid, ids in windows.items()},
        'selected_chars': used, 'read_char_budget': flow.read_char_budget,
        'directory_sha256': fingerprint(nodes)}
    cache[key] = deepcopy(result)
    if not expanded:initial_keys[stable_key]=key
    flow.journal.save()
    return result


def plan_collection(flow, number, *, gap=False):
    """Freeze one bounded tool/query plan. Engines are fanned out by transport."""
    from company_requirements import ROUTES
    routes = {k: {'provider': v[0], 'tool': v[1], 'aspect': v[3]} for k, v in ROUTES.items()
              if v[0] not in flow.disabled_platforms}
    questions = [{'id': f'Q{number:02d}-{i+1}', 'question': q.format(target=flow.project.target_name)}
                 for i, q in enumerate(chapter(number)['questions'])]
    preview=flow.journal.data.get('draft_previews',{}).get(str(number),{})
    packet = {'target': flow.project.target_name, 'as_of_date': flow.intake.get('as_of_date'),
        'chapter': number, 'phase': 'gap' if gap else 'initial', 'questions': questions,
        'coverage': flow.coverage.get(number, {}).get('questions', []),
        'existing_draft': preview.get('text') or flow.sections.get(number, ''),
        'draft_review_status': preview.get('review_status','see_coverage'),
        'available_platform_routes': routes,
        'query_budget': flow.query_budget,
        'execution_policy': '每条网页查询覆盖全部已配置引擎，合并去重；本计划结束后不再自动追加补查。'}
    key = fingerprint([VERSION, packet, os.getenv('MODEL'), os.getenv('BASE_URL')])
    cache = flow.journal.data.setdefault('chapter_collection_plans', {})
    if key in cache:
        return deepcopy(cache[key])
    prompt = ('根据本章已有证据和正文安排一次集中取证。只补具体缺口、必要的新时点和相反证据，'
        '已知字段不要重复搜索；本轮最多query_budget条不同查询，按价值排序。'
        '材料需向公司索取时列retained_gaps，不把完整内部股东名册或现行内部协议当作网上必然存在。'
        '工具只选available_platform_routes中的route_id；网页查询不选单个引擎，程序覆盖所有已配置引擎。'
        '没有可合理补充的公开来源时queries和platform_routes可为空，但不得宣布缺口不存在。'
        '返回JSON {queries:[{question_id,query,reason,expected_fields:[]}],platform_routes:[],retained_gaps:[],reason}。'
        '\n资料：\n' + json.dumps(packet, ensure_ascii=False))
    answer = parse_json(flow.chapter_invoke(number)('公司本章集中取证计划', prompt, None))
    queries = answer.get('queries'); selected_routes = answer.get('platform_routes', [])
    if not isinstance(queries, list) or not isinstance(selected_routes, list) or any(r not in routes for r in selected_routes):
        raise ValueError('invalid_chapter_collection_plan')
    ids = {q['id'] for q in questions}; unique = {}
    for q in queries:
        if not isinstance(q, dict) or q.get('question_id') not in ids or not str(q.get('query', '')).strip() or not q.get('reason'):
            raise ValueError('invalid_chapter_query_binding')
        unique.setdefault(q['query'].strip(), {**q, 'query': q['query'].strip()})
    rows = list(unique.values())
    value = {'input_sha256': key, 'response': answer, 'queries': rows[:flow.query_budget],
             'budget_deferred_queries': rows[flow.query_budget:], 'platform_routes': list(dict.fromkeys(selected_routes))}
    cache[key] = deepcopy(value); flow.journal.save()
    return value
