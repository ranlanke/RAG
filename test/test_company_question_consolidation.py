import json
import pytest
from company_question_consolidation import consolidate_questions


QUESTIONS=[{'id':'Q1','question':'登记要素是否一致？'},{'id':'Q2','question':'控制依据是什么？'}]


def response(support,query):
    return {'questions':[{'id':'Q1','status':'partial','support_ids':[support],
        'missing_fields':['其他批次可能已有的字段'],'reason':'局部资料判断。'},
        {'id':'Q2','status':'missing','support_ids':[],'missing_fields':['控制协议'],'reason':'本批未见协议。'}],
        'follow_up_queries':[{'question_id':'Q1','query':query,'reason':'局部未见。'}]}


def test_known_fields_across_batches_replace_false_gap_queries_and_preserve_real_actions():
    proofs=[{'id':'P1','text':'统一社会信用代码为91110000123456789X。'},
        {'id':'P2','text':'营业执照载注册资本100万元。'}]
    batches=[{'response':response('P1','搜索统一社会信用代码')},{'response':response('P2','搜索注册资本')}]
    calls=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(data)
        assert {p['id'] for p in data['proofs']}=={'P1','P2'}
        return json.dumps({'questions':[{'id':'Q1','status':'answered','support_ids':['P1','P2'],
            'missing_fields':[],'reason':'代码与资本由两个原文批次共同回答。'},
            {'id':'Q2','status':'missing','support_ids':[],'missing_fields':['当前控制协议'],
                'reason':'所有批次均未取得协议。','next_step':'向公司索取现行有效控制协议。'}],
            'follow_up_queries':[]})
    value=consolidate_questions('样本公司',QUESTIONS,proofs,batches,invoke)
    assert len(calls)==2 and value['questions'][0]['status']=='answered'
    assert value['follow_up_queries']==[] and value['questions'][1]['next_step'].startswith('向公司')
    assert value['consolidation']['initial_batches']==2
    consolidate_questions('样本公司',QUESTIONS,proofs,batches,
        lambda *a:pytest.fail('identical successful merge must be reused'),previous=value['consolidation'])


def test_large_batch_sets_merge_without_top_n_loss_or_query_count_limit():
    proofs=[{'id':'P'+str(i),'text':str(i)+'资本原文'*900} for i in range(21)]
    batches=[{'response':response(p['id'],'查询不同时间资本变更'+str(i))} for i,p in enumerate(proofs)]
    seen=set();receipts=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);ids=[p['id'] for p in data['proofs']];seen.update(ids)
        queries=[{'question_id':'Q1','query':str(i)+' 查询资本变化','reason':'不同日期。'} for i in ids]
        return json.dumps({'questions':[{'id':'Q1','status':'partial','support_ids':ids,
            'missing_fields':['登记档案'],'reason':'已核各时点资本，仍缺登记档案。'},
            {'id':'Q2','status':'missing','support_ids':[],'missing_fields':['控制协议'],'reason':'仍需公司提供。'}],
            'follow_up_queries':queries})
    value=consolidate_questions('样本公司',QUESTIONS,proofs,batches,invoke,receipt_callback=receipts.append)
    assert seen=={p['id'] for p in proofs}
    assert len(value['questions'][0]['support_ids'])==21 and len(value['follow_up_queries'])==21
    assert len(value['consolidation']['receipts'])>1 and receipts


def test_unbound_merge_is_saved_and_fails_instead_of_reusing_unconsolidated_queries():
    receipts=[]
    def invoke(*args):return json.dumps({'questions':[{'id':'Q1','status':'answered','support_ids':['INVENTED'],
        'reason':'错误引用。'}, {'id':'Q2','status':'missing','support_ids':[],'reason':'未见。'}]})
    with pytest.raises(ValueError,match='unbound_cross_batch'):
        consolidate_questions('样本公司',QUESTIONS,[{'id':'P1','text':'原件。'}],
            [{'response':response('P1','查询1')},{'response':response('P1','查询2')}],invoke,receipt_callback=receipts.append)
    assert receipts[-1]['receipts'][-1]['error']=='ValueError'
    assert receipts[-1]['receipts'][-1]['response']['questions'][0]['support_ids']==['INVENTED']


def test_unique_known_identifier_format_is_bound_and_original_spelling_is_retained():
    from company_question_consolidation import _checked
    canonical='P-05f8ba74c46fcff7'
    proposed='P-5f8ba74c46fcff7'
    answer=response(proposed,'查询登记档案')
    checked=_checked(answer,QUESTIONS,{canonical:{'id':canonical}},'样本公司')
    row=checked['questions'][0]
    assert row['support_ids']==[canonical]
    assert row['support_id_normalizations']==[{'proposed_id':proposed,'bound_id':canonical,
        'basis':'unique_known_hex_identifier_format'}]
    assert answer['questions'][0]['support_ids']==[proposed]
    calls=[]
    def invoke(role,prompt,trace):
        data=json.loads(prompt.rsplit('资料：\n',1)[1]);calls.append(role)
        if len(calls)==1:return json.dumps(answer)
        assert data['proposed_chapter_result']['questions'][0]['support_ids']==[canonical]
        return json.dumps(response(canonical,'查询登记档案'))
    value=consolidate_questions('样本公司',QUESTIONS,[{'id':canonical,'text':'工商登记原件。'}],
        [{'response':response(canonical,'查询1')},{'response':response(canonical,'查询2')}],invoke)
    assert len(calls)==2 and '独立核对' in calls[1]
    receipt=value['consolidation']['receipts'][0]
    assert receipt['response']['questions'][0]['support_ids']==[proposed]
    assert receipt['checked_response']['questions'][0]['support_ids']==[canonical]
    assert receipt['checked_response']['questions'][0]['support_id_normalizations']


@pytest.mark.parametrize('proposed,known',[
    ('P-abc',{'P-0abc':{},'P-00abc':{}}),
    ('P-abc',{'P-abd':{}}),
    ('P-abc0',{'P-abc':{}}),
])
def test_ambiguous_or_changed_identifier_is_never_guessed(proposed,known):
    from company_question_consolidation import _checked
    with pytest.raises(ValueError,match='unbound_cross_batch_question_support'):
        _checked(response(proposed,'补采'),QUESTIONS,known,'样本公司')


def test_reordered_checkpoint_reuses_completed_merges_after_interruption():
    from company_flow_state import CompanyFlowLimit
    proofs=[{'id':'P1','text':'原文甲'},{'id':'P2','text':'原文乙'}]
    batches=[{'response':response(p['id'],'核对登记')} for p in proofs]
    receipts=[];calls=[]
    def invoke(role,prompt,trace):
        calls.append(role)
        if len(calls)==2:raise CompanyFlowLimit('test_pause')
        return json.dumps(response('P1','核对登记'))
    with pytest.raises(CompanyFlowLimit):
        consolidate_questions('样本',QUESTIONS,proofs,batches,invoke,receipt_callback=receipts.append)
    saved=json.loads(json.dumps(receipts[-1],sort_keys=True))
    resumed=[]
    def finish(role,prompt,trace):
        resumed.append(role)
        assert '独立核对' in role
        return json.dumps(response('P1','核对登记'))
    value=consolidate_questions('样本',QUESTIONS,list(reversed(proofs)),
        json.loads(json.dumps(list(reversed(batches)),sort_keys=True)),finish,previous=saved)
    assert len(resumed)==1
    consolidate_questions('样本',QUESTIONS,proofs,batches,
        lambda *a:pytest.fail('reordered evidence is not a new model request'),
        previous=json.loads(json.dumps(value['consolidation'],sort_keys=True)))


def test_source_metadata_cannot_block_merge_of_already_checked_judgments():
    proofs=[{'id':'P'+str(i),'text':'原文'+str(i),'scope':'target','period':'2025',
        'source_url':'https://example.test/'+str(i)+'a'*25000} for i in range(3)]
    batches=[{'response':response(p['id'],'核对登记'+p['id'])} for p in proofs]
    seen=set()
    def invoke(role,prompt,trace):
        assert len(prompt.encode())<=54000
        data=json.loads(prompt.rsplit('资料：\n',1)[1])
        assert all(p['scope']=='target' and p['period']=='2025' for p in data['proofs'])
        ids=[p['id'] for p in data['proofs']];seen.update(ids)
        value=response(ids[0],'核对登记');value['questions'][0]['support_ids']=ids
        return json.dumps(value)
    value=consolidate_questions('样本',QUESTIONS,proofs,batches,invoke)
    assert seen=={'P0','P1','P2'}
    assert set(value['questions'][0]['support_ids'])==seen
    assert all(len(p['source_url'])>25000 and p['text'] for p in proofs)
