import json

from report_agents.company_relay_prompt import compact_prompt,expand_prompt,sections


def canonical(text):
    parts=[];pos=0
    for start,end,value in sections(text):
        parts.extend((text[pos:start],json.dumps(value,ensure_ascii=False,separators=(',',':'))));pos=end
    return ''.join(parts)+text[pos:]


def test_duplicate_native_text_can_be_restored_without_changing_any_json_value():
    text='原文中有单位、否定、时间、引号和换行。\n"不得将万元改成元"。'*80
    value={'id':'I-1','text':text,'context':{'quote':text},'offset':23,'propositions':[{'id':'P-1','text':text}]}
    original='规则与不可信数据边界。\n材料：\n'+json.dumps(value,ensure_ascii=False)+'\n附件：\n'+json.dumps([value],ensure_ascii=False)
    packed,stats=compact_prompt(original)
    assert len(packed)<len(original)*.5 and stats['references']==1
    assert expand_prompt(packed)==canonical(original)
    assert compact_prompt(packed)[0]==packed


def test_short_or_existing_reference_packets_are_not_rewritten():
    for text in ['普通原文，没有JSON。','{"id":"I-1","text":"简单事实"}', '{"__company_text_ref__":"source supplied"}']:
        assert compact_prompt(text)==(text,{'references':0})


def test_different_values_ids_and_negative_conditions_never_merge():
    source='单位：万元。并非实际减资。'*50
    value={'a':source,'b':source,'other':source+'尚未生效','id':'I-1','nested':{'id':'I-2'}}
    original=json.dumps(value,ensure_ascii=False)
    packed,_=compact_prompt(original)
    assert json.loads(expand_prompt(packed))==value


def test_unfamiliar_numeric_precision_is_kept_raw():
    text='原文金额必须保持其精度。'*100
    original='{"value":0.1234567890123456789012345,"a":'+json.dumps(text,ensure_ascii=False)+',"b":'+json.dumps(text,ensure_ascii=False)+'}'
    assert compact_prompt(original)==(original,{'references':0})
