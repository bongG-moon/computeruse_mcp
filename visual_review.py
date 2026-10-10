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

    def open(self, runtime, record, step, target):
        runtime.check_active()
        context = _context(runtime, record, step)
        self._prune(context)
        engine = self.engine_factory(runtime, clock=self.clock, observation_ttl=self.ttl)
        try:
            binding = engine.bind(target)
            request = engine.request_review(binding['window_ref'], step['message'])
            observed = engine.observe(binding['window_ref'], goal=step['message'],
                image_delivery_enabled=True, max_controls=30, max_depth=2, max_elements=100)
            data = observed.get('structuredContent', {})
            images = [item for item in observed.get('content', []) if item.get('type') == 'image']
            if observed.get('isError') or not data.get('frame_id') or len(images) != 1:
                raise OperationError('완료 확인에 사용할 현재 화면을 읽지 못했습니다.', 'visual_review_capture_failed')
            identity = 'review-'+uuid.uuid4().hex
            public = {**context, 'review_id': identity, 'observation_id': data['observation_id'],
                'frame_id': data['frame_id'], 'window_ref': binding['window_ref'],
                'expected_condition': step['message'], 'expires_in_seconds': self.ttl,
                'image_size': data['image_size'], 'evidence_level': 'visual_assessed'}
            self.jobs[identity] = {'engine': engine, 'runtime': runtime, 'context': context,
                'target': dict(target), 'public': public, 'request': request,
                'expires': self.clock()+self.ttl, 'window_ref': binding['window_ref']}
            return {'state': 'needs_observation_review', 'task_verified': False, 'input_dispatched': False,
                'visual_review': copy.deepcopy(public), 'image_content': copy.deepcopy(images),
                'next_step': '현재 이미지를 보고 명시된 완료 조건만 판정하세요. 같은 run_id로 observation_review와 함께 이어가며 입력은 반복하지 않습니다.'}
        except Exception:
            engine.close()
            raise

    def verify(self, runtime, record, step, target, submission):
        if not isinstance(submission, dict):
            raise OperationError('시각 확인 요청의 식별자와 관찰 근거가 필요합니다.', 'invalid_visual_review')
        required = {'review_id', 'run_id', 'step_index', 'step_id', 'iteration_id', 'expected_condition_id',
                    'observation_id', 'frame_id', 'verdict', 'evidence'}
        if not required <= set(submission) or set(submission)-required-{'evidence_regions', 'session_id', 'window_ref'}:
            raise OperationError('시각 확인 응답의 실행·단계·화면 정보를 모두 전달하세요.', 'invalid_visual_review')
        context = _context(runtime, record, step)
        job = self.jobs.get(submission['review_id'])
        if job is None or job['runtime'] is not runtime or self.clock() > job['expires']:
            raise OperationError('시각 확인 요청이 만료되었습니다. 같은 실행에서 화면만 다시 읽으세요.', 'visual_review_expired')
        if context != job['context'] or target != job['target']:
            raise OperationError('시각 확인의 실행·반복 회차·대상 창이 달라졌습니다.', 'visual_review_context_mismatch')
        for key in required-{'verdict', 'evidence'}:
            if submission[key] != job['public'][key]:
                raise OperationError('다른 실행·단계·완료 조건·화면의 판정입니다.', 'visual_review_context_mismatch')
        for key in ('session_id', 'window_ref'):
            if key in submission and submission[key] != job['public'][key]:
                raise OperationError('확인 요청의 연결 정보가 다릅니다.', 'visual_review_context_mismatch')
        result = job['engine'].review(job['window_ref'], action_id=job['request']['action_id'],
            observation_id=submission['observation_id'], frame_id=submission['frame_id'],
            verdict=submission['verdict'], evidence=submission['evidence'], evidence_regions=submission.get('evidence_regions'))
        job['engine'].close(); self.jobs.pop(submission['review_id'])
        return {**result, 'visual_review': {**context, 'review_id': submission['review_id'],
                'verdict': submission['verdict'], 'evidence': submission['evidence'],
                'evidence_regions': copy.deepcopy(submission.get('evidence_regions'))},
                'review_state': {'pass': 'passed', 'fail': 'failed', 'uncertain': 'uncertain'}[submission['verdict']],
                'human_reviewed': False, 'model_image_verified': submission['verdict'] == 'pass'}
