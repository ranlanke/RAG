"""OpenAI-compatible relay with a killable per-request wall-clock deadline."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import site
import tempfile
from urllib.parse import urlsplit
from types import SimpleNamespace


class RelayHTTPError(RuntimeError):
    def __init__(self,status):
        super().__init__('relay_http_error');self.status_code=status


class IsolatedCompanyRelayModel:
    _company_isolated_relay=True
    streaming=True

    def __init__(self,*,model,base_url,api_key,max_tokens=12288,reasoning_effort='low',deadline_seconds=180,thinking_budget=0,provider_model=None,thinking_type=None,json_mode=False,compact_json=False,progress_watchdog=False,header_seconds=60,first_token_seconds=90,idle_seconds=20):
        self.model=model;self.base_url=base_url;self.api_key=api_key
        self.provider_model=provider_model or model
        self.max_tokens=max_tokens;self.reasoning_effort=reasoning_effort;self.deadline_seconds=deadline_seconds
        self.thinking_budget=thinking_budget
        self.thinking_type=thinking_type;self.json_mode=json_mode;self.compact_json=compact_json
        self.progress_watchdog=progress_watchdog;self.header_seconds=header_seconds
        self.first_token_seconds=first_token_seconds;self.idle_seconds=idle_seconds
        if min(header_seconds,first_token_seconds,idle_seconds)<=0:raise ValueError('invalid_relay_progress_budget')
        if thinking_budget and reasoning_effort:raise ValueError('conflicting_company_reasoning_controls')
        if thinking_type not in (None,'enabled','disabled'):raise ValueError('invalid_company_thinking_type')
        if thinking_type and (reasoning_effort or thinking_budget):raise ValueError('conflicting_company_thinking_controls')

    def invoke(self,messages):
        # JSON-object providers require an explicit JSON instruction even for
        # reading prompts that describe fields without naming the format.
        if self.json_mode and not any('json' in str(m.get('content','')).lower() for m in messages):
            messages=[{'role':'system','content':'Return a valid JSON object.'},*messages]
        packing=[]
        if self.compact_json:
            from .company_relay_prompt import compact_prompt
            packed=[]
            for message in messages:
                if isinstance(message.get('content'),str):
                    content,stats=compact_prompt(message['content'])
                    stats.update(original_sha256=hashlib.sha256(message['content'].encode()).hexdigest(),
                        wire_sha256=hashlib.sha256(content.encode()).hexdigest())
                    packed.append({**message,'content':content});packing.append(stats)
                else:packed.append(message)
            messages=packed
        payload={'model':self.provider_model,'messages':messages,'temperature':0.1,'max_tokens':self.max_tokens,
                 'stream':True,'stream_options':{'include_usage':True}}
        if self.reasoning_effort:payload['reasoning_effort']=self.reasoning_effort
        if self.thinking_budget:payload.update(thinking_budget=self.thinking_budget,enable_thinking=True)
        if self.thinking_type:
            host=urlsplit(self.base_url).hostname or ''
            if host.startswith('dashscope') and host.endswith('.aliyuncs.com'):
                payload['enable_thinking']=self.thinking_type=='enabled'
            else:payload['thinking']={'type':self.thinking_type}
        if self.json_mode:payload['response_format']={'type':'json_object'}
        config={'base_url':self.base_url,'payload':payload,'deadline_seconds':self.deadline_seconds}
        progress_path=None
        if self.progress_watchdog:
            fd,progress_path=tempfile.mkstemp(prefix='company-relay-progress-',suffix='.json');os.close(fd)
            config['progress_path']=progress_path
        env=dict(os.environ,ARK_API_KEY=self.api_key)
        # Windows venv python.exe is a launcher with a child process. Kill the
        # real interpreter directly so a deadline cannot leave an orphan call.
        executable=getattr(sys,'_base_executable',sys.executable) if os.name=='nt' else sys.executable
        if os.name=='nt':env['PYTHONPATH']=os.pathsep.join(site.getsitepackages())
        worker=Path(__file__).with_name('company_relay_worker.py')
        started=time.monotonic()
        try:
            process=subprocess.Popen([executable,str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,text=True,encoding='utf-8',env=env,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
            pending_input=json.dumps(config,ensure_ascii=False);progress={};last_count=0;last_advance=None;phase='total'
            try:
                while True:
                    try:
                        output,_=process.communicate(pending_input,timeout=.5 if progress_path else self.deadline_seconds+5)
                        break
                    except subprocess.TimeoutExpired:
                        pending_input=None
                        if not progress_path:raise
                        try:progress=json.loads(Path(progress_path).read_text(encoding='utf-8'))
                        except (OSError,ValueError):pass
                        now=time.monotonic();elapsed=now-started
                        count=progress.get('reasoning_chars',0)+progress.get('content_chars',0)
                        if count>last_count:last_count=count;last_advance=now
                        if elapsed>self.deadline_seconds+5:phase='total'
                        elif 'headers_seconds' not in progress and elapsed>self.header_seconds+5:phase='headers'
                        elif not count and elapsed>self.first_token_seconds+5:phase='first_token'
                        elif last_advance is not None and now-last_advance>self.idle_seconds:phase='idle_generation'
                        else:continue
                        raise
            except subprocess.TimeoutExpired:
                process.kill();process.communicate(timeout=5)
                exc=TimeoutError('company_relay_'+phase+'_request_deadline')
                exc.company_response_received='headers_seconds' in progress
                exc.company_retry_after_seconds=max(30,self.deadline_seconds-(time.monotonic()-started))
                exc.company_usage={'provider_model':self.provider_model,'requested_model':self.model,'prompt_packing':packing,
                    'relay_timing':{**progress,'seconds':round(time.monotonic()-started,3),'killed_at_deadline':True,'timeout_phase':phase}}
                raise exc
        finally:
            if progress_path:
                for filename in (progress_path,progress_path+'.tmp'):
                    try:Path(filename).unlink(missing_ok=True)
                    except OSError:pass
        if process.returncode:raise ConnectionError('company_relay_worker_failed')
        try:result=json.loads(output)
        except (ValueError,TypeError):raise ConnectionError('company_relay_worker_invalid_response') from None
        usage=result.get('usage') or {};timing=result.get('timing') or {}
        canonical={'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),
            'total_tokens':usage.get('total_tokens',0),'output_token_details':{
                'reasoning':(usage.get('completion_tokens_details') or {}).get('reasoning_tokens',0)},
            'relay_timing':timing,'provider_model':self.provider_model,'requested_model':self.model,
            'prompt_packing':packing}
        if not result.get('ok'):
            category=result.get('category')
            if category=='timeout':exc=TimeoutError('company_relay_total_or_read_timeout')
            elif category=='http_error':
                exc=RelayHTTPError(result.get('http_status'))
                exc.company_provider_error_code=result.get('provider_error_code')
            elif category=='concurrency_limit':
                exc=RuntimeError('concurrency limit')
                exc.company_retry_after_seconds=self.deadline_seconds
            elif category=='output_truncated':exc=ValueError('company_editor_response_truncated')
            elif category=='output_incomplete':exc=ValueError('company_editor_response_incomplete')
            else:exc=ConnectionError('company_relay_connection_interrupted')
            if result.get('retry_after_seconds') is not None:
                exc.response=SimpleNamespace(headers={'retry-after':str(result['retry_after_seconds'])})
            exc.company_response_received=bool(result.get('response_received'));exc.company_usage=canonical
            raise exc
        return SimpleNamespace(content=result['content'],usage_metadata=canonical,
            response_metadata={'finish_reason':result['finish_reason'],'relay_timing':timing})
