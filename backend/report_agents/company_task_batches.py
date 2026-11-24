"""Bounded, source-preserving model tasks and append-stable receipt reuse."""
import hashlib
import json
import os
import ast
import inspect
import textwrap
from copy import deepcopy

# UTF-8 bytes are a conservative upper bound, not a tokenizer measurement.
# Leave room for the chapter agent's instructions before the 60K dispatch guard.
TASK_BYTES = 54000
DISPATCH_BYTES = 60000


def task_limit(role):
    # Source audits use the shorter independent-review preamble. The final
    # dispatch guard still includes that preamble and never exceeds 60 KB.
    return 57000 if role.startswith('公司章节原文保留审稿') else TASK_BYTES


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def size(value):
    return len((value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(',', ':'))).encode())


def model_scope():
    return {k: os.getenv(k, '') for k in ('MODEL', 'BASE_URL', 'COMPANY_RELAY_PREFERRED_MODEL',
        'COMPANY_RELAY_PREFERRED_BASE_URL', 'COMPANY_RELAY_THINKING_TYPE',
        'COMPANY_RELAY_REASONING_EFFORT', 'COMPANY_RELAY_MAX_OUTPUT_TOKENS')}


def model_context(context):
    # Reading counters/digests stay in the checkpoint and admission gate. They
    # are not new facts and must not invalidate an unchanged evidence decision.
    return {k:v for k,v in context.items() if k!='source_reading_coverage'}


def rule_signature(*functions):
    # Hash validators, not their surrounding retry/checkpoint implementation.
    return digest([ast.dump(ast.parse(textwrap.dedent(inspect.getsource(f)))) for f in functions])


def evidence_order(row):
    context=row.get('context',{})
    proof=next(iter(row.get('provenance',[])),{})
    return (str(proof.get('source_id',proof.get('source_url',''))),
            digest(context.get('context_quote','')),row['information_id'])


def stable_batches(rows, key, packet, previous=(), *, limit=TASK_BYTES, max_items=12, order_key=None):
    """Keep old membership; changed members invalidate only their own task.

    No clipping: a single oversized record needs narrower verified evidence.
    """
    lookup = {key(r): r for r in rows}
    if len(lookup) != len(rows):
        raise ValueError('duplicate_task_record')
    used = set(); groups = []
    def append(members):
        group = []
        for row in members:
            if group and (len(group) >= max_items or size(packet(group + [row])) > limit):
                groups.append(group); group = []
            if size(packet([row])) > limit:
                raise ValueError('native_task_record_exceeds_input_budget:' + str(key(row)))
            group.append(row)
        if group: groups.append(group)
    for ids in previous:
        members = [lookup[i] for i in ids if i in lookup and i not in used]
        used.update(key(r) for r in members)
        if members: append(members)
    append(sorted([r for i,r in lookup.items() if i not in used],key=order_key or key))
    return groups


def checked_call(role, prompt, call, receipts, persist, validate, *, policy, repair_guidance=None, normalize=None):
    """Persist paid replies BEFORE validation; repair failures without replaying them."""
    if size(prompt) > task_limit(role):
        raise ValueError('model_task_exceeds_input_budget:' + role)
    legacy_key = digest([policy, model_scope(), role, prompt])
    # Checkpoints sort dictionary keys. JSON field order is not a changed task.
    prefix, marker, payload = prompt.rpartition('资料：\n')
    if marker:
        try:prompt=prefix+marker+json.dumps(json.loads(payload),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        except (ValueError,TypeError):pass
    key = digest([policy, model_scope(), role, prompt])
    receipt = receipts.setdefault(key, receipts.get(legacy_key, {'role': role}))
    if 'answer' not in receipt:
        if receipt.get('output_truncated'):
            raise ValueError('task_output_truncated_change_output_scope:'+role)
        try:
            answer = call(role, prompt)
        except Exception as exc:
            from .company_transport import failure_diagnostic
            if failure_diagnostic(exc).get('category')=='output_truncated':
                receipt['output_truncated']=True
                persist()
            raise
        receipt['answer'] = deepcopy(answer)
        persist()
    answer = deepcopy(receipt['answer'])
    try:
        if normalize:answer=normalize(answer)
        validate(answer)
    except (ValueError, KeyError, TypeError) as exc:
        receipt['validation_error'] = str(exc); persist()
        if getattr(exc, 'semantic_review_incomplete', False) or str(exc) == 'unbound_material_issue':
            # Missing/foreign evidence on a material verdict is not a field typo.
            # Keep the failed receipt; never repair it by inventing a source ID.
            raise
        if repair_guidance and not receipt.get('repair_guidance_sha256'):
            # A legacy failed schema repair did not receive the required field
            # constraints. Migrate it once; preserve its paid answer for audit.
            if 'repair_answer' in receipt:
                try:validate(normalize(receipt['repair_answer']) if normalize else receipt['repair_answer'])
                except (ValueError,KeyError,TypeError):
                    receipt['legacy_repair_answer']=receipt.pop('repair_answer')
                    receipt['repair_attempted']=False
            receipt['repair_guidance_sha256']=digest(repair_guidance);persist()
        repair_prompt=(prompt+'\n上次回答未通过结构或证据编号校验：'+str(exc)+
            ('\n'+repair_guidance if repair_guidance else '')+
            '\n只修复上次回答的这些错误，不能增加无依据事实。返回原任务要求的完整JSON。上次回答：\n'+
            json.dumps(answer,ensure_ascii=False,sort_keys=True,separators=(',',':')))
        if size(repair_prompt)>task_limit(role) and repair_guidance and str(exc).startswith(
                ('unbound_','incomplete_summary_delivery_checks','invalid_summary_delivery_check')):
            # Schema repair needs the complete facts and ID bindings, not a
            # second copy of native contexts already independently audited.
            _, marker, data=prompt.rpartition('资料：\n')
            if marker:
                payload=json.loads(data)
                def expand(value):
                    if isinstance(value,dict) and set(value)=={'text_ref'}:
                        return payload['shared_texts'][value['text_ref']]
                    if isinstance(value,dict):return {k:expand(v) for k,v in value.items()}
                    if isinstance(value,list):return [expand(v) for v in value]
                    return value
                packet={'sources':[{'information_id':r['information_id'],'text':expand(r['text'])}
                    for r in payload.get('sources',[])],
                    'paragraphs':expand(payload.get('paragraphs',[])),
                    'validation_error':str(exc),'previous_answer':answer}
                repair_prompt=('只修复既有审稿JSON的字段和编号绑定，不重新作事实审稿，不改写主张、证据解释或通过/阻塞结论。'
                    '原文核验已另行完成；下面保留完整事实文本、当前段落和上次审稿记录。返回修复后的完整审稿JSON。'
                    +repair_guidance+'\n资料：\n'+json.dumps(packet,ensure_ascii=False,sort_keys=True,separators=(',',':')))
        if size(repair_prompt)>task_limit(role):
            raise ValueError('task_record_repair_exceeds_input_budget:'+role) from exc
        if 'repair_answer' not in receipt:
            if receipt.get('repair_attempted'):raise ValueError('task_record_repair_attempts_exhausted:'+role) from exc
            receipt['repair_attempted']=True;persist()
            try:
                receipt['repair_answer']=deepcopy(call(role+'记录修复',repair_prompt));persist()
            except Exception as failure:
                from company_flow_state import CompanyFlowLimit
                from .company_transport import failure_diagnostic
                if isinstance(failure,CompanyFlowLimit) or failure_diagnostic(failure).get('request_invoked') is False:
                    receipt['repair_attempted']=False;persist()
                raise
        answer=deepcopy(receipt['repair_answer'])
        if normalize:answer=normalize(answer)
        validate(answer)
    receipt.pop('validation_error', None)
    return answer
