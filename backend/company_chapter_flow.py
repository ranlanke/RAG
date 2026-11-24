"""One company workflow: reuse -> bounded reading/search -> chapter -> report.

Source admission, literal validation, numeric tables and independent narrative
audits are reused from the existing company implementation. Project-scoped
retrieval embeddings are temporary. Only successful final delivery permits
temporary-state cleanup.
"""
from copy import deepcopy, copy
from datetime import datetime
import json
import hashlib
import time
import os
import re
import threading
from types import SimpleNamespace

from company_chapters import VERSION as TEMPLATE_VERSION, chapter, outline, selected_chapters
from company_flow_state import FlowJournal, VERSION, fingerprint, CompanyFlowLimit, compact_editorial_progress
from company_reading import ReadingIndex, source_view, assess_questions
from company_requirements import bind_queries
from report_agents.company_chapter_agent import create_chapter_agents
from report_agents.company_report_agent import CompanyReportAgent
from report_agents.company_corpus import build_corpus_review, research_context, VERSION as CORPUS_VERSION
from report_agents.company_corpus_writer import collect_statements, render_compact_chapters
from report_agents.company_information_usage import build_information_usage, source_information_map, routing_scope
from report_agents.company_editorial import (build_editorial_package,digest_groups,apply_narrative_uses,
    comparable_conflicts,VERSION as EDITORIAL_VERSION,record_payload,fingerprint as editorial_fingerprint,editorial_policy_fingerprint)
from report_agents.company_information_usage import VERSION as USAGE_VERSION
from report_agents.company_delivery_contract import fill_evidence_gaps,followup_chapter,split_sections
from report_agents.report_style import normalize_report_headings
from report_agents.company_evidence import source_body
from report_agents.company_reading_status import pending_native_reading

PACKAGE_KEYS={2:'governance',3:'history',4:'operations',5:'reputation',6:'business',
    7:'management',8:'people',9:'judicial',10:'diligence',11:'risk_register',12:'assessment'}


def _retryable_platform_gap(attempt):
    if attempt.get('status') not in {'failed','not_queried'}:return False
    if isinstance(attempt.get('retryable'),bool):return attempt['retryable']
    return attempt.get('reason') not in {'unauthorized','quota_exceeded','access_denied','tool_unavailable'}


class ChapterFlow:
    def __init__(self,workflow,db,project,intake,emit=None,pause_requested=None):
        from research_store import research_store
        from research_workflow import _emit
        self.workflow=workflow;self.db=db;self.project=project;self.intake=dict(intake)
        self.emit=emit;self.pause_requested=pause_requested;self._emit=_emit
        self.store=research_store
        scope={k:self.intake.get(k) for k in ('target_name','official_identifier','as_of_date',
            'industry','focus_modules','focus_questions','special_requirements','search_queries',
            'ai_search_enabled','required_chapters','scoring_required')}
        scope.update(template_version=TEMPLATE_VERSION,flow_version=VERSION)
        self.journal=FlowJournal(self.store,project.id,scope)
        def model_policy(role):
            if '用途' in role or '逐项' in role:return routing_scope(project)
            if '审稿' in role or '复审' in role or '主张核验' in role or '主张复核' in role:
                from report_agents.company_semantic_review import policy
                owner=re.search(r'company_chapter_(\d+)_agent',role)
                return policy(chapter=int(owner.group(1)) if owner else None)
            if '章节' in role and ('整合' in role or '写作' in role):
                from report_agents.company_chapter_synthesis import SYNTHESIS_POLICY
                return SYNTHESIS_POLICY
            if '问题' in role:
                from company_reading import QUESTION_POLICY
                return [QUESTION_POLICY,TEMPLATE_VERSION]
            return [VERSION,TEMPLATE_VERSION,CORPUS_VERSION]
        self.journal.model_policy=model_policy
        self.reader=ReadingIndex();self.writer=CompanyReportAgent();self.writer.outline=outline()
        self.progressive = os.getenv('COMPANY_PROGRESSIVE_CHAPTER_ENABLED','true').lower() not in {'false','0','off'}
        workflow.company_progressive_transport=self.progressive
        self.read_char_budget=max(3600,int(os.getenv('COMPANY_CHAPTER_READ_CHARS','36000')))
        self.query_budget=max(1,int(os.getenv('COMPANY_CHAPTER_QUERY_BUDGET','4')))
        self.disabled_platforms=set(str(self.intake.get('disabled_platforms','')).split(','))
        self.progressive_selections={}
        self.sections={};self.packages={};self.reads={};self.usages={};self.editorials={};self.coverage={}
        self.final_receipts=[]
        self.raw_invoke=lambda role,prompt,trace=None:workflow._invoke_company_report_editor(
            role,prompt,emit=emit,usage_sink=lambda value:self.journal.usage({**value,'stage':role,'input_sha256':fingerprint([role,prompt])}))
        self.review_calls={}
        self.review_allowance=int(os.getenv('COMPANY_CHAPTER_REVIEW_MAX_CALLS','-1'))
        from report_agents.company_transport import bounded_company_invoke
        # One input exhausting three attempts must not suppress every other
        # unread batch. Allow three exhausted inputs before a global outage
        # stop; fatal auth, balance and configuration errors still stop at once.
        def invoke_attempt(role,prompt,trace=None):
            if self.pause_requested and self.pause_requested():
                from research_workflow import WorkflowPaused
                raise WorkflowPaused('任务暂停请求已收到；不再发送新的模型请求。')
            if self.review_allowance>=0 and '审稿' in role:
                import re
                owner=re.search(r'company_chapter_(\d+)_agent',role)
                key=owner.group(1) if owner else 'report'
                if self.review_calls.get(key,0)>=self.review_allowance:
                    from report_agents.company_transport import CompanyModelUnavailable
                    raise CompanyModelUnavailable(attempted=False,diagnostic={'category':'review_deferred',
                        'blocked_by':'chapter_review_allowance','attempts':0,'request_invoked':False,'request_sent':False})
                self.review_calls[key]=self.review_calls.get(key,0)+1
            return self.journal.invoke(self.raw_invoke,role,prompt,trace)
        transport=bounded_company_invoke(
            invoke_attempt,
            max_attempts=3,threshold=9,retry_base_seconds=10,cooldown_seconds=30)
        self.model_concurrency=max(1,min(6,int(os.getenv('COMPANY_MODEL_CONCURRENCY','1'))))
        self.model_slots=threading.BoundedSemaphore(self.model_concurrency)
        def serial_invoke(role,prompt,trace=None):
            with self.model_slots:
                if self.pause_requested and self.pause_requested():
                    transport._company_last_call.attempts=0
                    from research_workflow import WorkflowPaused
                    raise WorkflowPaused('任务暂停请求已收到；不再发送新的模型请求。')
                cached=self.journal.model_response(role,prompt)
                if cached is not None:
                    # A cache hit is not a transport attempt and must remain
                    # usable even after an unrelated provider circuit opens.
                    transport._company_last_call.attempts=0
                    return cached['answer']
                return transport(role,prompt,trace)
        serial_invoke._company_transport_guard=True
        serial_invoke._company_last_call=transport._company_last_call
        self.invoke=serial_invoke
        project.company_research_intake=dict(intake)
        project.company_template_version=TEMPLATE_VERSION
        project._company_flow_journal=self.journal
        project._company_flow_invoke=self.invoke
        project._company_chapter_agents=create_chapter_agents(project,_emit,emit)
        project.company_reading_mode='detailed'
        project.company_delivery_contract_required=True
        project.company_verified_chapters_required=True
        project.company_flow_version=VERSION
        project.company_professional_platform_attempts=[]
        project.company_judicial_query_attempts=[]

    def event(self,label,detail='',*,number=None,status='running',**extra):
        self._emit(self.emit,'report',chapter(number)['agent_id'] if number else 'company_chapter_flow',
            label,detail,status=status,chapter=number,flow_version=VERSION,**extra)

    def checkpoint(self,stage,number=None):
        self.journal.data['stage']=stage
        self.journal.data['current_chapter']=number
        self.journal.data['source_ids']=[s.id for s in self.project.sources or []]
        self.journal.data['sections']={str(n):text for n,text in self.sections.items()}
        self.journal.data['packages']=self.journal.pack_state('packages',self.packages)
        self.journal.data['reads']=self.journal.pack_state('reads',{str(n):r for n,r in self.reads.items()})
        self.journal.data['usages']=self.journal.pack_state('usages',{str(n):u for n,u in self.usages.items()})
        self.journal.data['editorials']=self.journal.pack_state('editorials',{str(n):e for n,e in self.editorials.items()})
        self.journal.data['coverage']={str(n):c for n,c in self.coverage.items()}
        self.journal.data['platform_attempts']=self.project.company_professional_platform_attempts
        self.journal.data['judicial_attempts']=self.project.company_judicial_query_attempts
        self.journal.save()
        self.project.updated_at=datetime.utcnow();self.db.commit()
        if self.journal.data.get('budget_stop_reason'):
            raise CompanyFlowLimit('company_flow_'+self.journal.data['budget_stop_reason']+'_budget_exhausted')
        if self.pause_requested and self.pause_requested():
            from research_workflow import WorkflowPaused
            self.project.status='paused';self.journal.data['status']='paused'
            self.journal.save();self.db.commit()
            raise WorkflowPaused('任务已暂停；本章资料和写作结果已保存')

    def restore(self):
        if not self.journal.restore():return
        data=self.journal.data
        for name in ('sections','reads','usages','editorials','coverage'):
            setattr(self,name,{int(n):r for n,r in data.get(name,{}).items()})
        self.packages=data.get('packages',{})
        for name,value in self.packages.items():setattr(self.project,name,value)
        self.project.company_professional_platform_attempts=data.get('platform_attempts',[])
        self.project.company_judicial_query_attempts=data.get('judicial_attempts',[])
        self.project.company_question_coverage=self.coverage
        self.project.company_summary_receipt=data.get('summary_receipt',{})
        self.event('恢复逐章研究检查点','已保存内容仍按当前原件、模板和回执复验。',status='done')

    def chapter_invoke(self,number):
        agent=self.project._company_chapter_agents[number]
        def bound(role,prompt,trace=None):return agent.invoke(self.invoke,role,prompt,trace)
        bound._company_transport_guard=True
        bound._company_last_call=self.invoke._company_last_call
        return bound

    def publish_packages(self,view):
        for name,value in vars(view).items():
            if name.startswith('company_') and (name.endswith('_package') or name.endswith('_diagnostic')):
                if name.endswith('_package') and isinstance(value,dict):
                    from report_agents.company_time_context import annotate_time_context
                    annotate_time_context(value,list(self.project.sources or []),self.intake.get('as_of_date',''))
                self.packages[name]=deepcopy(value)
                setattr(self.project,name,value)

    def merge(self):
        facts=[];states={};receipts=[]
        for number,review in sorted(self.reads.items()):
            facts.extend(review.get('facts',[]));receipts.extend(review.get('review_receipts',[]))
            for sid,state in review.get('source_review',{}).items():
                def coverage_score(value):
                    return (value.get('reviewed_windows',0)/max(1,value.get('review_windows',0)),
                        -len(value.get('structural_gaps',[])),value.get('accepted_claims',0))
                reused=states.get(sid,{}).get('reused_by_chapters',[])+[number]
                if sid not in states or coverage_score(state)>coverage_score(states[sid]):states[sid]=deepcopy(state)
                states[sid]['reused_by_chapters']=reused
        facts=list({r.get('statement_id') or fingerprint(r):r for r in facts}.values())
        for sid,state in states.items():state['accepted_claims']=sum(r.get('source_id')==sid for r in facts)
        active_usages={n:u for n,u in self.usages.items() if u.get('version')==USAGE_VERSION and
            u.get('review_scope')==routing_scope(self.project)}
        records=[(r,False) for n,u in sorted(active_usages.items()) for r in u.get('records',[])]
        records += [(r,True) for r in self.routed_records()]
        unique={}
        for record,is_routed in records:
            iid=record['information_id']
            if iid in unique and fingerprint(record_payload(unique[iid]))!=fingerprint(record_payload(record)):
                previous=record_payload(unique[iid]);incoming=record_payload(record)
                same_native={k:v for k,v in previous.items() if k!='decision'}=={k:v for k,v in incoming.items() if k!='decision'}
                same_destination=all(previous.get('decision',{}).get(k)==incoming.get('decision',{}).get(k)
                    for k in ('status','chapter','aspect'))
                # The queue transports native evidence. A refreshed routing
                # explanation cannot replace the destination's audited copy.
                # Its changed input still refreshes that chapter in finalize.
                if not (is_routed and same_native and same_destination):
                    raise ValueError('conflicting_chapter_information_identifier')
            unique.setdefault(iid,record)
        records=list(unique.values())
        statements=[s for n,u in sorted(active_usages.items()) for s in u.get('_statements',[])]
        batches=[b for n,e in sorted(self.editorials.items()) if n in active_usages for b in e.get('batches',[])]
        findings=[f for n,e in sorted(self.editorials.items()) if n in active_usages for f in e.get('findings',[])]
        from report_agents.company_chapter_placements import accounted_ids
        accepted_ids=accounted_ids({'batches':batches})
        review={'version':CORPUS_VERSION,'target_name':self.project.target_name,
            'research_as_of':self.intake.get('as_of_date',''),'research_context':research_context(self.project),
            'facts':facts,'source_review':states,'review_receipts':receipts,
            'diagnostic':{'native_windows_acknowledged':all(
                s.get('reviewed_windows',0)==s.get('review_windows',0) and not s.get('unread_ranges') for s in states.values()),
                'pending_selection_repairs':sum(len(r.get('response',{}).get('pending_selection_repairs') or [])
                    for r in {r['input_sha256']:r for r in receipts if r.get('input_sha256')}.values()),
                'all_candidate_windows_reviewed':all(
                r.get('diagnostic',{}).get('all_candidate_windows_reviewed',False) and
                not any(b.get('error') for b in r.get('review_receipts',[])) for r in self.reads.values()),
                'reading_scope':'full_native_plan_with_explicit_window_acknowledgements',
                'accepted_claims':len(facts)}}
        usage={'version':USAGE_VERSION,'target_name':self.project.target_name,'records':records,
            'review_scope':routing_scope(self.project),
            'review_receipts':[r for u in self.usages.values() for r in u.get('review_receipts',[])],
            'diagnostic':{'pending_information_ids':[r['information_id'] for r in records
                if r.get('decision',{}).get('status') not in {'use','exclude','defer'}]}}
        usage['source_map']=source_information_map(list(self.project.sources or []),review,usage)
        usable=[r for r in records if r.get('decision',{}).get('status')=='use']
        editorial={'version':EDITORIAL_VERSION,'policy_sha256':editorial_policy_fingerprint(),'target_name':self.project.target_name,
            'input_sha256':editorial_fingerprint([record_payload(r) for r in usable]),
            'batches':batches,'findings':findings,'conflicts':comparable_conflicts(records),
            'diagnostic':{'usable_information':len(usable),'narrative_information':len(accepted_ids),
                'pending_information_ids':[r['information_id'] for r in usable if r['information_id'] not in accepted_ids],
                'failed_batches':sum(not b.get('accepted') for b in batches),
                'review_incomplete_batches':sum(b.get('review_status')=='review_incomplete' for b in batches),
                'revision_required_batches':sum(b.get('review_status')=='revision_required' for b in batches),
                'accepted_batches':sum(bool(b.get('accepted')) for b in batches)}}
        self.project.company_source_review_package=review
        self.project.company_information_usage_package=usage
        self.project.company_editorial_package=editorial
        self.project.company_corpus_digest_package={'statements':statements,'groups':digest_groups(editorial),
            'diagnostic':editorial['diagnostic']}
        self.project.company_question_coverage=self.coverage
        return review,usage,editorial

    def selection(self,number,*,expanded=False):
        prior=self.reads.get(number,{}).get('reading_diagnostic',{})
        if prior.get('expanded_in_file'):
            terms=prior.get('gap_terms',[])
        else:
            coverage=self.coverage.get(number,{})
            terms=coverage.get('local_search_terms',[])+[str(field) for q in coverage.get('questions',[])
                if q.get('status') not in {'answered','not_applicable'} for field in q.get('missing_fields',[])]
        selection=self.reader.select(self.project,number,expanded=expanded,gap_terms=terms,
            window_ids_by_source=self.progressive_selections.get(number,{}).get('window_ids_by_source') if self.progressive else None)
        if self.progressive:
            order={tuple(value):i for i,value in enumerate(
                self.progressive_selections.get(number,{}).get('priority_windows',[]))}
            if order:
                docs,states,duplicates=selection['prepared']
                for doc in docs:
                    doc['windows'].sort(key=lambda w:order.get((doc['source_id'],w['window_id']),len(order)))
                docs.sort(key=lambda d:min((order.get((d['source_id'],w['window_id']),len(order))
                    for w in d['windows']),default=len(order)))
                selection['prepared']=(docs,states,duplicates)
                selection['input_sha256']=fingerprint([selection['input_sha256'],
                    self.progressive_selections[number]['priority_windows']])
        priority={r['source_id'] for r in self.journal.data.get('confirmed_original_requests',{}).values()
            if r['chapter']==number and r.get('status')=='confirmed'}
        if priority:
            docs,states,duplicates=selection['prepared']
            selection['prepared']=(sorted(docs,key=lambda d:d['source_id'] not in priority),states,duplicates)
            selection['sources'].sort(key=lambda s:str(s.id) not in priority)
        shared=self.journal.data.get('shared_read_receipts',{})
        selection['input_sha256']=fingerprint([selection['input_sha256'],[record_payload(r) for r in self.routed_records(number)],
            *([sorted(priority)] if priority else []),
            sorted((key,fingerprint(r.get('response',{}))) for key,r in shared.items())])
        return selection

    def read(self,number,*,expanded=False,cached_only=False,assess=True,reading_plan=None):
        if self.progressive:
            from company_progressive_reading import plan_reading
            self.progressive_selections[number]=reading_plan or plan_reading(self,number,expanded=expanded)
        selection=self.selection(number,expanded=expanded)
        view=source_view(self.project,selection['sources'])
        view.company_reading_mode='detailed'
        if self.progressive and chapter(number)['mode']=='research':
            view.company_current_reading_chapter=number
        view.company_cached_reading_only=cached_only
        local_invoke=self.invoke
        # Specialized tables are computed from native documents, not an LLM
        # paraphrase. Only current-chapter records enter its narrative prompt.
        section=self.sections.get(number,f'## {number}. '+chapter(number)['title'])
        old=self.reads.get(number,{})
        partial=self.journal.data.get('partial_reads',{}).get(str(number),[])
        shared=self.journal.data.get('shared_read_receipts',{})
        view.company_corpus_analysis_cache={**old,'review_receipts':list(shared.values())+old.get('review_receipts',[])+partial}
        def save_read_receipts(receipts):
            with self.journal.lock:
                shared=self.journal.data.setdefault('shared_read_receipts',{})
                changed=any(r.get('error') or r['input_sha256'] not in shared or
                    fingerprint(r.get('response'))!=fingerprint(shared[r['input_sha256']].get('response')) for r in receipts)
                if not changed:return  # Identical shared receipts are already durable.
                self.journal.data.setdefault('partial_reads',{})[str(number)]=deepcopy(receipts)
                for receipt in receipts:
                    if not receipt.get('error'):shared[receipt['input_sha256']]=deepcopy(receipt)
                self.journal.save()
        view.company_corpus_receipt_callback=save_read_receipts
        view.company_native_page_reader=self.read_original_page
        prepared=selection['prepared'];reused={}
        if self.progressive:
            from company_reading_reuse import split_reused,merge_reused
            plan=self.progressive_selections[number]
            completed=set(map(tuple,old.get('targeted_reread_windows',[]))) if old.get('reading_plan_sha256')==plan['input_sha256'] else set()
            force=[pair for pair in plan.get('reread_windows',[]) if tuple(pair) not in completed]
            if plan.get('reread_nodes'):
                view.company_reading_gap_request={'plan':plan['input_sha256'],'missing':plan['reread_nodes']}
            prepared,reused=split_reused(prepared,self.reads.values(),self.project.target_name,
                self.intake.get('as_of_date',''),force)
        review=build_corpus_review(view,{'facts':[]},{number:section},local_invoke,self._emit,self.emit,prepared=prepared)
        if self.progressive:
            merge_reused(review,reused)
            review['reading_plan_sha256']=plan['input_sha256']
            done={(str(sid),wid) for sid,st in review['source_review'].items() for d in st.get('review_decisions',[])
                if not d.get('error') for wid in d.get('window_ids',[])}
            review['targeted_reread_windows']=[pair for pair in plan.get('reread_windows',[]) if tuple(pair) in done]
        review['reading_manifest']=selection['manifest']
        review['reading_diagnostic']=selection['diagnostic']
        if self.progressive:
            plan=self.progressive_selections.get(number,{})
            review['reading_diagnostic']['retrieval']=deepcopy(plan.get('retrieval',{}))
            review['reading_diagnostic']['relevant_unread_node_ids']=deepcopy(
                plan.get('unread_relevant_node_ids',{}))
        # Successful shared supplements may change without changing the raw
        # source. Bind the completed read to those receipts for final refresh.
        review['input_sha256']=self.selection(number,expanded=expanded)['input_sha256']
        # Previous literal evidence is retained only against the same source
        # body. A new partial plan must not erase successfully read facts.
        if self.progressive:
            current_hashes={sid:st.get('original_sha256') for sid,st in review.get('source_review',{}).items()}
            retained=[]
            for prior_review in self.reads.values():
                for fact in prior_review.get('facts',[]):
                    if current_hashes.get(str(fact.get('source_id')))!=fact.get('original_sha256'):continue
                    fact=deepcopy(fact)
                    prior_state=prior_review.get('source_review',{}).get(str(fact.get('source_id')),{})
                    fact.setdefault('context',{})['source_reading_coverage']={k:deepcopy(prior_state.get(k)) for k in (
                        'original_sha256','reading_scope','review_windows','reviewed_windows','unread_ranges','structural_gaps')}
                    retained.append(fact)
            review['facts']=list({f['statement_id']:f for f in retained+review.get('facts',[])}.values())
        self.reads[number]=review
        # Preserve completed native windows before a later coverage model can fail.
        self.checkpoint('native_reading_saved',number)
        for request in self.journal.data.get('confirmed_original_requests',{}).values():
            if request['chapter']!=number or request.get('status')!='confirmed':continue
            st=review.get('source_review',{}).get(request['source_id'],{})
            if st.get('original_sha256')!=request['original_sha256']:
                request['status']='source_changed';continue
            request.update(status='read_ranges' if st.get('reviewed_windows',0)>0 else 'attempted_unread',
                reading_receipt={'review_windows':st.get('review_windows',0),
                    'reviewed_windows':st.get('reviewed_windows',0),'state':st.get('state'),
                    'review_sha256':review.get('input_sha256')})
        # Rebuild the numeric/table package with any newly accepted native
        # passages. This is program parsing; it does not reread the document
        # through another generative model request.
        cleaned=json.dumps({'facts':review.get('facts',[])},ensure_ascii=False)
        if self.progressive:
            # The model has already assigned literal native evidence to this
            # chapter. Do not parse every table in the complete long originals
            # or multiply each platform field into another routing request.
            section='## '+str(number)+'. '+chapter(number)['title']
            package={'facts':[]}
        else:
            section,package=self.writer.render_evidence_chapter(view,number,cleaned,local_invoke,self._emit,self.emit)
        self.publish_packages(view)
        self.sections[number]=section
        from company_progressive_reading import chapter_fact_candidates,recalled_fact_candidates
        proofs=(chapter_fact_candidates(review,number,self.usages.get(number),
            recalled=recalled_fact_candidates(self,number)) if self.progressive else
            [r for r in review.get('facts',[]) if r.get('chapter')==number])+package.get('facts',[])+self.routed_records(number)
        previous_coverage=self.coverage.get(number,{})
        partial_coverage=self.journal.data.get('partial_coverage',{}).get(str(number),{})
        reusable=list(self.journal.data.get('question_batch_receipts',{}).values())
        previous_coverage={**previous_coverage,'batches':reusable+previous_coverage.get('batches',[])+partial_coverage.get('batches',[]),
            'consolidation':partial_coverage.get('consolidation') or previous_coverage.get('consolidation')}
        def save_coverage(receipt):
            # Preserve successes across changed batches and interrupted reads.
            # assess_questions still requires the exact current prompt/policy
            # hash; this store never restores a previous overall judgment.
            cache=self.journal.data.setdefault('question_batch_receipts',{})
            for row in receipt.get('batches',[]):
                if row.get('input_sha256') and not row.get('error') and isinstance(row.get('response'),dict):
                    cache[row['input_sha256']]=deepcopy(row)
            self.journal.data.setdefault('partial_coverage',{})[str(number)]=receipt
            self.journal.save()
        if assess:
            self.journal.data.setdefault('pending_question_coverage',{})[str(number)]=True
            self.journal.save()
            coverage=assess_questions(number,self.project.target_name,proofs,self.chapter_invoke(number),previous_coverage,
                receipt_callback=save_coverage,originals=self.project.sources or [],research_scope=self.intake)
            if not coverage.get('errors'):
                self.journal.data['pending_question_coverage'].pop(str(number),None)
        else:
            coverage=previous_coverage if previous_coverage.get('questions') else {'complete':False,'errors':[], 'questions':[
                {'id':f'Q{number:02d}-{i+1}','question':q.format(target=self.project.target_name),
                 'status':'pending_review','missing_fields':[], 'reason':'先保存已有证据支持的初稿，再核对本章剩余缺口。'}
                for i,q in enumerate(chapter(number)['questions'])]}
        self.coverage[number]=coverage
        self.event('本章问题核对待重试' if coverage.get('errors') else '本章已有资料核对完成',
            number=number,status='warning' if coverage.get('errors') else 'done',
            selected_chars=selection['diagnostic']['selected_chars'],
            original_chars=selection['diagnostic']['source_chars'],
            question_states=[q['status'] for q in coverage['questions']],expanded=expanded)
        self.journal.data.setdefault('partial_reads',{}).pop(str(number),None)
        self.checkpoint('reading',number)
        if assess and coverage.get('errors'):
            from report_agents.company_transport import CompanyModelUnavailable
            first=(coverage.get('error_diagnostics') or [{}])[0]
            raise CompanyModelUnavailable(diagnostic={**first,'category':first.get('category') or 'question_review_incomplete',
                'stage':'chapter_question_coverage','blocked_by':'unfinished_question_review',
                'request_invoked':False,'request_sent':False,'attempts':0,'retryable':True})
        return selection

    def collect(self,number,*,gap=False):
        if number not in selected_chapters(self.intake) or not self.intake.get('ai_search_enabled',True):return 0
        record=self.journal.data['chapters'].setdefault(str(number),{})
        flag='gap_search_done' if gap else 'initial_search_done'
        if self.progressive and record.get(flag+'_attempted'):
            self.event('本轮取证已执行，不重复扩大检索',number=number,status='done')
            return 0
        if record.get(flag) and not gap:return 0
        self.journal.reserve('search_queries',0)
        request_counts={key:self.journal.data['metrics'].get(key,0) for key in ('search_queries','platform_requests')}
        local={**self.intake,'focus_modules':[chapter(number)['id']],
            'max_search_rounds':1,'search_queries':[],'_company_chapter_pass':True,
            '_company_query_cache':self.journal}
        full_requirements=getattr(self.project,'company_research_requirements',None)
        plan=self.workflow.plan_research(self.project,local)
        coverage=self.coverage.get(number,{})
        queries=coverage.get('follow_up_queries') or []
        if self.progressive:
            from company_progressive_reading import plan_collection
            frozen=(deepcopy(record[flag+'_plan']) if record.get(flag+'_status')=='interrupted_budget'
                and record.get(flag+'_plan') else plan_collection(self,number,gap=gap))
            self.journal.reserve('search_queries',0)
            record[flag+'_attempted']=True
            record[flag+'_plan']=deepcopy(frozen)
            # Persist BEFORE any side effect. Interrupted rounds are visible
            # as interrupted, not silently reissued as fresh initial searches.
            self.journal.save()
            local['_company_platform_routes']=frozen['platform_routes']
            local['_company_all_web_engines']=True
            queries=[{'module':chapter(number)['id'],'query':q['query'],'search_tier':'open_web',
                'gap_question_id':q['question_id'],'expected_fields':q.get('expected_fields',[]),
                'company_all_web_engines':True} for q in frozen['queries']]
        elif gap:
            queries=[{'module':chapter(number)['id'],'query':q['query'],'search_tier':'open_web',
                'gap_question_id':q['question_id']} for q in queries]
        else:
            queries=self.workflow._expand_search_queries(plan,local)
            if not any(c.get('initial_search_done') for c in self.journal.data['chapters'].values()):
                queries += [{'module':chapter(number)['id'],'query':q,'search_tier':'open_web'}
                    for q in self.intake.get('search_queries',[])]
        queries=bind_queries(queries,plan.get('company_requirements',[]))
        gap_scope=fingerprint([queries,coverage.get('questions',[]),
            [(str(s.id),fingerprint(source_body(s))) for s in self.project.sources or []]])
        if gap and record.get(flag) and record.get('gap_search_scope')==gap_scope:return 0
        from company_candidate_planning import select_originals
        local['_company_candidate_selector']=lambda batches:select_originals(self,number,batches,gap=gap)
        receipt={'phase':'gap' if gap else 'initial','queries':deepcopy(queries),
            'sources_before':len(self.project.sources or []),'status':'running'}
        record.setdefault('search_receipts',[]).append(receipt)
        local['_company_chapter_query_items']=queries
        self.event('本章定向补查' if gap else '本章检索缺失原件',number=number,
            query_count=len(queries),question_ids=[q.get('gap_question_id') for q in queries])
        added=0
        failures=self.journal.data['metrics'].get('search_failures',0)
        audit_start=len(self.project.company_professional_platform_attempts)
        try:
            if not gap or self.progressive:
                added+=self.workflow.collect_structured_sources(self.db,self.project,local,emit=self.emit)
            if queries:
                added+=self.workflow.collect_web_sources(self.db,self.project,[],[],30,emit=self.emit,
                    plan=plan,intake=local,reuse_archived=False,pause_requested=self.pause_requested)
        except Exception as exc:
            if isinstance(exc,CompanyFlowLimit):
                record[flag+'_status']='interrupted_budget'
                receipt['budget_stop_reason']=self.journal.data.get('budget_stop_reason')
                receipt['request_deltas']={key:self.journal.data['metrics'].get(key,0)-value for key,value in request_counts.items()}
                if not any(receipt['request_deltas'].values()):record[flag+'_attempted']=False
            receipt.update(status='interrupted',error_type=type(exc).__name__)
            self.journal.save()
            raise
        finally:
            self.project.company_research_requirements=full_requirements
            self.retain_platform_sources(local)
        self.db.expire(self.project,['sources'])
        attempts=self.project.company_professional_platform_attempts[audit_start:]
        collection_gaps=any(a.get('status') in {'failed','not_queried'} for a in attempts)
        failed=(self.journal.data['metrics'].get('search_failures',0)>failures or
            any(_retryable_platform_gap(a) for a in attempts))
        record[flag]=not failed
        if gap:record['gap_search_scope']=gap_scope
        record[flag+'_status']='retryable_transport_failure' if failed else 'finished_with_gaps' if collection_gaps else 'finished'
        receipt.update(status='failed' if failed else 'finished_with_gaps' if collection_gaps else 'finished' if queries else 'no_query',
            added_sources=added,sources_after=len(self.project.sources or []),
            platform_attempts=deepcopy(attempts))
        self.checkpoint('gap_search' if gap else 'initial_search',number)
        return added

    def read_original_page(self,source_id,page,missing_fields):
        if self.progressive and os.getenv('COMPANY_PDF_OCR_ENABLED','false').lower() in {'false','0','off'}:
            return {'status':'unavailable','reason':'ocr_disabled_image_retained_unread','pixels_read':False,
                    'physical_page':page,'missing_fields':missing_fields}
        from report_agents.company_page_context import read_page_context,page_input_fingerprint
        source=next((s for s in self.project.sources or [] if str(s.id)==str(source_id)),None)
        if source is None:return {'status':'unavailable','reason':'original_source_not_in_task','pixels_read':False}
        key=fingerprint(['company-page-context-v2',source_id,fingerprint(source_body(source)),source.source_metadata,
            page,missing_fields,page_input_fingerprint(source,page,self.store)])
        cached=self.journal.data.setdefault('page_context_receipts',{}).get(key)
        if cached and cached.get('status')=='transcribed':return deepcopy(cached)
        try:receipt=read_page_context(source,page,self.store)
        except Exception as exc:receipt={'status':'failed','exception_type':type(exc).__name__,'pixels_read':False,'reason':'original_page_ocr_failed'}
        self.journal.data['page_context_receipts'][key]=receipt;self.journal.save()
        return receipt

    def retain_platform_sources(self,intake):
        # Raw envelopes are the reading source, not the clipped display table.
        # Preserve even empty/failed responses, explicitly as collection gaps.
        for key in self.journal.data.get('platform_raw_records',{}):
            row=self.journal.cached('platform_raw_records',key)
            title='平台原始返回-'+key
            self.workflow._save_structured_source(self.db,self.project,row['provider'],'raw_receipt',title,
                json.dumps(row['raw_payload'],ensure_ascii=False,indent=2),
                {'tool':row['tool'],'target':row['target'],'query':row['arguments'].get('searchKey') or row['arguments'].get('company_name') or row['arguments'].get('firm_name') or row['arguments'].get('query') or '',
                 'raw_arguments':row['arguments'],'retrieved_at':row['retrieved_at'],
                 'collection_failure':row['collection_failure'],'raw_receipt_sha256':key,
                 'company_platform_raw_response':row['raw_payload']},intake=intake,emit=self.emit)

    def routed_records(self,number=None):
        sources={str(s.id):s for s in self.project.sources or []}
        records=[]
        for value in self.journal.data.get('routed_information',{}).values():
            if value.get('routing_version')!=USAGE_VERSION:continue
            if value.get('review_scope')!=routing_scope(self.project):continue
            if any(sid not in sources or fingerprint(source_body(sources[sid]))!=digest
                    for sid,digest in value.get('source_bindings',{}).items()):continue
            row=value['record']
            if number is None or row['decision']['chapter']==number:records.append(deepcopy(row))
        return records

    def route_information(self,usage,number):
        if usage.get('current_chapter')==number:
            # Chapter-local relevance is complete without assigning a destination
            # to unused material. Preserve it in the usage ledger and shared reads.
            for row in usage['records']:
                if row.get('decision',{}).get('status')=='use' and row['decision'].get('chapter')!=number:
                    raise ValueError('chapter_local_usage_destination_mismatch')
            usage['outbound_routes']=[]
            usage.setdefault('diagnostic',{})['pending_information_ids']=[r['information_id'] for r in usage['records']
                if r.get('decision',{}).get('status') not in {'use','defer'}]
            return usage
        sources={str(s.id):s for s in self.project.sources or []};outbound=[];own=[]
        usage.setdefault('version',USAGE_VERSION);usage.setdefault('review_scope',routing_scope(self.project))
        # Outbound decisions are still this reader's completed work. Keep them
        # for cache revalidation instead of reclassifying them on every sweep.
        for row in usage['records']:
            iid=row['information_id'];queue=self.journal.data.setdefault('routed_information',{})
            if queue.get(iid,{}).get('origin_chapter')==number:queue.pop(iid)
            for other,prior in self.usages.items():
                if other==number:continue
                if prior.get('version')!=USAGE_VERSION or prior.get('review_scope')!=routing_scope(self.project):continue
                displaced=[r for r in prior.get('records',[]) if r['information_id']==iid and
                    r.get('decision',{}).get('chapter')!=row.get('decision',{}).get('chapter')]
                if not displaced:continue
                native=lambda r:{k:v for k,v in record_payload(r).items() if k!='decision'}
                if any(native(r)!=native(row) for r in displaced):
                    raise ValueError('conflicting_chapter_information_identifier')
                prior['records']=[r for r in prior['records'] if r['information_id']!=iid]
                old_editorial=self.editorials.pop(other,{})
                self.journal.data.setdefault('partial_editorials',{}).setdefault(str(other),[]).extend(old_editorial.get('batches',[]))
                self.sections[other]='## '+str(other)+'. '+chapter(other)['title']
                self.journal.data['chapters'].setdefault(str(other),{})['input_sha256']='routing_destination_changed'
            destination=row.get('decision',{}).get('chapter')
            if row.get('decision',{}).get('status')=='use' and destination!=number:
                if not isinstance(destination,int) or not 2<=destination<=11:
                    # Assessment is derived after substantive chapters, never
                    # a destination for otherwise unassigned primary evidence.
                    row['decision']={**row['decision'],'status':'pending_destination_clarification'}
                    own.append(row);continue
                bindings={str(proof['source_id']):fingerprint(source_body(sources[str(proof['source_id'])]))
                    for proof in row.get('provenance',[]) if str(proof.get('source_id')) in sources}
                if not bindings:
                    row['decision']={**row['decision'],'status':'pending_source_binding'};own.append(row);continue
                self.journal.data.setdefault('routed_information',{})[row['information_id']]={
                    'routing_version':USAGE_VERSION,'review_scope':routing_scope(self.project),
                    'record':deepcopy(row),'source_bindings':bindings,'origin_chapter':number}
                outbound.append({'information_id':row['information_id'],'destination_chapter':destination,
                    'reason':row['decision'].get('relevance'),'source_bindings':bindings})
            else:own.append(row)
        incoming=self.routed_records(number)
        usage['routing_input_records']=deepcopy(usage['records'])
        usage['records']=list({r['information_id']:r for r in own+incoming}.values())
        usage['outbound_routes']=outbound
        usage.setdefault('diagnostic',{})['pending_information_ids']=[r['information_id'] for r in usage['records']
            if r.get('decision',{}).get('status') not in {'use','exclude'}]
        return usage

    def write(self,number):
        relevance_chapter=number if chapter(number)['mode']=='research' else None
        view=source_view(self.project,self.project.sources or [])
        for name in PACKAGE_KEYS.values():
            if name!=PACKAGE_KEYS[number]:setattr(view,'company_'+name+'_package',{})
        review=self.reads[number]
        if self.progressive and relevance_chapter is not None:
            from company_progressive_reading import chapter_fact_candidates,recalled_fact_candidates
            review={**review,'facts':chapter_fact_candidates(review,number,self.usages.get(number),
                recalled=recalled_fact_candidates(self,number))}
        statements=[r for r in collect_statements(view,review,include_platform_fields=not self.progressive)
            if relevance_chapter is not None or r['chapter']==number]
        for row in statements:
            row['context']={**row.get('context',{}),'chapter_question_owner':number}
            if self.progressive:
                context=row['context']
                coverage=(row.get('raw_record',{}).get('_prior_admission_coverage')
                    or context.get('source_reading_coverage') or {})
                from company_progressive_reading import compact_reading_coverage
                context['source_reading_coverage']=compact_reading_coverage(coverage)
        digest={'statements':statements,'groups':[],'diagnostic':{}}
        local_invoke=self.invoke
        previous_usage=self.usages.get(number,{})
        routing_previous={**previous_usage,'records':previous_usage.get('routing_input_records',previous_usage.get('records',[]))}
        routing_receipts=self.journal.data.setdefault('routing_step_receipts',{})
        def save_routing(receipt):
            key=fingerprint([receipt.get('review_scope'),receipt.get('routing_record_hashes')])
            routing_receipts[key]=deepcopy(receipt);self.journal.save()
        def load_routing(rows):
            from report_agents.company_information_usage import routing_fingerprint
            scope=routing_scope(self.project);found={}
            for receipt in routing_receipts.values():
                if (receipt.get('routing_version')!=USAGE_VERSION or receipt.get('review_scope')!=scope
                        or receipt.get('current_chapter')!=relevance_chapter
                        or receipt.get('target_name')!=self.project.target_name):continue
                for row in rows:
                    iid=row['information_id']
                    if receipt.get('routing_record_hashes',{}).get(iid)==routing_fingerprint(row,self.project.target_name,scope,outline(),relevance_chapter):
                        if iid in receipt.get('decisions',{}):found[iid]=receipt['decisions'][iid]
            return found
        usage=build_information_usage(digest,{number:self.sections[number]},outline(),self.project.target_name,
            local_invoke,previous=routing_previous,review_scope=routing_scope(self.project),
            current_chapter=relevance_chapter,
            emit=self._emit,trace_emit=self.emit,workers=self.model_concurrency,
            receipt_callback=save_routing,journal_loader=load_routing)
        usage=self.route_information(usage,number)
        self.usages[number]=usage
        # Preview and final rendering must resolve IDs against this chapter's
        # newly routed evidence, not the project's previous chapter snapshot.
        view.company_information_usage_package=usage
        self.checkpoint('information_routed',number)
        partial=self.journal.data.get('partial_editorials',{}).get(str(number),[])
        previous={**self.editorials.get(number,{}),'version':EDITORIAL_VERSION,
            'target_name':self.project.target_name,'batches':self.editorials.get(number,{}).get('batches',[])+partial}
        saved_editorial=self.editorials.get(number,{})
        if saved_editorial.get('review_mode')=='company-core-semantic-v1':
            from report_agents.company_editorial import package_errors
            if not package_errors(saved_editorial,[r for r in usage['records'] if r.get('decision',{}).get('status')=='use'],self.project.target_name):
                previous=saved_editorial
        draft_generation=fingerprint([[record_payload(r) for r in usage['records']],editorial_policy_fingerprint()])
        def save_editorial(receipt):
            history=self.journal.data.setdefault('partial_editorials',{}).setdefault(str(number),[])
            # A revalidated cache hit or a deferred review contains no new
            # model evidence. Preserve the original receipt once instead of
            # copying it into the journal on every source-bound replay.
            if receipt.get('reused') or not any(receipt.get('step_attempts',{}).values()):
                volatile={'attempts','errors','step_attempts','review_status','reused','draft_generation'}
                signature=fingerprint({k:v for k,v in receipt.items() if k not in volatile})
                if any(b.get('draft_generation')==draft_generation and
                    fingerprint({k:v for k,v in b.items() if k not in volatile})==signature for b in reversed(history)):
                    return
            self.journal.data['partial_editorials'][str(number)]=compact_editorial_progress(
                history+[{**deepcopy(receipt),'draft_generation':draft_generation}])
            self.journal.save()
            if self.progressive and receipt.get('findings'):
                # receipt_callback runs in the caller's as_completed loop,
                # not the model worker: preserve proposed paragraphs with their
                # audit state even when the independent review has failed.
                saved=self.journal.data['partial_editorials'][str(number)]
                drafted=list({b['input_sha256']:b for b in saved if b.get('findings') and
                    b.get('draft_generation')==draft_generation}.values())
                if receipt.get('review_mode')=='company-core-semantic-v1':
                    # Each semantic receipt is the whole current chapter,
                    # not another independent slice to concatenate.
                    drafted=[receipt]
                partial_package={'batches':drafted,'findings':[f for b in drafted for f in b.get('findings',[])]}
                preview=render_compact_chapters(view,{'groups':digest_groups(partial_package)},outline(),
                    {number:self.sections[number]})[number]
                record=self.journal.data['chapters'].setdefault(str(number),{})
                record.update(status='partial_draft' if receipt.get('accepted') else 'pending_review',draft_saved=True)
                self.journal.data.setdefault('draft_previews',{})[str(number)]={
                    'text':preview,'review_status':'partial_review' if all(b.get('accepted') for b in drafted) else 'pending_review'}
                self.persist_chapter_draft(number,content=preview,review_status=self.journal.data['draft_previews'][str(number)]['review_status'])
                self.checkpoint('chapter_partial_written',number)
                self.event('章节草稿先保存，其余批次继续核对',number=number,status='done',body_chars=len(preview))
        if self.progressive and os.getenv('COMPANY_CHAPTER_DRAFT_FIRST_ENABLED','false').lower()=='true':
            drafts=build_editorial_package(usage,outline(),self.project.target_name,self.invoke,
                previous=previous,emit=self._emit,trace_emit=self.emit,receipt_callback=save_editorial,
                chapter_agents=self.project._company_chapter_agents,originals=self.project.sources or [],draft_only=True)
            needed={r['information_id'] for r in usage['records'] if r.get('decision',{}).get('status')=='use'}
            mapped={i for b in drafts.get('batches',[]) for finding in b.get('findings',[]) for i in finding.get('information_ids',[])}
            # A synthesis plan may deliberately keep redundant/off-topic records
            # out of prose; accounted-for evidence is not unwritten content.
            mapped.update(s['information_id'] for b in drafts.get('batches',[]) if b.get('synthesis_applied')
                for s in b.get('synthesis',{}).get('plan',{}).get('source_handling',[]))
            mapped.update(p['information_id'] for b in drafts.get('batches',[]) if b.get('accepted') for p in b.get('placements',[]))
            state=self.journal.data['chapters'].setdefault(str(number),{})
            unfinished={i for b in drafts.get('batches',[]) if b.get('draft_scope_complete') is False for i in b.get('information_ids',[])}
            state.update(draft_complete=needed<=mapped and not unfinished,unwritten_information_ids=sorted((needed-mapped)|unfinished),
                drafted_information_count=len((needed & mapped)-unfinished),usable_information_count=len(needed))
            self.checkpoint('chapter_draft_pass_saved',number)
            self.event('本章写作阶段已保存，开始有额度的独立审稿',number=number,status='done' if needed<=mapped else 'warning')
            previous={**drafts,'batches':previous.get('batches',[])+drafts.get('batches',[])}
        editorial=build_editorial_package(usage,outline(),self.project.target_name,self.invoke,
            previous=previous,emit=self._emit,trace_emit=self.emit,receipt_callback=save_editorial,
            chapter_agents=self.project._company_chapter_agents,originals=self.project.sources or [])
        apply_narrative_uses(usage,editorial)
        usage['_statements']=statements
        self.usages[number]=usage;self.editorials[number]=editorial
        self.merge()
        from report_agents.company_chapter_placements import appendix_html
        self.project.company_chapter_appendix_html=appendix_html(self.project.company_information_usage_package,self.project.company_editorial_package)
        rendered=render_compact_chapters(self.project,{'groups':digest_groups(editorial)},outline(),
            {number:self.sections[number]})
        self.sections[number]=self.project._company_chapter_agents[number].execute(
            lambda:rendered[number],phase='chapter_narrative')
        row=self.journal.data['chapters'].setdefault(str(number),{})
        pending=list(dict.fromkeys(editorial['diagnostic']['pending_information_ids']+
            usage.get('diagnostic',{}).get('pending_information_ids',[])))
        def planned_pending(st):
            if not self.progressive:return pending_native_reading(st)
            from report_agents.company_reading_status import completed_reading_state
            checked=completed_reading_state(st,[{**m,'evidence_id':review['source_review'].get(str(m['source_id']),{}).get('evidence_id')}
                for m in review.get('reading_manifest',[])],
                self.journal.data.get('shared_read_receipts',{}).values(),self.project,self.read_original_page)
            row.setdefault('material_limit_receipts',{})[str(st.get('evidence_id'))]=[
                g for g in checked.get('structural_gaps',[]) if g.get('gap_kind')=='material_unavailable']
            return (st.get('reviewed_windows',0)!=st.get('review_windows',0) or
                pending_native_reading({**checked,'unread_ranges':[]}))
        review_pending=bool(pending or self.coverage.get(number,{}).get('errors') or
            any(b.get('error') for b in review.get('review_receipts',[])) or
            not review.get('diagnostic',{}).get('all_candidate_windows_reviewed') or
            any(planned_pending(st) for st in review.get('source_review',{}).values()))
        if self.progressive:
            plan=self.progressive_selections.get(number,{})
            from company_progressive_reading import candidate_receipts
            ledger=candidate_receipts(plan,review)
            unresolved={q['id'] for q in self.coverage.get(number,{}).get('questions',[])
                if q.get('status') not in {'answered','not_applicable'}}
            relevant={qid:[nid for nid in nodes if nid in ledger and ledger[nid]['status'] not in {'read','exclude'}]
                for qid,nodes in plan.get('question_ranked_node_ids',{}).items()
                if qid.split('#',1)[0] in unresolved}
            relevant={qid:ids for qid,ids in relevant.items() if ids}
            selected_unread=any(st.get('reviewed_windows',0)<st.get('review_windows',0)
                for st in review.get('source_review',{}).values())
            vector_status=plan.get('retrieval',{}).get('vector_status','disabled')
            row_pending=bool(relevant or selected_unread or
                (unresolved and vector_status.startswith('unavailable')))
            absence=bool(re.search(r'资料(?:未|没有|不)(?:包含|提供|涉及)|未找到(?:相关|具体)?资料|无法基于现有证据',
                self.sections[number]))
            row=self.journal.data['chapters'].setdefault(str(number),{})
            row['retrieval_receipt']=deepcopy(plan.get('retrieval',{}))
            row['candidate_ledger']=ledger
            row['relevant_unread_by_question']=relevant
            row['selected_originals_pending']=selected_unread
            row['absence_claim_pending_review']=absence and row_pending
            review_pending=review_pending or row['absence_claim_pending_review']
        if editorial.get('review_mode')=='company-core-semantic-v1':
            from report_agents.company_editorial import package_errors
            records=[r for r in usage['records'] if r.get('decision',{}).get('status')=='use']
            review_pending=bool(review_pending or package_errors(editorial,records,self.project.target_name))
            from report_agents.company_rendered_findings import finding_is_rendered
            missing_rendered=[f['finding_id'] for f in editorial.get('findings',[])
                if not finding_is_rendered(self.project,f,self.sections[number],records)]
            row['rendered_paragraphs_complete']=not missing_rendered
            row['missing_rendered_paragraphs']=missing_rendered
            review_pending=review_pending or bool(missing_rendered)
            synthesis=editorial.get('batches',[{}])[0].get('synthesis',{})
            row['composition_complete']=bool(synthesis.get('draft')) and not review_pending
            row['evidence_questions']=deepcopy(synthesis.get('plan',{}).get('questions',[]))
            row['acceptance_scope']='synthesized_chapter_existing_evidence' if synthesis else 'core_facts_existing_evidence_with_disclosed_gaps'
            row['reading_complete']=bool(review.get('diagnostic',{}).get('all_candidate_windows_reviewed'))
            row['deferred_reading_diagnostic']=deepcopy(review.get('diagnostic',{}))
        row.update(status='pending_review' if review_pending else
            'written_with_gaps' if not self.coverage.get(number,{}).get('complete') or
                (editorial.get('review_mode')=='company-core-semantic-v1' and not row.get('reading_complete')) else 'written',
            input_sha256=self.reads[number]['input_sha256'],
            questions=self.coverage.get(number,{}).get('questions',[]),
            pending_information_ids=pending)
        if editorial.get('review_mode')=='company-core-semantic-v1' and not review_pending:
            self.journal.data.setdefault('draft_previews',{})[str(number)]={
                'text':self.sections[number],'review_status':row['status']}
            self.persist_chapter_draft(number,content=self.sections[number],review_status=row['status'])
        self.event('本章草稿与待复核结果已保存' if review_pending else '本章正文与核验结果已保存',
            number=number,status='warning' if review_pending else 'done',
            chapter_status=row['status'],body_chars=len(self.sections[number]))
        self.checkpoint('chapter_written',number)

    def run_chapter(self,number,*,local_only=False):
        if self.progressive:
            return self.run_progressive_chapter(number,local_only=local_only)
        expanded=self.reads.get(number,{}).get('reading_diagnostic',{}).get('expanded_in_file',False)
        current=self.selection(number,expanded=expanded)
        saved=self.journal.data['chapters'].get(str(number),{})
        search_settled=(local_only or
            number not in selected_chapters(self.intake) or not self.intake.get('ai_search_enabled',True) or
            (saved.get('initial_search_done') and (self.coverage.get(number,{}).get('complete') or saved.get('gap_search_done'))))
        # Reuse requires both unchanged source inputs and revalidated narrative
        # receipts, never just a green status string.
        if (search_settled and saved.get('input_sha256')==current['input_sha256'] and number in self.editorials
                and self.reads.get(number,{}).get('diagnostic',{}).get('all_candidate_windows_reviewed') is True
                and not any(b.get('error') for b in self.reads.get(number,{}).get('review_receipts',[]))
                and not any(pending_native_reading(st) for st in self.reads.get(number,{}).get('source_review',{}).values())
                and not self.coverage.get(number,{}).get('errors')
                and self.usages[number].get('version')==USAGE_VERSION
                and self.usages[number].get('review_scope')==routing_scope(self.project)
                and not self.usages[number].get('diagnostic',{}).get('pending_information_ids')
                and not self.editorials[number].get('diagnostic',{}).get('pending_information_ids')):
            from report_agents.company_editorial import package_errors
            records=[r for r in self.usages[number].get('records',[]) if r.get('decision',{}).get('status')=='use']
            if not package_errors(self.editorials[number],records,self.project.target_name):
                self.event('复用已核本章正文',number=number,status='done');return
        self.project.status='report_generating'
        self.event('开始本章：先核对本地资料',number=number)
        self.read(number,expanded=expanded)
        if not expanded and (not self.coverage[number]['complete'] or self.reads[number].get('reading_diagnostic',{}).get('unread_ranges')):
            self.event('本章执行一次文件内补读',number=number)
            self.read(number,expanded=True)
        # A coverage model can judge answers, but cannot silently cancel the
        # selected chapter's requested external research. Each selected chapter
        # gets its own initial collection; identical requests still use cache.
        if not local_only:
            added=self.collect(number)
            if added:self.read(number,expanded=True)
            while not self.coverage[number]['complete']:
                before_originals={fingerprint(source_body(s)) for s in self.project.sources or []
                    if source_body(s)[0] and source_body(s)[1]!='discovery_summary'}
                added=self.collect(number,gap=True)
                if added:self.read(number,expanded=True)
                after_originals={fingerprint(source_body(s)) for s in self.project.sources or []
                    if source_body(s)[0] and source_body(s)[1]!='discovery_summary'}
                if not added or not after_originals-before_originals:break
        if not self.coverage[number]['complete']:
            self.event('本章保留未解决问题',number=number,status='warning',
                stop_reason='bounded_search_finished_or_no_new_proof',
                questions=self.coverage[number]['questions'])
        self.write(number)

    def run_progressive_chapter(self,number,*,local_only=False):
        from company_progressive_reading import plan_reading, VERSION as RESEARCH_VERSION
        record=self.journal.data['chapters'].setdefault(str(number),{})
        self.project.status='report_generating'
        self.event('开始本章：目录定位与已有证据复用',number=number)
        self.restore_draft_preview(number)
        # Unchanged completed rounds do not restart on resume. A changed body
        # is reread and rewrites only the chapter; no fresh external gap round.
        before=fingerprint([(str(s.id),fingerprint(source_body(s))) for s in self.project.sources or []])
        dependencies=fingerprint([record_payload(r) for r in self.routed_records(number)])
        policy=fingerprint([RESEARCH_VERSION,routing_scope(self.project),editorial_policy_fingerprint()])
        if (record.get('research_policy')==RESEARCH_VERSION and record.get('settled_sources_sha256')==before
                and record.get('settled_policy_sha256')==policy
                and record.get('settled_dependency_sha256')==dependencies
                and record.get('status') in {'written','written_with_gaps'}
                and not any(st.get('reviewed_windows',0)<st.get('review_windows',0)
                    for st in self.reads.get(number,{}).get('source_review',{}).values())
                and not any(r['chapter']==number and r.get('status')=='confirmed'
                    for r in self.journal.data.get('confirmed_original_requests',{}).values())
                and number in self.editorials and self.sections.get(number)):
            self.event('复用已保存本章正文与有界取证记录',number=number,status='done')
            return
        # A previous settled round does not prevent resuming a new saved draft.
        record['round_closed']=False
        selection_plan=plan_reading(self,number)
        self.progressive_selections[number]=selection_plan
        if (not local_only and not record.get('initial_search_done_attempted') and
                selection_plan['response']['initial_search_required'] and
                not selection_plan.get('mandatory_node_ids')):
            self.collect(number)
        drafted_from_cache=False
        # If writing was interrupted after persisting a proposal, finish that
        # exact source-bound draft before spending another native-read batch.
        # Otherwise every resume can keep adding evidence ahead of the writer.
        native_states=self.reads.get(number,{}).get('source_review',{})
        def selected_pending():
            return any(st.get('reviewed_windows',0)<st.get('review_windows',0)
                for st in self.reads.get(number,{}).get('source_review',{}).values())
        def finish_selected_reading(*,refresh_coverage=False,expanded=False,reading_plan=None):
            read_new=False
            active_plan=reading_plan or selection_plan
            read_options={'expanded':expanded,'reading_plan':reading_plan} if reading_plan is not None else {}
            for _ in range(max(3,len(active_plan['window_ids_by_source']))):
                if not selected_pending():break
                # Preserve each native receipt; assess the chapter only after
                # the selected window batches are complete.
                self.read(number,assess=False,**read_options)
                read_new=True
            if selected_pending():return False
            if read_new or refresh_coverage:self.read(number,cached_only=True,**read_options)
            return not selected_pending()
        def leave_selected_pending():
            record.update(status='pending_selected_reading',draft_saved=False)
            self.checkpoint('chapter_selected_originals_pending',number)
        source_hashes={str(s.id):hashlib.sha256(source_body(s)[0].encode()).hexdigest()
                       for s in self.project.sources or []}
        pending_synthesis=any(b.get('synthesis',{}).get('bounded_tasks') or b.get('synthesis',{}).get('plan')
            for b in self.editorials.get(number,{}).get('batches',[])+self.journal.data.get('partial_editorials',{}).get(str(number),[]))
        if ((record.get('draft_saved') or pending_synthesis) and not record.get('round_closed') and
                record.get('status') in {'partial_draft','pending_review','pending_writing'} and native_states and
                all(source_hashes.get(str(sid))==st.get('original_sha256') for sid,st in native_states.items())):
            if not pending_synthesis and not finish_selected_reading(refresh_coverage=bool(self.coverage.get(number,{}).get('errors'))):
                leave_selected_pending();return
            self.write(number)
            drafted_from_cache=True
        if not drafted_from_cache and self.journal.data.get('shared_read_receipts') and not record.get('draft_saved'):
            self.read(number,cached_only=True,assess=False)
            if any(f.get('chapter')==number for f in self.reads[number].get('facts',[])):
                # Older interrupted checkpoints have receipts but no final coverage signature.
                coverage=self.coverage.get(number,{})
                pending_coverage=(self.journal.data.get('pending_question_coverage',{}).get(str(number)) or
                    (self.journal.data.get('partial_coverage',{}).get(str(number)) and not coverage.get('input_sha256')))
                if not finish_selected_reading(refresh_coverage=(bool(pending_coverage) or
                        not coverage.get('questions') or bool(coverage.get('errors')))):
                    leave_selected_pending();return
                self.write(number)
                record['draft_saved']=True
                self.checkpoint('chapter_draft_saved',number)
                self.persist_chapter_draft(number)
                drafted_from_cache=True
        if not drafted_from_cache:
            self.read(number,assess=False)
            if not finish_selected_reading(refresh_coverage=True):
                leave_selected_pending();return
            self.write(number)
        def editorial_pending():
            errors=[str(error) for batch in self.editorials.get(number,{}).get('batches',[])
                for error in batch.get('errors',[])]
            if any(error.startswith('chapter_synthesis_') for error in errors):
                record.update(status='pending_writing')
                self.checkpoint('chapter_synthesis_pending',number)
                return True
            if any(error.startswith('semantic_review_failed:') for error in errors):
                # A failed audit is unfinished review, not evidence of a research gap.
                record.update(status='pending_review')
                self.checkpoint('chapter_semantic_review_pending',number)
                return True
            return False
        if editorial_pending():return
        selected_read_key=fingerprint([before,selection_plan['input_sha256']])
        pending_selected=any(st.get('reviewed_windows',0)<st.get('review_windows',0)
            for st in self.reads.get(number,{}).get('source_review',{}).values())
        if drafted_from_cache and pending_selected and record.get('selected_read_complete_sha256')!=selected_read_key:
            # A cached first draft must not skip the relevant unread originals
            # already selected for this chapter and jump straight to web search.
            self.read(number)
            record['selected_read_complete_sha256']=selected_read_key
            self.checkpoint('chapter_selected_read_complete',number)
            self.write(number)
            if editorial_pending():return
        elif not pending_selected:
            record['selected_read_complete_sha256']=selected_read_key
        # Before declaring a gap, revisit unread ranges against the questions
        # using the facts and coverage from the first pass. This is one bounded
        # local pass; it never reopens the external collection round.
        if not self.coverage.get(number,{}).get('complete') and selection_plan['unselected_node_ids']:
            supplement=plan_reading(self,number,expanded=True)
            old_windows=selection_plan['window_ids_by_source']
            new_windows=supplement['window_ids_by_source']
            acknowledged={sid:{wid for d in st.get('review_decisions',[]) if not d.get('error')
                for wid in d.get('window_ids',[])} for sid,st in self.reads[number].get('source_review',{}).items()
                if st.get('original_sha256')==source_hashes.get(sid)}
            targeted_done=set(map(tuple,self.reads[number].get('targeted_reread_windows',[]))) if self.reads[number].get('reading_plan_sha256')==supplement['input_sha256'] else set()
            needs_targeted=any(tuple(pair) not in targeted_done for pair in supplement.get('reread_windows',[]))
            if needs_targeted or any(set(windows)-acknowledged.get(sid,set()) for sid,windows in new_windows.items()):
                self.event('本章已有原文补读',number=number,status='running')
                self.read(number,expanded=True,assess=False,reading_plan=supplement)
                if not finish_selected_reading(refresh_coverage=True,expanded=True,reading_plan=supplement):
                    leave_selected_pending();return
                self.write(number)
                if editorial_pending():return
                record['local_reconciliation_plan_sha256']=supplement['input_sha256']
                self.checkpoint('chapter_local_reconciliation_complete',number)
        preview=self.journal.data.get('draft_previews',{}).get(str(number),{})
        text=preview.get('text') or self.sections.get(number,'')
        if not any(line.strip() and not line.lstrip().startswith('#') for line in text.splitlines()):
            # A saved heading is a checkpoint, not a draft or a research gap.
            record.update(draft_saved=False,status='pending_writing')
            self.checkpoint('chapter_writing_pending',number)
            self.event('本章尚无正文，保留已有证据与计划等待续写',number=number,status='warning')
            return
        record['draft_saved']=True
        self.checkpoint('chapter_draft_saved',number)
        self.persist_chapter_draft(number)
        self.event('本章初稿已保存，开始一次集中补查',number=number,status='done',body_chars=len(self.sections[number]))
        if not local_only and not record.get('gap_search_done_attempted') and not self.coverage.get(number,{}).get('complete'):
            added=self.collect(number,gap=True)
            pending_confirmed=any(r['chapter']==number and r.get('status')=='confirmed'
                for r in self.journal.data.get('confirmed_original_requests',{}).values())
            if added or pending_confirmed:
                supplement=plan_reading(self,number,expanded=True)
                self.read(number,expanded=True,assess=False,reading_plan=supplement)
                if not finish_selected_reading(refresh_coverage=True,expanded=True,reading_plan=supplement):
                    leave_selected_pending();return
                self.write(number)
                if editorial_pending():return
        record.update(research_policy=RESEARCH_VERSION,round_closed=True,
            settled_policy_sha256=policy,
              settled_dependency_sha256=fingerprint([record_payload(r) for r in self.routed_records(number)]),
            settled_sources_sha256=fingerprint([(str(s.id),fingerprint(source_body(s))) for s in self.project.sources or []]),
            remaining_questions=deepcopy(self.coverage.get(number,{}).get('questions',[])),
            unselected_reading_plan=deepcopy(self.progressive_selections.get(number,{})))
        self.persist_chapter_draft(number)
        self.checkpoint('chapter_research_settled',number)
        self.event('本章取证已收尾，保留具体缺口并进入下一章',number=number,status='done',
            chapter_status=record.get('status'),body_chars=len(self.sections[number]))

    def restore_draft_preview(self,number):
        """A failed audit does not erase a native-bound, explicitly pending draft."""
        from report_agents.company_editorial import parse_proposal
        import hashlib
        usage=self.usages.get(number,{})
        lookup={r['information_id']:r for r in usage.get('records',[])}
        hashes={str(s.id):hashlib.sha256(source_body(s)[0].encode()).hexdigest() for s in self.project.sources or []}
        found=[]
        saved=self.journal.data.get('partial_editorials',{}).get(str(number),[])
        generation=next((b['draft_generation'] for b in reversed(saved) if b.get('draft_generation')),None)
        batches={b['input_sha256']:b for b in saved if generation is None or b.get('draft_generation')==generation}
        for b in batches.values():
            ids=b.get('information_ids') or []
            if not ids or any(i not in lookup for i in ids):continue
            rows=[lookup[i] for i in ids]
            if any(not r.get('context',{}).get('original_sha256') or
                any(hashes.get(str(p.get('source_id')))!=r['context']['original_sha256'] for p in r.get('provenance',[])) for r in rows):continue
            if b.get('review_mode')=='company-core-semantic-v1':
                from report_agents.company_semantic_review import prepare_findings
                try:findings=prepare_findings(b.get('findings',[]),rows,number,b['aspect'])
                except (ValueError,KeyError,TypeError):continue
            else:
                findings,_=parse_proposal({'findings':b.get('findings',[]),'placements':b.get('placements',[])},
                    rows,number,b['aspect'])
            found.extend(findings)
        if not found:return
        view=source_view(self.project,self.project.sources or [])
        preview=render_compact_chapters(view,{'groups':digest_groups({'findings':found})},outline(),
            {number:self.sections.get(number,'')})[number]
        self.journal.data.setdefault('draft_previews',{})[str(number)]={'text':preview,'review_status':'pending_review'}
        self.persist_chapter_draft(number,content=preview,review_status='pending_review')
        self.journal.save()

    def persist_chapter_draft(self,number,*,content=None,review_status=None):
        """Persist actual narrative before supplementation, not just receipts."""
        if not hasattr(self.workflow,'save_artifact') or not hasattr(self.db,'add'):return
        preview=self.journal.data.get('draft_previews',{}).get(str(number),{})
        if (content is None and preview and
                self.journal.data['chapters'].get(str(number),{}).get('status') in {'pending_review','partial_draft'}):
            content=preview['text']
            review_status=preview['review_status']
        artifact=self.workflow.save_artifact(self.db,self.project,'company_chapter_draft',
            self.project.title+' · '+chapter(number)['title'],self.sections[number] if content is None else content,
            {'chapter':number,'delivery_status':'review_draft','full_report_accepted':False,
             'narrative_review_status':review_status or self.journal.data['chapters'].get(str(number),{}).get('status'),
             'coverage':deepcopy(self.coverage.get(number,{})),
             'research_state':deepcopy(self.journal.data['chapters'].get(str(number),{}))})
        self.journal.data['chapters'][str(number)]['draft_artifact_id']=artifact.id
        self.db.commit()

    def finalize(self):
        # New evidence can revise earlier chapters. Refresh only changed native
        # dependencies; this final sweep performs no additional external search.
        for number in range(2,12):
            old=self.journal.data['chapters'].get(str(number),{})
            expanded=self.reads.get(number,{}).get('reading_diagnostic',{}).get('expanded_in_file',False)
            now=self.selection(number,expanded=expanded)
            if old.get('input_sha256')!=now['input_sha256']:
                self.event('后续原件影响前章，定向复核',number=number)
                self.run_chapter(number,local_only=True)
        self.merge()
        candidate_queues=self.journal.data.get('chapter_candidate_queues',{})
        candidate_audit=None
        if candidate_queues:
            candidate_audit=self.workflow.save_artifact(self.db,self.project,'company_collection_audit',
                self.project.title+' · 逐章候选原件取用与未读递延台账',
                json.dumps({'version':'company-question-led-original-selection-v1','queues':candidate_queues,
                    'plans':self.journal.data.get('candidate_selection_receipts',{}),
                    'raw_retention':'候选原始返回在本次任务临时存储保留；递延候选未被当作已读原文或已接纳事实。'},ensure_ascii=False),
                {'audit_role':'chapter_candidate_selection'})
            self.project.company_collection_audit_artifact_id=candidate_audit.id
        from report_agents.company_chapter_placements import appendix_html
        self.project.company_chapter_appendix_html=appendix_html(self.project.company_information_usage_package,self.project.company_editorial_package)
        self.event('汇总综合评估与最终摘要')
        # Assessment is deterministic over already retained company proofs;
        # it does not open another full-document generative reading pass.
        view=source_view(self.project,self.project.sources or [])
        text,_=self.writer.render_evidence_chapter(view,12,json.dumps({'facts':self.project.company_source_review_package['facts']},
            ensure_ascii=False),self.chapter_invoke(12),self._emit,self.emit)
        self.publish_packages(view);self.sections[12]=text
        from report_agents.company_research_completeness import relationship_evidence
        self.project.company_relationship_package=relationship_evidence(self.project,self.invoke)
        self.sections[1]=self.writer._write_summary(self.project,'{}',self.sections,self.invoke,self._emit,self.emit)
        self.sections[13]='## 13. '+chapter(13)['title']+'\n\n来源索引由本报告实际引用生成。'
        self.sections[14]=self.project._company_chapter_agents[14].execute(
            lambda:followup_chapter(self.project,outline()),phase='followup')
        self.sections=fill_evidence_gaps(self.sections,self.project.company_editorial_package,outline(),
            usage=self.project.company_information_usage_package,project=self.project)
        report='\n\n'.join(normalize_report_headings(self.sections[n],{n:chapter(n)['title']}) for n in range(1,15))
        report=self.workflow._append_professional_evidence_assets(self.project,report,'{}',emit=self.emit)
        report=self.workflow._append_report_evidence_index(self.project,report,'{}',emit=self.emit)
        report,metadata=self.workflow._prepare_company_delivery(self.project,report,emit=self.emit)
        # Final review may repair accepted batches. Persist those actual receipts,
        # so resume does not restore the pre-repair narrative over the final one.
        final_editorial=self.project.company_editorial_package
        for number in self.editorials:
            own=self.editorials[number]
            for key in ('batches','findings'):
                own[key]=[deepcopy(row) for row in final_editorial.get(key,[]) if row.get('chapter')==number]
            usable=[r for r in self.usages[number].get('records',[]) if r.get('decision',{}).get('status')=='use']
            own['input_sha256']=editorial_fingerprint([record_payload(r) for r in usable])
            from report_agents.company_chapter_placements import accounted_ids
            accepted=accounted_ids(own)
            own['diagnostic']['pending_information_ids']=[r['information_id'] for r in usable if r['information_id'] not in accepted]
        self.sections.update(split_sections(report))
        self.checkpoint('final_review')
        self.project.company_chapter_agents_metadata=[a.metadata() for a in self.project._company_chapter_agents.values()]
        from research_workflow import COMPANY_REPORT_VERSION
        metadata.update(report_pipeline_version=COMPANY_REPORT_VERSION,
            company_flow_version=VERSION,company_template_version=TEMPLATE_VERSION,
            company_question_coverage=deepcopy(self.coverage),
            company_flow_summary=self.summary(),company_summary_evidence=getattr(self.project,'company_summary_package',{}),
            company_summary_diagnostic=getattr(self.project,'company_summary_diagnostic',{}),
            company_source_review_evidence=self.project.company_source_review_package,
            company_relationship_evidence=self.project.company_relationship_package,
            company_candidate_planning={'audit_artifact_id':candidate_audit.id if candidate_audit else None,
                'chapters':{n:{'candidates':len(queue),'dispositions':dict(__import__('collections').Counter(
                    r.get('disposition') for r in queue.values()))} for n,queue in candidate_queues.items()}},
            company_qualitative_assessment_evidence=getattr(self.project,'company_qualitative_assessment_package',{}),
            company_professional_platform_attempts=self.project.company_professional_platform_attempts)
        for number,name in PACKAGE_KEYS.items():
            metadata['company_'+name+'_evidence']=getattr(self.project,'company_'+name+'_package',{})
            metadata['company_'+name+'_diagnostic']=getattr(self.project,'company_'+name+'_diagnostic',{})
        try:self.workflow._validate_delivery_report(self.project,report)
        except RuntimeError as exc:
            metadata.update(quality_warning=str(exc),delivery_status='review_draft')
        else:metadata['delivery_status']='final_candidate'
        pending_absence=[n for n,row in self.journal.data['chapters'].items()
            if row.get('absence_claim_pending_review')]
        if pending_absence:
            metadata.update(quality_warning='相关原文未读时存在资料缺失断言：第'+
                '、'.join(map(str,pending_absence))+'章',delivery_status='review_draft')
        artifact=self.workflow.save_artifact(self.db,self.project,'desktop_report',self.project.title+' · 公司研究报告',report,metadata)
        # These are final evidence receipts, referenced by the report and its
        # download UI, rather than disposable original documents/query caches.
        for kind,attribute,label in (
                ('company_source_coverage','company_source_coverage_package','逐来源处理台账'),
                ('company_information_usage','company_information_usage_package','逐项信息使用与排除清单'),
                ('company_editorial','company_editorial_package','正文精编与语义验收'),
                ('company_corpus_digest','company_corpus_digest_package','章节原文与证据表')):
            package=getattr(self.project,attribute,{}) or {}
            if not package:continue
            receipt=self.workflow.save_artifact(self.db,self.project,kind,self.project.title+' · '+label,
                json.dumps(package,ensure_ascii=False),{'report_artifact_id':artifact.id,
                    'company_flow_version':VERSION,'summary':package.get('summary',package.get('diagnostic',{}))})
            artifact.artifact_metadata={**(artifact.artifact_metadata or {}),kind+'_artifact_id':receipt.id}
            self.final_receipts.append(receipt)
        appendix=getattr(self.project,'company_chapter_appendix_html','')
        if appendix:
            receipt=self.workflow.save_artifact(self.db,self.project,'company_evidence_annex',self.project.title+' · 章节证据附件',appendix,
                {'report_artifact_id':artifact.id,'company_flow_version':VERSION,'placement_scope':'chapter_writer_independently_reviewed',
                 'sha256':__import__('hashlib').sha256(appendix.encode()).hexdigest()})
            artifact.artifact_metadata={**(artifact.artifact_metadata or {}),'company_chapter_appendix_artifact_id':receipt.id}
            self.final_receipts.append(receipt)
        self.db.commit()
        self.journal.data['report_artifact_id']=artifact.id
        self.checkpoint('report_exported')
        return artifact

    def summary(self):
        self.journal.data['metrics']['elapsed_seconds']=round(time.monotonic()-self.journal.started,3)
        return {'version':VERSION,'run_id':self.journal.data['run_id'],
            'metrics':deepcopy(self.journal.data['metrics']),'limits':dict(self.journal.limits),
            'chapters':{n:{k:v for k,v in c.items() if k!='raw'} for n,c in self.journal.data['chapters'].items()},
            'token_usage_complete':self.journal.data['metrics']['reported_usage_calls']==self.journal.data['metrics']['model_calls'],
            'model_concurrency':self.model_concurrency,
            'embeddings_created':sum(p.get('retrieval',{}).get('new_embeddings',0)
                for p in self.journal.data.get('progressive_reading_plans',{}).values()),
            'parsed_source_versions':self.reader.parsed_sources,
            'reading_mode':'chapter_hybrid_required_native_passages'}

    def run(self):
        before={s.id for s in self.project.sources or []}
        self.restore()
        try:
            if self.journal.data.get('status')=='cleanup_pending':
                artifact=self.workflow._latest_artifact(self.project,'desktop_report')
                if artifact and artifact.id==self.journal.data.get('report_artifact_id') and self.delivery_ready(artifact):
                    self.finish_cleanup(artifact)
                    return [artifact],0
                raise ValueError('cleanup_report_dependency_changed')
            self.journal.data['status']='running'
            self.intake=self.workflow.resolve_business_entity(self.project,self.intake,emit=self.emit)
            plan=self.workflow.plan_research(self.project,self.intake,emit=self.emit)
            self.journal.data['plan']=plan
            self.checkpoint('planning')
            for number in range(2,12):self.run_chapter(number)
            artifact=self.finalize()
            # Preserve the existing proposal deliverable. Its optional model
            # pass uses the same task budget, with a workflow-local adapter.
            proposal_workflow=copy(self.workflow)
            proposal_workflow._invoke=lambda role,prompt,emit=None,**kwargs:self.invoke(role,prompt)
            proposal=proposal_workflow.generate_proposal(self.project,artifact.content,'{}',emit=self.emit)
            proposal=self.workflow._normalize_proposal_placeholders(proposal)
            warning=self.workflow._proposal_quality_warning(proposal,target_type='company',project=self.project)
            if warning:proposal=self.workflow._fallback_company_proposal(self.project)
            proposal_artifact=self.workflow.save_artifact(self.db,self.project,'proposal',
                self.project.title+' · 项目建议书',proposal,
                {'company_flow_version':VERSION,'report_artifact_id':artifact.id,'requires_human_review':True})
            self.checkpoint('proposal_exported')
            meta=artifact.artifact_metadata or {}
            ready=self.delivery_ready(artifact)
            self.project.status='completed' if ready else 'diagnostic'
            self.journal.data['status']='cleanup_pending' if ready else 'review_required'
            artifact.artifact_metadata={**meta,'company_flow_summary':self.summary(),
                'temporary_materials_status':'cleanup_pending' if ready else 'retained_for_review'}
            self.journal.save();self.db.commit()
            if ready:
                self.finish_cleanup(artifact)
            else:self.event('报告已生成待复核稿，保留临时资料用于续跑',status='warning')
            return [artifact,proposal_artifact,*self.final_receipts],len({s.id for s in self.project.sources or []}-before)
        except Exception as exc:
            from research_workflow import WorkflowPaused
            self.db.rollback()
            self.project.status='paused' if isinstance(exc,(WorkflowPaused,CompanyFlowLimit)) else 'failed'
            self.journal.data['status']='budget_exhausted' if isinstance(exc,CompanyFlowLimit) else self.project.status
            self.journal.data['error_type']=type(exc).__name__
            self.journal.save();self.db.commit()
            raise

    @staticmethod
    def delivery_ready(artifact):
        meta=artifact.artifact_metadata or {}
        return bool((meta.get('company_delivery_contract') or {}).get('delivery_ready') is True
            and meta.get('delivery_status')=='final_candidate' and not meta.get('quality_warning'))

    def finish_cleanup(self,artifact):
        if not self.delivery_ready(artifact):raise ValueError('cleanup_before_validated_export')
        cleaned=self.workflow._finalize_source_records(self.db,self.project,self.emit)
        if cleaned:
            try:self.store.delete_prefix(self.journal.prefix)
            except Exception:cleaned=False
        artifact.artifact_metadata={**(artifact.artifact_metadata or {}),
            'temporary_materials_status':'cleared_after_success' if cleaned else 'cleanup_pending',
            'company_flow_summary':self.summary()}
        self.project.status='completed';self.db.commit()
        if cleaned:self.event('报告已成功生成，临时阅读资料已清理',status='done')
        else:
            self.journal.data['status']='cleanup_pending';self.journal.save()
            self.event('报告已生成，临时资料清理尚未完成',status='warning')


def run_company_chapters(workflow,db,project,intake,emit=None,pause_requested=None):
    from report_agents.company_text import company_matching_cache
    with company_matching_cache():
        return ChapterFlow(workflow,db,project,intake,emit,pause_requested).run()
