"""Read-only validation of explicitly inspected, pre-boot expired queue records.

This never repairs, claims, releases, clears or writes a controller record.
"""
from pathlib import Path
import datetime as dt
import hashlib
import json
import re
import socket
import subprocess


class LivenessUnknown(ValueError):
    pass


def _bounded_json(path, limit=1048576):
    for component in (path,*path.parents):
        if component.is_symlink():raise LivenessUnknown('linked liveness path')
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise LivenessUnknown('liveness record absent, linked or oversized')
    raw=path.read_bytes()
    if len(raw)>limit:raise LivenessUnknown('liveness record exceeds bound')
    return raw,json.loads(raw)


def inspect_proof(path, runtime_root):
    _,proof=_bounded_json(path,65536)
    entries=proof.get('entries')
    if not isinstance(entries,list) or not 1<=len(entries)<=16:
        raise LivenessUnknown('invalid inspected stale-record list')
    result={}
    for entry in entries:
        relative=entry.get('relative_path')
        if not isinstance(relative,str):raise LivenessUnknown('invalid inspected path')
        relative=Path(relative)
        if relative.is_absolute() or '..' in relative.parts or relative.parent.name!='running':
            raise LivenessUnknown('inspected path outside running records')
        task=runtime_root/relative
        for p in (task,*task.parents):
            if p.is_symlink():raise LivenessUnknown('linked runtime path')
        if task in result:raise LivenessUnknown('duplicate inspected path')
        for key in ('task_sha256','identity_sha256'):
            if not isinstance(entry.get(key),str) or not re.fullmatch('[0-9a-f]{64}',entry[key]):
                raise LivenessUnknown('invalid inspected digest')
        result[task]=entry
    boot=proof.get('boot_epoch')
    if type(boot) is not int or boot<=0:raise LivenessUnknown('proof boot identity missing')
    return result,boot


def current_os_metadata():
    boot=subprocess.run(['/usr/sbin/sysctl','-n','kern.boottime'],check=True,
                        capture_output=True,text=True,timeout=3).stdout
    match=re.search(r'sec\s*=\s*(\d+)',boot)
    if not match:raise LivenessUnknown('boot time unavailable')
    pids=subprocess.run(['/bin/ps','-A','-o','pid='],check=True,
                        capture_output=True,text=True,timeout=3).stdout
    if len(pids)>65536:raise LivenessUnknown('process metadata exceeds bound')
    try:live={int(v) for v in pids.split()}
    except ValueError:raise LivenessUnknown('invalid process inventory') from None
    return int(match.group(1)),live,socket.gethostname()


def verified_expired(path, entry, boot_epoch, live_pids, hostname):
    """True only for unchanged, same-host owners dead since a later boot."""
    task_bytes,task=_bounded_json(path)
    task_id=task.get('task_id')
    if not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',task_id):
        return False
    if task_id!=entry.get('task_id') or path.name!=task_id+'.json':return False
    identity_path=path.parent.parent/'identities'/(task_id+'.json')
    identity_bytes,identity=_bounded_json(identity_path)
    if hashlib.sha256(task_bytes).hexdigest()!=entry['task_sha256']:return False
    if hashlib.sha256(identity_bytes).hexdigest()!=entry['identity_sha256']:return False
    if identity.get('task_id')!=task_id or identity.get('state')!='running':return False
    claimed=identity.get('claimed_by','').split(':')
    if len(claimed)!=3 or claimed[0]!=hostname or not claimed[1].isdigit() or not re.fullmatch('[0-9a-f]{8}',claimed[2]):return False
    if int(claimed[1]) in live_pids:return False
    if not re.fullmatch('[0-9a-f]{32}',str(identity.get('execution_id',''))):return False
    if identity.get('queue_name')!=path.name:return False
    try:
        lease=dt.datetime.fromisoformat(identity['lease_expires_at'])
        claimed_at=dt.datetime.fromisoformat(identity['claimed_at'])
        if lease.tzinfo is None or claimed_at.tzinfo is None:return False
        if not claimed_at.timestamp()<=lease.timestamp()<boot_epoch:return False
    except (KeyError,ValueError,TypeError):return False
    # Both the bytes and metadata must predate the boot; renewed/touched work blocks.
    if max(path.stat().st_mtime,identity_path.stat().st_mtime)>=boot_epoch:return False
    controller=task.get('_controller')
    if controller:
        if task.get('status')!='running':return False
        if any(controller.get(k)!=identity.get(k) for k in
               ('execution_id','claimed_by','claimed_at','lease_expires_at')):return False
    elif task.get('status')!='queued' or '_controller' in task:
        return False
    # The no-controller/queued body in a running directory is an interrupted
    # pre-boot claim, not a valid current lease. Identity still must pass above.
    if path.read_bytes()!=task_bytes or identity_path.read_bytes()!=identity_bytes:return False
    return True
