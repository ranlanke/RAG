"""Reuse acknowledged native windows, independently of chapter routing decisions."""
from copy import deepcopy
from collections import Counter, defaultdict


def validated_read_facts(prepared, reviews, target, as_of, originals, receipts=()):
    """Reuse literal facts only when current bytes and acknowledged windows agree."""
    from company_flow_state import fingerprint
    reviews=list(reviews)
    reused=reusable_windows(prepared,reviews,target,as_of)
    docs={str(d['source_id']):d for d in prepared[0]}
    proposals=defaultdict(list)
    for receipt in receipts:
        if receipt.get('error') or not receipt.get('input_sha256'):continue
        response=receipt.get('response') or {}
        acknowledged={(r.get('evidence_id'),r.get('window_id')) for r in response.get('window_reviews',[])
                      if r.get('disposition')=='accepted'}
        accepted={r.get('evidence_id') for r in response.get('decisions',[]) if r.get('disposition')=='accepted'}
        manifest={(r.get('evidence_id'),r.get('window_id')):r for r in receipt.get('window_manifest',[])}
        for row in response.get('facts',[]):
            pair=(row.get('evidence_id'),row.get('window_id'))
            if (pair in acknowledged and pair in manifest and pair[0] in accepted
                    and isinstance(row.get('quote'),str)):
                proposals[(pair[0],row['quote'])].append((row,manifest[pair],receipt['input_sha256']))
    valid={}
    for review in reviews:
        if review.get('target_name')!=target or review.get('research_as_of','')!=as_of:continue
        for fact in review.get('facts',[]):
            sid=str(fact.get('source_id'));doc=docs.get(sid)
            start=fact.get('original_offset');quote=fact.get('fact');end=fact.get('original_end')
            if (not doc or not isinstance(start,int) or not isinstance(quote,str) or not quote
                    or not fact.get('statement_id') or fact.get('original_sha256')!=doc['original_sha256']):continue
            if end is None:end=start+len(quote)
            if not isinstance(end,int) or start<0 or end!=start+len(quote) or originals.get(sid,'')[start:end]!=quote:continue
            cursor=start;bound=[]
            for window in sorted(doc['windows'],key=lambda w:w['start']):
                if window['end']<=cursor or window['start']>=end:continue
                pair=reused.get((sid,window['window_id']))
                if window['start']>cursor or pair is None:break
                owner,item=pair
                if window_verdict(owner,owner['source_review'][sid],window['window_id'])!='accepted':break
                bound.append([window['window_id'],item['sha256'],item['start'],item['end']])
                cursor=max(cursor,window['end'])
                if cursor>=end:break
            if cursor<end:
                # Older plans retained facts but replaced their range manifest.
                # Revalidate a saved, acknowledged proposal against today's native
                # window AND bound neighbor context; never mark that whole window read.
                from report_agents.company_model_reading import validate_model_selection
                restored=None
                for proposal,manifest,receipt_key in proposals.get((fact.get('evidence_id'),quote),[]):
                    window=next((w for w in doc['windows'] if w['window_id']==manifest['window_id']),None)
                    if (window is None or window['start']!=manifest.get('start')
                            or window['end']!=manifest.get('end')):continue
                    candidate,_=validate_model_selection(proposal,doc,target,as_of)
                    if (candidate and candidate['original_offset']==start and candidate['original_end']==end
                            and candidate['fact']==quote):
                        restored=candidate
                        bound=[[window['window_id'],window['native_window_sha256'],
                                window.get('native_context_sha256'),receipt_key]]
                        break
                if restored is None:continue
                fact={**fact,**restored}
            coverage={'original_sha256':doc['original_sha256'],'reading_scope':'reused_verified_native_fact',
                'review_windows':len(bound),'reviewed_windows':len(bound),'structural_gaps_count':0,
                'full_coverage_receipt_sha256':fingerprint([target,as_of,doc['original_sha256'],bound])}
            key=(sid,doc['original_sha256'],start,end,quote)
            valid.setdefault(key,{**deepcopy(fact),'_prior_admission_coverage':coverage})
    return list(valid.values())


def window_verdict(review,state,wid):
    for receipt in reversed(review.get('review_receipts',[])):
        if receipt.get('error'):continue
        for row in receipt.get('response',{}).get('window_reviews',[]):
            if row.get('evidence_id')==state.get('evidence_id') and row.get('window_id')==wid:
                if row.get('disposition') in {'accepted','no_relevant_claim','needs_context'}:
                    return row['disposition']
    return next((d['disposition'] for d in reversed(state.get('review_decisions',[]))
        if wid in d.get('window_ids',[]) and not d.get('error')
        and d.get('disposition') in {'accepted','no_relevant_claim','needs_context'}),None)


def reusable_windows(prepared, reviews, target, as_of):
    available={}
    for review in reviews:
        if review.get('target_name')!=target or review.get('research_as_of','')!=as_of:
            continue
        for item in review.get('reading_manifest',[]):
            sid=str(item['source_id']); state=review.get('source_review',{}).get(sid,{})
            gaps=state.get('structural_gaps',[])
            rejected=[r for r in review.get('rejected_selections',[])
                      if r.get('evidence_id')==state.get('evidence_id')]
            # A failed extraction invalidates its window, not every successful
            # window in that original. Legacy errors without locators stay unsafe.
            unsafe_gap=any(not g.get('window_id') or (g['window_id']==item['window_id']
                and (g.get('error') or g.get('stop_reason')!='available_native_context_exhausted_need_original_or_targeted_source')) for g in gaps)
            unsafe_rejection=bool(state.get('validation_rejections')) and (
                not rejected or any(not r.get('window_id') or r['window_id']==item['window_id'] for r in rejected))
            if not item.get('sha256') or unsafe_gap or unsafe_rejection:
                continue
            if (not window_verdict(review,state,item['window_id']) or not any(a<=item['start'] and b>=item['end']
                    for a,b in state.get('coverage_ranges',[]))):
                continue
            available[(sid,item['window_id'],item['sha256'],state.get('original_sha256'))]=(review,item)
    reused={}
    for doc in prepared[0]:
        for window in doc['windows']:
            key=(str(doc['source_id']),window['window_id'],window.get('native_window_sha256'),doc['original_sha256'])
            if key in available:reused[key[:2]]=available[key]
    return reused


def split_reused(prepared, reviews, target, as_of, force=()):
    """Window hashes bind source metadata, entity identity, date and original bytes."""
    reused=reusable_windows(prepared,reviews,target,as_of)
    for key in force:reused.pop(tuple(key),None)
    documents,states,duplicates=deepcopy(prepared)
    fresh=[]
    for doc in documents:
        doc['windows']=[w for w in doc['windows'] if (str(doc['source_id']),w['window_id']) not in reused]
        if doc['windows']:fresh.append(doc)
    return (fresh,states,duplicates),reused


def merge_reused(review,reused):
    facts=list(review.get('facts',[]))
    for (sid,wid),(prior,item) in reused.items():
        st=review['source_review'][sid]
        disposition=window_verdict(prior,prior['source_review'][sid],wid)
        st['reviewed_windows']+=1
        st.setdefault('coverage_ranges',[]).append([item['start'],item['end']])
        st['review_decisions'].append({'window_ids':[wid],'reading_ranges':[[item['start'],item['end']]],
            'disposition':disposition,'error':'','reused_native_read':True,'prior_read_sha256':prior.get('input_sha256')})
        for gap in prior['source_review'][sid].get('structural_gaps',[]):
            if gap.get('window_id')==wid and gap not in st.setdefault('structural_gaps',[]):
                st['structural_gaps'].append(deepcopy(gap))
        facts.extend(f for f in prior.get('facts',[]) if str(f.get('source_id'))==sid
            and f.get('original_sha256')==st['original_sha256'] and isinstance(f.get('original_offset'),int)
            and f['original_offset']<item['end'] and f.get('original_end',f['original_offset']+len(f.get('fact','')))>item['start'])
    review['facts']=list({f['statement_id']:deepcopy(f) for f in facts}.values())
    counts=Counter(str(f['source_id']) for f in review['facts'])
    from report_agents.company_reading_recovery import uncovered_ranges
    for sid in {sid for sid,_ in reused}:
        st=review['source_review'][sid];st['accepted_claims']=counts[sid]
        if st.get('scanned_chars') is not None:
            st['unread_ranges']=uncovered_ranges(st['scanned_chars'],st['coverage_ranges'])
            st['reading_scope']='partial_native_text' if st['unread_ranges'] else 'full_native_text'
        if st['reviewed_windows']==st['review_windows']:
            st['state']='reviewed_literal_claims' if counts[sid] else 'reviewed_no_accepted_claim'
    diagnostic=review['diagnostic']
    complete=all(st['reviewed_windows']==st['review_windows'] for st in review['source_review'].values())
    diagnostic.update(reused_native_windows=len(reused),accepted_claims=len(review['facts']),
        accepted_sources=len({f['source_id'] for f in review['facts']}),
        accepted_professional_sources=sum(bool(st.get('professional_platform')) and bool(st.get('accepted_claims'))
            for st in review['source_review'].values()),
        states=dict(Counter(st.get('state','pending') for st in review['source_review'].values())),
        unresolved_structural_gaps=sum(len(st.get('structural_gaps',[])) for st in review['source_review'].values()),
        native_windows_acknowledged=complete,
        all_candidate_windows_reviewed=complete and not diagnostic.get('pending_selection_repairs'))
    return review
