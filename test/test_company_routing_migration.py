import hashlib
import json
from copy import deepcopy
import pytest

from company_chapters import outline
from report_agents import company_information_usage as m
from test_company_information_usage import statement

OLD='7a29576dcc8e01b433a3e601693603f6064c3eb40de4e50ea412699084eb0de2'
NEW='61ab98860ccf49f557a94ebba5f9b2310f191f8eb178ea589fc38186c62d18af'

def fixture(target='样本公司',number=4,status='use'):
    s=statement();s['chapter']=number;s['context'].update(entity=target,period='2025',source_reading_coverage={'read':2})
    old=deepcopy(m.information_records([s])[0]);old['information_id']='I-legacy-identity'
    old['decision']=({'information_id':old['information_id'],'status':'use','chapter':number,
        'aspect':outline()[number-1][1][0],'relevance':'本条原文支持当前章节的具体观察。','voice':'按原披露主体陈述'}
        if status=='use' else {'information_id':old['information_id'],'status':'defer','reason':'原文内容与当前章节的问题没有直接关系。'})
    scope={'as_of_date':'2026-11-05','routing_policy_sha256':OLD,'routing_model':{'MODEL':'model-a'}}
    # Prior serialized fingerprint contract, independent of the new migration helper.
    payload={k:old.get(k) for k in ('information_id','text','context','statement_ids','chapters','provenance','evidence_ids','relevance')}
    h=hashlib.sha256(json.dumps({'version':m.VERSION,'target':target,'scope':scope,'outline':outline(),
        'record':payload,'review_contract':m.review_prompt([],target,outline(),number)},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    prior={'version':m.VERSION,'target_name':target,'current_chapter':number,'review_scope':scope,'records':[old],
        'routing_record_hashes':{old['information_id']:h}}
    s['context']['source_reading_coverage']={'read':19,'receipt':'new-progress'}
    return s,prior,{**scope,'routing_policy_sha256':NEW}

@pytest.mark.parametrize('target,number',[('海港制造',2),('松林软件',3),('星辰医药',4),('山川物流',5),('青禾农业',6),('远航设备',9)])
@pytest.mark.parametrize('status',['use','defer'])
def test_verified_upgrade_and_second_restore_need_no_model(target,number,status):
    s,prior,scope=fixture(target,number,status);untouched=deepcopy(prior)
    def no_call(*args):pytest.fail('verified unchanged evidence must not be charged again')
    p=m.build_information_usage({'statements':[s]},{number:''},outline(),target,no_call,
        previous=prior,review_scope=scope,current_chapter=number,workers=1)
    assert p['diagnostic']['cached_information_records']==1
    assert p['legacy_routing_migration']['verified_records']==1
    assert p['records'][0]['decision']['status']==status and prior==untouched
    p2=m.build_information_usage({'statements':[s]},{number:''},outline(),target,no_call,
        previous=json.loads(json.dumps(p)),review_scope=scope,current_chapter=number,workers=1)
    assert p2['diagnostic']['cached_information_records']==1

@pytest.mark.parametrize('field',['text','period','entity','source_url','condition','original_sha256','source_kind'])
def test_changed_native_evidence_never_migrates(field):
    s,prior,scope=fixture()
    if field in ('text','source_url'):s[field]+=' changed'
    else:s['context'][field]='changed'
    out=m.migrate_routing_receipts(prior,m.information_records([s]),'样本公司',scope,outline(),4)
    assert out['legacy_routing_migration']['verified_records']==0

@pytest.mark.parametrize('change',['unknown_old_policy','new_policy','model','date','target','chapter','receipt','ambiguous','bad_decision'])
def test_migration_rejects_unproven_compatibility(change):
    s,prior,scope=fixture();target='样本公司';number=4
    if change=='unknown_old_policy':prior['review_scope']['routing_policy_sha256']='unknown'
    elif change=='new_policy':scope['routing_policy_sha256']='different-semantics'
    elif change=='model':scope['routing_model']={'MODEL':'model-b'}
    elif change=='date':scope['as_of_date']='2026-11-06'
    elif change=='target':target='另一家公司'
    elif change=='chapter':number=5
    elif change=='receipt':prior['routing_record_hashes']['I-legacy-identity']='tampered'
    elif change=='ambiguous':prior['records']*=2
    elif change=='bad_decision':prior['records'][0]['decision']['chapter']=12
    out=m.migrate_routing_receipts(prior,m.information_records([s]),target,scope,outline(),number)
    assert not out.get('legacy_routing_migration',{}).get('verified_records')

def test_pause_is_not_converted_to_empty_failed_routing():
    from research_workflow import WorkflowPaused
    def pause(*args):raise WorkflowPaused('stop')
    with pytest.raises(WorkflowPaused):m.build_information_usage({'statements':[statement()]},{4:''},outline(),'公司',pause,current_chapter=4,workers=1)
