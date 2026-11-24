"""Reconcile partial-source objections with the chapter's actual evidence."""
from copy import deepcopy
import json
from .company_task_batches import checked_call, size, TASK_BYTES
from .company_review_evidence import review_evidence, excerpt_rows
from .company_review_references import normalize_review_references


def resolve_source_issues(issues, rows, findings, checks, target, call, tasks, persist):
    from .company_bounded_editorial import prompt
    retained=[];decisions=[]
    for issue in issues:
        paragraphs=[{'paragraph_id':f['finding_id'],'text':f['text'],'source_ids':f['information_ids']}
                    for f in findings if f['finding_id'] in issue['paragraph_ids']]
        related=[c for c in checks if c['paragraph_id'] in issue['paragraph_ids']]
        windows=review_evidence(issue['output_meaning']+'\n'+issue['source_meaning'],rows,limit=14000)
        packet={'target':target,'acceptance_profile':'practical_core_v1','issue':issue,'paragraphs':paragraphs,'completed_checks':related,
                'located_originals':excerpt_rows(windows),'checks_omitted_for_budget':False}
        rule=('交叉核验这一条审稿意见是否确实构成当前正文的实质错误。原文批次只见局部资料，不能凭本批未见就否认其他来源。'
              '依据正文、已有核验过程和定位的原文，返回block（确有影响主要判断的错误，仍需修改）、'
              'advice（一般措辞或精度建议，不阻塞交付）或resolved（完整证据已排除该问题）。'
              '不同年份、报告期或口径的数据可并存；较早已达成某指标不能证明较晚报告该指标错误；'
              '正文没有作出的假设性主张不能据此阻塞。公司披露不得冒充独立验证。'
              '明确错主体、错重要金额、否定方向相反、无依据的核心因果结论仍须block；'
              '不能为求通过而放行真实反证；完整语境仍不能排除实质错误时保留block。'
              '不要求逐条复述资料全部细节。不要修改正文，只判断该审稿意见。'
              '验收目标是有依据、可用的报告，不追求每句话严丝合缝。若同段已经列明实际评价或数据，'
              '前面的笼统缺口措辞不够精确，仅作为advice；不得为这种局部措辞重新修改和复审。'
              '但重要现有证据确实未进入正文，导致主要问题未回答或主要结论错误，仍须block。'
              '一般时间限定、表述精度或非关键细节不完整，未改变主要判断时只给advice。'
              '返回JSON {"issue_id":"原问题编号","status":"block/advice/resolved","source_ids":["已核来源编号"],"reason":"具体判断及依据"}。')
        request=prompt(rule,packet)
        while size(request)>TASK_BYTES and windows:
            windows.pop();packet['located_originals']=excerpt_rows(windows);request=prompt(rule,packet)
        while size(request)>TASK_BYTES and packet['completed_checks']:
            packet['completed_checks']=packet['completed_checks'][:-1];packet['checks_omitted_for_budget']=True
            request=prompt(rule,packet)
        allowed=set(issue['source_ids']) | {r['information_id'] for r in packet['located_originals']} | {
            i for c in packet['completed_checks'] for i in c['source_ids']}
        def normalize(answer):
            converted=normalize_review_references({'checks':[answer]},[r for r in rows if r['information_id'] in allowed])
            return converted['checks'][0]
        def validate(answer):
            if (not isinstance(answer,dict) or answer.get('issue_id')!=issue['issue_id'] or
                answer.get('status') not in {'block','advice','resolved'} or
                not isinstance(answer.get('reason'),str) or not answer['reason'].strip() or
                not isinstance(answer.get('source_ids'),list) or not answer['source_ids'] or
                any(not isinstance(i,str) or i not in allowed for i in answer['source_ids'])):
                raise ValueError('invalid_issue_crosscheck')
        answer=checked_call('公司审稿问题交叉核验',request,call,tasks,persist,validate,
                            policy='source-issue-resolution-v1',normalize=normalize)
        decisions.append({'original_issue':deepcopy(issue),'decision':answer})
        if answer['status']=='block':retained.append(issue)
    return retained,decisions


def apply_issue_decisions(review, decisions):
    """Keep raw verdicts while applying explicitly adjudicated materiality."""
    blocked_p={p for i in review['issues'] for p in i['paragraph_ids']}
    blocked_s={s for i in review['issues'] for s in i['source_ids']}
    cleared_p={p for d in decisions if d['decision']['status'] in {'resolved','advice'}
               for p in d['original_issue']['paragraph_ids']}
    cleared_s={s for d in decisions if d['decision']['status'] in {'resolved','advice'}
               for s in d['original_issue']['source_ids']}
    for field,key,blocked,cleared in (
        ('paragraphs','paragraph_id',blocked_p,cleared_p),
        ('critical_checks','paragraph_id',blocked_p,cleared_p),
        ('sources','information_id',blocked_s,cleared_s)):
        for record in review[field]:
            if field!='critical_checks' and record[key] in blocked:
                record['status']='block'
            elif record['status']=='block' and record[key] in cleared and record[key] not in blocked:
                record['original_status']='block'
                record['status']='advice'
                record['materiality_resolution']='完整语境交叉核验认定为一般建议或已排除，原核对内容保留，详见issue_crosschecks。'
    review['acceptance_profile']='practical_core_v1'
    review['issue_crosschecks']=decisions
