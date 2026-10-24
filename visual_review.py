"""Bounded client visual assessment for explicit saved visual checkpoints.

Human checkpoints are never upgraded by this service. A new challenge can only
read the current window; opening, rejecting or refreshing it cannot replay input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from interaction import InteractionEngine
from operations import OperationError


def _condition_id(step):
    return hashlib.sha256(json.dumps({'message': step.get('message'), 'review_mode': step.get('review_mode')},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _context(runtime, record, step):
    if step.get('operation') != 'checkpoint' or step.get('review_mode') != 'visual':
        raise OperationError('명시적인 시각 확인 단계에서만 모델 판정을 받을 수 있습니다.', 'visual_review_not_enabled')
    index = record.get('pending_step')
    if type(index) is not int or index < 0 or not isinstance(record.get('run_id'), str):
        raise OperationError('현재 대기 중인 실행 단계가 필요합니다.', 'visual_review_context_invalid')
    return {'run_id': record['run_id'], 'step_index': index,
        'step_id': step.get('step_id', 'step-'+str(index)),
        'iteration_id': step.get('iteration_id', 'single'),
        'expected_condition_id': _condition_id(step), 'session_id': runtime.id}


def _capture_target(runtime, step, target):
    """Let a declared popup return settle; never select an ambiguous candidate.

    The budget limits new native probes, not an in-progress Win32 call. No
    activation, input, automatic verdict or permission expansion occurs here.
    """
    if step.get('window_ref', 'main') != 'main':
        return target, {}
    from transient_popup import current_popup
    started = time.monotonic()
    attempts = 0
    returning = isinstance(step.get('return_from'), str) and bool(step['return_from'])
    last_reason = None
    while True:
        runtime.check_active()
        attempts += 1
        try:
            selected = current_popup(target, runtime) or target
            return selected, {'attempts': attempts, 'elapsed_ms': round((time.monotonic()-started)*1000, 2),
                'last_reason': last_reason, 'input_replayed': False,
                'retry_budget_ms': 1000 if returning else 0, 'individual_native_timeout_enforced': False}
        except OperationError as error:
            if error.code != 'popup_observation_ambiguous' or not returning:
                raise
            last_reason = error.code
            remaining = 1.0-(time.monotonic()-started)
            if attempts >= 3 or remaining <= 0:
                error.review_diagnostic = {'reason': error.code, 'automatic_replay': False,
                    'transition_observation': {'attempts': attempts,
                        'elapsed_ms': round((time.monotonic()-started)*1000, 2),
                        'retry_budget_ms': 1000, 'individual_native_timeout_enforced': False,
                        'input_replayed': False}}
                raise
            runtime.check_active()
            time.sleep(min(.05*attempts, remaining))


class WorkflowVisualReviews:
    def __init__(self, *, clock=time.monotonic, ttl=120, engine_factory=InteractionEngine):
        self.clock, self.ttl, self.engine_factory = clock, ttl, engine_factory
        self.jobs = {}

    def close(self):
        for job in self.jobs.values(): job['engine'].close()
        self.jobs.clear()

    def _prune(self, context):
        for identity, job in list(self.jobs.items()):
            if (self.clock() > job['expires'] or
                    all(job['context'][key] == context[key] for key in ('run_id', 'step_id', 'iteration_id'))):
                job['engine'].close(); self.jobs.pop(identity)
        while len(self.jobs) >= 16:
            self.jobs.pop(next(iter(self.jobs)))['engine'].close()

    def _response(self, job, *, reused=False):
        return {'state': 'needs_observation_review', 'task_verified': False, 'input_dispatched': False,
            'visual_review': copy.deepcopy(job['public']),
            'observation_review_template': copy.deepcopy(job['public']),
            'image_content': copy.deepcopy(job['images']),
            'review_guidance': {
                'template_usage': 'observation_review_template의 식별자는 그대로 두고 verdict, evidence, 필요한 evidence_regions만 추가하세요.',
                'evidence_regions': '완료 조건의 실제 값·결과 및 대상을 구별할 문맥을 모두 포함하세요. 시계처럼 무관한 영역은 제외할 수 있지만, 관련 업무 조건을 빼거나 임의로 통과시키면 안 됩니다. 미지정 시 전체 화면을 비교합니다.',
                'maximum_regions': 8, 'automatic_replay': False},
            'review_metrics': {'initial_observation_ms': job['observation_ms'],
                'request_reused': reused, 'new_capture_performed': not reused,
                'remaining_seconds': round(max(0, job['expires']-self.clock()), 3),
                'transition_observation': copy.deepcopy(job.get('transition_observation', {}))},
            'next_step': ('기존 확인 요청과 이미지를 다시 제시합니다. 새 캡처나 TTL 연장은 하지 않았습니다. '
                          if reused else '') +
                         '현재 이미지를 보고 명시된 완료 조건만 판정하세요. 같은 run_id로 observation_review와 함께 이어가며 입력은 반복하지 않습니다.'}

    def current_request(self, runtime, record, step, target):
        """Reissue the exact pending challenge, without recapture or TTL renewal.

        Invalid client IDs must not manufacture a new observation. A later
        verify still checks identity, geometry and fresh approved pixels.
        """
        runtime.check_active()
        context = _context(runtime, record, step)
        matches = [job for job in self.jobs.values() if job['runtime'] is runtime
                   and job['context'] == context and job['target'] == target
                   and self.clock() <= job['expires']]
        if len(matches) != 1:
            return None
        job = matches[0]
        job['engine'].target(job['owner_window_ref'])
        job['engine'].target(job['window_ref'])
        # This also enforces the original observation's expiry/consumption. It
        # does not bless changed pixels or establish a replacement window.
        job['engine']._observation(job['window_ref'], job['public']['observation_id'])
        return self._response(job, reused=True)

    def open(self, runtime, record, step, target):
        runtime.check_active()
        context = _context(runtime, record, step)
        self._prune(context)
        started = self.clock()
        engine = self.engine_factory(runtime, clock=self.clock, observation_ttl=self.ttl)
        try:
            from transient_popup import popup_proof
            owner_binding = engine.bind(target)
            capture_target, transition_observation = _capture_target(runtime, step, target)
            binding = engine.bind(capture_target)
            capture_proof = popup_proof(capture_target) if capture_target != target else None
            request = engine.request_review(binding['window_ref'], step['message'])
            observed = engine.observe(binding['window_ref'], goal=step['message'],
                image_delivery_enabled=True, max_controls=30, max_depth=2, max_elements=100,
                preserve_foreground=True, observation='visual')
            data = observed.get('structuredContent', {})
            images = [item for item in observed.get('content', []) if item.get('type') == 'image']
            if observed.get('isError') or not data.get('frame_id') or len(images) != 1:
                error = OperationError('완료 확인에 사용할 현재 화면을 읽지 못했습니다.', 'visual_review_capture_failed')
                error.review_diagnostic = {'reason': 'capture_failed', 'automatic_replay': False,
                    'source_diagnostic': copy.deepcopy(data.get('diagnostic') or data.get('inspection', {}).get('image_capture', {})),
                    'initial_observation_ms': round(max(0, self.clock()-started)*1000, 2)}
                raise error
            identity = 'review-'+uuid.uuid4().hex
            public = {**context, 'review_id': identity, 'observation_id': data['observation_id'],
                'frame_id': data['frame_id'], 'window_ref': binding['window_ref'],
                'expected_condition': step['message'], 'expires_in_seconds': self.ttl,
                'image_size': data['image_size'], 'evidence_level': 'visual_assessed'}
            self.jobs[identity] = {'engine': engine, 'runtime': runtime, 'context': context,
                'target': dict(target), 'capture_target': dict(capture_target), 'public': public, 'request': request,
                'owner_window_ref': owner_binding['window_ref'], 'capture_proof': capture_proof,
                'transition_observation': transition_observation,
                'expires': started+self.ttl, 'window_ref': binding['window_ref'],
                'images': copy.deepcopy(images), 'observation_ms': round(max(0, self.clock()-started)*1000, 2)}
            return self._response(self.jobs[identity])
        except Exception:
            engine.close()
            raise

    def verify(self, runtime, record, step, target, submission):
        if not isinstance(submission, dict):
            raise OperationError('시각 확인 요청의 식별자와 관찰 근거가 필요합니다.', 'invalid_visual_review')
        required = {'review_id', 'run_id', 'step_index', 'step_id', 'iteration_id', 'expected_condition_id',
                    'observation_id', 'frame_id', 'verdict', 'evidence'}
        metadata = {'session_id', 'window_ref', 'expected_condition', 'expires_in_seconds',
                    'image_size', 'evidence_level'}
        if not required <= set(submission) or set(submission)-required-metadata-{'evidence_regions'}:
            raise OperationError('시각 확인 응답의 실행·단계·화면 정보를 모두 전달하세요.', 'invalid_visual_review')
        context = _context(runtime, record, step)
        job = self.jobs.get(submission['review_id'])
        if job is None or job['runtime'] is not runtime or self.clock() > job['expires']:
            raise OperationError('시각 확인 요청이 만료되었습니다. 같은 실행에서 화면만 다시 읽으세요.', 'visual_review_expired')
        if context != job['context'] or target != job['target']:
            raise OperationError('시각 확인의 실행·반복 회차·대상 창이 달라졌습니다.', 'visual_review_context_mismatch')
        for key in required-{'verdict', 'evidence'}:
            if type(submission[key]) is not type(job['public'][key]) or submission[key] != job['public'][key]:
                raise OperationError('다른 실행·단계·완료 조건·화면의 판정입니다.', 'visual_review_context_mismatch')
        for key in metadata:
            if key in submission and (type(submission[key]) is not type(job['public'][key])
                                      or submission[key] != job['public'][key]):
                raise OperationError('확인 요청의 연결 정보가 다릅니다.', 'visual_review_context_mismatch')
        if job.get('capture_target', target) != target:
            from transient_popup import register_popup, popup_diagnostic
            # Each same-run request creates a fresh native-popup scope. Rebuild
            # its authority from current native evidence, accepting only the
            # identity/owner path captured for this exact review challenge.
            job['engine'].target(job['owner_window_ref'])
            job['engine'].target(job['window_ref'])
            if not register_popup(job['capture_target'], target, expected_proof=job['capture_proof']):
                cause = popup_diagnostic(job['capture_target'])
                raise OperationError('확인하던 팝업을 다시 증명하지 못했습니다: '+cause['code'], 'review_observation_changed')
        result = job['engine'].review(job['window_ref'], action_id=job['request']['action_id'],
            observation_id=submission['observation_id'], frame_id=submission['frame_id'],
            verdict=submission['verdict'], evidence=submission['evidence'], evidence_regions=submission.get('evidence_regions'))
        # Retain only condition-region hashes, never a full persisted screen.
        # This is usable by the immediately following step of this exact run.
        if (result.get('task_verified') is True and submission.get('evidence_regions')
                and job.get('capture_target', target) == target):
            from image_pixels import crop_regions, pixel_digest
            frame = job['engine'].observations[submission['observation_id']]['frame']
            regions = copy.deepcopy(submission['evidence_regions'])
            proofs = dict(getattr(runtime, '_workflow_visual_evidence', {}))
            proofs[record['run_id']] = {
                'step_index': context['step_index'], 'target': dict(target),
                'identity': copy.deepcopy(job['engine'].windows[job['window_ref']]['identity']),
                'geometry': frame['geometry'], 'regions': regions,
                'digests': [pixel_digest(png) for png in crop_regions(frame['png'], regions, runtime=runtime)],
                'expires': self.clock() + self.ttl}
            while len(proofs) > 16: proofs.pop(next(iter(proofs)))
            runtime._workflow_visual_evidence = proofs
        job['engine'].close(); self.jobs.pop(submission['review_id'])
        return {**result, 'visual_review': {**context, 'review_id': submission['review_id'],
                'verdict': submission['verdict'], 'evidence': submission['evidence'],
                'evidence_regions': copy.deepcopy(submission.get('evidence_regions'))},
                'review_state': {'pass': 'passed', 'fail': 'failed', 'uncertain': 'uncertain'}[submission['verdict']],
                'human_reviewed': False, 'model_image_verified': submission['verdict'] == 'pass'}
