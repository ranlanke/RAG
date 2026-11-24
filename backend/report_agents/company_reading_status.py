"""Separate completed reading with missing material from unfinished execution."""
from copy import deepcopy
from company_flow_state import fingerprint

EXHAUSTED='available_native_context_exhausted_need_original_or_targeted_source'


def classify_context_gap(gap,result):
    row={**deepcopy(gap),'gap_kind':'pending_reading'}
    row.pop('context_review_receipt_sha256',None)
    if row.get('error') or row.get('stop_reason')!=EXHAUSTED:return row
    pair=(row.get('evidence_id'),row.get('window_id'))
    if not all(pair):return row
    for history in reversed(result.get('context_recovery',[])):
        request=history.get('request') or {};answer=history.get('response') or {}
        if (request.get('evidence_id'),request.get('window_id'))!=pair:continue
        verdicts=[v for v in answer.get('window_reviews',[]) if
            (v.get('evidence_id'),v.get('window_id'))==pair]
        decisions=[d for d in answer.get('decisions',[]) if d.get('evidence_id')==pair[0]]
        fields=row.get('missing_fields')
        if (not verdicts or not decisions or any(d.get('disposition') not in {'accepted','needs_context','no_relevant_claim'} for d in decisions)
                or any(v.get('disposition')!='needs_context' for v in verdicts)
                or not isinstance(fields,list) or not fields or not all(isinstance(f,str) and f.strip() for f in fields)):
            return row
        if not any(v.get('missing_fields')==fields for v in verdicts):return row
        pages=[e.get('original_page_context') or {} for e in result.get('context_expansions',[])
            if (e.get('evidence_id'),e.get('window_id'))==pair]
        if not pages or any(p.get('status')=='failed' for p in pages):return row
        row.update(gap_kind='material_unavailable',context_review_receipt_sha256=fingerprint([request,answer]))
        return row
    return row


def pending_native_reading(state):
    if state.get('unread_ranges'):return True
    return any(not (g.get('gap_kind')=='material_unavailable' and
        g.get('stop_reason')==EXHAUSTED and g.get('context_review_receipt_sha256') and
        g.get('missing_fields') and not g.get('error')) for g in state.get('structural_gaps',[]))


def completed_reading_state(state, manifest, receipts, project, page_reader):
    """Revalidate cached context proofs for the gate without rewriting fact inputs."""
    from types import SimpleNamespace
    from .company_reading_recovery import bind_context_expansions
    result=deepcopy(state)
    doc={'source_id':str(next((m['source_id'] for m in manifest if m.get('evidence_id')==state.get('evidence_id')),'')),
         'evidence_id':state.get('evidence_id'),'original_sha256':state.get('original_sha256'),
         'windows':[m for m in manifest if m.get('evidence_id')==state.get('evidence_id')]}
    if not doc['source_id'] or not doc['windows']:return result
    view=SimpleNamespace(sources=project.sources,company_native_page_reader=page_reader)
    for index,gap in enumerate(result.get('structural_gaps',[])):
        if gap.get('gap_kind')=='material_unavailable' or gap.get('stop_reason')!=EXHAUSTED:continue
        pair=(gap.get('evidence_id'),gap.get('window_id'))
        for receipt in receipts:
            if receipt.get('error'):continue
            response=receipt.get('response') or {}
            expansions=[x for x in response.get('context_expansions',[])
                if (x.get('evidence_id'),x.get('window_id'))==pair]
            if not expansions:continue
            try:bind_context_expansions(view,[doc],{'context_expansions':expansions})
            except (ValueError,TypeError,KeyError):continue
            classified=classify_context_gap(gap,response)
            if classified.get('gap_kind')=='material_unavailable':
                result['structural_gaps'][index]=classified;break
    return result
