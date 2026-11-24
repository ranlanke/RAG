"""Evidence-bound history events; SVG layout makes no factual decisions."""
from copy import deepcopy
from datetime import date
import calendar
import hashlib
import html
import re
import unicodedata

ROLE='history_timeline'
POLICY={'version':'history-dual-track-v2','mode':'auto','preferred_events':'6-10'}
RULES=('历史工商变更与业务发展历程共用一张双轨时间线，置于业务发展历程与关键节点小节开头，两个小标题仍保留。'
    '先按证据判断是否适合画图；有多个有依据的关键节点时render，材料不足则omit并说明原因，不补造节点。'
    '轨道registry表示主体与工商沿革，business_capital表示业务与资本发展；按公司实际经历选约6至10个重点节点，'
    '不是硬性数量要求，不固定套用红筹、融资或上市；详细股数、协议和人员留正文。节点依实际时间先后排序。'
    '每个节点明确主体、事件、日期含义、计划/签约/审议/完成状态及information_ids。出资基准日不能当成整体变更登记完成日；'
    '披露日不能当事件日；日期不一致时保留差异和说明，不强行统一。'
    'sort_date仅填写date_label中最早明示的日期，格式YYYY、YYYY-MM或YYYY-MM-DD；无法确定则null，不补造月日。'
    '程序按可定位日期统一排序；区间重叠或仅有年份导致先后不明时注明组内不表示先后，无法定位的日期集中列示。'
    '共用时间轴只表示先后，不自动表示因果；causal_links仅在原文明确支持因果时提供，证据不足就留空。'
    '持股路径、设立链条、同一重组中的步骤先后也不是事件因果，不放入causal_links。'
    '不把任职、同时发生、时间先后当成控制或因果。图只概览关键节点，正文解释变化、差异和局限，减少逐项重复。'
    '不在正文另写Mermaid、SVG或第二张时间线，程序用history_timeline数据统一制图。')
SCHEMA=('另输出history_timeline:{decision:"render或omit",reason:"适用性判断",events:[{event_id:"T1",'
    'track:"registry或business_capital",date_label:"原文日期或区间",sort_date:"最早明示日期或null",date_kind:"event/disclosure/period/uncertain",'
    'entity:"原文主体简称",label:"简短事件",status:"实际状态",note:"必要的日期差异说明，可为空",'
    'information_ids:["实际编号"]}],causal_links:[{from_event:"T1",to_event:"T2",'
    'reason:"原文明确的因果含义",information_ids:["实际编号"]}]}。omit时events和causal_links均为空。')


def date_window(label):
    """Only explicit numeric Chinese/ISO dates; unsupported prose stays unplaced."""
    pattern=r'(?P<y>[12]\d{3})(?:年(?:(?P<cm>\d{1,2})月(?:(?P<cd>\d{1,2})日?)?)?|-(?P<im>\d{1,2})(?!\d)(?:-(?P<id>\d{1,2}))?)?(?!\d)'
    matches=list(re.finditer(pattern,label));parts=[]
    if not matches:return None
    def add(y,m=None,d=None):
        y=int(y);m=int(m) if m else None;d=int(d) if d else None
        start=date(y,m or 1,d or 1)
        end=date(y,m or 12,d or calendar.monthrange(y,m or 12)[1])
        parts.append((start,end,f'{y:04d}'+(f'-{m:02d}' if m else '')+(f'-{d:02d}' if d else '')))
    try:
        for i,match in enumerate(matches):
            y=match['y'];m=match['cm'] or match['im'];d=match['cd'] or match['id'];add(y,m,d)
            # A repeated month/day may omit the year: 2011年8月18日/8月24日.
            tail=label[match.end():matches[i+1].start() if i+1<len(matches) else len(label)]
            for short in re.finditer(r'(?<!\d)(\d{1,2})月(?:(\d{1,2})日?)?',tail):add(y,*short.groups())
    except ValueError:return None
    first=min(parts,key=lambda p:p[0])
    return first[0],max(p[1] for p in parts),first[2]


def ordered_timeline(data):
    """Normalize a copy; retain source labels, IDs, states and causal edges."""
    result=deepcopy(data);events=result['events'];known=[];unknown=[]
    for event in events:
        window=date_window(event['date_label'])
        event['sort_date']=window[2] if window else None
        event.pop('time_group',None)
        if window:known.append((window[0],window[1],event))
        else:unknown.append(event)
    known.sort(key=lambda item:item[0]);groups=[]
    for start,end,event in known:
        if groups and start<=groups[-1][0]:
            groups[-1][0]=max(groups[-1][0],end);groups[-1][1].append(event)
        else:groups.append([end,[event]])
    ordered=[]
    for number,(_,group) in enumerate(groups,1):
        if len(group)>1:
            for event in group:event['time_group']=f'G{number}'
        ordered.extend(group)
    result['events']=ordered+unknown
    return result


def validate_timeline(data,rows):
    if not isinstance(data,dict) or data.get('decision') not in {'render','omit'} or not isinstance(data.get('reason'),str) or not data['reason'].strip():
        raise ValueError('missing_history_timeline_decision')
    events=data.get('events');links=data.get('causal_links');allowed={r['information_id'] for r in rows}
    if not isinstance(events,list) or not isinstance(links,list):raise ValueError('invalid_history_timeline')
    def refs(value):
        return isinstance(value,list) and bool(value) and all(isinstance(i,str) and i in allowed for i in value) and len(set(value))==len(value)
    if data['decision']=='omit':
        if events or links:raise ValueError('omitted_timeline_has_events')
        return data
    seen=set()
    if not events:raise ValueError('empty_history_timeline')
    for event in events:
        if (not isinstance(event,dict) or any(not isinstance(event.get(k),str) or not event[k].strip()
                for k in ('event_id','date_label','entity','label','status')) or event['event_id'] in seen
                or event.get('track') not in {'registry','business_capital'}
                or event.get('date_kind') not in {'event','disclosure','period','uncertain'}
                or not isinstance(event.get('note',''),str) or not refs(event.get('information_ids'))):
            raise ValueError('invalid_history_event')
        seen.add(event['event_id'])
        if event.get('sort_date') is not None:
            window=date_window(event['date_label'])
            if not window or event['sort_date']!=window[2]:raise ValueError('history_sort_date_not_in_label')
    for link in links:
        if (not isinstance(link,dict) or link.get('from_event') not in seen or link.get('to_event') not in seen
                or link['from_event']==link['to_event'] or not isinstance(link.get('reason'),str) or not link['reason'].strip()
                or not refs(link.get('information_ids'))):raise ValueError('invalid_history_causal_link')
    return data


def timeline_text(data):
    data=ordered_timeline(data)
    lines=['**关键事件双轨时间线**','横轴仅表示时间先后，不表示因果关系。']
    for e in data['events']:
        order_note=('时间重叠组'+e['time_group']+'，组内不表示先后') if e.get('time_group') else ('时间待定位' if not e.get('sort_date') else '')
        lines.append(f"{e['event_id']}｜{'主体与工商沿革' if e['track']=='registry' else '业务与资本发展'}｜{e['date_label']}（{e['date_kind']}）｜{order_note}｜{e['entity']}｜{e['label']}｜{e['status']}｜{e.get('note','')}｜依据：{','.join(e['information_ids'])}")
    for link in data['causal_links']:
        lines.append(f"明确因果箭头：{link['from_event']} → {link['to_event']}；{link['reason']}；依据：{','.join(link['information_ids'])}")
    return '\n\n'.join(lines)


def timeline_finding(data,rows,contract):
    validate_timeline(data,rows)
    if data['decision']=='omit':return None
    data=ordered_timeline(data)
    return {'finding_id':f"C{contract['chapter']}-TIMELINE",'aspect':contract['sections'][min(1,len(contract['sections'])-1)],
        'text':timeline_text(data),'information_ids':list(dict.fromkeys(i for e in data['events']+data['causal_links'] for i in e['information_ids'])),
        'content_role':ROLE,'timeline_data':deepcopy(data)}


def render_svg(data,information):
    """Two lanes share equal event slots; no equal-duration or causal inference."""
    data=ordered_timeline(data)
    lookup={r['information_id']:r for r in information};events=data['events'];slot=200;left=155;width=left+slot*len(events)+30
    def wrap(value,n=10):
        # Chinese glyphs occupy about two Latin columns. Keep numbers and
        # names together where they fit, without dropping any source text.
        lines=[]
        for line in str(value).splitlines():
            current='';columns=0
            for token in re.findall(r'[0-9][0-9,.%]*|[A-Za-z][A-Za-z0-9-]*|.',line):
                size=sum(2 if unicodedata.east_asian_width(c) in {'W','F'} else 1 for c in token)
                if current and columns+size>n*2:
                    lines.append(current);current='';columns=0
                current+=token;columns+=size
            lines.append(current)
        return lines or ['']
    def evidence(ids):return list(dict.fromkeys(e for i in ids for e in lookup[i].get('evidence_ids',[])))
    cards=[];maxlines=0
    for event in events:
        lines=wrap(event['entity'])+wrap(event['label'])+wrap('状态：'+event['status'])
        if event.get('note'):lines+=wrap(event['note'])
        lines+=evidence(event['information_ids']);cards.append(lines);maxlines=max(maxlines,len(lines))
    dates=[wrap(e['date_label']+({'disclosure':'（披露日）','uncertain':'（日期有差异）'}.get(e['date_kind'],''))+
        (f"（{e['time_group']}组内先后未定）" if e.get('time_group') else '（时间待定位）' if not e.get('sort_date') else ''),13) for e in events]
    dateheight=max(map(len,dates))*19
    footers=[wrap(f"因果{i+1}：{v['from_event']} → {v['to_event']}；{v['reason']}；{' '.join(evidence(v['information_ids']))}",max(20,int(width/15)-4)) for i,v in enumerate(data['causal_links'])]
    h=max(140,maxlines*23+24);axis=110+h+dateheight+35;business=axis+35
    height=business+h+70+sum(len(lines)*22+14 for lines in footers)
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<title>工商沿革与业务资本发展双轨时间线</title>',
        '<desc>同一时间轴按节点先后排列，间距不代表时长；只有明确因果连线带箭头。日期、状态及出处见节点。</desc>',
        '<defs><marker id="causal" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8 Z" fill="#b45309"/></marker></defs>',
        '<rect width="100%" height="100%" fill="white"/>']
    def label(x,y,value,size=17,color='#1f2937'):
        return f'<text x="{x}" y="{y}" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="{size}" fill="{color}">{html.escape(str(value))}</text>'
    parts += [label(width/2,30,'工商沿革与业务资本发展双轨时间线',23),label(width/2,59,'可定位节点从左至右按时间排列；重叠组内不表示先后，待定位节点集中列末；间距不代表时长',16),
        label(77,110+h/2,'主体与'),label(77,137+h/2,'工商沿革'),label(77,business+h/2,'业务与'),label(77,business+27+h/2,'资本发展'),
        f'<path d="M{left} {axis} H{width-20}" stroke="#94a3b8" stroke-width="2" fill="none"/>']
    positions={}
    for index,(event,lines) in enumerate(zip(events,cards)):
        x=left+index*slot;cx=x+slot/2;y=110 if event['track']=='registry' else business
        positions[event['event_id']]=(cx,y+h if y==110 else y)
        parts += [f'<path d="M{cx} {y+h if y==110 else y} V{axis}" stroke="#cbd5e1" stroke-dasharray="4 4"/>',
            f'<circle cx="{cx}" cy="{axis}" r="5" fill="#475569"/>',
            f'<rect x="{x+5}" y="{y}" width="{slot-10}" height="{h}" rx="7" fill="{"#eff6ff" if y==110 else "#ecfdf5"}" stroke="#94a3b8"/>']
        for j,line in enumerate(lines):parts.append(label(cx,y+27+j*23,line,13 if line.startswith('E-') else 17))
        for j,line in enumerate(dates[index]):parts.append(label(cx,axis-dateheight+10+j*19,line,14))
    footer_y=business+h+36
    for index,link in enumerate(data['causal_links']):
        x1,y1=positions[link['from_event']];x2,y2=positions[link['to_event']]
        parts.append(f'<path d="M{x1} {y1} Q{(x1+x2)/2} {axis+25} {x2} {y2}" stroke="#b45309" stroke-width="2" fill="none" marker-end="url(#causal)"/>')
        for line in footers[index]:
            parts.append(label(width/2,footer_y,line,14,'#92400e'));footer_y+=22
        footer_y+=14
    parts.append('</svg>');return ''.join(parts)


def render_timeline(project,data):
    from research_store import research_store
    rows=project.company_information_usage_package['records']
    svg=render_svg(data,rows);key=f'projects/{project.id}/visual_evidence/history_{hashlib.sha256(svg.encode()).hexdigest()[:20]}.svg'
    research_store.put_bytes(key,svg.encode(),'image/svg+xml')
    return ('**关键事件双轨时间线**\n\n'+f'![工商沿革与业务资本发展双轨时间线]({key})\n\n'
        '图示：可定位节点按日期排序；时间重叠组内不表示先后，无法定位日期的节点集中列末；节点间距不代表时长。有箭头的连线仅表示已注明证据的因果关系。')
