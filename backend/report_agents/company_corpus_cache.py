"""Cache native extraction separately from derived alias enrichment.

Aliases are reading hints, not evidence of a legal relationship. All other
document fields, full-original hashes and window/context bytes stay in the key.
Cached selections remain proposals: current literal/subject validators run,
and the CURRENT alias binding enters each information record's semantic-review
identity. Reusing extraction never reuses an old chapter's semantic acceptance.
"""
from copy import deepcopy
import re


def alias_refresh_recovered(response):
    """An actual bound supplementary read can supersede a failed first pass."""
    refresh=response.get('alias_refresh_receipt') or {}
    windows={tuple(p) for p in refresh.get('windows',[]) if isinstance(p,(list,tuple)) and len(p)==2}
    receipt=response.get('window_review_receipt') or {}
    if (not refresh.get('error') or not windows or receipt.get('format_pending') is not False
            or not isinstance(receipt.get('response'),dict) or not receipt.get('raw_response')
            or not re.fullmatch(r'[0-9a-f]{64}',str(receipt.get('input_sha256','')))
            or response.get('pending_window_reviews')!=[] or response.get('window_review_error')
            or response.get('pending_selection_repairs')
            or any(g.get('stop_reason')=='context_call_deferred' for g in response.get('unresolved_context',[]))):
        return False
    acknowledged={(r.get('evidence_id'),r.get('window_id')) for r in receipt['response'].get('window_reviews',[])
        if isinstance(r,dict) and r.get('disposition') in {'accepted','no_relevant_claim','needs_context'}}
    return windows<=acknowledged


def redundant_aliases(document):
    aliases=(document.get('subject_binding') or {}).get('disclosed_aliases') or []
    texts=[str(document.get('source_title') or '')]
    for window in document.get('windows',[]):
        texts.extend(str(window.get(k) or '') for k in ('quote','context_quote','heading','original_page_context'))
    redundant=set()
    for alias in aliases:
        if not isinstance(alias,str) or not alias:continue
        longer=[other for other in aliases if isinstance(other,str) and len(other)>len(alias)]
        if not longer:continue  # Keep the most explicit legal-name hint.
        for text in texts:
            covered=[m.span() for other in longer for m in re.finditer(re.escape(other),text)]
            if any(not any(left<=m.start() and m.end()<=right for left,right in covered)
                   for m in re.finditer(re.escape(alias),text)):
                break
        else:redundant.add(alias)
    return redundant


def canonical_batch(batch):
    value=deepcopy(batch)
    for document in value:
        # Legacy sentence extraction has different alias/scope contracts.
        if not document.get('model_reading_mode'):continue
        binding=document.get('subject_binding') or {}
        if 'disclosed_aliases' in binding:binding['disclosed_aliases']=[]
    return value


def legacy_alias_batches(batch):
    """Best-effort migration of pre-canonical native-extraction keys.

    New receipts use canonical keys. Old receipts store only their exact hash,
    so try single alias additions and successive sorted suffixes/prefixes;
    a hash match proves that EVERY other input field is identical. No fuzzy
    hash, source matching or reuse of changed semantic context is permitted.
    """
    if not batch or not all(d.get('model_reading_mode') for d in batch):return
    removable=set.intersection(*(set((d.get('subject_binding') or {}).get('disclosed_aliases') or []) for d in batch))
    ordered=sorted(removable)
    removals={frozenset([a]) for a in ordered}
    removals.update(frozenset(ordered[:i]) for i in range(2,len(ordered)+1))
    removals.update(frozenset(ordered[i:]) for i in range(len(ordered)-1))
    removals.update(frozenset(set(ordered)-{a}) for a in ordered if len(ordered)>1)
    for remove in sorted(removals,key=lambda x:(len(x),sorted(x))):
        value=deepcopy(batch)
        for document in value:
            binding=document['subject_binding']
            binding['disclosed_aliases']=[a for a in binding.get('disclosed_aliases',[]) if a not in remove]
        yield value


def alias_review_windows(batch, prior_aliases, response):
    """Revisit previously unselected windows that gain an independent alias.

    Positive extraction proposals remain subject to current literal guards and
    fresh semantic-review identities. Negative selections need this extra read
    so a newly understood name cannot silently hide a previously omitted fact.
    """
    from .company_text import match_compact
    selected={(r.get('evidence_id'),r.get('window_id')) for r in response.get('facts',[]) if isinstance(r,dict)}
    required=set()
    for doc in batch:
        if not doc.get('model_reading_mode'):continue
        old=prior_aliases.get(doc['evidence_id'],[])
        added=set((doc.get('subject_binding') or {}).get('disclosed_aliases') or [])-set(old)
        if not added:continue
        old_names=[match_compact(a) for a in old if isinstance(a,str) and a]
        for window in doc['windows']:
            pair=(doc['evidence_id'],window['window_id'])
            if pair in selected:continue
            texts=[doc.get('source_title','')]+[window.get(k,'') for k in ('quote','context_quote','heading')]
            for value in texts:
                text=match_compact(value)
                covered=[m.span() for name in old_names for m in re.finditer(re.escape(name),text)]
                if any(any(not any(left<=m.start() and m.end()<=right for left,right in covered)
                           for m in re.finditer(re.escape(match_compact(alias)),text)) for alias in added if match_compact(alias)):
                    required.add(pair);break
    return required


def merge_alias_review(response, fresh, windows):
    result=deepcopy(response)
    result.setdefault('facts',[]).extend(r for r in fresh.get('facts',[]) if (r.get('evidence_id'),r.get('window_id')) in windows)
    result['window_reviews']=[r for r in result.get('window_reviews',[]) if (r.get('evidence_id'),r.get('window_id')) not in windows]
    result['window_reviews'].extend(r for r in fresh.get('window_reviews',[]) if (r.get('evidence_id'),r.get('window_id')) in windows)
    for name in ('context_expansions','unresolved_context','pending_selection_repairs'):
        result[name]=[r for r in result.get(name,[]) if (r.get('evidence_id'),r.get('window_id')) not in windows]+fresh.get(name,[])
    for row in result.get('decisions',[]):
        if any(f.get('evidence_id')==row.get('evidence_id') for f in result.get('facts',[])):row['disposition']='accepted'
    return result
