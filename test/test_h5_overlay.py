import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

def test_overlay_checks_every_base_before_mutating_and_can_restore_exact_backup(tmp_path):
    path=Path(__file__).resolve().parents[1]/'deploy/h5-workbench/apply_overlay.py'
    spec=importlib.util.spec_from_file_location('h5_overlay',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=tmp_path/'rag';(root/'backend').mkdir(parents=True);(root/'backend/app.py').write_bytes(b'old app')
    pack=tmp_path/'pack';(pack/'overlay/backend').mkdir(parents=True)
    (pack/'overlay/backend/app.py').write_bytes(b'new app');(pack/'overlay/backend/new.py').write_bytes(b'new feature')
    sha=lambda b:hashlib.sha256(b).hexdigest()
    (pack/'INTEGRATION-MANIFEST.json').write_text(json.dumps({'release':'test','files':{
        'backend/app.py':{'before_sha256':sha(b'old app'),'after_sha256':sha(b'new app')},
        'backend/new.py':{'before_sha256':None,'after_sha256':sha(b'new feature')},
    }}))
    (root/'backend/new.py').write_bytes(b'unrelated work')
    with pytest.raises(ValueError,match='merge required'):module.apply(root,pack,write=True)
    assert (root/'backend/app.py').read_bytes()==b'old app'
    (root/'backend/new.py').unlink()
    assert module.apply(root,pack)['dry_run']
    receipt=module.apply(root,pack,write=True)
    assert (root/'backend/app.py').read_bytes()==b'new app'
    assert module.apply(root,pack,write=True)['changed_files']==0
    module.rollback(root,receipt['backup'],write=True)
    assert (root/'backend/app.py').read_bytes()==b'old app'
    assert not (root/'backend/new.py').exists()
    receipt=module.apply(root,pack,write=True)
    (root/'backend/new.py').unlink()  # Simulate a write interruption before the last file.
    module.rollback(root,receipt['backup'],write=True)
    assert (root/'backend/app.py').read_bytes()==b'old app'

def test_overlay_rejects_outside_paths(tmp_path):
    spec=importlib.util.spec_from_file_location('h5_overlay_guard',Path(__file__).resolve().parents[1]/'deploy/h5-workbench/apply_overlay.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for name in ('../other','.env/../../other','E:/outside','/outside','foo\\bar'):
        with pytest.raises(ValueError):module.safe_path(tmp_path,name)
