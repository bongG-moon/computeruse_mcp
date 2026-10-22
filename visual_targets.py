"""Run-local recovery of a saved image target, before any input was delivered.

The connected image-capable host compares the saved reference with a fresh
window image. Its selection is an expiring, one-use observation reference;
neither this module nor the model rewrites the saved process or its thresholds.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid

from image_pixels import crop_regions, target_from_region, validate_region, pixel_digest, decode_png
from image_steps import IMAGE_MUTATIONS, execute_image_step, validate_image_step
from image_targets import match_image, png_dimensions, validate_image_target
from interaction import InteractionEngine, _same_pixels
from operations import OperationError


RECOVERABLE_CODES = frozenset({'image_not_found', 'image_ambiguous'})
REFRESH_CODES = frozenset({'target_recovery_expired', 'target_recovery_observation_changed',
                           'target_recovery_window_changed', 'target_recovery_geometry_changed',
                           'target_recovery_target_changed', 'target_recovery_scope_changed',
                           'target_recovery_context_changed', 'target_recovery_ambiguous'})
_REFERENCE_KEYS = ('recovery_id', 'run_id', 'step_index', 'step_id', 'iteration_id',
                   'observation_id', 'frame_id')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _overlap(a, b):
    return (a['x'] < b['x']+b['width'] and b['x'] < a['x']+a['width']
            and a['y'] < b['y']+b['height'] and b['y'] < a['y']+a['height'])


def _same_except_clock(left, right, regions):
    """An explicit clock exception never discards the rest of the scene."""
    a, b = decode_png(left), decode_png(right)
    if a[:4] != b[:4]:
        return False
    channels = a[3]
    for y, (before, after) in enumerate(zip(a[4], b[4])):
        if before == after:
            continue
        before, after = bytearray(before), bytearray(after)
        for region in regions:
            if region['y'] <= y < region['y']+region['height']:
                start, stop = region['x']*channels, (region['x']+region['width'])*channels
                before[start:stop] = after[start:stop]
        if before != after:
            return False
    return True


def _image_step(step):
    if not isinstance(step, dict) or step.get('operation') not in IMAGE_MUTATIONS:
        raise OperationError('입력 전 이미지 동작에서만 대상을 다시 찾을 수 있습니다.', 'invalid_target_recovery')
    return validate_image_step({key: copy.deepcopy(value) for key, value in step.items()
        if key not in {'program_id', 'window_ref', 'step_id', 'iteration_id'}})


def _context(runtime, record, step, target, *, executing=False):
    _image_step(step)
    index = record.get('pending_step')
    proof = record.get('step_delivery')
    states = {'not_sent', 'unknown'} if executing else {'not_sent'}
    if (type(index) is not int or index < 0 or not isinstance(record.get('run_id'), str)
            or not isinstance(proof, dict) or set(proof) != {'step_index', 'state'}
            or type(proof['step_index']) is not int or proof['step_index'] != index or proof['state'] not in states):
        raise OperationError('같은 단계의 입력 미전달 기록이 필요합니다. 입력을 반복하지 않습니다.',
                             'target_recovery_delivery_unproven')
    if (not isinstance(target, dict) or set(target) != {'pid', 'window_id'}
            or any(type(value) is not int or value < 1 for value in target.values())):
        raise OperationError('실행에 연결한 정확한 창이 필요합니다.', 'invalid_target_recovery')
    return {'run_id': record['run_id'], 'step_index': index,
        'step_id': step.get('step_id', 'step-'+str(index)),
        'iteration_id': step.get('iteration_id', 'single'), 'session_id': runtime.id,
        'task_id': record.get('task_id'), 'revision': record.get('revision'),
        'recipe_hash': record.get('recipe_hash'), 'inputs_hash': record.get('inputs_hash'),
        'snapshot_hash': record.get('snapshot_hash'), 'step_hash': _hash(step),
        'target_hash': _hash(target), 'saved_target_hash': _hash(step['image_target'])}


def _vision(enabled):
    if enabled is not True:
        raise OperationError('화면 이미지를 사용하는 연결에서만 현재 그림으로 대상을 확인할 수 있습니다.',
                             'vision_profile_required')


def _failed(error, *, attempted=False):
    return {'status': 'unknown' if attempted else 'blocked', 'task_verified': False,
        'input_dispatched': None if attempted else False, 'automatic_replay': False,
        'diagnostic': {'code': getattr(error, 'code', 'target_recovery_input_unknown' if attempted else 'invalid_target_recovery'),
            'message': str(error)[:1000], 'automatic_replay': False},
        'next_step': '입력 결과만 다시 관찰하세요. 같은 입력을 반복하지 마세요.' if attempted else
            '같은 실행에서 현재 화면으로 대상을 다시 확인하세요. 이 요청은 입력하지 않았습니다.'}


class WorkflowVisualTargets:
    def __init__(self, *, clock=time.monotonic, ttl=120, engine_factory=InteractionEngine):
        self.clock, self.ttl, self.engine_factory = clock, ttl, engine_factory
        self.jobs, self.lock = {}, threading.RLock()

    def close(self):
        with self.lock:
            for job in self.jobs.values(): job['engine'].close()
            self.jobs.clear()

    def _prune(self, context):
        for identity, job in list(self.jobs.items()):
            if (self.clock() > job['expires'] or all(job['context'][key] == context[key]
                    for key in ('run_id', 'step_index', 'step_id', 'iteration_id'))):
                self.jobs.pop(identity)['engine'].close()
        while len(self.jobs) >= 16:
            self.jobs.pop(next(iter(self.jobs)))['engine'].close()

    def open(self, runtime, record, step, target, *, failure, image_delivery_enabled=False):
        _vision(image_delivery_enabled)
        runtime.check_active()
        context = _context(runtime, record, step, target)
        if (not isinstance(failure, dict) or failure.get('input_dispatched') is not False
                or failure.get('diagnostic', {}).get('code') not in RECOVERABLE_CODES):
            raise OperationError('입력 전에 이미지를 찾지 못한 경우에만 현재 대상을 다시 확인합니다.',
                                 'target_recovery_delivery_unproven')
        engine = self.engine_factory(runtime, clock=self.clock, observation_ttl=self.ttl)
        try:
            binding = engine.bind(target)
            observed = engine.observe(binding['window_ref'],
                goal='저장된 이미지의 클릭 지점과 같은 역할의 현재 대상을 찾으세요. 색상만으로 다른 행을 선택하지 마세요.',
                image_delivery_enabled=True, max_controls=10, max_depth=2, max_elements=60)
            data = observed.get('structuredContent', {})
            images = [item for item in observed.get('content', []) if item.get('type') == 'image']
            if observed.get('isError') or not data.get('frame_id') or len(images) != 1:
                raise OperationError('현재 대상을 확인할 화면을 읽지 못했습니다.', 'target_recovery_capture_failed')
            saved = validate_image_target(step['image_target'])
            comparison = failure.get('diagnostic', {}).get('image_match', {})
            same_frame = comparison.get('frame_sha256') == hashlib.sha256(images[0]['data'].encode('ascii')).hexdigest()
            identity = 'target-'+uuid.uuid4().hex
            public = {**context, 'recovery_id': identity, 'window_ref': binding['window_ref'],
                'observation_id': data['observation_id'], 'frame_id': data['frame_id'],
                'image_size': data['image_size'], 'expires_in_seconds': self.ttl,
                'operation': step['operation'], 'reason': failure['diagnostic']['code'],
                'saved_target': {key: copy.deepcopy(saved[key]) for key in
                    ('width', 'height', 'anchor', 'capture_window', 'source_size') if key in saved},
                'image_roles': ['saved_target_reference', 'current_window'],
                'coordinate_space': 'current_window_image_pixels',
                'target_point_policy': 'center_of_target_region',
                'evidence_level': 'visual_assessed_target', 'task_modified': False}
            public['comparison'] = {key:copy.deepcopy(comparison[key]) for key in
                ('score','threshold','score_gap','capture_window','screenshot','frame_comparison') if key in comparison}
            public['review_candidates'] = copy.deepcopy(comparison.get('review_candidates', [])) if same_frame else []
            public['candidate_policy'] = 'review_only_not_click_coordinates'
            public['candidate_frame_matches_current'] = same_frame
            public['context_policy'] = 'whole_frame_except_explicit_clock_regions'
            public['volatile_region_policy'] = {
                'kind': 'clock', 'max_regions': 2, 'max_total_frame_fraction': .02,
                'requires_previous_condition_evidence': True,
                'forbidden': ['target', 'scope', 'condition_evidence', 'business_status', 'error_messages'],
                'does_not_prove': 'all_historical_business_conditions'}
            with self.lock:
                self._prune(context)
                self.jobs[identity] = {'engine': engine, 'runtime': runtime, 'context': context,
                    'target': dict(target), 'public': public, 'expires': self.clock()+self.ttl,
                    'window_ref': binding['window_ref'], 'verified': False}
            proof = getattr(runtime, '_workflow_visual_evidence', {}).get(record['run_id'])
            if (proof and proof['step_index'] == context['step_index']-1
                    and proof['target'] == target and proof['expires'] >= self.clock()
                    and proof['identity'] == engine.windows[binding['window_ref']]['identity']):
                self.jobs[identity]['condition_proof'] = copy.deepcopy(proof)
            return {'state': 'needs_target_review', 'task_verified': False, 'input_dispatched': False,
                'target_review': copy.deepcopy(public), 'image_content': [
                    {'type': 'image', 'mimeType': 'image/png', 'data': saved['template_png']}, *copy.deepcopy(images)],
                'next_step': '첫 이미지는 저장한 대상이며 anchor가 원래 클릭 지점입니다. 두 번째 현재 화면에서 같은 버튼·입력칸의 target_region과 식별 근거 evidence를 제출하세요. target_region의 정중앙이 실제 클릭 지점입니다. 넓은 행 전체를 target_region으로 지정하지 말고, 같은 아이콘이 여러 개면 행 이름을 포함한 scope_region으로 구분하세요. 대상이 없거나 비활성화되어 있으면 실행하지 마세요.'}
        except Exception:
            engine.close()
            raise

    def _job(self, runtime, record, step, target, identity, *, executing=False):
        if not isinstance(identity, str):
            raise OperationError('대상 확인 요청의 식별자가 필요합니다.', 'invalid_target_recovery')
        job = self.jobs.get(identity)
        if job is None or job['runtime'] is not runtime or self.clock() > job['expires']:
            raise OperationError('대상 확인 요청이 만료되었습니다. 현재 화면을 다시 읽으세요.', 'target_recovery_expired')
        if job['context'] != _context(runtime, record, step, target, executing=executing) or job['target'] != target:
            raise OperationError('작업 원본·입력값·실행 단계 또는 대상 창이 달라졌습니다.', 'target_recovery_context_mismatch')
        self._window(job)
        return job

    @staticmethod
    def _window(job):
        try: return job['engine'].target(job['window_ref'])
        except (OperationError, RuntimeError, OSError) as error:
            raise OperationError('관찰한 프로그램 또는 창이 바뀌었습니다. 현재 창을 다시 확인하세요.',
                                 'target_recovery_window_changed') from error

    def _frame(self, job):
        try:
            observed = job['engine']._observation(job['window_ref'], job['public']['observation_id'])
        except OperationError as error:
            raise OperationError('대상을 확인한 화면이 만료되었습니다.', 'target_recovery_expired') from error
        frame = observed['frame']
        if frame is None or frame['frame_id'] != job['public']['frame_id']:
            raise OperationError('확인한 화면과 현재 참조가 다릅니다.', 'target_recovery_observation_changed')
        return frame

    def _locate(self, job, current_png):
        runtime, frame = job['runtime'], self._frame(job)
        target = self._window(job)
        if (runtime.guard.image_geometry_resolver(target['window_id']) != frame['geometry']
                or png_dimensions(current_png) != tuple(frame['image_size'][key] for key in ('width', 'height'))):
            raise OperationError('관찰 뒤 창의 위치·배율·크기가 달라졌습니다.', 'target_recovery_geometry_changed')
        region, scope = job['region'], job['scope']
        selected = [scope, region] if scope is not None else [region]
        before, after = crop_regions(frame['png'], selected, runtime=runtime), crop_regions(current_png, selected, runtime=runtime)
        if not _same_pixels(before[-1], after[-1]):
            raise OperationError('관찰 뒤 클릭할 대상 그림이 바뀌었습니다.', 'target_recovery_target_changed')
        if scope is not None and not _same_pixels(before[0], after[0]):
            raise OperationError('관찰 뒤 대상을 구별할 주변 문구가 바뀌었습니다.', 'target_recovery_scope_changed')
        proof = job.get('condition_proof')
        if proof:
            if (self.clock() > proof['expires'] or proof['geometry'] != frame['geometry']
                    or [pixel_digest(png) for png in crop_regions(current_png, proof['regions'], runtime=runtime)] != proof['digests']):
                raise OperationError('이전 단계에서 확인한 업무 조건 영역이 바뀌거나 만료됐습니다.',
                                     'target_recovery_context_changed')
        # A prior checkpoint describes its own condition, not every business
        # field. Keep ALL other pixels protected, except explicitly identified
        # clocks. Never mask the original frame used for unique target matching.
        if not _same_pixels(frame['png'], current_png):
            if (proof is None or scope is None or not job.get('volatile_regions')
                    or not _same_except_clock(frame['png'], current_png, job['volatile_regions'])):
                raise OperationError('대상·구별 영역 밖 화면이 바뀌었습니다. 명시한 시계 영역 외의 업무 조건·상태 변화는 다시 확인해야 합니다.',
                                     'target_recovery_context_changed')
        # A model-selected scope is context, not proof of uniqueness. Even an
        # unchanged full frame can contain several identical empty grid rows.
        match_target = job['scope_target'] if scope is not None else job['image_target']
        expected = scope if scope is not None else region
        matched = match_image(runtime, match_target, current_png)
        if matched.get('status') != 'matched' or matched.get('rect') != expected:
            raise OperationError('현재 화면에서 같은 대상을 유일하게 구분하지 못했습니다. 고유한 행 값과 열 이름을 포함해 다시 확인하세요.',
                                 'target_recovery_ambiguous')
        self._window(job)
        if runtime.guard.image_geometry_resolver(target['window_id']) != frame['geometry']:
            raise OperationError('대상 확인 중 창 위치가 바뀌었습니다.', 'target_recovery_observation_changed')
        return {'status': 'matched', 'score': 1.0, 'candidate_count': 1, 'rect': dict(region),
            'screenshot': dict(frame['image_size']), 'x': region['x']+region['width']//2,
            'y': region['y']+region['height']//2}

    def verify(self, runtime, record, step, target, submission, *, image_delivery_enabled=False):
        _vision(image_delivery_enabled)
        runtime.check_active()
        required = {*_REFERENCE_KEYS, 'target_region', 'evidence'}
        optional = {'scope_region', 'volatile_regions', 'session_id', 'window_ref', 'step_hash', 'recipe_hash',
                    'inputs_hash', 'snapshot_hash', 'target_hash', 'saved_target_hash'}
        if not isinstance(submission, dict) or not required <= set(submission) or set(submission)-required-optional:
            raise OperationError('실행·단계·화면 식별자와 target_region, evidence를 함께 지정하세요.', 'invalid_target_recovery')
        with self.lock:
            job = self._job(runtime, record, step, target, submission['recovery_id'])
            if job['verified']:
                raise OperationError('이미 확인한 대상입니다. 같은 확인을 다시 사용하지 마세요.', 'target_recovery_expired')
            for key in (*_REFERENCE_KEYS, *(optional-{'scope_region', 'volatile_regions'})):
                if key in submission and (type(submission[key]) is not type(job['public'][key]) or submission[key] != job['public'][key]):
                    raise OperationError('다른 실행·단계·화면의 대상 확인입니다.', 'target_recovery_context_mismatch')
            evidence = submission['evidence']
            if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
                raise OperationError('현재 화면에서 대상을 구별한 문구와 상태를 설명하세요.', 'invalid_target_recovery')
            frame = self._frame(job)
            region = validate_region(submission['target_region'], **frame['image_size'], maximum=512)
            scope = submission.get('scope_region')
            if scope is not None:
                scope = validate_region(scope, **frame['image_size'])
                if (scope == region or scope['x'] > region['x'] or scope['y'] > region['y']
                        or scope['x']+scope['width'] < region['x']+region['width']
                        or scope['y']+scope['height'] < region['y']+region['height']):
                    raise OperationError('scope_region에는 대상과 구별할 주변 문구가 함께 포함되어야 합니다.', 'invalid_target_recovery')
            volatile = submission.get('volatile_regions', [])
            proof = job.get('condition_proof')
            if (not isinstance(volatile, list) or len(volatile) > 2
                    or volatile and (proof is None or scope is None)):
                raise OperationError('시계 제외는 이전 조건 증거와 고유한 구별 영역이 있을 때 최대 2개로 지정하세요.', 'invalid_target_recovery')
            clocks = []
            protected = [region, *([scope] if scope else []), *(proof or {}).get('regions', [])]
            for entry in volatile:
                if (not isinstance(entry, dict) or set(entry) != {'kind', 'region', 'evidence'}
                        or entry['kind'] != 'clock' or not isinstance(entry['evidence'], str)
                        or not entry['evidence'].strip() or len(entry['evidence']) > 1000):
                    raise OperationError('업무 상태가 아닌 시계만 kind=clock, region, evidence로 명시하세요.', 'invalid_target_recovery')
                clock_region = validate_region(entry['region'], **frame['image_size'], maximum=256)
                if any(_overlap(clock_region, item) for item in protected):
                    raise OperationError('시계 제외 영역은 대상·주변 문구·이전 조건과 겹칠 수 없습니다.', 'invalid_target_recovery')
                clocks.append(clock_region)
            if sum(item['width']*item['height'] for item in clocks) > frame['image_size']['width']*frame['image_size']['height']*.02:
                raise OperationError('시계 제외 영역 합계는 전체 화면의 2% 이하여야 합니다.', 'invalid_target_recovery')
            job.update(region=region, scope=scope, volatile_regions=copy.deepcopy(clocks),
                volatile_evidence=copy.deepcopy(volatile), image_target=target_from_region(frame['png'], region, runtime=runtime))
            if scope is not None:
                if max(scope['width'], scope['height']) > 512:
                    raise OperationError('구별 영역은 가로·세로 512픽셀 안에서 고유한 행 값과 열 이름을 포함해 지정하세요.', 'target_recovery_scope_too_large')
                job['scope_target'] = target_from_region(frame['png'], scope, runtime=runtime)
            fresh = runtime.capture_checkpoint(target)
            self._window(job)
            images = [item for item in fresh.get('content', []) if item.get('type') == 'image']
            if fresh.get('isError') or len(images) != 1 or images[0].get('mimeType') != 'image/png':
                raise OperationError('대상 확인 중 새 화면을 읽지 못했습니다.', 'target_recovery_observation_changed')
            self._locate(job, images[0]['data'])
            runtime.check_active()
            if self.clock() > job['expires']:
                raise OperationError('대상 확인 요청이 만료되었습니다.', 'target_recovery_expired')
            job.update(verified=True, evidence=evidence)
            return {'state': 'target_review_ready', 'recovery_id': submission['recovery_id'],
                'input_dispatched': False, 'task_verified': False, 'task_modified': False,
                'target_region': dict(region), 'scope_region': copy.deepcopy(scope),
                'volatile_regions': copy.deepcopy(volatile),
                'evidence_level': 'visual_assessed_target'}

    def execute(self, runtime, record, step, target, recovery_id, *, image_delivery_enabled=False,
                verification_state=None, save_verification=None):
        job, attempted = None, False
        try:
            _vision(image_delivery_enabled)
            runtime.check_active()
            with self.lock:
                job = self._job(runtime, record, step, target, recovery_id, executing=True)
                if not job['verified']:
                    raise OperationError('현재 대상의 시각 확인을 먼저 완료하세요.', 'invalid_target_recovery')
                self.jobs.pop(recovery_id)  # One use even if subsequent preparation fails.
            self._frame(job)
            operation_step = _image_step(step)
            operation_step['image_target'] = copy.deepcopy(job['image_target'])
            parent = self
            class RecoveryRuntime:
                def __getattr__(self, key): return getattr(runtime, key)
                def image_action(self, actual_step, actual_target):
                    nonlocal attempted
                    if actual_target != target:
                        raise OperationError('실행 직전 대상 창이 달라졌습니다.', 'target_recovery_window_changed')
                    parent._window(job)
                    parent._frame(job)
                    attempted = True
                    return runtime.guard.image_action(actual_step, actual_target,
                        lambda image_target, png: parent._locate(job, png), runtime.check_active)
            answer = execute_image_step(RecoveryRuntime(), operation_step, target,
                verification_state=verification_state, save_verification=save_verification, prepare_foreground=True)
            return {**answer, 'target_recovery_receipt': {'recovery_id': recovery_id,
                'evidence_level': 'visual_assessed_target', 'task_modified': False,
                'ephemeral': True}, 'automatic_replay': False}
        except Exception as error:
            return _failed(error, attempted=attempted)
        finally:
            if job is not None:
                # Failed context/vision checks must not invalidate a different
                # pending challenge. Remove only the job actually used here.
                with self.lock:
                    if self.jobs.get(recovery_id) is job: self.jobs.pop(recovery_id)
                job['engine'].close()
