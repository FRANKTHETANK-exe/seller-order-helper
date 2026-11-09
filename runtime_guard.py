"""Project-local copy of the reviewed read-only resource guard and request runner.

No scheduler, shared settings, model download, server startup or worker recovery.
The local lock in local_model.py only coordinates this prototype's own clients.
"""
from __future__ import annotations
import contextlib
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import math

ROOT=Path(__file__).resolve().parent
GIB=1024**3
MODELS=('granite4.2:3b',)
INSPECTED_STALE_PROOF=ROOT/'guard-evidence.json'
LIMITS={'num_ctx':4096,'num_predict':256,'num_thread':4,
        'request_seconds':60,'total_seconds':90,'max_response_bytes':32768,
        'max_stream_bytes':262144,'max_stream_lines':1024,
        'min_pressure_free_percent':40,'max_load_1m':6.0,
        'max_model_vram_bytes':10*GIB,'min_disk_free_bytes':20*GIB}
GENERATION_OPTIONS={'num_ctx':4096,'num_predict':256,'num_thread':4,'temperature':0,'seed':17}

# Dedicated Linux judge VMs can spend over 60 seconds reading/repacking a cold
# cached model. This fixed profile extends only that one owned request, including
# loading and inference. Controller/Mac budgets and every resource check remain.
STANDALONE_LINUX_REQUEST_SECONDS = 180
STANDALONE_LINUX_MAX_SWAP_MIB = 16


def request_budget_seconds(*, standalone_demo=False):
    if standalone_demo is True and sys.platform == 'linux':
        return STANDALONE_LINUX_REQUEST_SECONDS
    return LIMITS['request_seconds']


def swap_policy(*, standalone_demo=False):
    """Fixed local profile, never a CLI override or a paging-pressure claim."""
    if standalone_demo is True and sys.platform == 'linux':
        return {'name': 'linux_standalone_small_swap',
                'max_swap_mib': STANDALONE_LINUX_MAX_SWAP_MIB}
    return {'name': 'zero_swap', 'max_swap_mib': 0}

class PilotStop(RuntimeError):
    def __init__(self, message, *, stage=None, failure_code=None, http_status=None):
        super().__init__(message)
        self.stage = stage
        self.failure_code = failure_code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def save_new(path, value):
    with path.open('x') as out:
        json.dump(value, out, indent=2, sort_keys=True)
        out.write('\n')

def get_json(path):
    """Fixed loopback GET only; no proxies, redirects, auth or inherited URLs."""
    if path not in ('/api/version', '/api/tags', '/api/ps'):
        raise PilotStop('unapproved health route')
    conn = http.client.HTTPConnection('127.0.0.1', 11434, timeout=3)
    try:
        conn.request('GET', path)
        res = conn.getresponse()
        data = res.read(65537)
        if res.status != 200 or len(data) > 65536:
            raise PilotStop('bounded local health request failed')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise PilotStop('invalid health response')
        return value
    finally:
        conn.close()

def command(args):
    result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=3)
    if len(result.stdout) > 32768:
        raise PilotStop('resource response exceeded bound')
    return result.stdout

def queue_counts():
    sb = Path.home() / 'SecondBrain'
    stale = {}
    metadata = None
    if INSPECTED_STALE_PROOF is not None:
        from liveness import inspect_proof, current_os_metadata, verified_expired
        stale, proof_boot = inspect_proof(INSPECTED_STALE_PROOF, sb / 'runtime')
        metadata = current_os_metadata()
        # int-second boot timestamps can differ by one second due rounding.
        # Any larger future offset remains a hard stop.
        if proof_boot - metadata[0] > 1:
            raise PilotStop('liveness evidence is from a future boot')
    roots = [sb / 'runtime']
    development = sb / 'runtime/platform-phase2/development-runtime'
    if not development.is_dir():
        raise PilotStop('expected development runtime absent')
    entries = 0
    goals = 0
    for directory in development.iterdir():
        entries += 1
        if entries > 64:
            raise PilotStop('project runtime enumeration exceeds bound')
        if directory.name == 'development-workspaces':
            continue
        if directory.is_symlink():
            raise PilotStop('project runtime symlink requires review')
        if not directory.is_dir():
            continue
        if (directory / 'queue').is_dir() or (directory / 'running').is_dir():
            roots.append(directory)
        for goal in directory.iterdir():
            goals += 1
            if goals > 4096:
                raise PilotStop('goal runtime enumeration exceeds bound')
            if goal.is_dir() and (not goal.is_symlink()):
                if (goal / 'queue').is_dir() or (goal / 'running').is_dir():
                    roots.append(goal)
            if len(roots) > 4096:
                raise PilotStop('runtime enumeration exceeds bound')
    result = {'queue': 0, 'running': 0, 'runtime_directories_checked': len(roots), 'inspected_stale_preserved': 0}
    for root in roots:
        if not root.is_dir():
            raise PilotStop('expected controller runtime absent')
        for state in ('queue', 'running'):
            directory = root / state
            if directory.is_symlink() or not directory.is_dir():
                raise PilotStop('expected controller queue absent or linked')
            for count, item in enumerate(directory.iterdir(), 1):
                if count > 4096:
                    raise PilotStop('queue metadata enumeration exceeds bound')
                if item.suffix == '.json':
                    if state == 'running' and item in stale and verified_expired(item, stale[item], *metadata):
                        result['inspected_stale_preserved'] += 1
                    else:
                        result[state] += 1
    return result

def local_resources():
    """Bounded host/runtime metrics; does not inspect Second Brain queues."""
    if sys.platform == 'linux':
        from linux_resources import snapshot, LinuxMeasurementError
        try:
            return snapshot(ROOT)
        except LinuxMeasurementError:
            raise PilotStop('Linux host resource checks did not pass; no inference permitted',
                            stage='resource_guard', failure_code='RESOURCE_ADMISSION_DENIED') from None
    if sys.platform != 'darwin':
        raise PilotStop('Host resource measurements are unsupported on this platform',
                        stage='admission', failure_code='MODEL_PLATFORM_UNSUPPORTED')
    pressure = command(['/usr/bin/memory_pressure', '-Q'])
    match = re.search('System-wide memory free percentage:\\s*(\\d+)%', pressure)
    swap = command(['/usr/sbin/sysctl', '-n', 'vm.swapusage'])
    used = re.search('used\\s*=\\s*([\\d.]+)M', swap)
    if not match or not used:
        raise PilotStop('resource measurement unavailable')
    return {'memory_pressure_free_percent': int(match.group(1)), 'swap_used_mib': float(used.group(1)), 'load_1m': os.getloadavg()[0], 'disk_free_bytes': shutil.disk_usage(ROOT).free}

def resources():
    result = local_resources()
    result['queue_counts'] = queue_counts()
    return result

def guard(snapshot, loaded, own_model=None, *, check_controller=True,
          standalone_demo=False):
    policy = swap_policy(standalone_demo=standalone_demo)
    if not isinstance(snapshot, dict):
        raise PilotStop('resource measurement is invalid', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED')
    for name in ('swap_used_mib', 'memory_pressure_free_percent', 'load_1m'):
        value = snapshot.get(name)
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise PilotStop('resource measurement is invalid', stage='resource_guard',
                            failure_code='RESOURCE_ADMISSION_DENIED')
    if (snapshot['memory_pressure_free_percent'] > 100
            or type(snapshot.get('disk_free_bytes')) is not int
            or not 0 <= snapshot['disk_free_bytes'] <= 2**63 - 1):
        raise PilotStop('resource measurement is invalid', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED')
    if policy['max_swap_mib']:
        available, total = (snapshot.get('memory_available_bytes'),
                            snapshot.get('memory_total_bytes'))
        if (check_controller is not False or snapshot.get('resource_platform') != 'linux'
                or type(available) is not int or type(total) is not int
                or not 0 < total <= 2**63 - 1 or not 0 <= available <= total
                or not math.isclose(snapshot['memory_pressure_free_percent'],
                    100 * available / total, rel_tol=1e-9, abs_tol=1e-6)):
            raise PilotStop('Linux standalone measurement is invalid',
                            stage='resource_guard', failure_code='RESOURCE_ADMISSION_DENIED')
    if snapshot.get('resource_platform') == 'linux' and own_model not in MODELS:
        available = snapshot.get('memory_available_bytes')
        if type(available) is not int or available < 4 * GIB:
            raise PilotStop('Linux startup headroom is below 4 GiB',
                            stage='resource_guard', failure_code='RESOURCE_ADMISSION_DENIED')
    if snapshot['swap_used_mib'] > policy['max_swap_mib']:
        message = ('swap exceeds the Linux standalone allowance' if policy['max_swap_mib']
                   else 'swap is nonzero; admission requires review')
        raise PilotStop(message, stage='resource_guard',
                        failure_code='SWAP_NONZERO')
    if snapshot['memory_pressure_free_percent'] < LIMITS['min_pressure_free_percent']:
        raise PilotStop('memory threshold reached; wait')
    if own_model is None and snapshot['load_1m'] > LIMITS['max_load_1m']:
        raise PilotStop('host load too high; wait')
    if snapshot['disk_free_bytes'] < LIMITS['min_disk_free_bytes']:
        raise PilotStop('disk headroom too low')
    if check_controller:
        queue_counts_value = snapshot.get('queue_counts')
        if (not isinstance(queue_counts_value, dict)
                or type(queue_counts_value.get('queue')) is not int
                or type(queue_counts_value.get('running')) is not int):
            raise PilotStop('controller queue state is unknown')
        if queue_counts_value['queue'] or queue_counts_value['running']:
            raise PilotStop('existing controller has queued/running work; wait')
    if not isinstance(loaded, dict) or not isinstance(loaded.get('models'), list) or len(loaded['models']) > 4:
        raise PilotStop('loaded-model health is unknown or malformed')
    names = []
    for item in loaded['models']:
        if not isinstance(item, dict):
            raise PilotStop('loaded-model entry malformed')
        model = item.get('name') or item.get('model')
        size = item.get('size_vram')
        if not isinstance(model, str) or not model or type(size) is not int or (size < 0):
            raise PilotStop('loaded-model identity/memory is unknown')
        names.append(model)
        if own_model is None or model != own_model:
            raise PilotStop('another model is loaded; do not evict it')
        if size > LIMITS['max_model_vram_bytes']:
            raise PilotStop('observed model memory threshold exceeded')
    if len(names) > 1:
        raise PilotStop('multiple loaded models; wait')

class StableSwapBaseline:
    """One-call, opt-in policy for stable pre-existing swap allocation.

    The controller guard still requires zero swap. This ephemeral policy admits
    only after three fresh identical measurements and requires every later
    monitor sample to match exactly. It never mutates persistent settings.
    """
    def __init__(self, *, sample_fn=None, loaded_fn=None, sleep_fn=None,
                 samples=3, interval_seconds=10):
        if samples != 3 or interval_seconds != 10:
            raise ValueError('stable-swap policy requires exactly three samples 10 seconds apart')
        self.sample_fn = sample_fn or resources
        self.loaded_fn = loaded_fn or (lambda: get_json('/api/ps'))
        self.sleep_fn = sleep_fn or time.sleep
        self.samples = samples
        self.interval_seconds = interval_seconds
        self.baseline_mib = None
        self.established_at = None
        self.observations = []
        self.closed = False

    @staticmethod
    def _swap(sample):
        value = sample.get('swap_used_mib') if isinstance(sample, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise PilotStop('swap baseline is unavailable or not positive',
                            stage='resource_guard', failure_code='SWAP_BASELINE_INVALID')
        return float(value)

    def establish(self):
        if self.closed or self.baseline_mib is not None:
            raise PilotStop('stable-swap baseline is single-use', stage='resource_guard',
                            failure_code='SWAP_BASELINE_INVALID')
        for index in range(self.samples):
            sample = self.sample_fn()
            loaded = self.loaded_fn()
            swap = self._swap(sample)
            guard({**sample, 'swap_used_mib': 0.0}, loaded)
            if self.baseline_mib is None:
                self.baseline_mib = swap
            elif swap != self.baseline_mib:
                self.close()
                raise PilotStop('swap changed during baseline sampling',
                                stage='resource_guard', failure_code='SWAP_BASELINE_DRIFT')
            self.observations.append(swap)
            if index + 1 < self.samples:
                self.sleep_fn(self.interval_seconds)
        self.established_at = time.monotonic()
        return self.baseline_mib

    def check(self, sample, loaded, own_model=None, *, check_controller=True):
        if (self.closed or self.baseline_mib is None or self.established_at is None
                or time.monotonic() - self.established_at > 60):
            raise PilotStop('stable-swap baseline is not active',
                            stage='resource_guard', failure_code='SWAP_BASELINE_INVALID')
        if self._swap(sample) != self.baseline_mib:
            raise PilotStop('swap increased or changed during inference',
                            stage='resource_guard', failure_code='SWAP_BASELINE_DRIFT')
        guard({**sample, 'swap_used_mib': 0.0}, loaded, own_model=own_model,
              check_controller=check_controller)

    def close(self):
        self.closed = True

def resource_observation(sample, elapsed, *, standalone_demo):
    """Record bounded, nonsecret monitor data without inventing queue coverage."""
    observation = {
        'elapsed': round(elapsed, 2),
        'pressure_free_percent': sample['memory_pressure_free_percent'],
        'swap_used_mib': sample['swap_used_mib'],
        'controller_queue_visibility': ('not_checked_standalone_mode'
                                        if standalone_demo else 'checked'),
    }
    if not standalone_demo:
        observation['queue_counts'] = sample['queue_counts']
    for key in ('memory_available_bytes', 'memory_total_bytes', 'load_1m',
                'disk_free_bytes'):
        value = sample.get(key)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            observation[key] = value
    return observation

def inspect(*, stable_swap_policy=None):
    try:
        version = get_json('/api/version')
    except Exception:
        raise PilotStop('local model runtime lookup failed', stage='admission',
                        failure_code='ADMISSION_CHECK_FAILED') from None
    try:
        tags = get_json('/api/tags')
    except Exception:
        raise PilotStop('model tag lookup failed', stage='tag_lookup',
                        failure_code='MODEL_TAG_LOOKUP_FAILED') from None
    try:
        loaded = get_json('/api/ps')
        snapshot = resources()
    except PilotStop as error:
        if error.failure_code in ('SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'):
            raise
        raise PilotStop('local resource admission was not granted', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    except Exception:
        raise PilotStop('local resource admission could not be verified', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    installed = {x.get('name'): x.get('digest') for x in tags.get('models', [])}
    for model in MODELS:
        if model not in installed:
            raise PilotStop('pinned cached model is absent; no download attempted',
                            stage='tag_lookup', failure_code='MODEL_TAG_UNAVAILABLE')
        if not isinstance(installed[model], str) or not installed[model]:
            raise PilotStop('pinned model digest is unavailable', stage='tag_lookup',
                            failure_code='MODEL_DIGEST_UNAVAILABLE')
    try:
        if stable_swap_policy is None:
            guard(snapshot, loaded)
        else:
            if stable_swap_policy.baseline_mib is None:
                stable_swap_policy.establish()
            stable_swap_policy.check(snapshot, loaded)
    except PilotStop as error:
        if error.failure_code in ('SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'):
            raise
        raise PilotStop('local resource admission was not granted', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    except Exception:
        raise PilotStop('local resource admission was not granted', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    return {'observed_at': now(), 'version': version.get('version'), 'cached_models': list(MODELS), 'model_digests': {m: installed[m] for m in MODELS}, 'loaded_models': loaded.get('models', []), 'resources': snapshot, 'admission': 'not_granted_by_read_only_check',
            'swap_baseline_mib': stable_swap_policy.baseline_mib if stable_swap_policy else None}

def inspect_standalone(expected_digests):
    """Standalone admission for a macOS or compatible Linux judge host.

    This deliberately does not read controller queues or stale-claim evidence.
    The operator must attest that no other AI task is queued/running; all local
    Ollama, model digest and resource checks remain active.
    """
    if sys.platform not in ('darwin', 'linux'):
        raise PilotStop('standalone admission requires macOS or compatible Linux',
                        stage='admission', failure_code='MODEL_PLATFORM_UNSUPPORTED')
    if (not isinstance(expected_digests, dict) or set(expected_digests) != set(MODELS)
            or any(not isinstance(v, str) or not re.fullmatch(r'[a-f0-9]{64}', v)
                   for v in expected_digests.values())):
        raise PilotStop('expected model digest contract is invalid')
    version = get_json('/api/version')
    try:
        tags = get_json('/api/tags')
    except Exception:
        raise PilotStop('model tag lookup failed', stage='tag_lookup',
                        failure_code='MODEL_TAG_LOOKUP_FAILED') from None
    loaded = get_json('/api/ps')
    snapshot = local_resources()
    installed = {x.get('name'): x.get('digest') for x in tags.get('models', [])}
    for model in MODELS:
        if model not in installed:
            raise PilotStop('pinned cached model is absent; no download attempted',
                            stage='tag_lookup', failure_code='MODEL_TAG_UNAVAILABLE')
        if not isinstance(installed[model], str) or not installed[model]:
            raise PilotStop('pinned model digest is unavailable', stage='tag_lookup',
                            failure_code='MODEL_DIGEST_UNAVAILABLE')
        if installed[model] != expected_digests[model]:
            raise PilotStop('cached Granite digest changed; no inference permitted',
                            stage='digest_check', failure_code='MODEL_DIGEST_MISMATCH')
    try:
        guard(snapshot, loaded, check_controller=False, standalone_demo=True)
    except PilotStop as error:
        if error.failure_code in ('SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'):
            raise
        raise PilotStop('local resource admission was not granted', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    except Exception:
        raise PilotStop('local resource admission was not granted', stage='resource_guard',
                        failure_code='RESOURCE_ADMISSION_DENIED') from None
    return {'observed_at': now(), 'version': version.get('version'),
            'cached_models': list(MODELS),
            'model_digests': {m: installed[m] for m in MODELS},
            'loaded_models': loaded.get('models', []), 'resources': snapshot,
            'admission': 'portable_local_checks_passed',
            'swap_policy': swap_policy(standalone_demo=True),
            'controller_queue_visibility': 'not_checked_standalone_mode'}

def generate(model, prompt, response_format=None, *, standalone_demo=False,
             stable_swap_policy=None):
    """Bounded streamed generation with resource monitoring and request cancellation.

    Closing the request is not authority to kill/restart shared daemon processes.
    Any cancellation stops the complete pilot and requires checking /api/ps.
    """
    budget_seconds = request_budget_seconds(standalone_demo=standalone_demo)
    payload = {'model': model, 'prompt': prompt, 'think': False, 'stream': True, 'keep_alive': 0, 'options': dict(GENERATION_OPTIONS)}
    if response_format is not None:
        if response_format != 'json' and not isinstance(response_format, dict):
            raise PilotStop('unsupported draft response format')
        payload['format'] = response_format
    if len(prompt.encode()) > 4096:
        raise PilotStop('prompt exceeds pilot bound')
    # Cold model loading can delay the first response headers. Use the resolved
    # request profile here; the watchdog below still bounds the whole request,
    # including all reads, and the resource monitor remains active throughout.
    conn = http.client.HTTPConnection('127.0.0.1', 11434,
                                      timeout=budget_seconds)
    finished = threading.Event()
    stopped = []
    observations = []
    monitor_state = {'phase': 'waiting', 'last_observation': None}
    start = time.monotonic()

    def cancel(_reason, *, stage='model_request', failure_code='MODEL_REQUEST_FAILED'):
        if finished.is_set():
            return
        if not stopped:
            stopped.append((stage, failure_code))
        if conn.sock:
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)
        conn.close()

    def monitor():
        while not finished.wait(2):
            try:
                if time.monotonic() - start > budget_seconds:
                    raise PilotStop('request wall limit exceeded', stage='model_request',
                                    failure_code='MODEL_REQUEST_FAILED')
                monitor_state['phase'] = 'resources'
                sample = local_resources() if standalone_demo else resources()
                monitor_state['last_observation'] = resource_observation(
                    sample, time.monotonic() - start,
                    standalone_demo=standalone_demo)
                monitor_state['phase'] = 'loaded_model_health'
                loaded = get_json('/api/ps')
                monitor_state['phase'] = 'guard'
                observations.append(dict(monitor_state['last_observation']))
                if stable_swap_policy is None:
                    guard(sample, loaded, own_model=model,
                          check_controller=not standalone_demo,
                          standalone_demo=standalone_demo)
                else:
                    stable_swap_policy.check(sample, loaded, own_model=model,
                          check_controller=not standalone_demo)
                observations[-1]['model_vram_bytes'] = max((m['size_vram'] for m in loaded['models']), default=0)
                monitor_state['phase'] = 'waiting'
            except Exception as exc:
                if finished.is_set():
                    return
                stage = getattr(exc, 'stage', None)
                code = getattr(exc, 'failure_code', None)
                if stage not in ('resource_guard', 'model_request') or code not in (
                        'RESOURCE_ADMISSION_DENIED', 'MODEL_REQUEST_FAILED',
                        'SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'):
                    stage, code = (('resource_guard', 'RESOURCE_ADMISSION_DENIED')
                                   if monitor_state['phase'] in ('resources', 'loaded_model_health', 'guard')
                                   else ('model_request', 'MODEL_REQUEST_FAILED'))
                cancel('monitor stopped', stage=stage, failure_code=code)
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    watchdog = threading.Timer(budget_seconds, cancel,
        kwargs={'_reason':'request wall limit exceeded','stage':'model_request',
                'failure_code':'MODEL_REQUEST_FAILED'})
    watchdog.daemon = True
    watchdog.start()
    watcher.start()
    try:
        conn.request('POST', '/api/generate', body=json.dumps(payload), headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise PilotStop('inference HTTP failure; no retry', stage='model_request',
                            failure_code='MODEL_HTTP_ERROR', http_status=response.status)
        chunks = []
        total = 0
        text_total = 0
        lines = 0
        final = None
        while True:
            line = response.readline(LIMITS['max_response_bytes'] + 1)
            if not line:
                break
            total += len(line)
            lines += 1
            if len(line) > LIMITS['max_response_bytes'] or total > LIMITS['max_stream_bytes'] or lines > LIMITS['max_stream_lines']:
                raise PilotStop('stream envelope bound exceeded', stage='response_parse',
                                failure_code='MODEL_RESPONSE_INVALID')
            try:
                item = json.loads(line)
            except (ValueError, UnicodeError):
                raise PilotStop('model response was not valid bounded JSON',
                                stage='response_parse',
                                failure_code='MODEL_RESPONSE_INVALID') from None
            if type(item) is not dict:
                raise PilotStop('model stream item was not an object', stage='response_parse',
                                failure_code='MODEL_RESPONSE_INVALID')
            if item.get('error'):
                raise PilotStop('inference returned an error; no retry', stage='model_request',
                                failure_code='MODEL_REQUEST_FAILED')
            if item.get('thinking'):
                raise PilotStop('unexpected thinking output; stop without retaining it',
                                stage='response_parse', failure_code='MODEL_RESPONSE_INVALID')
            text = item.get('response', '')
            if not isinstance(text, str):
                raise PilotStop('malformed response', stage='response_parse',
                                failure_code='MODEL_RESPONSE_INVALID')
            text_total += len(text.encode('utf-8'))
            if text_total > LIMITS['max_response_bytes']:
                raise PilotStop('generated text byte bound exceeded', stage='response_parse',
                                failure_code='MODEL_RESPONSE_INVALID')
            chunks.append(text)
            if time.monotonic() - start > budget_seconds:
                raise PilotStop('request wall limit exceeded')
            if item.get('done') is True:
                final = item
                break
            if time.monotonic() - start > budget_seconds:
                raise PilotStop('request wall limit exceeded')
        if stopped:
            stage, failure_code = stopped[0]
            raise PilotStop('model request stopped by its bounded monitor', stage=stage,
                            failure_code=failure_code)
        if final is None or not ''.join(chunks).strip():
            raise PilotStop('empty/incomplete response', stage='response_parse',
                            failure_code='MODEL_RESPONSE_INVALID')
        metric_keys = ('total_duration', 'load_duration', 'prompt_eval_count', 'prompt_eval_duration', 'eval_count', 'eval_duration', 'done_reason')
        metrics = {key: final.get(key) for key in metric_keys}
        duration = metrics.get('eval_duration')
        metrics['tokens_per_second'] = metrics['eval_count'] * 1000000000.0 / duration if isinstance(duration, (int, float)) and duration > 0 and isinstance(metrics['eval_count'], int) else None
        return {'code': ''.join(chunks), 'metrics': metrics, 'wall_seconds': time.monotonic() - start, 'resource_samples': observations, 'options': payload['options'], 'request_budget_seconds': budget_seconds}
    except Exception as exc:
        monitor_failure = stopped[0] if stopped else None
        stage = getattr(exc, 'stage', None)
        failure_code = getattr(exc, 'failure_code', None)
        # Cancellation normally interrupts socket I/O with a transport exception.
        # Preserve the monitor's cause instead of relabeling it as a network error.
        if monitor_failure is not None:
            stage, failure_code = monitor_failure
        http_status = getattr(exc, 'http_status', None)
        if stage not in ('model_request', 'response_parse', 'resource_guard'):
            stage, failure_code = 'model_request', 'MODEL_REQUEST_FAILED'
        if failure_code not in ('MODEL_HTTP_ERROR', 'MODEL_RESPONSE_INVALID',
                                'RESOURCE_ADMISSION_DENIED', 'MODEL_REQUEST_FAILED',
                                'SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'):
            failure_code = 'MODEL_REQUEST_FAILED' if stage == 'model_request' else (
                'RESOURCE_ADMISSION_DENIED' if stage == 'resource_guard' else 'MODEL_RESPONSE_INVALID')
        failure = PilotStop('request stopped; no more inference; server cancellation/unload must be checked',
                            stage=stage, failure_code=failure_code, http_status=http_status)
        failure.evidence = {'monitor_stage': monitor_failure[0] if monitor_failure else None,
            'monitor_failure_code': monitor_failure[1] if monitor_failure else None,
            'monitor_phase': monitor_state['phase'], 'last_observation': monitor_state['last_observation'],
            'transport_error_type': type(exc).__name__, 'resource_samples': observations}
        raise failure from None
    finally:
        finished.set()
        watchdog.cancel()
        conn.close()
        watcher.join(timeout=4)
        # Keep the one-call policy active through GraniteClient's post-request
        # cleanup verification; on an exception this path closes it immediately.
        if stable_swap_policy is not None and sys.exc_info()[0] is not None:
            stable_swap_policy.close()
