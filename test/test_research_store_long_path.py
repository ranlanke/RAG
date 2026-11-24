from pathlib import Path

from research_store import ResearchObjectStore


def test_atomic_write_near_windows_path_limit(tmp_path):
    store=ResearchObjectStore()
    store.local_root=tmp_path
    stem='cache/'+'a'*64+'.json'
    extra=max(1,250-len(str(tmp_path/stem))-1)
    key='cache/'+'x'*extra+'/'+'a'*64+'.json'
    target=Path(tmp_path,key)
    assert len(str(target))<=259
    store.put_text(key,'原文回执')
    assert store.get_text(key)=='原文回执'
