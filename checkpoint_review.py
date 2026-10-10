"""Local screenshot review for text-only MCP clients. Never executes a step."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
import uuid

from settings import atomic_json

HELPER_NAME = 'Computer Use MCP 화면 확인.exe'


class CheckpointReviews:
    def __init__(self, *, process_factory=subprocess.Popen, clock=time.monotonic):
        self.process_factory, self.clock = process_factory, clock
        self.lock, self.jobs = threading.RLock(), {}

    def open(self, runtime, record, images, arguments):
        with self.lock:
            for old in [k for k, j in self.jobs.items() if j['status'] not in {'starting', 'pending'}][:-39]:
                del self.jobs[old]
            key = (getattr(runtime, 'id', None), record.get('run_id'), record.get('checkpoint', {}).get('id'))
            for identity, job in self.jobs.items():
                if job['key'] == key:
                    return self.status(identity, runtime)
            identity = uuid.uuid4().hex
            helper = Path(__file__).with_name(HELPER_NAME)
            try:
                runtime.check_active()
                if not all(isinstance(k, str) and k for k in key) or len(images) != 1:
                    raise ValueError('확인할 실행 단계와 이미지가 정확하지 않습니다.')
                if not helper.is_file():
                    raise ValueError('화면 확인 도우미가 없습니다. 같은 버전의 ZIP 전체를 설치하세요.')
                image = images[0]
                if image.get('mimeType') != 'image/png' or len(image.get('data', '')) > 24_000_000:
                    raise ValueError('확인 이미지 형식을 지원하지 않습니다.')
                pixels = base64.b64decode(image['data'], validate=True)
                if not pixels.startswith(b'\x89PNG\r\n\x1a\n') or len(pixels) < 24:
                    raise ValueError('화면 이미지가 올바르지 않습니다.')
                width, height = int.from_bytes(pixels[16:20], 'big'), int.from_bytes(pixels[20:24], 'big')
                if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 20_000_000:
                    raise ValueError('확인 이미지 크기가 올바르지 않습니다.')
                folder = Path(runtime.run_dir) / ('checkpoint-review-' + identity)
                folder.mkdir(parents=True, exist_ok=False)
                nonce = secrets.token_hex(32)
                (folder / 'screen.png').write_bytes(pixels)
                request = {'nonce': nonce, 'review_id': identity,
                           'message': str(record['checkpoint'].get('message', '작업 결과를 확인하세요.'))[:2000]}
                atomic_json(folder / 'request.json', request)
                child = self.process_factory([str(helper), str(folder / 'request.json')], shell=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                job = {'key': key, 'runtime': runtime, 'folder': folder, 'nonce': nonce, 'process': child,
                       'expires': self.clock() + 600, 'startup_expires': self.clock() + 3, 'status': 'starting',
                       'arguments': {k: copy.deepcopy(arguments[k]) for k in ('task_id', 'targets', 'inputs', 'delivery_mode', 'execution_mode') if k in arguments}}
                job['arguments'].update(resume_run_id=key[1], acknowledge_checkpoint=key[2])
                self.jobs[identity] = job
                # A created process is not proof of a visible review window.
                deadline = self.clock() + 3
                while self.clock() < deadline and child.poll() is None and not (folder / 'ready.json').is_file():
                    time.sleep(.05)
                return self.status(identity, runtime)
            except (OSError, ValueError, RuntimeError) as exc:
                if identity not in self.jobs and 'folder' in locals():
                    self._remove_pixels({'folder': folder})
                return {'status': 'unavailable', 'review_visible': False, 'human_reviewed': False,
                        'diagnostic': {'code': 'checkpoint_review_unavailable', 'source': 'mcp', 'stage': 'local_review', 'message': str(exc)},
                        'next_step': '확인 창을 열지 못했습니다. 단계가 완료됐다고 판단하거나 승인하지 마세요. 도우미 파일과 세션 상태를 확인하세요.'}

    def _remove_pixels(self, job):
        for name in ('screen.png', 'request.json', 'ready.json', 'result.json'):
            try:
                (job['folder'] / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _cancel(self, job):
        try:
            (job['folder'] / 'cancel.flag').touch()
        except OSError:
            pass
        job['status'] = 'cancelled'

    def _read(self, path, job):
        if not path.is_file():
            return None
        if path.stat().st_size > 32768:
            raise ValueError('확인 창 응답이 너무 큽니다.')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or value.get('nonce') != job['nonce']
                or value.get('review_id') != job['folder'].name.removeprefix('checkpoint-review-')):
            raise ValueError('확인 창 응답이 현재 요청과 다릅니다.')
        return value

    def status(self, identity, runtime):
        with self.lock:
            job = self.jobs.get(identity)
            if not job:
                return {'status': 'unknown', 'human_reviewed': False, 'review_visible': False}
            active = job['runtime'] is runtime and getattr(runtime, 'id', None) == job['key'][0] and getattr(runtime, 'state', None) == 'active'
            if active:
                try:
                    runtime.check_active()
                except (RuntimeError, ValueError, OSError):
                    active = False
            if not active or self.clock() >= job['expires']:
                self._cancel(job)
            if job['status'] in {'starting', 'pending'}:
                try:
                    decision = self._read(job['folder'] / 'result.json', job)
                    ready = self._read(job['folder'] / 'ready.json', job)
                    if decision is not None:
                        if decision.get('status') not in {'confirmed', 'rejected', 'cancelled'}:
                            raise ValueError('알 수 없는 확인 응답입니다.')
                        job['status'] = decision['status']
                    elif job['process'].poll() is not None:
                        job['status'] = 'cancelled'
                    elif ready and ready.get('visible') is True:
                        job['status'] = 'pending'
                    elif self.clock() >= job['startup_expires']:
                        self._cancel(job)
                        job['status'] = 'unavailable'
                        job['diagnostic'] = {'code': 'checkpoint_review_not_visible', 'source': 'mcp', 'stage': 'local_review_startup'}
                except (ValueError, OSError):
                    self._cancel(job)
            state = job['status']
            value = {'status': state, 'review_id': identity, 'review_visible': state == 'pending',
                     'run_id': job['key'][1], 'checkpoint_id': job['key'][2],
                     'human_reviewed': state == 'confirmed', 'model_image_verified': False,
                     'task_verified': False, 'input_dispatched': False, 'automatic_acknowledgement': False}
            if job.get('diagnostic'):
                value['diagnostic'] = dict(job['diagnostic'])
            if state == 'confirmed':
                value.update(next_tool='computer_run_task', next_arguments=copy.deepcopy(job['arguments']),
                             message='사용자가 이 단계의 화면을 확인했습니다. 반환된 같은 실행의 인자로 이어갈 수 있습니다. 자동 실행하지 않았습니다.')
            elif state in {'starting', 'pending'}:
                value.update(next_tool='computer_review_checkpoint', next_arguments={'review_id': identity},
                             message='이 PC의 화면 확인 창에서 결과를 확인하고 확인 완료 또는 결과 다름을 선택하세요. 모델은 이미지를 읽지 않았습니다.')
            else:
                value.update(message='확인이 취소되었거나 결과가 다릅니다. 이 단계를 승인하거나 입력을 반복하지 말고 현재 상태를 확인하세요.')
                if state == 'unavailable':
                    value['message'] = '화면 확인 창이 제한 시간 안에 나타나지 않았습니다. 사용자의 응답을 기다리는 상태가 아닙니다. 도우미 실행 상태를 확인하세요.'
            if state not in {'starting', 'pending'}:
                self._remove_pixels(job)
            return value

    def stop(self, runtime=None):
        with self.lock:
            for job in self.jobs.values():
                if runtime is None or job['runtime'] is runtime:
                    self._cancel(job)
                    child = job['process']
                    if child.poll() is None:
                        try:
                            child.wait(timeout=.6)
                        except subprocess.TimeoutExpired:
                            # Only this exact helper child, never the business
                            # process or another user's process by name/PID.
                            try:
                                child.terminate()
                                child.wait(timeout=.6)
                            except (subprocess.TimeoutExpired, OSError):
                                pass
                        except OSError:
                            pass
                    self._remove_pixels(job)

    def acknowledgement(self, runtime, run_id, checkpoint_id):
        with self.lock:
            key = (getattr(runtime, 'id', None), run_id, checkpoint_id)
            for identity, job in self.jobs.items():
                if job['key'] == key:
                    return self.status(identity, runtime)
            return {'status': 'unavailable', 'human_reviewed': False, 'input_dispatched': False,
                    'message': '이 단계의 로컬 화면 확인 기록이 없습니다. 승인 ID 없이 같은 실행을 이어가 확인 화면을 다시 여세요.'}
