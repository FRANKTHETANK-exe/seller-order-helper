"""Fixed localhost Granite client with one-call admission and no automatic retry."""
from __future__ import annotations

import sys
import json
from pathlib import Path
import time
from uuid import uuid4

from facts import OrderFacts
from suggestions import (MAX_MODEL_OUTPUT_BYTES, MAX_PROMPT_BYTES, PHRASE_IDS,
                         STYLE_IDS, _prompt, _selection, _unique_object)
import runtime_guard as guard

MODEL = 'granite4.2:3b'
MODEL_DIGEST = '40577dc168a3a9ad34e9a1234e0c2570be86097fa75a236d4574ae985705d3c4'
ROOT = Path(__file__).resolve().parent


SAFE_STAGES = frozenset({'tag_lookup', 'digest_check', 'admission', 'resource_guard',
                         'model_request', 'response_parse', 'selection_contract'})
SAFE_CODES = frozenset({'MODEL_TAG_LOOKUP_FAILED', 'MODEL_TAG_UNAVAILABLE',
    'MODEL_DIGEST_UNAVAILABLE', 'MODEL_DIGEST_MISMATCH', 'ADMISSION_LOCK_FAILED',
    'ADMISSION_CHECK_FAILED', 'RESOURCE_ADMISSION_DENIED', 'MODEL_REQUEST_FAILED',
    'MODEL_HTTP_ERROR', 'MODEL_RESPONSE_INVALID', 'SELECTION_CONTRACT_REJECTED',
    'MODEL_SETUP_ERROR', 'MODEL_PLATFORM_UNSUPPORTED', 'SWAP_NONZERO', 'SWAP_BASELINE_INVALID', 'SWAP_BASELINE_DRIFT'})


class LocalModelHeld(ValueError):
    """Stopped request with bounded non-sensitive diagnostic fields only."""
    def __init__(self, message, *, stage=None, failure_code=None,
                 request_attempted=False, attempt_count=0, http_status=None):
        super().__init__(message)
        self.stage = stage if stage in SAFE_STAGES else 'admission'
        self.failure_code = failure_code if failure_code in SAFE_CODES else 'MODEL_SETUP_ERROR'
        self.request_attempted = request_attempted is True
        self.attempt_count = attempt_count if attempt_count in (0, 1) else 0
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


def request_facts(prompt):
    if type(prompt) is not str or len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise LocalModelHeld('Unsupported or oversized suggestion prompt', stage='selection_contract',
                             failure_code='SELECTION_CONTRACT_REJECTED')
    try:
        request = json.loads(prompt, object_pairs_hook=_unique_object)
        context = request['facts']
        if request.get('kind') == 'paypal_sandbox_review_v1':
            from sandbox_ai_review import SandboxOrderFacts, _prompt as sandbox_prompt
            configuration_id = None
            pin_path = None
            if context.get('source') == 'reviewer_live_paypal_sandbox_read':
                # The configuration identity is deliberately absent from the
                # model prompt. Rebind it locally from one of the two
                # owner-only profiles so reviewer-live facts can be validated
                # without sending a profile path or identity to Granite.
                from reviewer_sandbox_order import (DEFAULT_JUDGE_ORDER_PIN,
                    DEFAULT_ORDER_PIN, active_reviewer_pin_path, read_order_pin)
                active_pin = active_reviewer_pin_path()
                candidates = ((active_pin,) if active_pin is not None else
                              (DEFAULT_ORDER_PIN, DEFAULT_JUDGE_ORDER_PIN))
                for candidate in candidates:
                    try:
                        pin = read_order_pin(candidate)
                    except Exception:
                        continue
                    if pin.order_id == context.get('order_id'):
                        configuration_id = pin.configuration_id
                        pin_path = candidate
                        break
                if configuration_id is None:
                    raise ValueError('Prompt order does not match the active owner-only pin')
            facts = SandboxOrderFacts(order_id=context['order_id'],
                order_status=context['order_status'], payment_truth=context['payment_truth'],
                capture_status=context['capture_status'], currency=context['currency'],
                amount=context['amount'], source=context['source'], observed_at=context['observed_at'],
                configuration_id=configuration_id, _pin_path=pin_path)
            expected = json.loads(sandbox_prompt(facts, request['buyer_question']))
            if request != expected:
                raise ValueError('Prompt differs from the reviewed PayPal sandbox grounding contract')
            return facts, expected['selection_schema']
        facts = OrderFacts(context['order_id'], context['status'], context['capture_status'])
        expected = json.loads(_prompt(facts))
        if request != expected:
            raise ValueError('Prompt differs from the reviewed grounding contract')
        return facts, expected['selection_schema']
    except (ValueError, KeyError, TypeError, RecursionError):
        raise LocalModelHeld('Prompt must match a reviewed local suggestion contract and exact schema',
                             stage='selection_contract',
                             failure_code='SELECTION_CONTRACT_REJECTED') from None


class StubClient:
    kind = 'stub'
    model_name = 'stub'
    execution_verified = False

    def __init__(self, phrase_id='status_first', style_id='friendly'):
        if phrase_id not in PHRASE_IDS or style_id not in STYLE_IDS:
            raise ValueError('Unknown stub wording selection')
        self.phrase_id = phrase_id; self.style_id = style_id
        self.last_run = {'status': 'not_called', 'model': 'stub', 'inference_requests_attempted': 0}

    def __call__(self, prompt):
        facts, _ = request_facts(prompt)
        self.last_run = {'status': 'stub', 'model': 'stub', 'inference_requests_attempted': 0}
        parsed = json.loads(prompt)
        if parsed.get('kind') == 'paypal_sandbox_review_v1':
            from sandbox_ai_review import demo_question_intent
            return json.dumps({'order_id': facts.order_id,
                'question_intent': demo_question_intent(parsed['buyer_question']),
                'phrase_id': self.phrase_id, 'style_id': self.style_id})
        return json.dumps({'order_id': facts.order_id, 'category': facts.category,
                           'phrase_id': self.phrase_id, 'style_id': self.style_id})


class GraniteClient:
    kind = 'granite'
    model_name = MODEL

    def __init__(self, lock_path=None, *, standalone_demo=False,
                 stable_swap_policy=None):
        if sys.platform == 'win32':
            raise LocalModelHeld('Real Granite requires the supported POSIX lock; use offline stub mode on Windows', stage='admission', failure_code='MODEL_PLATFORM_UNSUPPORTED')
        try:
            import fcntl
        except ImportError:
            raise LocalModelHeld('Required POSIX model lock is unavailable; no inference permitted', stage='admission', failure_code='MODEL_PLATFORM_UNSUPPORTED') from None
        self._lock_module = fcntl
        self.lock_path = Path(lock_path) if lock_path else ROOT/'data/.granite.lock'
        self.standalone_demo = standalone_demo is True
        # Explicit opt-in only; normal app construction retains the zero-swap guard.
        self.stable_swap_policy = stable_swap_policy
        self._standalone_attested_until = 0.0
        self.execution_verified = False
        self._used = False
        self.last_run = {'status': 'not_called', 'model': MODEL, 'model_digest': MODEL_DIGEST,
                         'inference_requests_attempted': 0,
                         'request_attempted': False, 'attempt_count': 0,
                         'admission_profile': ('portable_standalone' if self.standalone_demo
                                               else 'secondbrain_controller')}

    def attest_standalone(self, response):
        """Require an explicit, time-bounded operator idle-workload attestation."""
        if not self.standalone_demo or response != 'RUN':
            return False
        self._standalone_attested_until = time.monotonic() + 600
        return True

    def preflight(self):
        if self.standalone_demo:
            return guard.inspect_standalone({MODEL: MODEL_DIGEST})
        return guard.inspect(stable_swap_policy=self.stable_swap_policy)

    def __call__(self, prompt):
        if self._used:
            raise LocalModelHeld('This client is single-use; no automatic second request')
        if (self.standalone_demo
                and time.monotonic() > self._standalone_attested_until):
            raise LocalModelHeld('Standalone idle-workload attestation is missing or expired')
        self._used = True
        phase = 'admission'
        self.last_run = {'status': 'admission_started', 'model': MODEL, 'model_digest': MODEL_DIGEST,
                         'request_id': uuid4().hex, 'inference_requests_attempted': 0,
                         'limits': dict(guard.GENERATION_OPTIONS),
                         'admission_profile': ('portable_standalone' if self.standalone_demo
                                               else 'secondbrain_controller'),
                         'controller_queue_visibility': ('not_checked_standalone_mode'
                             if self.standalone_demo else 'checked')}
        try:
            phase = 'selection_contract'
            facts, schema = request_facts(prompt)
            phase = 'admission'
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            if self.lock_path.is_symlink():
                raise LocalModelHeld('Linked local model lock is not allowed', stage='admission',
                                     failure_code='ADMISSION_LOCK_FAILED')
            try:
                lock_context = self.lock_path.open('a+')
            except Exception:
                raise LocalModelHeld('Local model admission lock is unavailable', stage='admission',
                                     failure_code='ADMISSION_LOCK_FAILED') from None
            with lock_context as lock:
                try:
                    self._lock_module.flock(lock, self._lock_module.LOCK_EX | self._lock_module.LOCK_NB)
                except Exception:
                    raise LocalModelHeld('Local model admission lock is busy', stage='admission',
                                         failure_code='ADMISSION_LOCK_FAILED') from None
                phase = 'tag_lookup'
                try:
                    checked = self.preflight()
                except Exception as error:
                    stage = getattr(error, 'stage', None)
                    code = getattr(error, 'failure_code', None)
                    if stage not in SAFE_STAGES or code not in SAFE_CODES:
                        stage, code = 'admission', 'ADMISSION_CHECK_FAILED'
                    raise LocalModelHeld('Local model admission check failed', stage=stage,
                                         failure_code=code) from None
                phase = 'digest_check'
                if checked['model_digests'].get(MODEL) != MODEL_DIGEST:
                    raise LocalModelHeld('Cached Granite digest changed; no inference permitted',
                                         stage='digest_check', failure_code='MODEL_DIGEST_MISMATCH')
                phase = 'resource_guard'
                self.last_run['inference_requests_attempted'] = 1
                self.last_run['request_attempted'] = True
                self.last_run['attempt_count'] = 1
                self.last_run['status'] = 'generation_started'
                phase = 'model_request'
                try:
                    result = guard.generate(MODEL, prompt, response_format=schema,
                                            standalone_demo=self.standalone_demo,
                                            stable_swap_policy=self.stable_swap_policy)
                except Exception as error:
                    stage = getattr(error, 'stage', None)
                    code = getattr(error, 'failure_code', None)
                    http_status = getattr(error, 'http_status', None)
                    if stage not in SAFE_STAGES or code not in SAFE_CODES:
                        stage, code = 'model_request', 'MODEL_REQUEST_FAILED'
                    raise LocalModelHeld('Local model request failed; no retry was made',
                                         stage=stage, failure_code=code,
                                         request_attempted=True, attempt_count=1,
                                         http_status=http_status) from None
                phase = 'response_parse'
                if (type(result) is not dict or type(result.get('code')) is not str
                        or type(result.get('metrics')) is not dict
                        or type(result.get('wall_seconds')) not in (int, float)
                        or type(result.get('resource_samples', [])) is not list):
                    raise LocalModelHeld('Local model response envelope was invalid',
                                         stage='response_parse', failure_code='MODEL_RESPONSE_INVALID',
                                         request_attempted=True, attempt_count=1)
                self.last_run.update(metrics=result['metrics'], wall_seconds=result['wall_seconds'])
                samples = result.get('resource_samples', [])
                if samples:
                    self.last_run.update(memory_min_free_percent=min(s['pressure_free_percent'] for s in samples),
                        model_vram_max_bytes=max(s.get('model_vram_bytes', 0) for s in samples),
                        swap_used_mib_max=max(s['swap_used_mib'] for s in samples))
                raw = result['code']
                if len(raw.encode('utf-8')) > MAX_MODEL_OUTPUT_BYTES:
                    raise LocalModelHeld('Model output exceeds the suggestion schema bound',
                                         stage='response_parse', failure_code='MODEL_RESPONSE_INVALID',
                                         request_attempted=True, attempt_count=1)
                contract = json.loads(prompt)
                if contract.get('kind') == 'paypal_sandbox_review_v1':
                    from sandbox_ai_review import validate_sandbox_selection
                    try:
                        choice = validate_sandbox_selection(raw, facts)
                    except Exception:
                        raise LocalModelHeld('Model response did not match the approved selection schema',
                            stage='selection_contract', failure_code='SELECTION_CONTRACT_REJECTED',
                            request_attempted=True, attempt_count=1) from None
                else:
                    try:
                        choice = _selection(raw, facts)
                    except Exception:
                        raise LocalModelHeld('Model response did not match the approved selection schema',
                            stage='selection_contract', failure_code='SELECTION_CONTRACT_REJECTED',
                            request_attempted=True, attempt_count=1) from None
                # keep_alive=0 belongs only to our request. Never unload another worker.
                time.sleep(1)
                loaded = guard.get_json('/api/ps')
                snapshot = (guard.local_resources() if self.standalone_demo
                            else guard.resources())
                if self.stable_swap_policy is None:
                    guard.guard(snapshot, loaded,
                                check_controller=not self.standalone_demo)
                else:
                    self.stable_swap_policy.check(snapshot, loaded,
                        check_controller=not self.standalone_demo)
                self.last_run['cleanup_confirmed'] = loaded['models'] == []
                self.last_run['status'] = 'completed'
                self.execution_verified = True
                if self.stable_swap_policy is not None:
                    self.stable_swap_policy.close()
                return json.dumps(choice, separators=(',', ':'))
        except Exception as error:
            attempted = self.last_run['inference_requests_attempted']
            self.last_run['status'] = 'stopped_after_request' if attempted else 'held_before_request'
            stage = getattr(error, 'stage', None)
            code = getattr(error, 'failure_code', None)
            if stage not in SAFE_STAGES or code not in SAFE_CODES:
                stage = phase
                code = {'tag_lookup':'MODEL_TAG_LOOKUP_FAILED',
                    'digest_check':'MODEL_DIGEST_MISMATCH', 'admission':'ADMISSION_CHECK_FAILED',
                    'resource_guard':'RESOURCE_ADMISSION_DENIED', 'model_request':'MODEL_REQUEST_FAILED',
                    'response_parse':'MODEL_RESPONSE_INVALID',
                    'selection_contract':'SELECTION_CONTRACT_REJECTED'}.get(stage,'MODEL_SETUP_ERROR')
            self.last_run.update(error_stage=stage, error_code=code,
                request_attempted=(attempted == 1), attempt_count=attempted)
            http_status = getattr(error, 'http_status', None)
            if type(http_status) is int and 100 <= http_status <= 599:
                self.last_run['error_http_status'] = http_status
            self.execution_verified = False
            if self.stable_swap_policy is not None:
                self.stable_swap_policy.close()
            # Read status once after an attempted request; no retry or explicit unload.
            if attempted:
                try:self.last_run['cleanup_confirmed'] = guard.get_json('/api/ps')['models'] == []
                except Exception:self.last_run['cleanup_confirmed'] = False
            raise LocalModelHeld('Granite held/stopped; see bounded diagnostic.', stage=stage,
                failure_code=code, request_attempted=(attempted == 1),
                attempt_count=attempted, http_status=http_status) from None
