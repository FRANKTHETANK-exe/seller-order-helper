"""Read-only Linux host measurements for standalone Granite admission.

No subprocess, provider call, workload eviction, or resource-setting change.
Requires a visible unified cgroup v2 hierarchy with no finite memory/CPU limits
on this process. Restricted or hidden hierarchies are held, not approximated
with host-wide numbers. This is not a scheduler or a proof that Ollama is idle.
"""
from __future__ import annotations

import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil

GIB = 1024 ** 3


class LinuxMeasurementError(ValueError):
    pass


def _text(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise LinuxMeasurementError('Linux resource metadata exceeded its bound')
    return raw.decode('ascii')


def parse_meminfo(text):
    names = {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}
    values = {}
    for line in text.splitlines():
        name = line.split(':', 1)[0]
        if name not in names:
            continue
        match = re.fullmatch(r'([A-Za-z]+):\s+([0-9]{1,16}) kB\s*', line)
        if not match or name in values:
            raise LinuxMeasurementError('Linux memory metadata is invalid')
        values[name] = int(match[2]) * 1024
    if (set(values) != names or values['MemTotal'] <= 0
            or values['MemAvailable'] > values['MemTotal']
            or values['SwapFree'] > values['SwapTotal']):
        raise LinuxMeasurementError('Linux memory metadata is incomplete or inconsistent')
    return values


def _unrestricted_cgroup(proc_root, cgroup_root):
    membership = _text(proc_root / 'self/cgroup', 4096).strip()
    if not membership.startswith('0::/') or '\n' in membership:
        raise LinuxMeasurementError('Unified cgroup v2 metadata is required')
    relative = membership[3:]
    path = PurePosixPath(relative)
    if (not path.is_absolute() or '..' in path.parts or '.' in relative.split('/')
            or '//' in relative or len(path.parts) > 32):
        raise LinuxMeasurementError('Linux resource hierarchy is not fully visible')
    mounts = []
    for line in _text(proc_root / 'self/mountinfo', 262144).splitlines():
        fields = line.split()
        if '-' in fields:
            split = fields.index('-')
            if len(fields) > split + 1 and fields[split + 1] == 'cgroup2':
                mounts.append(fields)
    if (len(mounts) != 1 or len(mounts[0]) < 7
            or mounts[0][3] != '/' or mounts[0][4] != '/sys/fs/cgroup'):
        raise LinuxMeasurementError('Linux resource hierarchy is not fully visible')
    controllers = _text(cgroup_root / 'cgroup.controllers', 4096).split()
    if not {'cpu', 'memory'} <= set(controllers):
        raise LinuxMeasurementError('Linux CPU and memory controls are not inspectable')
    current = cgroup_root.joinpath(*path.parts[1:])
    checked = 0
    while True:
        if not current.is_dir() or current.is_symlink():
            raise LinuxMeasurementError('Linux resource hierarchy is unavailable')
        for name in ('memory.max', 'memory.high', 'cpu.max'):
            try:
                value = _text(current / name, 128).strip()
            except FileNotFoundError:
                # Controls absent from a cgroup are not enabled there. All
                # parents, including the visible root, are still inspected.
                continue
            if name == 'cpu.max':
                if not re.fullmatch(r'max [1-9][0-9]{0,8}', value):
                    raise LinuxMeasurementError('Finite or unknown cgroup CPU limit')
            elif value != 'max':
                raise LinuxMeasurementError('Finite or unknown cgroup memory limit')
        checked += 1
        if current == cgroup_root:
            break
        current = current.parent
    if _text(proc_root / 'self/cgroup', 4096).strip() != membership:
        raise LinuxMeasurementError('Linux resource hierarchy changed during measurement')
    return checked


def snapshot(project_root, *, proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup')):
    """Internal injectable roots are used by fixture tests; no CLI overrides."""
    try:
        checked = _unrestricted_cgroup(proc_root, cgroup_root)
        memory = parse_meminfo(_text(proc_root / 'meminfo', 65536))
        load = os.getloadavg()[0]
        cpus = len(os.sched_getaffinity(0))
        disk = shutil.disk_usage(project_root).free
        # Measure available memory even while our admitted model occupies RAM.
        # The guard applies startup headroom before loading and its continuous
        # memory/swap thresholds during inference; measurement is not admission.
        if not math.isfinite(load) or load < 0 or cpus < 4 or disk < 0:
            raise LinuxMeasurementError('Linux host lacks measured admission headroom')
        return {
            'memory_pressure_free_percent': 100 * memory['MemAvailable'] / memory['MemTotal'],
            'memory_measurement': 'Linux MemAvailable / MemTotal; not macOS pressure',
            'memory_available_bytes': memory['MemAvailable'],
            'memory_total_bytes': memory['MemTotal'],
            'swap_used_mib': (memory['SwapTotal'] - memory['SwapFree']) / (1024 ** 2),
            'load_1m': load,
            'disk_free_bytes': disk,
            'cpu_affinity_count': cpus,
            'resource_platform': 'linux',
            'cgroup_scope': 'visible_v2_no_finite_memory_or_cpu_limits',
            'cgroup_ancestors_checked': checked,
        }
    except LinuxMeasurementError:
        raise
    except (OSError, ValueError, UnicodeError, AttributeError):
        raise LinuxMeasurementError('Linux resource measurement is unavailable') from None
