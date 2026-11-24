import json
import subprocess

import httpx
import pytest

from report_agents.company_relay_model import IsolatedCompanyRelayModel
from report_agents.company_relay_worker import request_once


def test_official_balance_code_is_retained_without_private_error_body(monkeypatch):
    from report_agents.company_relay_model import RelayHTTPError
    from report_agents.company_transport import failure_diagnostic,bounded_company_invoke,CompanyModelUnavailable
    monkeypatch.setenv('ARK_API_KEY','local-only')
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(429,json={
            'error':{'code':'1113','message':'secret error body'}}))) as client:
        result=request_once({'base_url':'https://open.bigmodel.cn/api/paas/v4','payload':{},'deadline_seconds':30},client)
    assert result['provider_error_code']=='1113' and 'secret error body' not in str(result)
    exc=RelayHTTPError(429);exc.company_provider_error_code=result['provider_error_code']
    diagnostic=failure_diagnostic(exc)
    assert diagnostic['category']=='balance' and diagnostic['retryable'] is False
    attempts=[]
    def invoke(*args):
        attempts.append(1);raise exc
    bounded=bounded_company_invoke(invoke,sleep=lambda _:pytest.fail('balance must not back off'))
    with pytest.raises(CompanyModelUnavailable):bounded('write','same evidence')
    assert len(attempts)==1


def test_explicit_relay_transport_options_leave_logical_identity_unchanged(monkeypatch):
    from research_workflow import ResearchWorkflow
    for key,value in {'ARK_API_KEY':'local-only','MODEL':'logical-model','BASE_URL':'http://logical/v1',
        'COMPANY_RELAY_ISOLATED_TRANSPORT_ENABLED':'true','COMPANY_RELAY_PREFERRED_MODEL':'kimi-k3',
        'COMPANY_RELAY_PREFERRED_BASE_URL':'http://physical/v1','COMPANY_RELAY_REASONING_EFFORT':''}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.delenv('COMPANY_REPORT_EDITOR_THINKING_BUDGET',raising=False)
    model=ResearchWorkflow.__new__(ResearchWorkflow)._get_company_report_editor_model()
    assert model.model=='logical-model' and model.provider_model=='kimi-k3'
    assert model.base_url=='http://physical/v1' and model.reasoning_effort==''
    import os
    assert os.environ['MODEL']=='logical-model' and os.environ['BASE_URL']=='http://logical/v1'


def run_worker(monkeypatch,items,deadline=180):
    monkeypatch.setenv('ARK_API_KEY','local-only-secret')
    data=''.join('data: '+json.dumps(i)+'\n\n' for i in items)+'data: [DONE]\n\n'
    client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,
        headers={'Content-Type':'text/event-stream'},content=data.encode())))
    try:return request_once({'base_url':'http://local.test/v1','payload':{},'deadline_seconds':deadline},client)
    finally:client.close()


def test_worker_requires_terminal_stop_and_keeps_usage_and_reasoning_counts(monkeypatch):
    rows=[{'choices':[{'delta':{'reasoning_content':'internal thought'}}]},
          {'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]},
          {'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}}]
    result=run_worker(monkeypatch,rows)
    assert result['ok'] and result['content']=='{"ok":true}'
    assert result['usage']['total_tokens']==20 and result['timing']['reasoning_chars']>0
    assert 'internal thought' not in json.dumps(result)


@pytest.mark.parametrize('finish,category',[(None,'output_incomplete'),('length','output_truncated')])
def test_valid_looking_partial_json_is_never_accepted(monkeypatch,finish,category):
    r=run_worker(monkeypatch,[{'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':finish}]}])
    assert not r['ok'] and r['category']==category and 'content' not in r


def test_http_200_upstream_error_does_not_turn_partial_content_into_success(monkeypatch):
    r=run_worker(monkeypatch,[{'choices':[{'delta':{'content':'{"ok":true}'}}]},
                              {'error':{'message':'Upstream response stream was interrupted'}}])
    assert not r['ok'] and r['category']=='connection' and 'content' not in r


def test_stream_events_cannot_extend_total_deadline(monkeypatch):
    r=run_worker(monkeypatch,[{'choices':[{'delta':{'content':'partial'}}]}],deadline=-1)
    assert r['category']=='timeout' and not r['ok']


def test_rate_limit_preserves_provider_retry_after(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY','local-only')
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'120'}))) as c:
        r=request_once({'base_url':'http://local/v1','payload':{},'deadline_seconds':30},c)
    assert not r['ok'] and r['http_status']==429 and r['retry_after_seconds']==120


def test_guardian_kills_and_reaps_only_its_worker_at_deadline(monkeypatch):
    import report_agents.company_relay_model as module
    calls=[]
    class Process:
        returncode=-9
        def communicate(self,*a,**kw):
            calls.append(('communicate',kw.get('timeout')))
            if len(calls)==1:raise subprocess.TimeoutExpired('worker',35)
            return '','private error must never be logged'
        def kill(self):calls.append(('kill',None))
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='local',base_url='http://local/v1',api_key='private',deadline_seconds=30)
    with pytest.raises(TimeoutError) as exc:model.invoke([{'role':'user','content':'native proof'}])
    assert calls==[('communicate',35),('kill',None),('communicate',5)]
    assert 'private' not in str(exc.value)


def test_parent_preserves_explicit_stop_and_normalizes_usage(monkeypatch):
    import report_agents.company_relay_model as module
    class Process:
        returncode=0
        def communicate(self,config,**kw):
            p=json.loads(config)['payload'];assert p['stream'] and p['max_tokens']==12288
            assert p['reasoning_effort']=='low' and 'api_key' not in config
            return json.dumps({'ok':True,'content':'complete','finish_reason':'stop',
                'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8},'timing':{'seconds':2}}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    r=IsolatedCompanyRelayModel(model='local',base_url='http://local/v1',api_key='private').invoke([])
    assert r.response_metadata['finish_reason']=='stop' and r.usage_metadata['total_tokens']==8


def test_disabled_thinking_and_json_mode_are_explicit_wire_parameters(monkeypatch):
    import report_agents.company_relay_model as module
    class Process:
        returncode=0
        def communicate(self,config,**kw):
            p=json.loads(config)['payload']
            assert p['thinking']=={'type':'disabled'} and 'reasoning_effort' not in p
            assert p['response_format']=={'type':'json_object'}
            return json.dumps({'ok':True,'content':'{}','finish_reason':'stop','usage':{},'timing':{}}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='local',base_url='http://local/v1',api_key='private',
        thinking_type='disabled',reasoning_effort='',json_mode=True)
    assert model.invoke([]).content=='{}'
    with pytest.raises(ValueError):
        IsolatedCompanyRelayModel(model='local',base_url='http://local/v1',api_key='private',thinking_type='disabled')


def test_dashscope_uses_explicit_non_thinking_parameter(monkeypatch):
    import report_agents.company_relay_model as module
    class Process:
        returncode=0
        def communicate(self,config,**kw):
            payload=json.loads(config)['payload']
            assert payload['enable_thinking'] is False and 'thinking' not in payload
            assert 'reasoning_effort' not in payload
            return json.dumps({'ok':True,'content':'{}','finish_reason':'stop','usage':{},'timing':{}}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='qwen-plus',base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
        api_key='private',reasoning_effort='',thinking_type='disabled')
    assert model.invoke([]).content=='{}'


def test_json_mode_adds_format_instruction_without_mutating_reading_messages(monkeypatch):
    import report_agents.company_relay_model as module
    original=[{'role':'user','content':'Read the original windows and return window_reviews.'}]
    class Process:
        returncode=0
        def communicate(self,config,**kw):
            payload=json.loads(config)['payload']
            assert payload['messages']==[{'role':'system','content':'Return a valid JSON object.'},*original]
            assert payload['response_format']=={'type':'json_object'}
            return json.dumps({'ok':True,'content':'{}','finish_reason':'stop','usage':{},'timing':{}}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='qwen-plus',base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
        api_key='private',reasoning_effort='',json_mode=True)
    assert model.invoke(original).content=='{}'
    assert len(original)==1


def test_explicit_backup_is_one_physical_request_and_records_both_models(monkeypatch):
    import report_agents.company_relay_model as module
    calls=[]
    class Process:
        returncode=0
        def communicate(self,config,**kw):
            calls.append(json.loads(config)['payload']['model'])
            return json.dumps({'ok':True,'content':'complete','finish_reason':'stop','usage':{},'timing':{}}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='logical-primary',provider_model='tested-backup',
        base_url='http://local/v1',api_key='private')
    r=model.invoke([])
    assert calls==['tested-backup']
    assert r.usage_metadata['provider_model']=='tested-backup'
    assert r.usage_metadata['requested_model']=='logical-primary'


def test_progress_watchdog_stops_stalled_generation_and_reports_client_cooldown(monkeypatch):
    from pathlib import Path
    import report_agents.company_relay_model as module
    from report_agents.company_transport import failure_diagnostic
    now=[0.0];killed=[];paths=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:now[0])
    class Process:
        returncode=-9
        def communicate(self,config=None,timeout=None):
            if timeout==5:return '',''
            now[0]+=.6
            if config:
                path=Path(json.loads(config)['progress_path']);paths.append(path)
                path.write_text(json.dumps({'headers_seconds':.1,'content_chars':0,'reasoning_chars':25}),encoding='utf-8')
            raise subprocess.TimeoutExpired('local',timeout)
        def kill(self):killed.append(True)
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**kw:Process())
    model=IsolatedCompanyRelayModel(model='local',base_url='http://local',api_key='private',progress_watchdog=True,
        idle_seconds=1,deadline_seconds=10)
    with pytest.raises(TimeoutError) as caught:model.invoke([])
    diagnostic=failure_diagnostic(caught.value)
    assert killed and diagnostic['usage']['relay_timing']['timeout_phase']=='idle_generation'
    assert diagnostic['retry_after_seconds']==30 and diagnostic['retry_after_source']=='client_watchdog'
    assert paths and not paths[0].exists()


def test_worker_progress_contains_only_timing_and_counters(tmp_path,monkeypatch):
    monkeypatch.setenv('ARK_API_KEY','local-only-secret')
    rows=[{'choices':[{'delta':{'reasoning_content':'private reasoning'}}]},
          {'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]}]
    body=''.join('data: '+json.dumps(row)+'\n\n' for row in rows)+'data: [DONE]\n\n'
    progress=tmp_path/'progress.json'
    with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body.encode()))) as client:
        result=request_once({'base_url':'http://local','payload':{},'deadline_seconds':30,'progress_path':str(progress)},client)
    text=progress.read_text(encoding='utf-8');data=json.loads(text)
    assert result['ok'] and data['content_chars']==11 and data['reasoning_chars']>0
    assert 'private reasoning' not in text and 'local-only-secret' not in text and '"ok"' not in text


def test_editor_factory_selects_guardian_without_changing_other_profiles(monkeypatch):
    from research_workflow import ResearchWorkflow
    monkeypatch.setenv('ARK_API_KEY','local-only');monkeypatch.setenv('BASE_URL','http://local/v1')
    monkeypatch.setenv('MODEL','local');monkeypatch.setenv('COMPANY_RELAY_ISOLATED_TRANSPORT_ENABLED','true')
    w=ResearchWorkflow.__new__(ResearchWorkflow)
    model=w._get_company_report_editor_model()
    assert isinstance(model,IsolatedCompanyRelayModel) and model.max_tokens<=12288


def test_real_worker_is_reaped_when_server_never_returns_headers(monkeypatch):
    import threading,time
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import report_agents.company_relay_model as module
    accepted=threading.Event();processes=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length',0)));accepted.set();time.sleep(15)
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    popen=module.subprocess.Popen
    def tracked(*a,**kw):
        process=popen(*a,**kw);processes.append(process);return process
    monkeypatch.setattr(module.subprocess,'Popen',tracked)
    started=time.monotonic()
    try:
        model=IsolatedCompanyRelayModel(model='local',base_url=f'http://127.0.0.1:{server.server_port}/v1',
            api_key='local-test-only',deadline_seconds=1)
        with pytest.raises(TimeoutError):model.invoke([{'role':'user','content':'local only'}])
        assert accepted.is_set() and time.monotonic()-started<10
        assert processes and processes[0].poll() is not None
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_relay_concurrency_error_requests_cooldown_without_assuming_remote_cancel(monkeypatch):
    import report_agents.company_relay_model as module
    from report_agents.company_transport import failure_diagnostic
    class Process:
        returncode=0
        def communicate(self,*a,**k):
            return json.dumps({'ok':False,'category':'concurrency_limit','response_received':True}),''
    monkeypatch.setattr(module.subprocess,'Popen',lambda *a,**k:Process())
    model=IsolatedCompanyRelayModel(model='local',base_url='http://local',api_key='private',deadline_seconds=180)
    with pytest.raises(RuntimeError) as caught:model.invoke([])
    assert failure_diagnostic(caught.value)['retry_after_seconds']==180


def test_official_provider_key_does_not_replace_existing_relay_credential(monkeypatch):
    import os
    from research_workflow import ResearchWorkflow
    values={'ARK_API_KEY':'existing-relay-test-key','COMPANY_RELAY_API_KEY':'official-test-key',
        'MODEL':'logical-model','BASE_URL':'http://logical/v1','COMPANY_RELAY_ISOLATED_TRANSPORT_ENABLED':'true',
        'COMPANY_RELAY_PREFERRED_MODEL':'glm-4.7-flash',
        'COMPANY_RELAY_PREFERRED_BASE_URL':'https://open.bigmodel.cn/api/paas/v4'}
    for key,value in values.items():monkeypatch.setenv(key,value)
    model=ResearchWorkflow.__new__(ResearchWorkflow)._get_company_report_editor_model()
    assert model.api_key=='official-test-key'
    assert model.base_url=='https://open.bigmodel.cn/api/paas/v4'
    assert model.provider_model=='glm-4.7-flash'
    assert os.environ['ARK_API_KEY']=='existing-relay-test-key'
