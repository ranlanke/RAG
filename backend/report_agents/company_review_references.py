"""Accept document-level review citations within the supplied read evidence."""
from copy import deepcopy
import json


def normalize_review_check_kinds(answer):
    """Check categories are descriptive labels, never evidence verdicts."""
    result = deepcopy(answer)
    if not isinstance(result, dict):
        return result
    allowed = {'direct_fact', 'calculation', 'relationship', 'absence', 'conclusion'}
    aliases = {'fact': 'direct_fact', '事实': 'direct_fact', '计算': 'calculation',
               '关系': 'relationship', '资料缺失': 'absence', '结论': 'conclusion',
               'meta_observation': 'conclusion'}
    for field in ('checks', 'critical_checks'):
        items = result.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            original = item.get('kind')
            if not isinstance(original, str) or not original.strip() or original in allowed:
                continue
            label = original.strip().lower()
            item['original_kind'] = original
            item['kind'] = label if label in allowed else aliases.get(label, 'conclusion')
            item['kind_normalization'] = 'alias' if label in allowed or label in aliases else 'fallback'
    # An unfamiliar issue label must not erase a substantive block.
    issue_kinds = {'wrong_subject', 'wrong_amount', 'wrong_time', 'negation_reversal', 'unsupported_claim',
                   'material_contradiction', 'misleading_scope', 'false_gap', 'substantial_repetition', 'missing_deliverable'}
    for field, key in (('checks', 'issue_kind'), ('issues', 'kind')):
        items = result.get(field, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get(key)
            if isinstance(label, str) and label.strip() and label not in issue_kinds:
                item['original_issue_kind'] = label
                item[key] = 'material_contradiction'
    return result


def normalize_review_references(answer, rows, paragraphs=()):
    """Expand a document to its read scope, never pick a purported exact fact.

    Verdicts and explanations are unchanged. Raw provider answers remain in
    task receipts; document_source_ids records the less precise attribution.
    """
    result = normalize_review_check_kinds(answer)
    if not isinstance(result, dict):
        return result
    information = {r['information_id'] for r in rows}
    documents = {}
    for row in rows:
        for document in row.get('evidence_ids', []):
            if isinstance(document, str) and document:
                documents.setdefault(document, set()).add(row['information_id'])
    for key in ('checks', 'critical_checks', 'paragraphs', 'issues'):
        items = result.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('source_ids'), list):
                continue
            ids = item['source_ids']
            if any(not isinstance(i, str) for i in ids):
                continue  # The existing schema validator supplies the type error.
            invalid = [i for i in ids if i not in information and i not in documents]
            if invalid:
                raise ValueError('unknown_review_source_reference:' + json.dumps({
                    'field': key + '.source_ids', 'invalid': invalid,
                    'allowed_information_ids': sorted(information),
                    'allowed_document_ids': sorted(documents)}, ensure_ascii=False))
            document_ids = list(dict.fromkeys(i for i in ids if i not in information))
            if not document_ids:
                continue
            item['original_source_ids'] = list(ids)
            item['document_source_ids'] = document_ids
            item['source_reference_scope'] = 'document'
            item['source_ids'] = list(dict.fromkeys(
                i for ref in ids for i in ([ref] if ref in information else sorted(documents[ref]))))
    return bind_delivery_references(result, paragraphs)


def bind_delivery_references(result, reviewed_paragraphs=()):
    """Delivery defects are grounded in reviewed paragraphs, not source facts."""
    if not isinstance(result, dict):
        return result
    def records(key):
        items = result.get(key)
        return [p for p in items if isinstance(p, dict)] if isinstance(items, list) else []
    paragraphs = {p['paragraph_id']: p for p in reviewed_paragraphs if isinstance(p, dict) and isinstance(p.get('paragraph_id'), str)}
    paragraphs.update({p['paragraph_id']: p for p in records('paragraphs') if isinstance(p.get('paragraph_id'), str)})
    gap_summaries = {p['paragraph_id'] for p in records('summary_delivery_checks')
                     if isinstance(p.get('paragraph_id'), str) and p.get('contains_evidence_gap') is True}
    anchored = set()
    for issue in records('issues'):
        if not isinstance(issue, dict) or issue.get('kind') not in {'missing_deliverable', 'substantial_repetition'}:
            continue
        ids = issue.get('paragraph_ids', [])
        if issue.get('source_ids') != [] or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in paragraphs for i in ids):
            continue
        if any(not isinstance(paragraphs[i].get('source_ids'), list) or
               any(not isinstance(s, str) for s in paragraphs[i]['source_ids']) for i in ids):
            continue
        sources = list(dict.fromkeys(s for i in ids for s in paragraphs[i].get('source_ids', [])))
        if sources:
            issue.update(source_ids=sources, source_reference_scope='reviewed_paragraph', paragraph_reference_ids=ids)
            anchored.update(ids)
    for check in records('critical_checks'):
        if (isinstance(check, dict) and check.get('source_ids') == [] and check.get('kind') == 'absence'
                and isinstance(check.get('paragraph_id'), str) and check['paragraph_id'] in anchored & gap_summaries):
            pid = check['paragraph_id']
            check.update(source_ids=list(paragraphs[pid]['source_ids']), source_reference_scope='reviewed_paragraph',
                         paragraph_reference_ids=[pid], verification_target='summary_delivery')
    return result


def document_reference_metadata(records):
    ids = sorted({i for r in records for i in r.get('document_source_ids', [])})
    return {'source_reference_scope': 'document', 'document_source_ids': ids} if ids else {}


def normalize_repair_references(answer, rows, allowed):
    """Keep document-level citations when an edit changes fact-level bindings."""
    result=deepcopy(answer)
    if not isinstance(result,dict) or not isinstance(result.get('replacements'),list):return result
    lookup={r['information_id']:r for r in rows}
    documents={}
    for row in rows:
        if row['information_id'] in allowed:
            for document in row.get('evidence_ids',[]):documents.setdefault(document,[]).append(row['information_id'])
    for item in result['replacements']:
        if not isinstance(item,dict) or not isinstance(item.get('information_ids'),list):continue
        refs=item['information_ids']
        if any(not isinstance(ref,str) for ref in refs):continue
        expanded=[];used=[]
        for ref in refs:
            if ref in allowed:expanded.append(ref);continue
            docs=[d for d in lookup.get(ref,{}).get('evidence_ids',[ref]) if d in documents]
            if not docs:expanded.append(ref);continue  # Existing validator rejects unknown scope.
            used.extend(docs)
            expanded.extend(i for d in docs for i in documents[d])
        if used:
            item.update(information_ids=list(dict.fromkeys(expanded)),original_information_ids=list(refs),
                        source_reference_scope='document',document_source_ids=list(dict.fromkeys(used)))
    return result
