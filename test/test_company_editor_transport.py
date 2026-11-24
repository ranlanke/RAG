"""Exercise the actual SDK's stream assembly and terminal metadata locally."""
import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

import research_workflow


def test_model_initialization_failure_is_pre_request_configuration_without_secret_echo(monkeypatch):
    from report_agents.company_transport import CompanyModelUnavailable
    workflow=research_workflow.ResearchWorkflow.__new__(research_workflow.ResearchWorkflow)
    def invalid(*a,**kw):raise ValueError('private credential and complete settings must not be logged')
    monkeypatch.setattr(workflow,'_get_company_report_editor_model',invalid)
    with pytest.raises(CompanyModelUnavailable) as caught:workflow._invoke_company_report_editor('reading','original')
    d=caught.value.diagnostic
    assert d['category']=='configuration' and d['attempts']==0 and d['request_sent'] is False
    assert not d['request_invoked'] and 'private' not in json.dumps(d)


def workflow_with_transport(monkeypatch, *, finish='stop', broken=False, streaming=True, concurrency_error=False, stream_error=None):
    requests = []

    class Body(httpx.SyncByteStream):
        def __init__(self, error=None):
            self.error=error

        def __iter__(self):
            for content in ('{"status":', '"通过"}'):
                item = {'id': 'local-stream', 'object': 'chat.completion.chunk',
                        'created': 0, 'model': 'local-model', 'choices': [
                            {'index': 0, 'delta': {'content': content}, 'finish_reason': None}]}
                yield ('data: ' + json.dumps(item, ensure_ascii=False) + '\n\n').encode()
            if self.error:
                item={'error':{'message':self.error,'type':'server_error','code':'stream_interrupted'}}
                yield ('data: '+json.dumps(item)+'\n\n').encode()
                return
            if broken:
                raise httpx.ReadError('connection interrupted after valid-looking JSON')
            if finish:
                item = {'id': 'local-stream', 'object': 'chat.completion.chunk',
                        'created': 0, 'model': 'local-model', 'choices': [
                            {'index': 0, 'delta': {}, 'finish_reason': finish}]}
                yield ('data: ' + json.dumps(item) + '\n\n').encode()
            yield b'data: [DONE]\n\n'

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if concurrency_error and len(requests)==1:
            error={'error':{'message':'Concurrency limit exceeded for account, please retry later',
                'type':'concurrency_limit','code':'concurrency_limit'}}
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                content=('data: '+json.dumps(error)+'\n\n').encode())
        if not streaming:
            return httpx.Response(200, json={'id': 'local-response',
                'object': 'chat.completion', 'created': 0, 'model': 'local-model',
                'choices': [{'index': 0, 'message': {'role': 'assistant',
                    'content': '{"status":"通过"}'}, 'finish_reason': finish}]})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            stream=Body(stream_error if len(requests)==1 else None))

    monkeypatch.setenv('ARK_API_KEY', 'local-test-only')
    monkeypatch.setenv('MODEL', 'local-model')
    monkeypatch.setenv('BASE_URL', 'http://localhost/v1')
    monkeypatch.setenv('COMPANY_REPORT_EDITOR_STREAMING_ENABLED', str(streaming).lower())
    client = httpx.Client(transport=httpx.MockTransport(handler))

    def factory(**kwargs):
        kwargs.pop('model_provider')
        return ChatOpenAI(**kwargs, http_client=client)

    monkeypatch.setattr(research_workflow, 'init_chat_model', factory)
    workflow = research_workflow.ResearchWorkflow.__new__(research_workflow.ResearchWorkflow)
    workflow._company_report_editor_model = None
    return workflow, requests, client


def test_complete_stream_is_assembled_before_any_editorial_acceptance(monkeypatch):
    workflow, requests, client = workflow_with_transport(monkeypatch)
    try:
        assert workflow._invoke_company_report_editor('公司交付正文独立审稿', '核对全部原文') == '{"status":"通过"}'
        assert len(requests) == 1 and requests[0]['stream'] is True
        assert requests[0]['stream_options']['include_usage'] is True
    finally:
        client.close()


def test_sdk_concurrency_error_without_http_status_is_retried_with_the_complete_input(monkeypatch):
    from report_agents.company_transport import bounded_company_invoke,failure_diagnostic,invocation_attempts
    workflow,requests,client=workflow_with_transport(monkeypatch,concurrency_error=True)
    failures=[];delays=[];prompt='核对完整原文与全部限定，不裁剪证据。'
    def invoke(role,text,trace=None):
        try:return workflow._invoke_company_report_editor(role,text)
        except Exception as exc:
            failures.append(failure_diagnostic(exc,stage=role));raise
    guarded=bounded_company_invoke(invoke,sleep=delays.append)
    try:
        assert guarded('公司原文结构上下文补读',prompt)=='{"status":"通过"}'
        assert len(requests)==2 and invocation_attempts(guarded)==2 and delays==[30]
        assert failures[0]['category']=='concurrency_limit' and failures[0]['retryable'] is True
        assert failures[0]['http_status'] is None
        assert all(r['messages'][1]['content']==prompt for r in requests)
    finally:client.close()


def test_sdk_interrupted_stream_discards_valid_looking_partial_json_and_retries_complete_input(monkeypatch):
    from report_agents.company_transport import bounded_company_invoke,failure_diagnostic,invocation_attempts
    workflow,requests,client=workflow_with_transport(monkeypatch,
        stream_error='Upstream response stream was interrupted')
    failures=[];delays=[];prompt='保留完整原文、所有限定和问题，失败后仍发送同一输入。'
    def invoke(role,text,trace=None):
        try:return workflow._invoke_company_report_editor(role,text)
        except Exception as exc:
            failures.append(failure_diagnostic(exc,stage=role));raise
    guarded=bounded_company_invoke(invoke,sleep=delays.append)
    try:
        assert guarded('公司全量原文分析 Agent',prompt)=='{"status":"通过"}'
        assert len(requests)==2 and invocation_attempts(guarded)==2 and delays==[1]
        assert len(failures)==1 and failures[0]['category']=='connection' and failures[0]['retryable'] is True
        assert failures[0]['http_status'] is None
        assert requests[0]['messages']==requests[1]['messages']
        assert requests[1]['messages'][1]['content']==prompt
    finally:client.close()


@pytest.mark.parametrize('finish,broken', [(None, False), ('length', False), ('content_filter', False), ('stop', True)])
def test_partial_or_nonterminal_stream_cannot_pass_even_if_json_is_valid(monkeypatch, finish, broken):
    workflow, requests, client = workflow_with_transport(monkeypatch, finish=finish, broken=broken)
    try:
        events = []
        expected=httpx.ReadError if broken else ValueError
        with pytest.raises(expected):workflow._invoke_company_report_editor('公司交付正文独立审稿', '核对全部原文', events.append)
        assert requests and events[-1]['status'] == 'fallback'
    finally:
        client.close()


def test_nonstreaming_compatibility_still_requires_complete_response(monkeypatch):
    workflow, requests, client = workflow_with_transport(monkeypatch, streaming=False)
    try:
        assert workflow._invoke_company_report_editor('公司交付正文独立审稿', '核对全部原文') == '{"status":"通过"}'
        assert requests[0]['stream'] is False
    finally:
        client.close()


def test_focus_profile_budget_reaches_the_actual_sdk_wire_without_changing_the_editor(monkeypatch):
    workflow, requests, client = workflow_with_transport(monkeypatch)
    monkeypatch.setenv('COMPANY_MAIN_FOCUS_MAX_COMPLETION_TOKENS','32768')
    monkeypatch.setenv('COMPANY_MAIN_FOCUS_REASONING_EFFORT','low')
    try:
        assert workflow._invoke_company_report_editor('公司主报告事实选择','核对全部原文')=='{"status":"通过"}'
        assert requests[0]['max_completion_tokens']==32768
        assert requests[0]['reasoning_effort']=='low'
        assert requests[0]['stream'] is True
        assert workflow._get_company_report_editor_model() is not workflow._get_company_report_editor_model(profile='main_focus')
    finally:client.close()


def test_focus_thinking_budget_uses_compatible_extra_body_and_keeps_thinking_enabled(monkeypatch):
    workflow, requests, client = workflow_with_transport(monkeypatch)
    monkeypatch.setenv('COMPANY_MAIN_FOCUS_THINKING_BUDGET','8192')
    monkeypatch.delenv('COMPANY_MAIN_FOCUS_REASONING_EFFORT',raising=False)
    try:
        assert workflow._invoke_company_report_editor('公司主报告事实选择','核对全部原文')=='{"status":"通过"}'
        assert requests[0]['thinking_budget']==8192 and requests[0]['enable_thinking'] is True
        assert 'reasoning_effort' not in requests[0]
    finally:client.close()


def test_nonstreaming_compatibility_does_not_accept_missing_finish_reason(monkeypatch):
    workflow, requests, client = workflow_with_transport(monkeypatch, streaming=False, finish=None)
    try:
        with pytest.raises(ValueError,match='response_incomplete'):
            workflow._invoke_company_report_editor('公司交付正文独立审稿', '核对全部原文')
    finally:
        client.close()


@pytest.mark.parametrize('finish,broken,expected', [
    ('stop', False, '{"status":"通过"}'), (None, False, ''),
    ('length', False, ''), ('stop', True, ''),
])
def test_company_cleaning_streams_using_normal_model_and_rejects_incomplete_json(monkeypatch, finish, broken, expected):
    workflow, requests, client = workflow_with_transport(monkeypatch, finish=finish, broken=broken)
    model = workflow._get_company_report_editor_model()
    model.streaming = False  # normal workflow model does not default to streaming
    monkeypatch.setattr(workflow, '_get_model', lambda: model)
    monkeypatch.delenv('COMPANY_CLEANING_STREAMING_ENABLED', raising=False)
    try:
        assert workflow._invoke('公司数据清洗 Agent', '完整保留原文事实') == expected
        assert requests[0]['stream'] is True
    finally:
        client.close()


@pytest.mark.parametrize('role', ['公司数据清洗 Agent', '数据清洗 Agent'])
def test_cleaning_nonstream_compatibility_and_other_tasks_keep_normal_route(monkeypatch, role):
    workflow, requests, client = workflow_with_transport(monkeypatch, streaming=False)
    model = workflow._get_company_report_editor_model()
    monkeypatch.setattr(workflow, '_get_model', lambda: model)
    monkeypatch.setenv('COMPANY_CLEANING_STREAMING_ENABLED', 'false')
    try:
        assert workflow._invoke(role, '完整保留原文事实') == '{"status":"通过"}'
        assert requests[0]['stream'] is False
    finally:
        client.close()


@pytest.mark.parametrize('finish,expected',[('stop','{"status":"通过"}'),(None,''),('length','')])
def test_company_original_extraction_uses_same_complete_stream_rule_without_changing_other_tasks(monkeypatch,finish,expected):
    workflow,requests,client=workflow_with_transport(monkeypatch,finish=finish)
    model=workflow._get_company_report_editor_model()
    monkeypatch.setattr(workflow,'_get_model',lambda:model)
    try:
        assert workflow._invoke('公司监管原件提取','核对原文',stream=True)==expected
        assert requests[0]['stream'] is True
    finally:
        client.close()
