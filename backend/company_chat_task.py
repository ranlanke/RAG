"""Small conversation checkpoint: user intent only, never retrieved facts."""
import re

FORMAT = re.compile(r'^(?:请)?(?:把(?:上面|刚才|之前)?(?:的)?(?:回答|答案|内容|结果)?|将(?:回答|答案|内容|结果))?(?:整理|改|转换|转)?成(?:一个)?(表格|列表|清单|段落)[。！!？?\s]*$')
HISTORY_FORMAT = re.compile(r'^(?:请|能否|能不能|能|可以)?(?:把|将)(?:上一条|上一轮|刚才|上面|上述)'
    r'(?:的)?(?:回答|答案|内容|结果)?(?:写|改写|整理|改|转换|转)成'
    r'(?:[一二两三四五六七八九十\d]+[个点条项]|简明的?|简短的?)?(表格|列表|清单|段落)'
    r'(?:吗)?[。！!？?，,\s]*(?:保留(?:原文)?引用[。！!？?\s]*)?$')
FILE_FORMAT = re.compile(r'^(?:请)?(?:把|将)(?:刚才|上面|上述|之前)?的?(?:分析|回答|答案|内容|结果|表格|图表)'
    r'(?:[、，,和及与](?:分析|表格|图表|来源说明))*(?:整理|转换|转|导出|保存|生成)(?:成|为)?'
    r'(?:一份|一个)?(?:可下载的?)?\s*(Word|Excel|PNG)\s*(?:文件|文档|简报|报告|表格|图片)?[。！!？?\s]*$', re.I)
SINGULAR = re.compile(r'它(?!们)|该公司|这家公司|这个项目|该项目')
POSSESSIVE = re.compile(r'^(?:那[么，,]?)?(?:它|该公司|这家公司|该企业|这家企业|该项目|这个项目)的')
ANSWER_REFERENCE = re.compile(r'^(?:请)?(?:这|那)(?:[一二两三四五六七八九十\d]+[个条项]|两者|几项)(?:里面|之中|中|里|当中|分别|有|哪个|哪些)|^(?:请)?第[一二三四五六七八九十\d]+[个条项](?:是|有|为|呢|怎么|是否)')
LIST_MEMBERSHIP = re.compile(r'^(?:请问)?(?:这|那)([一二两三四五六七八九十]|\d{1,2})[个条项]'
    r'(?:里面|之中|中|里|当中)?(?:有没有|有)([\w\-]{1,30}?)(?:吗)?[？?。]*$')
LIST_COUNTS = {**dict(zip('一二三四五六七八九十', range(1, 11))), '两': 2}
NARROWER = re.compile(r'^(?:请)?(?:其余|剩余|其他|未披露|缺失)[^。！？?]{0,100}[。！？?]\s*(?:请)?只(?:补充|回答|列出|看|查)(?:上(?:一)?轮|刚才|上面)[^。！？?]{0,50}[。！？?]?\s*$')
PRIOR_EVIDENCE = re.compile(r'^(?:请)?(?:刚才|之前|前面|上一条|上一轮|上文)(?:提到|说到|列出|列举|讨论|回答|引用)的')
SCOPED_SUBJECT = re.compile(r'这家公司|该公司|这家企业|该企业|这个项目|该项目')
SWITCH_HEADER = re.compile(r'^(?:请)?(?:改问|换成|换到|接下来问|现在问)\s*([^：:，。！？?\n]{1,96}'
                           r'(?:公司|企业|项目|部门|团队|产品))[：:]\s*\S')
CORRECTION = re.compile(r'^(?:我指的是|我说的是|指的是)')
BUDGET_PRESENTATION = r'保留预算口径并引用[，,](?:仅用两句话回答[，,])?(?:800字以内[。]?)?'


def fresh(question):
    switch = SWITCH_HEADER.match(question)
    if switch:
        return {'request': question, 'subjects': [switch[1].strip()], 'topic': '', 'format': 'auto',
                'constraints': [], 'structured': True, 'needs_clarification': False}
    pair = re.match(r'^(?:请)?(?:比较|对比)([^，。？?的]{1,60}?)(?:和|与|、)([^，。？?的]{1,60}?)(?:的|在|之间|有什么|有哪些|有何|[。？?]|$)', question)
    subjects = [pair[1].strip(), pair[2].strip()] if pair else []
    # shortcut: recognize only a complete, literal company/field template; other wording uses the model.
    single = re.fullmatch(r'(?:请)?(?:介绍|查询|列出|分析|告诉我)?([^，。？?的与和\s]{1,60}?公司)'
        r'(20\d{2}年)?的(营业收入|收入|客户数量|客户数|目标用户|收费方式|价格)(?:是(?:多少|什么|谁)|有多少)?[？?。\s]*', question)
    project = re.fullmatch(r'(?:请)?(?:查询|列出|介绍)?([^，。？?的与和、\s]{1,60}项目)'
        r'(20\d{2}(?:年度|年))?的?(批准预算|预算额度|预算|试验预算|负责人|客户数量|客户数)'
        r'(?:是(?:多少(?:万元)?|什么|谁)|有多少)?[？?。\s]*'
        r'(' + BUDGET_PRESENTATION + r')?', question)
    single = single or project
    if single and single[1] not in ('该公司', '这家公司', '那个公司', '这个公司', '我们公司', '这个项目', '该项目', '那个项目', '本项目'):
        subjects = [single[1]]
    else:
        single = None
    constraints = [single[2]] if single and single[2] else []
    return {'request': question, 'subjects': subjects, 'topic': single[3] if single else '', 'format': 'auto',
            'constraints': constraints,
            'structured': bool(subjects), 'needs_clarification': False}


def local_followup(question, task):
    """Return (handled, query, next state); leave uncertain language to the model."""
    if not task or not task.get('request'):
        return False, None, None
    if task.get('rewrite_failed') and (SINGULAR.search(question) or FORMAT.fullmatch(question.strip()) or HISTORY_FORMAT.fullmatch(question.strip()) or FILE_FORMAT.fullmatch(question.strip())
            or re.fullmatch(r'(?:请)?(?:继续|再详细|再展开|展开说|为什么|可以展开吗|客户数(?:量)?|收费方式|收入|价格|怎么收费|有哪些风险)[呢？?。\s]*', question)):
        return True, None, {**task, 'unresolved_question': question}
    output = FORMAT.fullmatch(question.strip()) or HISTORY_FORMAT.fullmatch(question.strip())
    if output and not task.get('needs_clarification'):
        return True, task['request'], {**task, 'format': output[1], 'export_format': None}
    file_output = FILE_FORMAT.fullmatch(question.strip())
    if file_output and not task.get('needs_clarification'):
        return True, task['request'], {**task, 'export_format': {'word':'docx','excel':'xlsx','png':'png'}[file_output[1].lower()]}
    if not task.get('structured'):
        return False, None, None
    subjects = task['subjects']
    if SINGULAR.search(question) and len(subjects) > 1:
        return True, None, {**task, 'needs_clarification': True, 'unresolved_question': question}
    if len(subjects) != 1 or task.get('needs_clarification'):
        return False, None, None
    # shortcut: only short, explicit field requests; new objects and ordinals use rewriting.
    field = re.fullmatch(r'(?:那)?(客户数|客户数量|负责人|收入|营业收入|收费方式|价格|目标用户|商业模式|有哪些风险|怎么收费)[呢？?。\s]*', question)
    if field:
        topic = field[1]
        query = subjects[0] + ('' if topic in ('有哪些风险', '怎么收费') else '的') + topic + '？'
        output_format = task.get('format', 'auto')
    else:
        match = re.fullmatch(r'(?:那)?(?:它|该公司|这家公司|这个项目|该项目)(?:的)?'
            r'((?:客户数(?:量)?|负责人|营业收入|收入|目标用户|商业模式|收费方式|价格)(?:是(?:多少|什么|谁)|有多少)?|怎么收费|有哪些风险)'
            r'[呢？?。\s]*(仅回答姓名并引用[。]?)?', question)
        if not match:
            return False, None, None
        topic = match[1]
        query = subjects[0] + ('' if topic in ('有哪些风险', '怎么收费') else '的') + topic + '？'
        output_format = task.get('format', 'auto')
        if match[2]:
            query += match[2]
            output_format = 'auto'
    # Presentation rules for the old budget question do not constrain a new field.
    # Also handle tasks saved by the initial optimization without rewriting history.
    constraints = [value for value in task.get('constraints', []) if not re.fullmatch(BUDGET_PRESENTATION, value)]
    if constraints:
        query += '；' + '；'.join(constraints)
    if len(query) > 1500:
        return False, None, None
    return True, query, {**task, 'request': query, 'topic': topic, 'format': output_format, 'export_format': None,
                         'constraints': constraints, 'needs_clarification': False}


def scoped_followup(question, task, has_previous_answer, single_selected_document=False):
    """Reuse a confirmed scope for explicit references, without guessing a new object."""
    if not task.get('request') or task.get('needs_clarification') or task.get('rewrite_failed'):
        return None
    if has_previous_answer and (ANSWER_REFERENCE.search(question) or NARROWER.fullmatch(question) or PRIOR_EVIDENCE.match(question)):
        query = task['request'] + '\n本轮追问：' + question
        if len(query) <= 1500:
            return query, {**task, 'export_format': None}
    match = POSSESSIVE.match(question)
    subjects = task.get('subjects', [])
    scope = SCOPED_SUBJECT.search(task['request']) if single_selected_document and not subjects else None
    if match and (len(subjects) == 1 or scope):
        rest = question[match.end():]
        # Coordinate comparisons and topic changes still go through intent resolution.
        if rest and not re.search(r'比较|对比|相比|另|或者|和谁|与谁|[：:]|(?:和|与|及).{0,40}(?:公司|企业|项目)', rest):
            subject = subjects[0] if subjects else '所选资料中的' + scope[0]
            query = subject + '的' + rest
            if len(query) <= 1500:
                return query, {**fresh(query), 'subjects': subjects, 'structured': bool(subjects)}
    return None


def previous_list_membership(question, answer):
    """Inspect a literal label against a count-confirmed list in the previous answer."""
    match = LIST_MEMBERSHIP.fullmatch(question.strip())
    if not match:
        return None
    count = int(match[1]) if match[1].isdigit() else LIST_COUNTS[match[1]]
    if not 2 <= count <= 10:
        return None
    cleaned = re.sub(r'\[id_[^\]]+\]|\*\*|__', '', answer)
    items = re.findall(r'^\s*(?:[-*]|\d+[.、])\s*([^\n]+)', cleaned, re.M)
    if not items:
        lists = re.findall(r'(?:包括|包含|分别为|分别是|分别有|有)[：:]?([^。！？\n]+)', cleaned)
        if len(lists) != 1:
            return None
        items = re.split(r'、|，|,|(?:以及|和|及|与)(?=[^、，,]+$)', lists[0])
    items = [item.strip().rstrip('。') for item in items]
    if len(items) != count or not all(items) or any(len(item) > 80 for item in items):
        return None
    label = match[2]
    matching = [item for item in items if label.casefold() in item.casefold()]
    prefix = f'按上一轮列出的 {count} 项，'
    if matching:
        return prefix + f'包含「{label}」的条目是' + '、'.join(matching) + '。'
    return prefix + f'没有条目的名称包含「{label}」。'


def from_rewrite(payload, question, previous, user_text):
    query = payload['query']
    if query is None:
        return {**(previous or fresh(question)), 'needs_clarification': True, 'unresolved_question': question}
    task = fresh(query)
    switch = SWITCH_HEADER.match(question)
    # Older providers may return only query; retain compatibility without guessing entities.
    if 'subjects' not in payload:
        if switch and switch[1].strip() in query:
            return {**task, 'subjects': [switch[1].strip()], 'structured': True}
        return task
    subjects, constraints = payload['subjects'], payload.get('constraints', [])
    source = question + '\n' + user_text + '\n' + (previous or {}).get('request', '')
    for values, limit in ((subjects, 8), (constraints, 8)):
        if (not isinstance(values, list) or len(values) > limit
                or any(not isinstance(value, str) or not 0 < len(value) <= 120 or value not in source for value in values)):
            raise ValueError('invalid task metadata')
    topic, output = payload.get('topic', ''), payload.get('format', 'auto')
    if not isinstance(topic, str) or len(topic) > 200 or output not in ('auto', '表格', '列表', '清单', '段落'):
        raise ValueError('invalid task metadata')
    if any(subject not in query for subject in subjects):
        raise ValueError('query lost resolved subject')
    intent = previous.get('unresolved_question', question) if previous and CORRECTION.search(question) else question
    for terms in ('收费|价格|定价|费用|计费', '客户', '收入|营收'):
        if re.search(terms, intent) and not re.search(terms, query):
            raise ValueError('query lost explicit field')
    if switch and switch[1].strip() in query:
        subjects = [switch[1].strip()]
    return {**task, 'subjects': list(dict.fromkeys(subjects)), 'topic': topic, 'format': output,
            'constraints': list(dict.fromkeys(constraints)), 'structured': True}
