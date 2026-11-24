"""Qualitative conclusions and relationship edges from checked report evidence.

These are separate semantic tasks: neither needs a numerical score, a registry
table, a particular chapter, nor a lexical list of company relationship words.
The program checks provenance/response integrity; an independent reader checks
meaning. Failed attempts remain diagnostics rather than invented conclusions.
"""
from copy import deepcopy
import hashlib
import html
import json
import re
from pathlib import Path
import os

from .company_governance import refs, _table

VERSION='company-research-completeness-v1'
GRAPH_START='<!-- company-governance-graphs:start -->'
GRAPH_END='<!-- company-governance-graphs:end -->'


def _json(value):
    return json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',str(value).strip()))


def _native_quote(text,quote):
    # PDF line wrapping is formatting, not a new semantic admission rule.
    indices=[i for i,c in enumerate(text) if not c.isspace()]
    native=''.join(text[i] for i in indices)
    needle=''.join(c for c in str(quote) if not c.isspace())
    at=native.find(needle) if needle else -1
    if at<0:return None
    start=indices[at];end=indices[at+len(needle)-1]+1
    return {'quote':text[start:end],'quote_start':start,'quote_end':end}


def _batches(rows,limit=18000):
    batch=[];size=0
    for row in rows:
        length=len(json.dumps(row,ensure_ascii=False))
        if batch and size+length>limit:
            yield batch;batch=[];size=0
        batch.append(row);size+=length
    if batch:yield batch


def _failure(exc):
    from company_flow_state import CompanyFlowLimit
    if isinstance(exc,CompanyFlowLimit):raise exc
    from .company_transport import failure_diagnostic
    return failure_diagnostic(exc,stage='company_research_completeness')


def _invoke(project,invoke,role,prompt):
    journal=getattr(project,'_company_flow_journal',None)
    from company_chapters import CONTRACT_SHA256
    backend=Path(__file__).resolve().parents[1]
    policies=[Path(__file__),Path(__file__).with_name('company_chapter_agent.py'),
        backend/'research_workflow.py',backend/'company_chapter_flow.py']
    key=hashlib.sha256(json.dumps([[hashlib.sha256(path.read_bytes()).hexdigest() for path in policies],
        CONTRACT_SHA256,getattr(project,'company_research_intake',{}),
        role,prompt,os.getenv('MODEL'),os.getenv('BASE_URL')],ensure_ascii=False).encode()).hexdigest()
    if journal is not None:
        cached=journal.data.get('completeness_receipts',{}).get(key)
        if cached is not None:return deepcopy(cached)
    value=_json(invoke(role,prompt,None))
    if journal is not None:
        journal.data.setdefault('completeness_receipts',{})[key]=deepcopy(value);journal.save()
    return value


def qualitative_assessment(project,chapter,invoke,outline):
    """Summarize accepted prose with source-bound, independently checked judgments."""
    findings=(getattr(project,'company_editorial_package',{}) or {}).get('findings',[])
    intake=getattr(project,'company_research_intake',{}) or {}
    request_scope={key:intake.get(key) for key in ('as_of_date','focus_questions','special_requirements','scoring_required')}
    rows=[{'id':'QF'+str(i),'original_finding_id':f['finding_id'],'chapter':f['chapter'],'text':f['text'],
        'evidence_ids':f.get('evidence_ids',[])} for i,f in enumerate(findings) if 2<=f.get('chapter',0)<=11]
    package={'version':VERSION,'batches':[],'findings':[],'errors':[]}
    for batch in _batches(rows):
        receipt={'inputs':batch,'accepted':False}
        try:
            answer=_invoke(project,invoke,'公司定性综合评价编写',
                '根据已核正文撰写第12章定性综合评价。综合说明能成立的优势、限制和影响判断的具体风险，'
                '把公司披露、独立验证和推论分开。不要重复整份报告；每批至多三段。'
                '缺少评分原始指标不妨碍有边界的定性判断，但不得编造分数、等级或无风险结论。'
                '每段引用支持它的原段id。未涉及的领域不作整体判断，不补模板套话。'
                '返回JSON {findings:[{id,text,support_ids:[]}]}。\n资料：\n'+
                json.dumps({'target':project.target_name,'checked_findings':batch,'request_scope':request_scope,
                    'final_review_feedback':getattr(project,'company_assessment_editorial_feedback',[])},ensure_ascii=False))
            receipt['proposal']=answer
            by_id={r['id']:r for r in batch};proposed=answer.get('findings',[])
            if not proposed or len({f['id'] for f in proposed})!=len(proposed):raise ValueError('invalid_qualitative_findings')
            for f in proposed:
                if not f.get('text') or not f.get('support_ids') or any(i not in by_id for i in f['support_ids']):
                    raise ValueError('unbound_qualitative_support')
            review=_invoke(project,invoke,'公司定性综合评价独立核对',
                '独立核对候选定性评价。原段只能支持其明确范围；判断必须有解释，不把披露等同独立验证，'
                '不把缺资料当作无风险，不从单个指标推出整体评级，不得编造评分或默认结论。'
                '逐段检查全部主张及支持id，无法支持则reject。返回JSON '
                '{findings:[{id,status:supported/reject,reason}],issues:[]}。\n资料：\n'+
                json.dumps({'originals':batch,'candidates':proposed,'request_scope':request_scope},ensure_ascii=False))
            receipt['review']=review
            decisions={r['id']:r for r in review.get('findings',[])}
            if review.get('issues') or set(decisions)!={r['id'] for r in proposed} or any(r.get('status')!='supported' for r in decisions.values()):
                raise ValueError('qualitative_review_rejected')
            for f in proposed:
                f={**f,'evidence_ids':sorted({e for i in f['support_ids'] for e in by_id[i]['evidence_ids']})}
                package['findings'].append(f)
            receipt['accepted']=True
        except Exception as exc:
            receipt['error']=_failure(exc);package['errors'].append(receipt['error'])
        package['batches'].append(receipt)
    # Already accepted literal facts are a safe, explicitly scoped fallback.
    # There is no program-generated assessment of the company's merits.
    lines=[chapter.rstrip()]
    if package['findings']:
        lines+=['','### 有证据支持的定性综合评价','']
        lines += [f['text']+refs(f)+'\n' for f in package['findings']]
    elif rows:
        lines+=['','### 已核事实与评价边界','']
        lines += [r['text']+refs(r)+'\n' for r in rows]
    if rows and not any(d.get('score') is not None for d in (getattr(project,'company_assessment_package',{}) or {}).get('dimensions',[])):
        lines+=['','数值评分：未形成；本章定性判断的依据和适用范围见各段引用。']
    project.company_qualitative_assessment_package=package
    diagnostic=getattr(project,'company_assessment_diagnostic',{})
    diagnostic['qualitative_findings']=len(package['findings'])
    diagnostic['qualitative_errors']=deepcopy(package['errors'])
    diagnostic['presentation']='qualitative_with_unscored_rubric_in_metadata'
    return '\n'.join(lines)


def relationship_evidence(project,invoke):
    """Read every admitted native fact; don't restrict edges to table parsers."""
    facts=(getattr(project,'company_source_review_package',{}) or {}).get('facts',[])
    rows=[{'id':'R'+str(i),'text':str(r.get('evidence_quote') or r.get('fact') or ''),
        'evidence_ids':r.get('evidence_ids') or [r.get('evidence_id')],
        'period':r.get('period',''),'scope':r.get('scope',''),
        'time_context':r.get('time_context',{}),'document_date_proof':r.get('document_date_proof',{}),
        'subject_binding':r.get('subject_binding',{}),'source_title':r.get('source_title',''),
        'boundary':r.get('boundary','')} for i,r in enumerate(facts)]
    rows=[r for r in rows if r['text'] and all(r['evidence_ids'])]
    package={'version':VERSION,'target':project.target_name,'edges':[],'batches':[],'errors':[]}
    seen=set()
    for batch in _batches(rows):
        receipt={'inputs':batch,'accepted':False}
        try:
            answer=_invoke(project,invoke,'公司关系图原文关系提取',
                '从已核原文提取可绘制的明确关系。读取所有材料，不限工商字段、固定关键词或章节。'
                'source和target必须是具名主体，relation按原文说明关系、比例和限制；保留period。'
                '可绘制股东持股、表决、明确控制、任职、共同普通合伙人、投资、关联交易等实际披露关系。'
                '共同关系须经实际共同节点分别连边，不臆造双方控制或关联公司身份。'
                '持股不自动等于实控，合并财务数据不自动等于持股。没有关系可返回空数组。'
                '每条必须绑定一条资料id和该条中逐字连续quote，quote足以支持两端、关系、时点。'
                '返回JSON {edges:[{id,source,target,relation,period,kind:equity/relationship/beneficial,support_id,quote}]}。\n资料：\n'+
                json.dumps({'target':project.target_name,'records':batch},ensure_ascii=False))
            receipt['proposal']=answer;edges=answer.get('edges');by_id={r['id']:r for r in batch}
            if not isinstance(edges,list):raise ValueError('invalid_relationship_edges')
            if len({e['id'] for e in edges})!=len(edges):raise ValueError('duplicate_relationship_edge_ids')
            valid=[];receipt['invalid_edges']=[]
            for e in edges:
                row=by_id.get(e.get('support_id'))
                native=_native_quote(row['text'],e.get('quote')) if row else None
                if not native or not all(e.get(k) for k in ('source','target','relation')) or e.get('kind') not in {'equity','relationship','beneficial'}:
                    receipt['invalid_edges'].append({'edge':e,'error':'unbound_relationship_edge'});continue
                valid.append({**e,'period':e.get('period') or '',**native})
            edges=valid
            if edges:
                review=_invoke(project,invoke,'公司关系图独立核对',
                    '独立逐边核对原文。检查具名两端、关系方向、性质、比例、日期、范围及引文是否支持。'
                    '不允许把持股画为实控，把共同普通合伙人画为双方控制，或省略原文限制。'
                    '只接受原文明确的关系。返回JSON {edges:[{id,status:supported/reject,reason}]}。\n资料：\n'+
                    json.dumps({'records':batch,'edges':edges},ensure_ascii=False))
                receipt['review']=review;decisions={r['id']:r for r in review.get('edges',[])}
                if set(decisions)!={e['id'] for e in edges}:raise ValueError('incomplete_relationship_review')
                for e in edges:
                    if decisions[e['id']].get('status')!='supported':continue
                    row=by_id[e['support_id']]
                    key=json.dumps([e[k] for k in ('source','target','relation','period','kind')],ensure_ascii=False)
                    if key in seen:continue
                    seen.add(key);package['edges'].append({**e,'evidence_ids':row['evidence_ids']})
            receipt['accepted']=True
        except Exception as exc:
            receipt['error']=_failure(exc);package['errors'].append(receipt['error'])
        package['batches'].append(receipt)
    return package


def graph_block(text):
    match=re.search(re.escape(GRAPH_START)+r'.*?'+re.escape(GRAPH_END),text,re.S)
    return match[0] if match else ''


def render_relationship_graphs(project,store):
    """Paginate all edges for legibility; never silently truncate a graph."""
    package=getattr(project,'company_relationship_package',{}) or {}
    edges=package.get('edges',[])
    if not edges:return ''
    titles={'equity':'股权结构分析示意图','relationship':'实控人/关联方分析示意图','beneficial':'披露实益持股关系示意图'}
    lines=[GRAPH_START];assets=[]
    def wrap(value,width):
        # Keep English words and numeric quantities intact. Chinese labels
        # balance their lines instead of leaving a one-character company suffix.
        value=str(value)
        if value and all(ord(c)>127 for c in value):
            count=(len(value)+width-1)//width
            size=(len(value)+count-1)//count
            return [value[i:i+size] for i in range(0,len(value),size)]
        tokens=re.findall(r'[A-Za-z0-9][A-Za-z0-9,%.:/+_-]*|[^A-Za-z0-9]',value)
        lines=[];line='';used=0
        for token in tokens:
            length=sum(1 if ord(c)>127 else .55 for c in token)
            if line and used+length>width:
                lines.append(line.rstrip());line='';used=0
            if not line and token.isspace():continue
            # A long unbroken identifier retains every character; wrap only
            # that exceptional token, never split an ordinary share quantity.
            if length>width:
                size=max(1,int(width/.55))
                pieces=[token[i:i+size] for i in range(0,len(token),size)]
                lines.extend(pieces[:-1]);token=pieces[-1]
                length=len(token)*.55
            line+=token;used+=length
        if line:lines.append(line.rstrip())
        return lines or ['']
    def label(x,y,value,width=14):
        return ''.join(f'<text x="{x}" y="{y+i*30}" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="24">{html.escape(v)}</text>' for i,v in enumerate(wrap(str(value),width)))
    for kind,title in titles.items():
        selected=[e for e in edges if e['kind']==kind]
        for offset in range(0,len(selected),4):
            page=selected[offset:offset+4];body=[];y=115
            for e in page:
                n=max(len(wrap(e['source'],13)),len(wrap(e['target'],13)),len(wrap(e['relation'],14))+len(wrap(e.get('period',''),14))+1)
                h=max(140,n*30+50)
                body += [f'<rect x="30" y="{y}" width="340" height="{h}" rx="8" fill="#eff6ff" stroke="#3b82f6"/>',
                    f'<rect x="750" y="{y}" width="340" height="{h}" rx="8" fill="#ecfdf5" stroke="#10b981"/>',
                    f'<path d="M370 {y+14} H740" stroke="#64748b" marker-end="url(#arrow)"/>',
                    label(200,y+35,e['source'],13),label(920,y+35,e['target'],13),label(560,y+38,e['relation'],14)]
                relation_lines=len(wrap(e['relation'],14))
                body += [label(560,y+45+relation_lines*30,e.get('period',''),14)]
                body += [label(560,y+h-12,' '.join(e['evidence_ids']),24)]
                y+=h+32
            svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="{y+20}" viewBox="0 0 1120 {y+20}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" fill="#64748b"/></marker></defs><rect width="100%" height="100%" fill="white"/>'+label(560,35,title,60)+label(560,65,'按原文关系分别连边；时点与证据编号逐边标注',60)+''.join(body)+'</svg>'
            digest=hashlib.sha256(svg.encode()).hexdigest()[:20]
            key=f'projects/{project.id}/visual_evidence/{kind}_{digest}.svg'
            store.put_bytes(key,svg.encode(),'image/svg+xml')
            assets.append({'kind':kind,'object_key':key,'edges':deepcopy(page)})
            caption=title+(f'（{offset//4+1}）' if len(selected)>4 else '')
            lines+=['### '+caption,'',f'![{caption}]({key})','']
        if selected:
            lines+=_table(['关系起点','关系终点','原文关系','适用时点','出处'],[
                [e['source'],e['target'],e['relation'],e.get('period',''),refs(e)] for e in selected])
    package['assets']=assets
    lines.append(GRAPH_END)
    return '\n'.join(lines)
