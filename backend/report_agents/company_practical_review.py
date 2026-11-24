"""One overall acceptance after complete source audits, without sentence polishing."""
from copy import deepcopy
import json
from .company_task_batches import checked_call,size,TASK_BYTES
from .company_review_evidence import review_evidence,excerpt_rows
from .company_review_references import normalize_review_references
from .company_issue_resolution import apply_issue_decisions


DETAIL_RULES=(
    '用户验收规则：不影响主要判断的措辞、范围限定和轻微解释不阻塞出稿，不要求修改，不为其追加核对。'
    '例如“股份支付间接体现研发团队稳定性或激励力度”，以及把“本公司记账本位币为人民币”简写为“全公司统一”，'
    '在未改变核心财务数值或主要结论时均只advice；不能仅因推断缺乏直接依据或用词范围不严密判block。'
    'unsupported_claim、misleading_scope、false_gap、substantial_repetition只有确实改变核心结论'
    '或遗漏关键证据才允许block；必须填写material_impact为core_conclusion或key_evidence_omission，'
    '并用impact_reason明确哪项主要结论错误或哪个主要问题因此未回答。'
    '普通细节填material_impact=minor_detail，仅放advice。不得把普通细节升级成核心问题。')


def materiality_decision(issue, decision):
    """Soft objections cannot trigger repair without a stated material impact."""
    result=deepcopy(decision)
    if (result.get('status')=='block' and issue.get('kind') in
            {'unsupported_claim','misleading_scope','false_gap','substantial_repetition'} and not (
            result.get('material_impact') in {'core_conclusion','key_evidence_omission'} and
            isinstance(result.get('impact_reason'),str) and result['impact_reason'].strip())):
        result.update(status='advice',original_status='block',
            reason='一般细节不阻塞出稿、不触发修改或追加核对；未说明对主要结论的具体影响。原意见：'+result.get('reason',''))
    return result


def practical_review(rows,findings,target,contract,source_verdicts,source_issues,call,tasks,persist):
    from .company_bounded_editorial import prompt
    from .company_semantic_review import validate_review
    issues=[{**i,'issue_id':'SRC-'+str(n)} for n,i in enumerate(source_issues)]
    body=[{'paragraph_id':f['finding_id'],'text':f['text'],'source_ids':f['information_ids'],
           'content_role':f.get('content_role','body')} for f in findings]
    windows=review_evidence('\n'.join(f['text'] for f in findings),rows,limit=3000)
    issue_windows={i['issue_id']:review_evidence(i['output_meaning']+'\n'+i['source_meaning'],
        rows,limit=18000) for i in issues}
    def display(windows):
        excerpts=excerpt_rows(windows)
        for row in excerpts:
            row['context']={k:v for k,v in row['context'].items() if k in
                {'subject_basis','period','scope','source_kind','time_context','review_windows','review_scope'}}
            row['provenance']=[{k:v for k,v in p.items() if k in
                {'source_id','source_url','source_title','original_offset'}} for p in row['provenance']]
        return excerpts
    packet={'target':target,'chapter_contract':contract,'paragraphs':body,
            'completed_source_audits':[{'information_id':s['information_id'],'status':s['status'],
                **({'source_audit_scope':s['source_audit_scope']} if s.get('source_audit_scope') else {})} for s in source_verdicts],
            'source_issues':issues,'located_originals':display(windows),
            'issue_originals':{k:display(v) for k,v in issue_windows.items()}}
    disputed={iid for issue in issues for iid in issue['source_ids']}
    packet['issue_source_contexts']={r['information_id']:{k:v for k,v in r.get('context',{}).items()
        if k in {'subject_basis','scope','period','time_context','currency'}} for r in rows if r['information_id'] in disputed}
    rule=('进行一次整章实用验收，不做逐句细审，不要求修改一般细节。全部来源已针对当前正文完成已读证据核对，'
          'source_audit_scope为verified_read_facts时核对的是已读逐字事实及限定，未重读全部相邻原文；关键疑问须结合issue_originals定位原文判断。'
          'completed_source_audits为其真实结果，原始核验完整保留。结合全文、原文核验意见和定位原文做最终判断。'
          '仅明显错主体、关键数字或核心结论错误、重要已有材料明显漏用、模板主要问题未回答才block。'
          '措辞不够严密、非关键时间限定或细节省略只advice；同段或本章已经给出评价与数据而缺口句不精确，也只advice。'
          '不得从旧材料推断新的无依据事实，不得因本次片段未覆盖就推翻已完成的原文核验。'
          '优先检查每条issue_originals，它包含跨来源为该问题定位的直接原文，不可照搬先前局部批次的反对意见。'
          '较早已经达到某指标不意味着较晚截至时点仍报告该指标是错误；截至日期不等于首次达成日期。'
          '核对金额必须先对齐主体和报表范围；母公司单体与合并报表金额不同不是矛盾，'
          '必须结合issue_source_contexts中的原有主体口径及正文口径，不得以母公司金额推翻合并金额。'
          '荣誉或名录未逐字列出具体批次号、公司评价缺少非关键限定，均只属于advice，不是金额错误。'
          '现有资料确实不足且正文说明范围，可以通过。公司披露不应冒充独立验证。'
          '保留既定章节表格、关系图、时间线和小结要求；小结不应列资料缺口。'
          '每个paragraphs至少一个合并的核心核对critical_checks即可，不逐句拆分。'
          '每条source_issues必须在source_issue_decisions明确判block/advice/resolved并说明理由；不得漏掉反证。'
          'issues仅列新增实质错误，每条block要有具体影响和实际来源，非实质意见只放advice。'
          +DETAIL_RULES+
          '返回JSON {"paragraphs":[{"paragraph_id":"实际编号","status":"pass/advice/block","source_ids":[],"reason":"理由"}],'
          '"critical_checks":[{"paragraph_id":"实际编号","kind":"direct_fact/calculation/relationship/absence/conclusion",'
          '"claim":"本段核心含义","evidence_check":"结合已核证据的整体判断","source_ids":[],"status":"pass/advice/block"}],'
          '"source_issue_decisions":[{"issue_id":"SRC编号","status":"block/advice/resolved","reason":"实际影响及依据",'
          '"material_impact":"minor_detail/core_conclusion/key_evidence_omission","impact_reason":"对主要判断的具体影响；一般细节留空"}],'
          '"issues":[{"issue_id":"唯一编号","kind":"wrong_subject/wrong_amount/wrong_time/negation_reversal/unsupported_claim/material_contradiction/misleading_scope/false_gap/substantial_repetition/missing_deliverable",'
          '"paragraph_ids":[],"source_ids":[],"source_meaning":"依据","output_meaning":"正文含义","reason":"实质影响",'
          '"material_impact":"core_conclusion/key_evidence_omission","impact_reason":"对主要判断的具体影响"}],'
          '"summary_delivery_checks":[{"paragraph_id":"小结编号","contains_evidence_gap":false,"reason":"依据"}],"advice":[]}。')
    request=prompt(rule,packet)
    if size(request)>TASK_BYTES:
        # Completed pass receipts stay in the ledger. The final decision only
        # needs their counts plus the non-pass records and actual objections.
        from collections import Counter
        packet['source_audit_summary']={'total':len(source_verdicts),
            'statuses':dict(Counter(s['status'] for s in source_verdicts))}
        related={iid for issue in issues for iid in issue['source_ids']}
        packet['completed_source_audits']=[s for s in packet['completed_source_audits'] if s['status']=='block' or s['information_id'] in related]
        packet['source_audit_records_scope']='仅展示问题关联与block记录；其余状态的数量见source_audit_summary，完整逐条回执已保存；所有问题仍完整列在source_issues。'
        request=prompt(rule,packet)
    if size(request)>TASK_BYTES:
        # Citation catalogs are retained locally for receipt validation. Keep
        # verbatim prose and disputed originals ahead of repeated ID lists.
        packet['paragraphs']=[{k:v for k,v in p.items() if k!='source_ids'} for p in body]
        packet['paragraph_evidence_scope']='逐段引用清单及完整原文核验回执已保存；本次保留完整正文、争议和定位原文，普通整段核对可空source_ids表示沿用该段既有核对范围，不得猜测编号。'
        request=prompt(rule,packet)
    while size(request)>TASK_BYTES and windows:
        windows.pop();packet['located_originals']=display(windows);request=prompt(rule,packet)
    while size(request)>TASK_BYTES and any(issue_windows.values()):
        key=max(issue_windows,key=lambda k:size(issue_windows[k]))
        issue_windows[key].pop();packet['issue_originals'][key]=display(issue_windows[key]);request=prompt(rule,packet)
    def normalize(answer):
        result=normalize_review_references(answer,rows,body)
        # An overall paragraph verdict may omit individual citations. Bind it
        # to the supplied paragraph evidence, explicitly as a broad scope.
        bindings={p['paragraph_id']:p['source_ids'] for p in body}
        for field in ('paragraphs','critical_checks'):
            for item in result.get(field,[]):
                if item.get('source_ids')==[] and bindings.get(item.get('paragraph_id')):
                    item.update(source_ids=list(bindings[item['paragraph_id']]),
                        source_reference_scope='reviewed_paragraph',
                        paragraph_reference_ids=[item['paragraph_id']])
        if isinstance(result.get('advice'),list):
            result['advice']=[a['reason'] if isinstance(a,dict) and isinstance(a.get('reason'),str) else a for a in result['advice']]
        # An explicit whole-paragraph verdict is the overall check in this
        # mode; preserve that verdict rather than asking for a duplicate field.
        checked={c.get('paragraph_id') for c in result.get('critical_checks',[]) if isinstance(c,dict)}
        for p in result.get('paragraphs',[]):
            if p.get('paragraph_id') not in checked and p.get('reason') and isinstance(p.get('source_ids'),list):
                result.setdefault('critical_checks',[]).append({'paragraph_id':p['paragraph_id'],
                    'kind':'conclusion','claim':'本段核心含义整体核验','evidence_check':p['reason'],
                    'source_ids':p['source_ids'],'status':p['status'],'check_scope':'paragraph_overall'})
        return result
    def assembled(answer):
        result=deepcopy(answer)
        decisions=result.pop('source_issue_decisions',None)
        if (not isinstance(decisions,list) or len(decisions)!=len(issues) or
            {d.get('issue_id') for d in decisions if isinstance(d,dict)}!={i['issue_id'] for i in issues}):
            raise ValueError('incomplete_source_issue_decisions')
        lookup={d['issue_id']:d for d in decisions};records=[]
        for issue in issues:
            d=materiality_decision(issue,lookup[issue['issue_id']])
            if d.get('status') not in {'block','advice','resolved'} or not isinstance(d.get('reason'),str) or not d['reason'].strip():
                raise ValueError('invalid_source_issue_decision')
            records.append({'original_issue':issue,'decision':d})
            if d['status']=='block':result['issues'].append(issue)
        retained=[]
        for issue in result['issues']:
            # Source decisions have already passed the same gate above.
            if issue['issue_id'] in lookup:
                retained.append(issue);continue
            d=materiality_decision(issue,{'status':'block',**{k:issue.get(k) for k in
                ('reason','material_impact','impact_reason')}})
            if d['status']=='block':retained.append(issue)
            else:records.append({'original_issue':deepcopy(issue),'decision':d})
        result['issues']=retained
        result['sources']=deepcopy(source_verdicts)
        apply_issue_decisions(result,records)
        validate_review(result,rows,findings)
        return result
    answer=checked_call('公司整章实用验收',request,call,tasks,persist,assembled,
                        policy='practical-overall-v3-details-nonblocking',normalize=normalize)
    return assembled(answer)
