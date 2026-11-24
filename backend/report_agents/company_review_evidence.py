"""Locate missing review context in already admitted evidence, without rereading."""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import zip_longest
import re
from .company_task_batches import digest, size, model_context


def review_evidence(claim, rows, *, limit=22000):
    """Return ranked verbatim windows with field offsets and source identity.

    Retrieval is not a verdict or an exhaustive absence check. The reviewer
    must retain not_assessed when these windows cannot resolve the claim.
    """
    from company_chapter_retrieval import _bm25_tokens, _bm25_rank
    windows = {}; passages = []; postings = defaultdict(list); lengths = []
    for row in sorted(rows, key=lambda r: r['information_id']):
        context = model_context(row.get('context', {}))
        fields = [('text', row['text']), ('context.context_quote', context.get('context_quote'))]
        if isinstance(context.get('original_page_context'), str):
            fields.append(('context.original_page_context', context['original_page_context']))
        elif isinstance(context.get('original_page_context'), dict):
            fields.append(('context.original_page_context.text', context['original_page_context'].get('text')))
        provenance = [{k: v for k, v in p.items() if k != 'native_quote'}
                      for p in row.get('provenance', [])]
        identity = [row.get('evidence_ids', []),
                    [(p.get('source_id'), p.get('source_url')) for p in provenance], context.get('original_sha256')]
        for field, text in fields:
            if not isinstance(text, str) or not text.strip():
                continue
            for start in range(0, len(text), 900):
                end = min(len(text), start + 1400)
                quote = text[start:end]
                # Deduplicate identical original windows, not different sources.
                key = digest([identity, quote])
                if key in windows:
                    continue
                item = {'information_id': row['information_id'], 'evidence_ids': row.get('evidence_ids', []),
                        'quote': quote, 'field': field, 'start': start, 'end': end,
                        'field_sha256': digest(text), 'provenance': provenance,
                        'excerpt_only': start > 0 or end < len(text),
                        'context': {k: v for k, v in context.items()
                                    if k not in {'context_quote', 'original_page_context'}}}
                windows[key] = item
                passages.append((key, digest(identity), field, quote))
                tokens = Counter(_bm25_tokens(quote)); position = len(lengths)
                lengths.append(sum(tokens.values()))
                for token, count in tokens.items():
                    postings[token].append((position, count))
                if end == len(text):
                    break
    index = (postings, lengths, sum(lengths) / max(1, len(lengths)))
    facets = [s.strip() for s in re.split(r'[，；;]', claim) if len(s.strip()) >= 6]
    # Exact amounts get a recall route of their own; long financial prose
    # otherwise lets repeated account names hide the disputed number.
    amounts=list(dict.fromkeys(re.findall(r'(?<![\w.])\d[\d,]*\.\d{2,}(?!\d)',claim)))[:6]
    queries = list(dict.fromkeys(amounts+[claim] + (facets if len(facets) > 1 else [])))
    pools = [_bm25_rank(query, passages, index, limit=len(passages)) for query in queries]
    ranked = list(dict.fromkeys(k for group in zip_longest(*pools) for k in group if k is not None))
    selected = []; seen_quotes = set()
    for key in ranked:
        item = windows[key]
        normalized = ''.join(item['quote'].split())
        source = (tuple(item['evidence_ids']), tuple((p.get('source_id'), p.get('source_url')) for p in item['provenance']))
        if (source, normalized) in seen_quotes:
            continue
        if size(selected + [item]) > limit:
            continue
        selected.append(item); seen_quotes.add((source, normalized))
        if len(selected) >= 6:
            break
    return selected


def bind_reviewed_sources(findings, review):
    """Only bind IDs actually cited by completed model checks; keep body text."""
    previous=deepcopy(findings)
    for finding in findings:
        ids = {i for c in review.get('critical_checks', [])
               if c.get('paragraph_id') == finding['finding_id'] for i in c.get('source_ids', [])}
        finding['information_ids'] = list(dict.fromkeys(finding['information_ids'] + sorted(ids)))
    refresh_summary_bindings(findings,previous)


def refresh_summary_bindings(findings,previous):
    """Keep a summary valid when only its unchanged body's citations grow."""
    from .company_chapter_synthesis import body_key
    old=[f for f in previous if f.get('content_role')!='chapter_summary']
    body=[f for f in findings if f.get('content_role')!='chapter_summary']
    if (not old or len(old)!=len(body) or any(
            (a['finding_id'],a['text'],a.get('content_role','body'))!=
            (b['finding_id'],b['text'],b.get('content_role','body')) or
            not set(a['information_ids'])<=set(b['information_ids']) for a,b in zip(old,body))):return
    for finding in findings:
        if finding.get('content_role')=='chapter_summary' and finding.get('summary_of_sha256')==body_key(old):
            finding['summary_of_sha256']=body_key(body)


def excerpt_rows(windows):
    """Keep source IDs and exact window locations when grouping quoted excerpts."""
    groups = {}
    for window in windows:
        iid = window['information_id']
        if iid not in groups:
            groups[iid] = {'information_id': iid, 'text': '', 'evidence_ids': window['evidence_ids'],
                           'context': {**window['context'], 'review_windows': [],
                                       'review_scope': '相关原文片段，非全文；偏移相对指定字段'},
                           'provenance': window['provenance']}
        row = groups[iid]
        row['text'] += ('\n\n' if row['text'] else '') + window['quote']
        row['context']['review_windows'].append({k: window[k] for k in ('field', 'start', 'end', 'field_sha256')})
    return list(groups.values())
