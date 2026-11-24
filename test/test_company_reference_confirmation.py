import json
import hashlib
from types import SimpleNamespace
import pytest

from company_candidate_planning import resolve_references
from report_agents.company_evidence import source_body
from test_company_candidate_planning import flow
from test_company_chapter_flow import source


def test_model_confirms_existing_source_and_replays_without_guessing_or_calls(tmp_path):
    calls=[]
    def invoke(role,prompt,*a):
        calls.append(role);data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert data['references'][0]['entry']['candidate_id']=='task_original_9'
        # The literal number is deliberately different from the real source.
        assert data['task_originals'][0]['source_id']=='17'
        return json.dumps({'mappings':[{'reference_id':'R1','target_kind':'existing','source_id':'17','reason':'标题与原选取理由相符'}]})
    f=flow(tmp_path,invoke);f.project.sources=[source(17)]
    refs=[{'entry':{'candidate_id':'task_original_9','reason':'需补读历史沿革'}}]
    result=resolve_references(f,3,refs)
    assert result['mappings'][0]['source_id']=='17'
    request=next(iter(f.journal.data['confirmed_original_requests'].values()))
    assert request['original_reference']['entry']['reason']=='需补读历史沿革'
    f.reads[3]={'source_review':{'17':{'reviewed_windows':3}}}
    assert resolve_references(f,3,refs)==result and len(calls)==1
    f.project.sources[0].content+='新原文'
    resolve_references(f,3,refs)
    assert len(calls)==2


def test_ambiguous_or_invalid_confirmation_remains_recorded_without_blocking(tmp_path):
    calls=[]
    def invoke(*a):
        calls.append(1)
        return json.dumps({'mappings':[{'reference_id':'R1','target_kind':'existing','source_id':'999','reason':'不能绑定'}]})
    f=flow(tmp_path,invoke);f.project.sources=[source(1)]
    refs=[{'entry':{'candidate_id':'unclear','reason':'可能有用'}}]
    result=resolve_references(f,3,refs)
    assert result['unresolved'] and not result['mappings']
    assert not f.journal.data.get('confirmed_original_requests')
    assert resolve_references(f,3,refs)==result and len(calls)==1


def test_confirmed_existing_source_reuses_prior_native_read_receipts(tmp_path,monkeypatch):
    from test_company_progressive_chapter import progressive_flow
    f,calls,_=progressive_flow(tmp_path,monkeypatch,[source()]);f.read(6)
    original=source_body(f.project.sources[0])[0]
    before=len(f.journal.data.get('shared_read_receipts',{}))
    f.journal.data['confirmed_original_requests']={'test':{'chapter':6,'source_id':'1',
        'original_sha256':hashlib.sha256(original.encode()).hexdigest(),'status':'confirmed','reason':'复用已核财务原文'}}
    f.read(6,expanded=True)
    request=f.journal.data['confirmed_original_requests']['test']
    assert request['status']=='read_ranges' and request['reading_receipt']['reviewed_windows']>0
    assert len(f.journal.data['shared_read_receipts'])==before


@pytest.mark.parametrize('fail',[False,True])
def test_reference_only_read_never_replaces_existing_narrative(fail):
    from scripts.manual.company_bounded_chapter_run import read_confirmed_originals
    f=SimpleNamespace(sections={3:'已验收正文'},checkpoint=lambda *a:None)
    def read(number,**kwargs):
        assert kwargs=={'expanded':True,'assess':False}
        f.sections[number]='临时证据标题'
        if fail:raise RuntimeError('interrupted')
    f.read=read
    if fail:
        with pytest.raises(RuntimeError):read_confirmed_originals(f,3)
    else:read_confirmed_originals(f,3)
    assert f.sections[3]=='已验收正文'
