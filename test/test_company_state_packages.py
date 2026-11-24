import json
from pathlib import Path
import pytest
from company_flow_state import FlowJournal
from research_store import ResearchObjectStore


def journal(tmp_path):
    store=ResearchObjectStore();store.mode='local';store.local_root=tmp_path/'objects'
    return FlowJournal(store,1,{'target':'完整资料测试'})


def test_cached_resume_at_call_ceiling_clears_old_time_stop_without_granting_calls(tmp_path,monkeypatch):
    from company_flow_state import CompanyFlowLimit
    monkeypatch.setenv('COMPANY_FLOW_MAX_MODEL_CALLS','1')
    j=journal(tmp_path);j.data['metrics']['model_calls']=1;j.data['budget_stop_reason']='elapsed_seconds'
    j.remember('query_cache','native',{'text':'已经取得的原文。'})
    restored=journal(tmp_path);assert restored.restore()
    assert not restored.data.get('budget_stop_reason')
    assert restored.cached('query_cache','native')['text']=='已经取得的原文。'
    calls=[]
    with pytest.raises(CompanyFlowLimit):restored.invoke(lambda *a:calls.append(True),'write','native')
    assert not calls and restored.data['metrics']['model_calls']==1


def test_resume_with_usage_above_new_limit_retains_stop_marker(tmp_path,monkeypatch):
    monkeypatch.setenv('COMPANY_FLOW_MAX_MODEL_CALLS','1')
    j=journal(tmp_path);j.data['metrics']['model_calls']=2;j.data['budget_stop_reason']='model_calls';j.save()
    restored=journal(tmp_path);assert restored.restore()
    assert restored.data['budget_stop_reason']=='model_calls'


def test_large_packages_round_trip_without_repeating_in_the_checkpoint(tmp_path):
    j=journal(tmp_path)
    original={'2':{'facts':[{'fact':'原文事实','context_quote':'完整相邻原文。'*30000}],
        'review_receipts':[{'input_sha256':'actual-original','response':{'facts':['完整回执']}}]}}
    for name in ('reads','packages','usages','editorials'):
        j.data[name]=j.pack_state(name,original)
    j.save()
    pointer=json.loads(j.store.get_text(j.prefix+'/current.json'))
    checkpoint=j.store.get_text(pointer['key'])
    assert len(checkpoint)<4000 and '完整相邻原文' not in checkpoint
    restored=journal(tmp_path);assert restored.restore()
    assert all(restored.data[name]==original for name in ('reads','packages','usages','editorials'))


def test_restoring_rejects_changed_package_bytes(tmp_path):
    j=journal(tmp_path);j.data['reads']=j.pack_state('reads',{'2':{'facts':['原文']}});j.save()
    path=j.store._local_path(j.data['reads']['key']);path.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='state_object_hash_mismatch'):journal(tmp_path).restore()


def test_repeated_context_is_shared_and_marker_like_data_survives(tmp_path):
    j=journal(tmp_path);context='完整相邻表头与原文。'*1000
    original={'facts':[{'fact':str(i),'context_quote':context} for i in range(40)],
        'literal':{'_state_text_v1':True,'key':'普通数据','sha256':'普通数据'}}
    j.data['reads']=j.pack_state('reads',original);j.save()
    assert len(list(j.store._local_path(j.prefix+'/state/text').glob('*.txt')))==1
    payload=j.store.get_text(j.data['reads']['key'])
    assert len(payload)<20000
    restored=journal(tmp_path);assert restored.restore()
    assert restored.data['reads']==original
    text=next(j.store._local_path(j.prefix+'/state/text').glob('*.txt'));text.write_text('改动',encoding='utf-8')
    with pytest.raises(ValueError,match='state_text_hash_mismatch'):journal(tmp_path).restore()


def test_legacy_inline_packages_still_restore_and_deferred_candidates_commit_together(tmp_path):
    j=journal(tmp_path);j.data['reads']={'2':{'facts':['旧格式原文']}};j.data['candidate_originals']={}
    j.remember('candidate_originals','first',{'native':'完整正文1'},persist=False)
    j.remember('candidate_originals','second',{'native':'完整正文2'},persist=False)
    assert not j.store._local_path(j.prefix+'/current.json').exists()
    j.save();restored=journal(tmp_path);assert restored.restore()
    assert restored.data['reads']==j.data['reads']
    assert restored.cached('candidate_originals','first')=={'native':'完整正文1'}
    assert restored.cached('candidate_originals','second')=={'native':'完整正文2'}


@pytest.mark.parametrize('failure',[TimeoutError('request_deadline'),RuntimeError('concurrency limit')])
def test_relay_cooldown_is_shared_across_different_batches_and_resume(tmp_path,monkeypatch,failure):
    from report_agents.company_transport import CompanyModelUnavailable
    import company_flow_state as module
    monkeypatch.setattr(module.time,'time',lambda:1000.0)
    failure.company_retry_after_seconds=180
    j=journal(tmp_path);calls=[]
    def invoke(*a):calls.append(1);raise failure
    with pytest.raises(type(failure)):j.invoke(invoke,'first batch','native evidence')
    assert j.data['provider_retry_not_before']==1180
    restored=journal(tmp_path);assert restored.restore()
    with pytest.raises(CompanyModelUnavailable) as caught:
        restored.invoke(invoke,'different batch','other native evidence')
    assert caught.value.diagnostic['request_invoked'] is False
    assert calls==[1] and restored.data['metrics']['model_calls']==1
