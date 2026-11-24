import json
from copy import deepcopy
import pytest

from company_flow_state import FlowJournal
from test_company_chapter_flow import local_store
from report_agents.company_task_batches import stable_batches,checked_call,size,TASK_BYTES


@pytest.fixture(autouse=True)
def strict_review_path(monkeypatch):
    # Keep coverage of resumable per-claim audits alongside default practical tests.
    monkeypatch.setenv('COMPANY_EDITORIAL_ACCEPTANCE_PROFILE','strict_core')


def test_source_audit_uses_short_preamble_headroom_without_relaxing_other_tasks():
    from report_agents.company_task_batches import task_limit,DISPATCH_BYTES
    role='公司章节原文保留审稿'
    prompt='x'*55000
    calls=[]
    def call(role,prompt):calls.append(role);return {'ok':True}
    assert checked_call(role,prompt,call,{},lambda:None,lambda a:None,policy='v1')=={'ok':True}
    assert task_limit(role)==task_limit(role+'记录修复')<DISPATCH_BYTES
    with pytest.raises(ValueError,match='input_budget'):
        checked_call('review',prompt,call,{},lambda:None,lambda a:None,policy='v1')
    with pytest.raises(ValueError,match='input_budget'):
        checked_call(role,'x'*60001,call,{},lambda:None,lambda a:None,policy='v1')
    assert calls==[role]


def test_failed_legacy_record_repair_gets_explicit_constraints_once():
    tasks={};calls=[]
    def validate(a):
        if not a.get('source_ids'):raise ValueError('unbound_critical_check')
    def failed(role,prompt):calls.append(role);return {'source_ids':[]}
    with pytest.raises(ValueError):checked_call('review','same input',failed,tasks,lambda:None,validate,policy='v1')
    assert len(calls)==2
    def repair(role,prompt):
        assert 'Use actual inspected source IDs' in prompt
        calls.append(role);return {'source_ids':['S1']}
    args={'policy':'v1','repair_guidance':'Use actual inspected source IDs'}
    expected=checked_call('review','same input',repair,tasks,lambda:None,validate,**args)
    assert len(calls)==3 and next(iter(tasks.values()))['legacy_repair_answer']=={'source_ids':[]}
    assert checked_call('review','same input',lambda *a:pytest.fail('repeat repair'),
        json.loads(json.dumps(tasks)),lambda:None,validate,**args)==expected


def test_large_binding_repair_keeps_complete_facts_without_repeating_native_context():
    packet={'sources':[{'information_id':'S1','text':{'text_ref':'fact'},'context':{'context_quote':'x'*48000}}],
        'shared_texts':{'fact':'完整原文事实与适用条件。'},
        'paragraphs':[{'paragraph_id':'P1','text':'待核正文。','source_ids':['S1']}]}
    prompt='审稿。资料：\n'+json.dumps(packet,ensure_ascii=False)
    calls=[];tasks={}
    def validate(a):
        if not a['source_ids']:raise ValueError('unbound_critical_check')
    def call(role,prompt):
        calls.append(role);assert size(prompt)<=TASK_BYTES
        if len(calls)==1:return {'source_ids':[],'evidence_check':'完整核验记录。'*500}
        assert '完整原文事实与适用条件。' in prompt and '待核正文。' in prompt
        assert 'x'*100 not in prompt
        return {'source_ids':['S1'],'evidence_check':'完整核验记录。'*500}
    result=checked_call('review',prompt,call,tasks,lambda:None,validate,
        policy='v1',repair_guidance='填入实际核验的编号。')
    assert result['source_ids']==['S1'] and len(calls)==2
from report_agents.company_chapter_synthesis import synthesize
from report_agents.company_bounded_editorial import bounded_review
from test_company_semantic_review import passing


@pytest.mark.parametrize('target,number',[('海港制造',2),('松林软件',3),('星辰医药',4),('山川物流',5),('青禾农业',6),('远航设备',9)])
def test_paid_response_survives_crash_before_consumer_checkpoint(tmp_path,target,number,monkeypatch):
    store=local_store(tmp_path);scope={'target':target,'chapter':number,'as_of':'2026-11-18'}
    j=FlowJournal(store,1,scope);calls=[]
    def call(*args):calls.append(args);return '{"result":"source bound"}'
    assert j.invoke(call,'核对',target)=='{"result":"source bound"}'
    recovered=FlowJournal(store,1,scope);assert recovered.restore()
    assert recovered.invoke(call,'核对',target)=='{"result":"source bound"}'
    assert len(calls)==1
    assert recovered.data['metrics']['model_calls']==len([e for e in recovered.data['events'] if e['kind']=='model'])
    recovered.invoke(call,'核对',target+' changed evidence');assert len(calls)==2
    monkeypatch.setenv('MODEL','different-provider-model')
    recovered.invoke(call,'核对',target);assert len(calls)==3


def test_batches_keep_old_membership_when_preceding_id_is_added():
    rows=[{'id':str(i),'text':'原文'+str(i)} for i in range(4,9)]
    groups=stable_batches(rows,lambda r:r['id'],lambda r:r,max_items=2)
    layout=[[r['id'] for r in g] for g in groups]
    changed=stable_batches([{'id':'0','text':'新增'}]+list(reversed(rows)),lambda r:r['id'],lambda r:r,layout,max_items=2)
    assert [[r['id'] for r in g] for g in changed]==layout+[['0']]


def test_response_cache_respects_semantic_policy_and_coalesces_concurrent_calls(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    j=FlowJournal(local_store(tmp_path),1,{'target':'通用企业'});calls=[]
    j.model_policy=lambda role:'policy-one'
    def call(*args):calls.append(args);return '{"ok":true}'
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert list(pool.map(lambda _:j.invoke(call,'核对','同一原文'),range(3)))==['{"ok":true}']*3
    assert len(calls)==1
    j.model_policy=lambda role:'policy-two'
    j.invoke(call,'核对','同一原文');assert len(calls)==2


def test_restored_successes_do_not_spend_transport_retry_allowance(tmp_path,monkeypatch):
    from test_company_chapter_flow import flow
    f,calls,_=flow(tmp_path,monkeypatch)
    for _ in range(8):assert f.invoke('缓存恢复测试','同一任务')
    assert len(calls)==1
    assert f.journal.data['metrics']['model_calls']==1
    assert f.invoke._company_last_call.attempts==0


def test_question_cache_does_not_hash_retry_module_but_rules_invalidate(monkeypatch):
    import company_reading as module
    fact={'fact':'公司披露实际执行记录。','source_url':'https://example.test/a','scope':'target'}
    calls=[]
    def invoke(role,prompt,trace):
        calls.append(role);d=json.loads(prompt.rsplit('资料：\n',1)[1])
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[d['proofs'][0]['id']],
            'missing_fields':['其他资料'],'reason':'本批已有执行事实'} for q in d['questions']]})
    prior=module.assess_questions(4,'任意目标',[fact],invoke)
    monkeypatch.setattr(module.Path,'read_text',lambda *a,**kw:pytest.fail('whole-module policy hash'))
    module.assess_questions(4,'任意目标',[fact],invoke,previous=json.loads(json.dumps(prior)))
    assert len(calls)==1
    monkeypatch.setattr(module,'QUESTION_POLICY','new-semantic-contract')
    module.assess_questions(4,'任意目标',[fact],invoke,previous=prior)
    assert len(calls)==2


def test_reading_progress_does_not_change_evidence_identity_or_model_packet():
    from report_agents.company_information_usage import semantic_context,routing_fingerprint
    from report_agents.company_chapter_synthesis import evidence_packet
    from company_chapters import outline
    rows=evidence('任意企业',5,1);old=deepcopy(rows)
    rows[0]['context']['source_reading_coverage']={'reviewed_windows':3,'full_coverage_receipt_sha256':'new-receipt'}
    assert semantic_context(rows[0]['context'])==semantic_context(old[0]['context'])
    assert routing_fingerprint(rows[0],'任意企业',{},outline(),5)==routing_fingerprint(old[0],'任意企业',{},outline(),5)
    assert evidence_packet(rows)==evidence_packet(old)
    rows[0]['context']['period']='2026'
    assert evidence_packet(rows)!=evidence_packet(old)


def test_bounded_question_context_keeps_native_qualifying_conditions():
    from company_reading import assess_questions
    quote='仅在取得股东会授权且满足指定条件时适用；金额单位为万元。'
    fact={'fact':'某企业可以实施此事项。','context_quote':quote,
        'source_url':'https://example.test/native','scope':'target','period':'2025'}
    def invoke(role,prompt,trace):
        assert quote in prompt
        d=json.loads(prompt.rsplit('资料：\n',1)[1])
        return json.dumps({'questions':[{'id':q['id'],'status':'partial','support_ids':[d['proofs'][0]['id']],
            'missing_fields':['实际执行记录'],'reason':'制度包含必要条件，不能视为实际已实施'} for q in d['questions']]})
    assert not assess_questions(4,'任意公司',[fact],invoke)['errors']


def test_invalid_response_is_saved_and_oversize_never_dispatched():
    tasks={};calls=[]
    def call(*a):calls.append(a);return {'invalid':True}
    def validate(a):raise ValueError('invalid_structure')
    for _ in range(2):
        with pytest.raises(ValueError,match='invalid_structure'):
            checked_call('review','same',call,tasks,lambda:None,validate,policy='v1')
    assert len(calls)==2
    with pytest.raises(ValueError,match='input_budget'):
        checked_call('review','中'*TASK_BYTES,call,tasks,lambda:None,validate,policy='v1')
    assert len(calls)==2


def test_truncated_output_is_not_repaid_until_request_changes():
    tasks={};calls=[]
    def call(role,prompt):
        calls.append(prompt)
        if prompt=='long':raise ValueError('output_truncated')
        return {'ok':True}
    with pytest.raises(ValueError,match='output_truncated'):
        checked_call('merge','long',call,tasks,lambda:None,lambda a:None,policy='v1')
    restored=json.loads(json.dumps(tasks,sort_keys=True))
    with pytest.raises(ValueError,match='change_output_scope'):
        checked_call('merge','long',call,restored,lambda:None,lambda a:None,policy='v1')
    assert checked_call('merge','shortened output',call,restored,lambda:None,lambda a:None,policy='v1')=={'ok':True}
    assert calls==['long','shortened output']


def test_large_merge_limits_output_without_invalidating_small_cached_merges():
    from report_agents.company_bounded_editorial import draft_merge_prompt,prompt
    data={'partial_drafts':[{'findings':[{'text':'已核事实','information_ids':['S1']}]}]}
    before=deepcopy(data)
    assert draft_merge_prompt('合并规则',data)==prompt('合并规则',data)
    large=deepcopy(data)
    large['partial_drafts'][0]['findings'][0]['text']='已核事实'*4000
    rendered=draft_merge_prompt('合并规则',large)
    assert '正文最多2500字' in rendered and '表格最多16行' in rendered
    assert json.loads(rendered.rsplit('资料：\n',1)[1])==large
    assert data==before


def test_merge_paragraph_citations_resolve_only_exact_unambiguous_input_bindings():
    from report_agents.company_bounded_editorial import draft_references
    group=[{'findings':[{'finding_id':'P-abc','information_ids':['I-source-a']},
                       {'finding_id':'P-def','information_ids':['I-source-b','I-source-c']}]}]
    answer={'findings':[{'text':'正文保持原样。','information_ids':['I-abc','P-def','I-source-a']} ]}
    before=deepcopy(answer);result=draft_references(answer,group)
    assert result['findings'][0]['information_ids']==['I-source-a','I-source-b','I-source-c']
    assert result['findings'][0]['text']==answer['findings'][0]['text'] and answer==before
    assert result['findings'][0]['paragraph_source_references']['I-abc']==['I-source-a']
    unknown={'findings':[{'information_ids':['I-invented']}]}
    assert draft_references(unknown,group)==unknown
    ambiguous=deepcopy(group);ambiguous[0]['findings'].append({'finding_id':'P-abc','information_ids':['I-other']})
    mixed={'findings':[{'information_ids':['I-abc']}]}
    assert draft_references(mixed,ambiguous)==mixed
    collision=deepcopy(group);collision[0]['findings'].append({'finding_id':'P-other','information_ids':['I-abc']})
    assert draft_references(mixed,collision)==mixed


def test_restored_json_key_order_is_not_another_paid_task():
    receipts={};calls=[]
    packet={'partial_drafts':[{'findings':[{'text':'已核事实','aspect':'业务','information_ids':['S1']}]}]}
    def call(role,prompt):calls.append(prompt);return {'ok':True}
    def run(data,tasks):
        return checked_call('公司章节分批正文整合','规则。资料：\n'+json.dumps(data,ensure_ascii=False),
            call,tasks,lambda:None,lambda a:None,policy='v1')
    assert run(packet,receipts)=={'ok':True}
    restored=json.loads(json.dumps(receipts,sort_keys=True))
    assert run(json.loads(json.dumps(packet,sort_keys=True)),restored)=={'ok':True}
    assert len(calls)==1
    packet['partial_drafts'][0]['findings'][0]['text']='事实发生改变'
    run(packet,restored);assert len(calls)==2


def test_pending_checkpoint_compaction_keeps_latest_recovery_and_accepted_bodies():
    from company_flow_state import compact_editorial_progress
    base={'review_mode':'company-core-semantic-v1','chapter':4,'draft_generation':'generation',
        'policy_sha256':'policy','records_sha256':'records','input_sha256':'body',
        'synthesis':{'input_sha256':'sources','bounded_tasks':{}}}
    history=[]
    for i in range(30):
        base['synthesis']['bounded_tasks'][str(i)]={'answer':i};history.append(deepcopy(base))
    accepted={**deepcopy(history[10]),'accepted':True}
    changed={**deepcopy(history[20]),'records_sha256':'changed source'}
    lost=deepcopy(history[-1]);lost['synthesis']['bounded_tasks']={}
    original=deepcopy(history)
    result=compact_editorial_progress(history+[accepted,changed,lost])
    assert result==[history[-1],accepted,changed,lost]
    assert history==original
    assert compact_editorial_progress(json.loads(json.dumps(result,sort_keys=True)))==result


def evidence(target,number,count):
    return [{'information_id':f'S{i:03}','text':f'{target}在2025年完成第{i}项业务。',
        'context':{'period':'2025','context_quote':f'原始文件{i}：'+('业务事项、金额单位及期间。'*80)},
        'provenance':[{'source_id':str(i),'source_url':f'https://example.test/{i}'}],
        'evidence_ids':[f'E-{i}'],'decision':{'status':'use','chapter':number,'aspect':'业务'}} for i in range(count)]


def fake_synthesis(role,prompt):
    data=json.loads(prompt.rsplit('资料：\n',1)[1])
    if role=='公司章节分批结论核对':
        qs=[q for n in data['partial_results'] for q in n['questions']]
        return {'questions':[{**qs[0],'source_ids':list(dict.fromkeys(i for q in qs for i in q['source_ids']))}]}
    if role=='公司章节分批正文整合':
        return {'findings':[f for d in data['partial_drafts'] for f in d['findings']]}
    if role=='公司章节小结补写':
        return {'chapter_summary':{'text':data['body'][0]['text'],'information_ids':data['body'][0]['information_ids']}}
    sources=data['sources'];ids=[r['information_id'] for r in sources]
    if role=='公司章节证据整合':
        return {'questions':[{'question_id':f'Q{i}','aspect':a,'question':'本节有什么依据？','status':'supported' if ids else 'not_obtained',
            'source_ids':ids,'assessment':'依本批原文，有实际业务记录。'} for i,a in enumerate(data['contract']['sections'])],
            'source_handling':[{'information_id':i,'disposition':'main','reason':'本章原文'} for i in ids]}
    assert role=='公司章节整合写作'
    def text(r):return r['text'] if isinstance(r['text'],str) else data['texts'][r['text']['text_ref']]
    return {'findings':[{'finding_id':r['information_id'],'aspect':data['contract']['sections'][0],
        'text':text(r),'information_ids':[r['information_id']]} for r in sources]}


@pytest.mark.parametrize('target,number',[('工业甲',4),('服务乙',5),('医药丙',9)])
def test_narrowed_section_resumes_old_failed_draft_once(target,number):
    rows=evidence(target,number,1)
    contract={'chapter':number,'title':'整章标题','sections':['业务'],'questions':[],
        'requirements':'还有其他小节','chapter_summary_required':False}
    old=synthesize(rows,target,contract,fake_synthesis,plan_only=True)
    old['attempts']['draft']=2;old['error']='invalid_synthesis_paragraph'
    calls=[]
    def call(role,prompt):
        assert role=='公司章节整合写作'
        assert '唯一允许的aspect为["业务"]' in prompt
        calls.append(role);return fake_synthesis(role,prompt)
    completed=synthesize(rows,target,contract,call,old,section_scope=True)
    assert len(calls)==1 and completed['plan']==old['plan']
    assert synthesize(rows,target,contract,lambda *a:pytest.fail('paid twice'),completed,section_scope=True)==completed
    saved=[]
    def wrong(role,prompt):
        response=fake_synthesis(role,prompt);response['findings'][0]['aspect']='其他小节'
        return response
    with pytest.raises(ValueError):
        synthesize(rows,target,contract,wrong,old,save=lambda v:saved.append(v),section_scope=True)
    assert saved[-1]['attempts']['draft']==2
    with pytest.raises(ValueError):
        synthesize(rows,target,contract,lambda *a:pytest.fail('reset retries'),saved[-1],section_scope=True)


def test_known_findings_array_wrapper_keeps_content_validation():
    from report_agents.company_editorial import parse_json_response
    from report_agents.company_chapter_synthesis import validate_draft
    rows=evidence('任意公司',5,1);contract={'sections':['业务']}
    findings=[{'finding_id':'F1','aspect':'业务','text':rows[0]['text'],'information_ids':['S000']}]
    raw=json.dumps(findings)
    with pytest.raises(ValueError,match='not_json_object'):parse_json_response(raw)
    parsed=parse_json_response(raw,array_field='findings')
    assert parsed['findings']==findings
    validate_draft(parsed,rows,contract)
    parsed['findings'][0]['information_ids']=['invented']
    with pytest.raises(ValueError):validate_draft(parsed,rows,contract)


@pytest.mark.parametrize('target,number',[('制造企业甲',4),('物流企业乙',5),('服务企业丙',9)])
def test_oversized_saved_body_is_focused_without_rereading_or_replanning(target,number):
    from report_agents.company_chapter_synthesis import input_key,recover_synthesis
    rows=evidence(target,number,1)
    contract={'chapter':number,'title':'经营情况','sections':['经营'],'questions':[],
        'requirements':'按主要问题组织证据','chapter_summary_required':False}
    state=synthesize(rows,target,contract,fake_synthesis,plan_only=True)
    state['draft']={'findings':[{'finding_id':'F1','aspect':'经营','text':rows[0]['text']*500,'information_ids':['S000']}]}
    state['bounded_tasks']={'previous':{'answer':{'kept':True}}}
    original=deepcopy(state['draft']);calls=[]
    def call(role,prompt):
        assert role=='公司章节分批正文聚焦';calls.append(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert data['allowed_information_ids']==['S000'] and 'questions' not in data
        assert data['section_assessment']==[state['plan']['questions'][0]['assessment']]
        return {'text':rows[0]['text'],'information_ids':['S000'],'content_role':'body'}
    result=synthesize(rows,target,contract,call,previous=state)
    assert len(calls)==1 and result['expanded_draft']==original and len(result['draft']['findings'][0]['text'])<900
    synthesize(rows,target,contract,lambda *a:pytest.fail('repeated focused body'),json.loads(json.dumps(result,sort_keys=True)))
    failed={'synthesis':{}}
    restarted={'synthesis':{'input_sha256':input_key(rows,target,contract),'bounded_tasks':{}}}
    assert recover_synthesis([{'synthesis':result},failed,restarted],rows,target,contract)==result
    assert recover_synthesis([{'synthesis':result},failed],rows,target+'changed',contract) is result


@pytest.mark.parametrize('target,number',[('设备甲',2),('物流乙',6),('医药丙',9)])
def test_large_synthesis_interruption_resume_and_incremental_inputs(target,number):
    rows=evidence(target,number,30);contract={'chapter':number,'title':'业务','sections':['业务'],
        'questions':['业务情况'],'requirements':'','chapter_summary_required':True}
    saved=[];calls=[]
    def call(role,prompt):
        assert size(prompt)<=TASK_BYTES
        calls.append((role,prompt));return fake_synthesis(role,prompt)
    state=synthesize(rows,target,contract,call,save=lambda v:saved.append(deepcopy(v)))
    before=len(calls)
    restored=json.loads(json.dumps(state,ensure_ascii=False,sort_keys=True))
    assert synthesize(rows,target,contract,lambda *a:pytest.fail('unchanged paid task'),restored)==state
    assert len(calls)==before
    # Replay an earlier persisted checkpoint after a simulated process exit.
    partial=next(v for v in saved if v.get('bounded_tasks') and not v.get('plan'))
    recovered=synthesize(rows,target,contract,call,previous=partial)
    assert recovered['draft']['findings']==state['draft']['findings']
    # Appending one source cannot resend previously completed native plan batches.
    calls.clear();updated=synthesize(evidence(target,number,31),target,contract,call,previous=state)
    plan_calls=[p for role,p in calls if role=='公司章节证据整合']
    assert len(plan_calls)==1
    assert 'S030' in plan_calls[0] and 'S000' not in plan_calls[0]


def test_bounded_review_checks_false_gap_in_last_batch_and_resumes():
    rows=evidence('任意公司',5,30)
    findings=[{'finding_id':'F','text':rows[0]['text'],'information_ids':['S000']},
        {'finding_id':'G','text':'未取得业务记录。','information_ids':[],'content_role':'evidence_gap'}]
    tasks={};calls=[]
    def call(role,prompt):
        assert size(prompt)<=TASK_BYTES;calls.append(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':
            ids=[r['information_id'] for r in data['sources']]
            issue={'issue_id':'one','kind':'false_gap','paragraph_ids':['G'],'source_ids':['S029'],
                'source_meaning':'原文有业务记录','output_meaning':'正文写没有','reason':'重要事实被遗漏'}
            return {'sources':[{'information_id':i,'status':'block' if i=='S029' else 'pass',
                'paragraph_ids':['G'],'reason':'已核原文'} for i in ids], 'issues':[issue] if 'S029' in ids else []}
        if role=='公司审稿问题交叉核验':
            assert data['issue']['source_ids']==['S029']
            return {'issue_id':data['issue']['issue_id'],'status':'block','source_ids':['S029'],'reason':'最后一批有明确业务记录，原假缺口仍须修改'}
        if role=='公司章节一致性审稿':return {'issues':[],'advice':[]}
        return passing(data)
    result=bounded_review(rows,findings,'任意公司',call,tasks,lambda:None)
    assert result['issues'] and next(p for p in result['paragraphs'] if p['paragraph_id']=='G')['status']=='block'
    assert bounded_review(rows,findings,'任意公司',lambda *a:pytest.fail('repeat paid review'),
        json.loads(json.dumps(tasks,sort_keys=True)),lambda:None)==result


def test_paragraph_audit_receives_native_verdict_without_other_summary_scope():
    rows=evidence('通用企业',5,1)
    rows[0]['context']['context_quote']='原文独有的实际执行细节，事实摘要未重复。'
    findings=[{'finding_id':'BODY','text':'原文记录实际执行情况。','information_ids':['S000']},
        {'finding_id':'SUMMARY','text':'据披露，公司有实际执行记录。','information_ids':['S000'],
            'content_role':'chapter_summary'}]
    seen=[]
    def call(role,prompt):
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':
            return {'sources':[{'information_id':'S000','status':'pass','paragraph_ids':['BODY','SUMMARY'],
                'reason':'完整原文包含事实摘要之外的实际执行细节。'}],'issues':[]}
        if role=='公司章节一致性审稿':return {'issues':[],'advice':[]}
        assert '完整原文包含事实摘要之外的实际执行细节。' in prompt
        assert '原文独有的实际执行细节，事实摘要未重复。' in prompt
        paragraph=data['paragraphs'][0]
        if paragraph['paragraph_id']=='BODY':assert data['read_only_context']==[]
        else:assert data['read_only_context'][0]['paragraph_id']=='BODY'
        seen.append(paragraph['paragraph_id'])
        return passing(data)
    result=bounded_review(rows,findings,'通用企业',call,{},lambda:None)
    assert not result['issues'] and seen==['BODY','SUMMARY']


def test_large_paragraph_audit_batches_all_sources_preserves_block_and_resumes():
    rows=evidence('其他公司',6,30)
    for i,r in enumerate(rows):r['text']+=(f'第{i}项原文的主体与金额说明。'*80)
    findings=[{'finding_id':'F','text':'公司所有项目均已完成。',
        'information_ids':[r['information_id'] for r in rows]}]
    seen=[];tasks={}
    def call(role,prompt):
        from report_agents.company_task_batches import task_limit
        assert size(prompt)<=task_limit(role)
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节原文保留审稿':
            return {'sources':[{'information_id':r['information_id'],'status':'pass',
                'paragraph_ids':['F'],'reason':'已核原文'+r['information_id']} for r in data['sources']],'issues':[]}
        if role=='公司章节一致性审稿':return {'issues':[],'advice':[]}
        if role=='公司跨批主张复核':
            return {'checks':[deepcopy(next(c for c in data['batch_checks'] if c['status']=='contradicted'))]}
        if role=='公司审稿问题交叉核验':
            return {'issue_id':data['issue']['issue_id'],'status':'block',
                    'source_ids':data['issue']['source_ids'],'reason':'完整证据确认重要事实被错误否定，保留阻塞'}
        ids={r['information_id'] for r in data['sources']};seen.append(ids)
        if role=='公司局部证据主张核验':
            assert '局部证据核验' in prompt
            c={'claim_id':data['claims'][0]['claim_id'],'kind':'direct_fact',
                'status':'contradicted' if 'S029' in ids else 'not_assessed',
                'source_ids':['S029'] if 'S029' in ids else [],'reason':'最后一项尚未完成',
                'issue_kind':'wrong_time','source_meaning':'最后一项尚未完成'}
            return {'checks':[c]}
        a=passing(data)
        if 'S029' in ids:
            a['paragraphs'][0]['status']='block';a['critical_checks'][0]['status']='block'
            a['issues']=[{'issue_id':'last','kind':'wrong_time','paragraph_ids':['F'],'source_ids':['S029'],
                'source_meaning':'最后一项尚未完成','output_meaning':'全部完成','reason':'时间状态错误'}]
        return a
    result=bounded_review(rows,findings,'其他公司',call,tasks,lambda:None)
    assert len(seen)>1 and set.union(*seen)=={r['information_id'] for r in rows}
    assert len(result['paragraphs'])==1 and result['paragraphs'][0]['status']=='block'
    assert any(i['source_ids']==['S029'] for i in result['issues'])
    assert bounded_review(rows,findings,'其他公司',lambda *a:pytest.fail('repeated audit'),
        json.loads(json.dumps(tasks,sort_keys=True)),lambda:None)==result


@pytest.mark.parametrize('target',['工业企业甲','服务企业乙'])
def test_large_history_keeps_existing_timeline_contract(target):
    from test_company_history_timeline import sample
    from report_agents.company_chapter_synthesis import contract_for
    from report_agents.company_history_timeline import validate_timeline
    _,timeline,outline,_,_=sample(target)
    rows=evidence(target,3,30);contract=contract_for(3,outline,target);contract['chapter_summary_required']=False
    def call(role,prompt):
        assert size(prompt)<=TASK_BYTES
        d=json.loads(prompt.rsplit('资料：\n',1)[1])
        if role=='公司章节整合写作':
            assert '不固定套用红筹' in prompt
            sid=d['sources'][0]['information_id'];event=deepcopy(timeline['events'][0])
            event.update(event_id='T'+sid,information_ids=[sid])
            return {'findings':[{'finding_id':sid+str(i),'aspect':a,'text':target+'2020年设立。',
                'information_ids':[sid]} for i,a in enumerate(contract['sections'])],
                'history_timeline':{**deepcopy(timeline),'events':[event],'causal_links':[]}}
        if role=='公司章节分批正文整合':
            return {'findings':[f for part in d['partial_drafts'] for f in part['findings']],
                'history_timeline':{**deepcopy(timeline),'events':[e for part in d['partial_drafts']
                    for e in part['history_timeline']['events']],'causal_links':[]}}
        return fake_synthesis(role,prompt)
    state=synthesize(rows,target,contract,call)
    validate_timeline(state['draft']['history_timeline'],rows)
    assert state['draft']['history_timeline']['decision']=='render'
    assert state['draft']['history_timeline']['causal_links']==[]
    assert synthesize(rows,target,contract,lambda *a:pytest.fail('timeline repeated'),state)==state
