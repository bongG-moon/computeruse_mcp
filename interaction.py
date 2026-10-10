"""One observation/action contract shared by conversational desktop clients.

No model is called here. UIA facts and vision-assessed completion stay distinct.
Visual regions are short-lived references to a delivered observation; they are
never accepted as arbitrary desktop coordinates or durable replay selectors.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from image_pixels import crop_png, crop_regions, pixel_digest, target_from_region, validate_region
from image_targets import match_image, png_dimensions
from image_steps import execute_image_step
from inspection import inspect_window
from operations import OperationError, Operations, validate_selector
from replay_preflight import prepare_image_foreground, validate_geometry


def _answer(data, images=()):
    return {'structuredContent': data,
            'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}, *copy.deepcopy(list(images))]}


def _same_pixels(left, right):
    return left == right or pixel_digest(left) == pixel_digest(right)


def _identity(state, window_id):
    if (not isinstance(state, dict) or state.get('process_exited') is not False
            or state.get('target_present') is not True or type(state.get('creation_time')) is not int
            or not state.get('executable')):
        raise OperationError('연결한 프로그램의 현재 창을 확인하지 못했습니다.', 'window_ref_unavailable')
    row = next((item for item in state.get('windows', []) if item.get('window_id') == window_id), None)
    if row is None or not row.get('class_name') or not row.get('thread_id'):
        raise OperationError('창의 클래스와 소유 스레드를 확인하지 못했습니다.', 'window_identity_unavailable')
    return {'creation_time': state['creation_time'], 'executable': state['executable'],
            **{key: row.get(key) for key in ('window_id', 'pid', 'class_name', 'thread_id', 'owner_window_id')}}


class InteractionEngine:
    def __init__(self, runtime, *, clock=time.monotonic, observation_ttl=120):
        self.runtime, self.clock, self.ttl = runtime, clock, observation_ttl
        self.windows, self.observations, self.latest, self.actions = {}, {}, {}, {}
        self.operations = Operations(runtime)

    def close(self):
        for binding in self.windows.values():
            binding['probe'].close()
        self.windows.clear(); self.observations.clear(); self.latest.clear(); self.actions.clear()

    def bind(self, target):
        """The caller discovers/launches apps; binding proves the current identity."""
        self.runtime.check_active()
        if (not isinstance(target, dict) or set(target) != {'pid', 'window_id'}
                or any(type(value) is not int or value < 1 for value in target.values())):
            raise OperationError('현재 프로그램의 정확한 창을 연결하세요.', 'invalid_target')
        for ref, binding in list(self.windows.items()):
            if binding['target'] == target:
                try:
                    self.target(ref)
                    return self._binding(ref)
                except (OperationError, RuntimeError, OSError):
                    binding['probe'].close()
                    self.windows.pop(ref)
        probe = self.runtime.create_transition_probe(dict(target))
        try:
            identity = _identity(probe.capture(), target['window_id'])
        except Exception:
            probe.close()
            raise
        ref = 'window-' + uuid.uuid4().hex
        self.windows[ref] = {'target': dict(target), 'identity': identity, 'probe': probe}
        while len(self.windows) > 32:
            old = next(iter(self.windows))
            self.windows.pop(old)['probe'].close()
            observation = self.latest.pop(old, None)
            self.observations.pop(observation, None)
        return self._binding(ref)

    def _binding(self, ref):
        binding = self.windows[ref]
        return {'window_ref': ref, 'target': dict(binding['target']),
                'app_instance_id': hashlib.sha256(json.dumps({key: binding['identity'][key]
                    for key in ('creation_time', 'executable')}, sort_keys=True).encode()).hexdigest()[:24]}

    def target(self, window_ref):
        self.runtime.check_active()
        binding = self.windows.get(window_ref)
        if binding is None:
            raise OperationError('이 연결에서 만든 현재 창 참조가 필요합니다.', 'unknown_window_ref')
        if _identity(binding['probe'].snapshot(), binding['target']['window_id']) != binding['identity']:
            raise OperationError('프로그램 또는 창이 교체되었습니다. 현재 창을 다시 연결하세요.', 'window_ref_changed')
        return dict(binding['target'])

    def observe(self, window_ref, *, goal='', search='', observation='auto', within=None,
                image_delivery_enabled=False, max_controls=80, max_depth=12, max_elements=600):
        target = self.target(window_ref)
        if type(image_delivery_enabled) is not bool:
            raise OperationError('이미지 전달 프로필을 확인하세요.', 'invalid_image_profile')
        # Text clients keep all local image recording/replay facilities. This
        # facade does not silently send screenshots to a text-only connection.
        wants_image = image_delivery_enabled and observation != 'uia'
        focus = None
        if wants_image:
            focus = prepare_image_foreground(self.runtime, target)
            if focus is not None:
                return _answer({**focus, **self._binding(window_ref), 'state': 'blocked', 'dispatch': 'not_sent'})
        geometry = validate_geometry(self.runtime.guard.image_geometry_resolver(target['window_id'])) if wants_image else None
        answer = inspect_window(self.runtime, target, max_controls=max_controls, max_depth=max_depth,
            max_elements=max_elements, search=search, within=within, observation='both' if wants_image else 'uia',
            requested_goal=goal)
        if answer.get('isError'):
            return answer
        self.target(window_ref)
        if geometry is not None and self.runtime.guard.image_geometry_resolver(target['window_id']) != geometry:
            raise OperationError('관찰 중 창 위치나 배율이 바뀌었습니다. 다시 읽으세요.', 'observation_geometry_changed')
        data = copy.deepcopy(answer['structuredContent'])
        images = [item for item in answer.get('content', []) if item.get('type') == 'image'] if wants_image else []
        frame = None
        if images:
            if len(images) != 1 or images[0].get('mimeType') != 'image/png':
                raise OperationError('현재 화면의 PNG 한 장이 필요합니다.', 'unsupported_observation_image')
            size = png_dimensions(images[0].get('data'))
            frame = {'frame_id': 'frame-' + uuid.uuid4().hex, 'png': images[0]['data'],
                     'image_size': {'width': size[0], 'height': size[1]}, 'geometry': geometry}
        identity = 'observation-' + uuid.uuid4().hex
        candidates = {}
        for index, control in enumerate(data['inspection']['controls']):
            if control.get('selector') is not None:
                ref = identity + ':' + str(index)
                control['target_ref'] = ref
                candidates[ref] = copy.deepcopy(control['selector'])
        previous = self.latest.get(window_ref)
        if previous is not None:
            self.observations.pop(previous, None)
        self.latest[window_ref] = identity
        self.observations[identity] = {'window_ref': window_ref, 'created': self.clock(), 'frame': frame,
                                      'candidates': candidates, 'consumed': False}
        # Bounded pixel lifetime: only the most recent observation of up to eight
        # windows is retained. Active logical identities do not retain pixels.
        while len(self.observations) > 8:
            self.observations.pop(next(iter(self.observations)))
        data.update(self._binding(window_ref), observation_id=identity, observed_at=self.clock(),
                    requested_goal=goal, state='observed', dispatch='not_sent', task_verified=False,
                    capability={'uia': True, 'image_delivery_enabled': image_delivery_enabled,
                        'visual_targeting': frame is not None, 'model_understanding_inferred': False})
        if frame:
            data.update(frame_id=frame['frame_id'], image_size=frame['image_size'],
                        physical_bounds=list(geometry[:4]),
                        coordinate_space='returned_image_pixels', transform_id=frame['frame_id'])
        data['next_action'] = ('Use a unique UIA target_ref or the observed image_region. '
            'For identical icons include scope_region containing the distinguishing row label. '
            'Never replace a requested button with its parent row. Use computer_process_editor for manual selection.')
        return _answer(data, images)

    def _observation(self, window_ref, observation_id):
        record = self.observations.get(observation_id)
        if (record is None or record['window_ref'] != window_ref or self.latest.get(window_ref) != observation_id
                or self.clock()-record['created'] > self.ttl or record['consumed']):
            raise OperationError('현재 창의 새 관찰이 필요합니다. 이전 입력은 반복하지 마세요.', 'observation_expired')
        return record

    def act(self, window_ref, step, *, target_ref=None, observation_id=None, image_region=None,
            scope_region=None, image_delivery_enabled=False, completion=None):
        """One semantic step; the returned dispatch state prevents implicit retries."""
        started = self.clock()
        target = self.target(window_ref)
        original_binding = self._binding(window_ref)
        if not isinstance(step, dict):
            raise OperationError('실행할 한 단계의 동작이 필요합니다.')
        step = copy.deepcopy(step)
        if completion is not None and (not isinstance(completion, str) or not completion.strip() or len(completion) > 2000):
            raise OperationError('화면으로 확인할 구체적인 완료 조건을 지정하세요.')
        if image_region is not None and (target_ref is not None or 'selector' in step):
            raise OperationError('한 동작에는 요소 또는 이미지 대상 하나만 지정하세요.')
        record = None
        if target_ref is not None:
            if not isinstance(target_ref, str):
                raise OperationError('현재 관찰의 대상 참조를 지정하세요.')
            observation_id = observation_id or target_ref.rsplit(':', 1)[0]
            record = self._observation(window_ref, observation_id)
            selector = record['candidates'].get(target_ref)
            if selector is None or ('selector' in step and step['selector'] != selector):
                raise OperationError('관찰한 대상과 요청한 선택 기준이 다릅니다.', 'target_ref_mismatch')
            step['selector'] = copy.deepcopy(selector)
        if image_region is not None:
            if image_delivery_enabled is not True:
                raise OperationError('화면 이해가 가능한 연결에서만 관찰 이미지 대상을 지정하세요.', 'vision_profile_required')
            record = self._observation(window_ref, observation_id)
            frame = record['frame']
            if frame is None:
                raise OperationError('실제로 전달된 화면 이미지가 없습니다.', 'image_not_observed')
            image_region = validate_region(image_region, **frame['image_size'], maximum=512)
            if scope_region is not None:
                scope_region = validate_region(scope_region, **frame['image_size'])
                if (scope_region['x'] > image_region['x'] or scope_region['y'] > image_region['y']
                        or scope_region['x']+scope_region['width'] < image_region['x']+image_region['width']
                        or scope_region['y']+scope_region['height'] < image_region['y']+image_region['height']
                        or scope_region == image_region):
                    raise OperationError('구별할 행의 문구가 포함되도록 대상보다 넓은 범위를 지정하세요.', 'invalid_image_scope')
            template = target_from_region(frame['png'], image_region)
            operation = step.pop('operation', None)
            if operation == 'set_value':
                operation = 'type_text'; step['replace_all'] = True
            if operation not in {'click', 'double_click', 'right_click', 'type_text', 'press_key', 'hotkey', 'scroll'}:
                raise OperationError('이 이미지 대상에는 클릭·입력·키·스크롤 동작을 지정하세요.')
            image_step = {**step, 'operation': 'image_'+operation, 'image_target': template}
            from image_steps import validate_image_step
            validate_image_step(image_step)
            scope_png = crop_png(frame['png'], scope_region) if scope_region else None
            def locate(image_target, current_png):
                self.target(window_ref)
                if (self.runtime.guard.image_geometry_resolver(target['window_id']) != frame['geometry']
                        or png_dimensions(current_png) != tuple(frame['image_size'][key] for key in ('width', 'height'))):
                    raise OperationError('화면 관찰 이후 창 위치나 배율이 달라졌습니다.', 'observation_geometry_changed')
                if scope_region:
                    current_scope, current_target = crop_regions(current_png, [scope_region, image_region])
                    if not _same_pixels(scope_png, current_scope):
                        raise OperationError('선택한 행 또는 주변 문구가 바뀌었습니다. 다시 관찰하세요.', 'observation_scope_changed')
                else:
                    found = match_image(self.runtime, image_target, current_png)
                    if found.get('status') != 'matched':
                        return found
                    if found.get('rect') != image_region:
                        raise OperationError('관찰한 대상의 위치가 바뀌었습니다. 다른 행을 대신 누르지 않았습니다.', 'observation_target_moved')
                    current_target = crop_png(current_png, image_region)
                if not _same_pixels(template['template_png'], current_target):
                    raise OperationError('관찰 이후 대상 그림이 바뀌었습니다. 새 화면을 읽으세요.', 'observation_target_changed')
                return {'status': 'matched', 'score': 1.0, 'candidate_count': 1,
                    'rect': dict(image_region), 'screenshot': dict(frame['image_size']),
                    'x': image_region['x']+image_region['width']//2,
                    'y': image_region['y']+image_region['height']//2}
            parent = self
            class VisualRuntime:
                def __getattr__(self, key): return getattr(parent.runtime, key)
                def image_action(self, operation_step, action_target):
                    return parent.runtime.guard.image_action(operation_step, action_target, locate, parent.runtime.check_active)
            record['consumed'] = True  # Any exception after this point requires a fresh observation.
            answer = self._execute_once(lambda: execute_image_step(VisualRuntime(), image_step, target, prepare_foreground=True))
        else:
            if scope_region is not None:
                raise OperationError('scope_region은 이미지 대상과 함께 지정하세요.')
            if 'selector' in step:
                validate_selector(step['selector'])
            if completion and not step.get('expect') and not step.get('window_transition') and step.get('operation') in {'click', 'double_click', 'right_click'}:
                step['completion_mode'] = 'human'  # Internal deferred completion; no popup is opened.
            from operations import validate_step
            validate_step(step)
            if record is not None:
                record['consumed'] = True
            answer = self._execute_once(lambda: self.operations.execute(step, target, delivery_mode='foreground'))
        result = self._action_result(answer, window_ref, started, original_binding)
        if (result['task_verified'] is not True and result['dispatch'] != 'not_sent' and completion
                and result.get('binding_current') is not False):
            try:
                result = self.request_review(window_ref, completion, result)
            except Exception as exc:
                # A dialog may close or replace itself after a successful
                # click. Losing the review target does not erase its receipt.
                prior = result.pop('diagnostic', None)
                result.update(status='unknown', state='uncertain', task_verified=False,
                    verification_deferred=False, expected_condition=completion,
                    diagnostic={'code': getattr(exc, 'code', 'post_action_observation_unavailable'),
                        'stage': 'post_action_review', 'message': str(exc)[:1000], 'automatic_replay': False},
                    next_tool='computer_open', next_action='rebind_and_observe',
                    next_step='입력 후 대상 창이 바뀌어 결과 확인을 준비하지 못했습니다. 현재 창을 다시 연결해 결과만 관찰하세요. 앞선 입력은 반복하지 마세요.',
                    automatic_replay=False)
                if prior: result['prior_action_diagnostic'] = prior
        return result

    def request_review(self, window_ref, condition, action_result=None):
        """Internal shared review primitive; does not claim an input was sent."""
        self.target(window_ref)
        if not isinstance(condition, str) or not condition.strip() or len(condition) > 2000:
            raise OperationError('화면에서 확인할 구체적인 완료 조건을 지정하세요.')
        result = copy.deepcopy(action_result) if action_result is not None else {
            'task_verified': False, 'input_dispatched': False, 'dispatch': 'not_sent',
            'evidence_level': 'unverified', 'automatic_replay': False}
        action_id = 'action-'+uuid.uuid4().hex
        self.actions[action_id] = {'window_ref': window_ref, 'created': self.clock(),
            'condition': condition, 'reviewed': False, 'result': copy.deepcopy(result)}
        while len(self.actions) > 32:
            self.actions.pop(next(iter(self.actions)))
        result.update(state='needs_observation_review', action_id=action_id, expected_condition=condition,
            next_action='observe', review_contract={'action_id': action_id, 'window_ref': window_ref,
                'required': ['observation_id', 'frame_id', 'verdict', 'evidence'],
                'input_will_not_be_replayed': True, 'evidence_level': 'visual_assessed'})
        return result

    @staticmethod
    def _execute_once(callback):
        try:
            return callback()
        except Exception as error:
            # Cancellation or transport loss can happen after the final native
            # input but before its response. A thrown exception never proves
            # non-delivery; callers must observe the result, not resend it.
            return {'status': 'unknown', 'task_verified': False, 'input_dispatched': None,
                'diagnostic': {'code': getattr(error, 'code', 'interaction_interrupted'),
                    'message': str(error)[:1000], 'automatic_replay': False}}

    def _action_result(self, answer, window_ref, started, original_binding=None):
        answer = copy.deepcopy(answer)
        dispatched = answer.get('input_dispatched')
        if dispatched is False: dispatch = 'not_sent'
        elif answer.get('task_verified') is True or answer.get('verification_deferred') is True: dispatch = 'sent'
        else: dispatch = 'unknown'
        verified = answer.get('task_verified') is True
        binding = original_binding or {'window_ref': window_ref}
        try:
            binding = self._binding(window_ref)
            changed_target = answer.get('target')
            if (verified and isinstance(changed_target, dict) and changed_target != binding['target']
                    and answer.get('transition', {}).get('state') == 'verified'):
                binding = self.bind(changed_target)
        except Exception as exc:
            # The operation receipt exists already. A replacement dialog may
            # disappear, or cancellation may clear bindings, before the new
            # reference is installed. Neither proves input was not delivered.
            prior = answer.pop('diagnostic', None)
            answer.update(status='unknown', task_verified=False, verification_deferred=False,
                binding_current=False, prior_action_verification={'task_verified': verified},
                diagnostic={'code': getattr(exc, 'code', 'post_action_binding_unavailable'),
                    'stage': 'post_action_rebind', 'message': str(exc)[:1000], 'automatic_replay': False},
                next_tool='computer_open', next_action='rebind_and_observe',
                next_step='입력 뒤 새 창 연결을 유지하지 못했습니다. 현재 창을 다시 연결해 결과만 관찰하세요. 앞선 입력은 반복하지 마세요.')
            if prior: answer['prior_action_diagnostic'] = prior
            verified = False
        answer.update(state='completed' if verified else 'blocked' if dispatch == 'not_sent' else 'uncertain',
            dispatch=dispatch, evidence_level='deterministic' if verified else 'unverified',
            current_binding=binding, automatic_replay=False,
            elapsed_ms=round((self.clock()-started)*1000, 2))
        return answer

    def review(self, window_ref, *, action_id, observation_id, frame_id, verdict, evidence, evidence_regions=None):
        """Accept an explicitly bound client visual assessment, never input."""
        target = self.target(window_ref)
        action = self.actions.get(action_id)
        if (action is None or action['window_ref'] != window_ref or action['reviewed']
                or self.clock()-action['created'] > self.ttl):
            raise OperationError('현재 실행의 확인 요청이 만료되었거나 이미 처리됐습니다.', 'review_expired')
        observation = self._observation(window_ref, observation_id)
        frame = observation['frame']
        if (frame is None or frame['frame_id'] != frame_id or observation['created'] < action['created']):
            raise OperationError('입력 후에 새로 관찰한 화면으로 확인하세요.', 'review_observation_mismatch')
        if verdict not in {'pass', 'fail', 'uncertain'} or not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 4000:
            raise OperationError('pass/fail/uncertain과 화면에서 확인한 문구·영역·상태를 설명하세요.')
        if evidence_regions is not None:
            if not isinstance(evidence_regions, list) or not 1 <= len(evidence_regions) <= 8:
                raise OperationError('완료를 확인한 이미지 영역은 1~8개로 지정하세요.')
            evidence_regions = [validate_region(region, **frame['image_size']) for region in evidence_regions]
        if self.runtime.guard.image_geometry_resolver(target['window_id']) != frame['geometry']:
            raise OperationError('확인할 화면의 위치가 바뀌었습니다. 다시 관찰하세요.', 'review_observation_changed')
        fresh = self.runtime.capture_checkpoint(target)
        images = [item for item in fresh.get('content', []) if item.get('type') == 'image']
        self.target(window_ref)
        usable = (not fresh.get('isError') and len(images) == 1 and images[0].get('mimeType') == 'image/png'
                  and png_dimensions(images[0].get('data')) == tuple(frame['image_size'][key] for key in ('width', 'height')))
        if usable:
            usable = (all(_same_pixels(before, after) for before, after in
                          zip(crop_regions(frame['png'], evidence_regions), crop_regions(images[0]['data'], evidence_regions)))
                      if evidence_regions else _same_pixels(frame['png'], images[0]['data']))
        if not usable:
            raise OperationError('확인 중 화면 내용이 바뀌었습니다. 새 관찰로 결과만 다시 확인하세요.', 'review_observation_changed')
        action['reviewed'] = True
        observation['consumed'] = True
        prior = copy.deepcopy(action['result'])
        previous_diagnostic = prior.pop('diagnostic', None)
        prior.pop('next_step', None)
        prior.pop('next_tool', None)
        return {**prior, 'status': 'verified_visual' if verdict == 'pass' else 'visual_review_failed' if verdict == 'fail' else 'visual_review_uncertain',
            'state': 'completed' if verdict == 'pass' else 'blocked' if verdict == 'fail' else 'uncertain',
            'verification_deferred': False,
            **({'prior_action_diagnostic': previous_diagnostic} if previous_diagnostic else {}),
            'task_verified': verdict == 'pass', 'evidence_level': 'visual_assessed', 'action_id': action_id,
            'review': {'verdict': verdict, 'evidence': evidence, 'expected_condition': action['condition'],
                'observation_id': observation_id, 'frame_id': frame_id, 'evidence_regions': evidence_regions,
                'assessor': 'connected_client_model'},
            'input_replayed': False, 'review_input_dispatched': False, 'automatic_replay': False}
