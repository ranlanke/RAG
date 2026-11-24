from copy import deepcopy
import hashlib
from types import SimpleNamespace as NS
import pytest
from company_flow_state import fingerprint
from report_agents.company_model_reading import VERSION,validate_model_selection
from report_agents.company_reading_recovery import repair_selection_records


def original(repeats=2):
    text='国内\n领先\n智能文\n档创作\n及辅助\n技术'
    native='样本公司年度报告\n'+('\n具体技术说明\n'.join([text]*repeats))+'\n尾部'
    raw='前文'*50+native;start=100;end=len(raw);digest=hashlib.sha256(raw.encode()).hexdigest()
    window={'window_id':'W23','quote':native,'start':start,'end':end,
        'native_window_sha256':fingerprint([digest,start,end,native]),
        'context_quote':raw,'context_start':0,'context_end':len(raw),
        'native_context_sha256':fingerprint([digest,0,len(raw),raw])}
    doc={'evidence_id':'E-ABCDEF123456','source_id':'1','source_url':'https://original.test/report',
        'source_title':'样本公司年报','source_kind':'issuer_disclosure','model_reading_mode':VERSION,
        'original_sha256':digest,'subject_binding':{'issuer_document':True,'entity_validation':'accepted'},
        'windows':[window]}
    row={'evidence_id':doc['evidence_id'],'window_id':'W23','chapter':5,'scope':'target',
        'quote':text,'quote_start':101,'relevance':'年报技术自评，保留自评边界。','subject_basis':'发行人年报。'}
    return raw,doc,row


def test_repeated_literal_retains_all_real_locations_and_corrects_wrong_offset():
    raw,doc,row=original();fact,error=validate_model_selection(row,doc,'样本公司','2026-11-05')
    assert not error and fact['fact']==row['quote']
    assert len(fact['native_quote_occurrences'])==2
    for span in fact['native_quote_occurrences']:assert raw[span['start']:span['end']]==row['quote']
    assert raw[fact['original_offset']:fact['original_end']]==row['quote']
    assert fact['context_quote']==raw and fact['selection_corrections'][0]['proposed']==101


def test_valid_explicit_occurrence_is_preserved_without_a_first_n_limit():
    raw,doc,row=original(41);last=raw.rfind(row['quote']);row['quote_start']=last
    fact,error=validate_model_selection(row,doc,'样本公司')
    assert not error and fact['original_offset']==last
    assert len(fact['native_quote_occurrences'])==41
    assert not fact['selection_corrections']


def test_nonliteral_proposal_is_not_a_fact_but_the_complete_original_stays_usable():
    raw,doc,row=original();bad=deepcopy(row);bad['quote']='公司收入1000亿元，国际领先。'
    fact,error=validate_model_selection(bad,doc,'样本公司')
    assert not error and fact['fact']==doc['windows'][0]['quote']
    assert '1000亿元' not in fact['fact']
    assert raw[fact['original_offset']:fact['original_end']]==fact['fact']
    assert fact['selection_corrections'][0]['reason']=='nonliteral_proposal_native_window_retained'
    assert fact['selection_corrections'][0]['proposed']==bad['quote']


def test_changed_original_hash_is_still_rejected():
    _,doc,row=original()
    bad_doc=deepcopy(doc);bad_doc['windows'][0]['quote']+='改动'
    assert validate_model_selection(row,bad_doc,'样本公司')[1]=='invalid_native_offset'


def test_old_failed_repair_is_cleared_after_current_literal_binding_succeeds():
    _,doc,row=original();result={'facts':[row],'pending_selection_repairs':[{'proposal_index':0}],
        'selection_repair_error':{'category':'connection'}}
    value=repair_selection_records(NS(target_name='样本公司',company_research_intake={}),[doc],result,
        lambda *args:pytest.fail('mechanical original offsets must not ask the model to count'))
    assert value['facts']==[row] and value['pending_selection_repairs']==[]
    assert 'selection_repair_error' not in value
