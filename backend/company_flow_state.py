"""Task-local, resumable company research state; no vector database required."""
from copy import deepcopy
import hashlib
import json
import math
import os
import threading
import time
import uuid

VERSION = 'company-chapter-flow-v1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',',':'),default=str).encode()).hexdigest()


class CompanyFlowLimit(RuntimeError):
    pass


def compact_editorial_progress(receipts):
    """Keep the latest complete recovery state for an unchanged pending body."""
    result=[];latest={}
    for receipt in receipts:
        synthesis=receipt.get('synthesis',{})
        eligible=(receipt.get('review_mode')=='company-core-semantic-v1' and
            not receipt.get('accepted') and synthesis.get('input_sha256') and receipt.get('records_sha256'))
        key=tuple(receipt.get(k) for k in ('chapter','draft_generation','policy_sha256','records_sha256','input_sha256'))+(synthesis.get('input_sha256'),)
        old=latest.get(key) if eligible else None
        if old is not None:
            prior=result[old]
            # Never compact a state that contains tasks absent from its successor.
            if (set(prior.get('synthesis',{}).get('bounded_tasks',{}))<=set(synthesis.get('bounded_tasks',{})) and
                    set(prior.get('bounded_review_tasks',{}))<=set(receipt.get('bounded_review_tasks',{})) and
                    len(prior.get('semantic_history',[]))<=len(receipt.get('semantic_history',[]))):
                result[old]=None
        if eligible:latest[key]=len(result)
        result.append(receipt)
    return [r for r in result if r is not None]


class FlowJournal:
    def __init__(self, store, project_id, scope):
        self.store=store
        self.prefix=f'projects/{int(project_id)}/company_chapter_flow'
        self.scope=deepcopy(scope)
        self.lock=threading.RLock()
        self.query_locks={}
        self.last_state_key=None
        self._written_state_texts=set()
        self.run_cost_reserved_yuan=0.0
        self.started=time.monotonic()
        self.data={'version':VERSION,'scope':deepcopy(scope),'run_id':uuid.uuid4().hex,
            'status':'running','chapters':{},'query_cache':{},'platform_cache':{},
            'source_ids':[],'events':[],'metrics':{'model_calls':0,'input_chars':0,
            'output_chars':0,'input_tokens':0,'output_tokens':0,'reported_usage_calls':0,
            'search_queries':0,'query_cache_hits':0,'platform_requests':0,'platform_cache_hits':0,'elapsed_seconds':0}}
        self.limits={key:int(os.getenv(env,str(default))) for key,env,default in (
            ('model_calls','COMPANY_FLOW_MAX_MODEL_CALLS',500),
            ('input_chars','COMPANY_FLOW_MAX_INPUT_CHARS',24000000),
            ('search_queries','COMPANY_FLOW_MAX_SEARCH_QUERIES',600),
            ('platform_requests','COMPANY_FLOW_MAX_PLATFORM_REQUESTS',500),
            ('elapsed_seconds','COMPANY_FLOW_MAX_SECONDS',14400))}
        if any(n<=0 for n in self.limits.values()):raise ValueError('invalid_company_flow_limits')

    def restore(self):
        try:pointer=json.loads(self.store.get_text(self.prefix+'/current.json'))
        except FileNotFoundError:return False
        key=self.prefix+'/'+str(pointer.get('sha256',''))+'.json'
        if pointer.get('key')!=key or len(str(pointer.get('sha256','')))!=64:
            raise ValueError('invalid_company_flow_pointer')
        raw=self.store.get_text(key)
        if hashlib.sha256(raw.encode()).hexdigest()!=pointer['sha256']:
            raise ValueError('company_flow_state_hash_mismatch')
        value=json.loads(raw)
        if value.get('version')!=VERSION or value.get('scope')!=self.scope:return False
        if value.get('status')=='completed':return False
        text_cache={}
        for name in ('reads','packages','usages','editorials'):
            item=value.get(name)
            if isinstance(item,dict) and item.get('_state_object_v1'):
                expected=self.prefix+'/state/'+name+'/'+str(item.get('sha256',''))+'.json'
                if item.get('key')!=expected or len(str(item.get('sha256','')))!=64:
                    raise ValueError('company_state_object_scope_mismatch')
                payload=self.store.get_text(expected)
                if hashlib.sha256(payload.encode()).hexdigest()!=item['sha256']:
                    raise ValueError('company_state_object_hash_mismatch')
                value[name]=self._unpack_state_text(json.loads(payload),text_cache)
        self.data=value
        self.last_state_key=key
        self.started=time.monotonic()-float(value['metrics'].get('elapsed_seconds',0))
        # At a quota ceiling cached work is still legal. New reservations keep
        # checking amount + usage, so clearing this old marker adds no calls.
        if value.get('budget_stop_reason') in self.limits and all(value['metrics'].get(k,0)<=limit for k,limit in self.limits.items()):
            self.data.pop('budget_stop_reason',None)
        return True

    def save(self):
        with self.lock:
            self.data['metrics']['elapsed_seconds']=round(time.monotonic()-self.started,3)
            self.data['limits']=dict(self.limits)
            raw=json.dumps(self.data,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
            digest=hashlib.sha256(raw.encode()).hexdigest()
            key=self.prefix+'/'+digest+'.json'
            self.store.put_text(key,raw,'application/json; charset=utf-8')
            pointer=json.dumps({'key':key,'sha256':digest})
            if self.store.mode=='local':
                path=self.store._local_path(self.prefix+'/current.json')
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_name('current.'+uuid.uuid4().hex+'.tmp')
                try:
                    temporary.write_text(pointer,encoding='utf-8')
                    # Antivirus/indexer readers can briefly lock the pointer on Windows.
                    for attempt in range(5):
                        try:
                            os.replace(temporary,path)
                            break
                        except PermissionError:
                            if attempt==4:raise
                            time.sleep(.05*(attempt+1))
                finally:temporary.unlink(missing_ok=True)
            else:self.store.put_text(self.prefix+'/current.json',pointer,'application/json; charset=utf-8')
            previous=self.last_state_key;self.last_state_key=key
            if previous and previous!=key:
                try:self.store.delete_object(previous)
                except Exception:pass  # Final scoped cleanup also removes old snapshots.

    def cached(self,kind,key):
        value=self.data[kind].get(key)
        if value is None:return None
        if not value.get('_cache_object_v1'):return deepcopy(value)
        expected=self.prefix+'/cache/'+kind+'/'+str(value.get('sha256',''))+'.json'
        if value.get('key')!=expected:raise ValueError('company_cache_object_scope_mismatch')
        raw=self.store.get_text(expected)
        if hashlib.sha256(raw.encode()).hexdigest()!=value['sha256']:
            raise ValueError('company_cache_object_hash_mismatch')
        return json.loads(raw)

    def pack_state(self,name,value):
        """Freeze completed packages once per checkpoint, retaining every byte."""
        if name not in {'reads','packages','usages','editorials'}:raise ValueError('invalid_company_state_field')
        if not value:return deepcopy(value)
        raw=json.dumps(self._pack_state_text(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
        digest=hashlib.sha256(raw.encode()).hexdigest()
        key=self.prefix+'/state/'+name+'/'+digest+'.json'
        self.store.put_text(key,raw,'application/json; charset=utf-8')
        return {'_state_object_v1':True,'key':key,'sha256':digest}

    def _pack_state_text(self,value):
        if isinstance(value,str) and len(value)>=1000:
            digest=hashlib.sha256(value.encode()).hexdigest()
            key=self.prefix+'/state/text/'+digest+'.txt'
            if digest not in self._written_state_texts:
                self.store.put_text(key,value,'text/plain; charset=utf-8');self._written_state_texts.add(digest)
            return {'_state_text_v1':True,'key':key,'sha256':digest}
        if isinstance(value,list):return [self._pack_state_text(v) for v in value]
        if isinstance(value,dict):
            result={k:self._pack_state_text(v) for k,v in value.items()}
            # Preserve even literal data whose field names resemble our markers.
            return {'_state_literal_dict_v1':result} if any(k in value for k in ('_state_text_v1','_state_literal_dict_v1')) else result
        return value

    def _unpack_state_text(self,value,cache):
        if isinstance(value,list):return [self._unpack_state_text(v,cache) for v in value]
        if not isinstance(value,dict):return value
        if set(value)=={'_state_literal_dict_v1'}:
            return {k:self._unpack_state_text(v,cache) for k,v in value['_state_literal_dict_v1'].items()}
        if value.get('_state_text_v1'):
            digest=str(value.get('sha256',''));key=self.prefix+'/state/text/'+digest+'.txt'
            if value.get('key')!=key or len(digest)!=64:raise ValueError('company_state_text_scope_mismatch')
            if key not in cache:
                raw=self.store.get_text(key)
                if hashlib.sha256(raw.encode()).hexdigest()!=digest:raise ValueError('company_state_text_hash_mismatch')
                cache[key]=raw
            return cache[key]
        return {k:self._unpack_state_text(v,cache) for k,v in value.items()}

    def remember(self,kind,key,value,*,persist=True):
        # Native provider responses live once, outside the small checkpoint.
        # Re-saving a chapter must not duplicate every earlier search body.
        raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)
        digest=hashlib.sha256(raw.encode()).hexdigest()
        object_key=self.prefix+'/cache/'+kind+'/'+digest+'.json'
        self.store.put_text(object_key,raw,'application/json; charset=utf-8')
        self.data[kind][key]={'_cache_object_v1':True,'key':object_key,'sha256':digest}
        if persist:self.save()

    def reserve(self, key, amount=1):
        with self.lock:
            m=self.data['metrics']
            if time.monotonic()-self.started>self.limits['elapsed_seconds']:
                self.data['budget_stop_reason']='elapsed_seconds'
                raise CompanyFlowLimit('company_flow_time_budget_exhausted')
            if m.get(key,0)+amount>self.limits[key]:
                self.data['budget_stop_reason']=key
                raise CompanyFlowLimit('company_flow_'+key+'_budget_exhausted')
            m[key]=m.get(key,0)+amount

    def model_response_key(self,role,prompt):
        from report_agents.company_task_batches import model_scope
        policy=self.model_policy(role) if callable(getattr(self,'model_policy',None)) else None
        return fingerprint(['model-response-v1',self.scope,model_scope(),policy,role,prompt])

    def model_response(self,role,prompt):
        with self.lock:
            self.data.setdefault('model_response_cache',{})
            cached=self.cached('model_response_cache',self.model_response_key(role,prompt))
            if cached is not None:
                self.data['metrics']['model_response_cache_hits']=self.data['metrics'].get('model_response_cache_hits',0)+1
            return cached

    def invoke(self, invoke, role, prompt, trace=None):
        # Exact transport receipts are not acceptance: callers always revalidate
        # them under current rules. Persist before a later stage can be paused.
        key=self.model_response_key(role,prompt)
        with self.lock:guard=self.query_locks.setdefault('model:'+key,threading.Lock())
        with guard:
            cached=self.model_response(role,prompt)
            if cached is not None:return cached['answer']
            return self._invoke_uncached(invoke,role,prompt,trace,key)

    def _invoke_uncached(self, invoke, role, prompt, trace, response_key):
        # Reserve before the request; failures still consume an actual attempt.
        if self.data.get('provider_retry_not_before',0)>time.time():
            from report_agents.company_transport import CompanyModelUnavailable
            raise CompanyModelUnavailable(diagnostic={'category':'protection_not_sent','blocked_by':'provider_retry_after',
                'attempts':0,'request_invoked':False,'request_sent':False,'retryable':False,
                'deferred_until':self.data['provider_retry_not_before'],'stage':role,'input_sha256':fingerprint([role,prompt])})
        with self.lock:
            if time.monotonic()-self.started>self.limits['elapsed_seconds']:
                self.data['budget_stop_reason']='elapsed_seconds'
                raise CompanyFlowLimit('company_flow_time_budget_exhausted')
            for key,amount in (('model_calls',1),('input_chars',len(prompt))):
                if self.data['metrics'].get(key,0)+amount>self.limits[key]:
                    self.data['budget_stop_reason']=key
                    raise CompanyFlowLimit('company_flow_'+key+'_budget_exhausted')
            cost_cap=float(os.getenv('COMPANY_FLOW_RUN_ESTIMATED_YUAN','0'))
            input_price=float(os.getenv('COMPANY_FLOW_INPUT_YUAN_PER_MILLION','0.8'))
            output_price=float(os.getenv('COMPANY_FLOW_OUTPUT_YUAN_PER_MILLION','2.8'))
            if any(not math.isfinite(v) or v<0 for v in (input_price,output_price)):
                raise ValueError('invalid_company_model_cost_rates')
            # Provider-specific uncached rates; reserve UTF-8 input bytes and
            # the full output allowance even for requests that later fail.
            cost=(len(prompt.encode('utf-8'))*input_price+int(os.getenv('COMPANY_RELAY_MAX_OUTPUT_TOKENS','12288'))*output_price)/1000000
            if cost_cap>0 and self.run_cost_reserved_yuan+cost>cost_cap:
                raise CompanyFlowLimit('company_flow_estimated_cost_budget_exhausted')
            self.run_cost_reserved_yuan+=cost
            self.data['metrics']['model_calls']+=1
            self.data['metrics']['input_chars']+=len(prompt)
        started=time.monotonic()
        error=None
        try:
            answer=invoke(role,prompt,trace)
            with self.lock:
                self.data['metrics']['output_chars']+=len(str(answer or ''))
                self.data.setdefault('model_response_cache',{})
                # Publish the answer, usage and model event in the same final
                # checkpoint; recovery must not see a charged call with no event.
                self.remember('model_response_cache',response_key,{'answer':answer},persist=False)
            return answer
        except Exception as exc:
            from report_agents.company_transport import failure_diagnostic
            error=failure_diagnostic(exc,stage=role,input_hash=fingerprint([role,prompt]),elapsed=time.monotonic()-started)
            if error.get('category') in {'rate_limit','concurrency_limit','timeout'} and error.get('retry_after_seconds'):
                self.data['provider_retry_not_before']=time.time()+error['retry_after_seconds']
            if error.get('request_invoked') is False:
                with self.lock:
                    self.data['metrics']['model_calls']-=1
                    self.data['metrics']['input_chars']-=len(prompt)
            raise
        finally:
            with self.lock:self.data['events'].append({'kind':'model','role':role,
                'input_chars':len(prompt),'input_sha256':fingerprint([role,prompt]),
                'seconds':round(time.monotonic()-started,3),'error':error,
                'request_invoked':error.get('request_invoked') if error else True,
                'request_sent':error.get('request_sent') if error else True})
            self.save()

    def usage(self, value):
        if not isinstance(value,dict) or not value:return
        with self.lock:
            self.data.setdefault('model_response_usage',[]).append(deepcopy(value))
            m=self.data['metrics'];m['reported_usage_calls']+=1
            m['input_tokens']+=int(value.get('input_tokens') or value.get('prompt_tokens') or 0)
            m['output_tokens']+=int(value.get('output_tokens') or value.get('completion_tokens') or 0)

    def query(self, request, execute):
        # Module labels do not change the provider request; target/scope and
        # all actual search parameters do. Only successful requests are cached.
        key=fingerprint({'scope':self.scope,'request':request})
        with self.lock:guard=self.query_locks.setdefault(key,threading.Lock())
        with guard:return self._query_locked(key,execute)

    def _query_locked(self,key,execute):
        with self.lock:
            cached=self.cached('query_cache',key)
            if cached is not None:
                self.data['metrics']['query_cache_hits']+=1
                return deepcopy(cached),True
        self.reserve('search_queries')
        try:result=execute()
        except Exception:
            with self.lock:
                metrics=self.data['metrics'];metrics['search_failures']=metrics.get('search_failures',0)+1
            raise
        # Provider failures must remain retryable, never become empty success.
        rows=list(result);audit=deepcopy(getattr(result,'audit',{}))
        failed=any(r.get('status') in {'error','failed','unavailable','not_configured'}
            for r in audit.get('provider_outcomes',[]) if isinstance(r,dict))
        states=list((audit.get('engine_states') or {}).values())
        if states:
            active=[s for s in states if s.get('status') not in {'not_selected','not_configured'}]
            failed=failed or any(s.get('status')!='returned' for s in active) or not active
        success=not failed and (bool(rows) or audit.get('status') in {'success','completed','empty'}
            or bool(states))
        if success:
            with self.lock:
                self.remember('query_cache',key,{'rows':deepcopy(rows),'audit':audit})
        else:
            with self.lock:
                metrics=self.data['metrics'];metrics['search_failures']=metrics.get('search_failures',0)+1
        return {'rows':rows,'audit':audit},False

    def platform(self,provider,tool,arguments,execute):
        from company_platform import failure_kind
        key=fingerprint([self.scope,provider,tool,arguments])
        with self.lock:
            cached=self.cached('platform_cache',key)
            if cached is not None:
                self.data['metrics']['platform_cache_hits']+=1
                return deepcopy(cached),True
        self.reserve('platform_requests')
        payload,session=execute()
        if not payload.get('error') and not failure_kind(payload):
            with self.lock:
                self.remember('platform_cache',key,deepcopy(payload))
        return (payload,session),False
