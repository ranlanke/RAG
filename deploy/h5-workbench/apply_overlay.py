"""Review/apply an exact-base additive overlay. No service, config, or DB actions."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def safe_path(root, name):
    if '\\' in name or ':' in name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('Unsafe overlay path')
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Overlay target escapes the checkout')
    return target


def atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        os.chmod(temporary, (path.stat().st_mode & 0o777) if path.exists() else 0o644)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply(root, package, *, write=False):
    root, package = Path(root).resolve(), Path(package).resolve()
    if not (root/'backend/app.py').is_file():
        raise ValueError('Expected an existing RAG source checkout')
    manifest = json.loads((package/'INTEGRATION-MANIFEST.json').read_text(encoding='utf-8'))
    changes = {}
    # Check every target before writing the first file. A newer production source
    # requires a reviewed merge, never an overwrite from this local snapshot.
    for name, item in manifest['files'].items():
        target, source = safe_path(root, name), safe_path(package/'overlay', name)
        payload = source.read_bytes()
        if digest(payload) != item['after_sha256']:
            raise ValueError('Overlay payload differs: '+name)
        current = target.read_bytes() if target.exists() else None
        if current is not None and digest(current) == item['after_sha256']:
            continue
        observed = digest(current) if current is not None else None
        if observed != item['before_sha256']:
            raise ValueError('Base file differs; merge required: '+name)
        changes[name] = (payload, current)
    result = {'release': manifest['release'], 'dry_run': not write, 'changed_files': len(changes),
              'server_or_database_actions': False}
    if not write or not changes:
        return result
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = safe_path(root, 'work/h5-integration-backups/'+stamp)
    backup.mkdir(parents=True, exist_ok=False)
    receipt = {'release': manifest['release'], 'root': str(root), 'files': {}}
    for name, (payload, current) in changes.items():
        if current is not None:
            atomic_write(safe_path(backup, name), current)
        receipt['files'][name] = {'before_sha256': digest(current) if current is not None else None,
                                 'after_sha256': digest(payload)}
    atomic_write(backup/'receipt.json', json.dumps(receipt, ensure_ascii=False, indent=2).encode())
    for name, (payload, _) in changes.items():
        atomic_write(safe_path(root, name), payload)
    result['backup'] = str(backup)
    return result


def rollback(root, backup, *, write=False):
    root, backup = Path(root).resolve(), Path(backup).resolve()
    if not backup.is_relative_to(root/'work/h5-integration-backups'):
        raise ValueError('Backup is outside the expected checkout')
    receipt = json.loads((backup/'receipt.json').read_text(encoding='utf-8'))
    if Path(receipt['root']).resolve() != root:
        raise ValueError('Backup belongs to another checkout')
    changes = {}
    for name, item in receipt['files'].items():
        target = safe_path(root, name)
        current = digest(target.read_bytes()) if target.is_file() else None
        if current == item['before_sha256']:
            continue  # A partially applied overlay may not have reached this file.
        if current != item['after_sha256']:
            raise ValueError('File changed after integration; merge required: '+name)
        previous = safe_path(backup, name).read_bytes() if item['before_sha256'] else None
        if previous is not None and digest(previous) != item['before_sha256']:
            raise ValueError('Backup file differs: '+name)
        changes[name] = previous
    if write:
        for name, previous in changes.items():
            target = safe_path(root, name)
            if previous is None:
                target.unlink()
            else:
                atomic_write(target, previous)
    return {'dry_run': not write, 'restored_files': len(changes), 'server_or_database_actions': False}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--rollback',type=Path)
    args=parser.parse_args()
    result=rollback(args.root,args.rollback,write=args.apply) if args.rollback else apply(args.root,Path(__file__).parent,write=args.apply)
    print(json.dumps(result,ensure_ascii=False,indent=2))
