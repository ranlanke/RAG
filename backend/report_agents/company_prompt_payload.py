"""Lossless interning of repeated text; no evidence truncation or filtering."""
from collections import Counter
import hashlib

REFERENCE_RULE='资料中仅含text_ref的对象引用shared_texts内的完整字符串；按引用展开后核对，未省略原文，不将字典键作为证据编号。'


def shared_payload(payload):
    counts=Counter()
    def count(value):
        if isinstance(value,str) and len(value)>160:counts[value]+=1
        elif isinstance(value,dict):
            for child in value.values():count(child)
        elif isinstance(value,list):
            for child in value:count(child)
    count(payload)
    keys={text:hashlib.sha256(text.encode()).hexdigest() for text,n in counts.items() if n>1}
    if not keys:return payload
    def pack(value):
        if isinstance(value,str) and value in keys:return {'text_ref':keys[value]}
        if isinstance(value,dict):return {k:pack(v) for k,v in value.items()}
        if isinstance(value,list):return [pack(v) for v in value]
        return value
    assert 'shared_texts' not in payload
    return {**pack(payload),'shared_texts':{key:text for text,key in keys.items()}}
