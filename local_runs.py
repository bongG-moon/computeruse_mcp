"""Local input form and frozen saved-task execution; no data in MCP replies."""
import copy
from pathlib import Path
import subprocess
import threading
import time
import uuid

from operations import OperationError
from local_delivery import require_local_task, receipt
from task_inputs import resolve_values
from task_revision import TaskRevisions


class LocalRuns:
    MAX_JOBS = 32
    INPUT_TIMEOUT = 600
    HUMAN_TIMEOUT = 600
    POLL_SECONDS = .2
    MAX_CONTINUATIONS = 64

    def __init__(self, manager):
        self.manager = manager
        self.jobs = {}
        self.lock = threading.RLock()

    def _receipt(self, value):
        aliases = getattr(self.manager, 'task_ids', None)
        return receipt(value, aliases.alias if aliases is not None else None)

    def start(self, task_id, revision=None):
        task = TaskRevisions(self.manager.tasks).get(task_id, revision)
        require_local_task(task)
        from workflows import require_runnable
        require_runnable(task)
        return self._new_job(task, self._work)

    def _new_job(self, task, worker, **fields):
        with self.lock:
            if any(not job['done'].is_set() or job.get('cleanup_pending') for job in self.jobs.values()):
                raise OperationError('로컬 입력 또는 실행을 먼저 마치세요.', 'local_run_busy')
            for old in self.jobs.values():
                if old.get('helper_role') == 'result' and not self._cleanup_form(old):
                    raise OperationError('이전 결과창 종료를 확인하지 못했습니다.', 'local_input_cleanup_pending')
            while len(self.jobs) >= self.MAX_JOBS:
                self.jobs.pop(next(iter(self.jobs)))
            key = uuid.uuid4().hex
            job = {'id': key, 'task': copy.deepcopy(task), 'cancel': threading.Event(),
                   'done': threading.Event(), 'result': {'status': 'awaiting_input', **({'task_id': task['id']} if task.get('id') else {})}, **fields}
            self.jobs[key] = job
            self._start_worker(job, worker)
            return self.status(key)

    def open_editor(self, task_id=None, name=None):
        """Choose registered apps locally when chat cannot read their names."""
        if task_id is not None:
            task = TaskRevisions(self.manager.tasks).get(task_id)
            return self._new_job(task, self._open_editor_work, kind='editor',
                                 editor_args={'program_ids': list(task['program_ids']), 'task_id': task_id,
                                              **({'name': name} if name is not None else {})})
        programs = [program for program in self.manager.config.get('programs', []) if program.get('enabled') is True]
        if not programs:
            raise OperationError('등록되어 사용할 수 있는 프로그램이 없습니다. 프로그램을 먼저 등록하세요.', 'local_programs_missing')
        labels = {str(index+1) + '. ' + str(program.get('name') or '프로그램').replace('\r', ' ').replace('\n', ' ')[:180]: program['id']
                  for index, program in enumerate(programs)}
        options = list(labels)
        selection = {'steps': [], 'variables': {'programs': {'type': 'list', 'description': '녹화할 프로그램',
            'items': {'type': 'enum', 'values': options}, 'max_items': min(10, len(options)), 'default': options[:1]}}}
        return self._new_job(selection, self._work, kind='editor', program_choices=labels,
                             editor_args=({'name': name} if name is not None else {}),
                             form_metadata={'title': '녹화할 프로그램 선택 · 이 PC에서만 표시',
                                            'intro': '편집창에서 사용할 프로그램을 선택하세요. 여러 프로그램을 오가는 작업은 함께 선택할 수 있습니다.',
                                            'confirm_label': '편집창 열기', 'multiple_choice_lists': True})

    def _open_editor_work(self, job):
        try:
            self._open_editor(job)
        except Exception as exc:
            self._failure(job, exc)
        finally:
            self._finish(job)

    def _open_editor(self, job):
        if job['cancel'].is_set():
            raise OperationError('편집창 열기를 취소했습니다.', 'local_input_cancelled')
        args = copy.deepcopy(job['editor_args'])
        if 'program_choices' in job:
            chosen = job['inputs']['programs']
            if len(chosen) != len(set(chosen)):
                raise OperationError('같은 프로그램을 두 번 선택할 수 없습니다.', 'local_input_invalid')
            args['program_ids'] = [job['program_choices'][label] for label in chosen]
        enabled = {program['id'] for program in self.manager.config.get('programs', []) if program.get('enabled') is True}
        if not args.get('program_ids') or not set(args['program_ids']) <= enabled:
            raise OperationError('선택한 프로그램 등록이 바뀌었습니다. 현재 목록에서 다시 선택하세요.', 'local_programs_changed')
        response = self.manager.easy._call('computer_process_editor', args, job['cancel'])
        value = response.get('structuredContent')
        if not isinstance(value, dict):
            raise OperationError('편집창 실행 결과를 확인하지 못했습니다.', 'local_operation_failed')
        job['local_result'] = copy.deepcopy(value)
        job['result'] = self._receipt(value)

    def _start_worker(self, job, target):
        job['done'].clear()
        worker = threading.Thread(target=target, args=(job,), daemon=True, name='local-run-'+job['id'][:8])
        job['worker'] = worker
        worker.start()

    def resume(self, key):
        """Resume only the retained run with its original revision and values."""
        with self.lock:
            if key not in self.jobs:
                raise OperationError('로컬 실행을 찾지 못했습니다.', 'local_run_missing')
            job = self.jobs[key]
            if job.get('cleanup_pending'):
                raise OperationError('이전 입력창 종료를 확인하지 못했습니다.', 'local_input_cleanup_pending')
            if not job['done'].is_set() or job['result'].get('task_verified') is True:
                return self.status(key)
            if job['cancel'].is_set():
                raise OperationError('중지한 실행은 자동으로 이어가지 않습니다.', 'local_input_cancelled')
            if not job.get('run_id') or 'inputs' not in job:
                raise OperationError('이어갈 실행 기록이 없습니다. 입력을 다시 보내지 않았습니다.', 'local_run_missing')
            if any(not other['done'].is_set() or other.get('cleanup_pending') for other in self.jobs.values()):
                raise OperationError('진행 중인 로컬 실행을 먼저 마치세요.', 'local_run_busy')
            if job.get('helper_role') == 'result' and not self._cleanup_form(job):
                raise OperationError('이전 결과창 종료를 확인하지 못했습니다.', 'local_input_cleanup_pending')
            job.pop('result_displayed', None)
            self._start_worker(job, self._resume_work)
            return self.status(key)

    def status(self, key, *, cancel=False):
        with self.lock:
            if key not in self.jobs: raise OperationError('로컬 실행을 찾지 못했습니다.')
            job = self.jobs[key]
            if type(cancel) is not bool: raise OperationError('취소 요청 형식을 확인하세요.')
            if cancel: job['cancel'].set()
            if (job['done'].is_set() and job.get('helper_role') == 'result'
                    and job.get('helper') is not None and job['helper'].poll() is not None):
                self._cleanup_form(job)
            return {**self._receipt(job['result']), 'local_run_id': key,
                    'pending': not job['done'].is_set() or job.get('cleanup_pending', False)}

    def busy(self):
        """Result display alone is idle; input, execution and cleanup are busy."""
        with self.lock:
            return any(not job['done'].is_set() or job.get('cleanup_pending') for job in self.jobs.values())

    def is_worker_thread(self):
        with self.lock:
            return any(job.get('worker') is threading.current_thread() and not job['done'].is_set()
                       for job in self.jobs.values())

    def cancel(self):
        with self.lock:
            for job in self.jobs.values():
                job['cancel'].set()
                if job['done'].is_set() and job.get('helper_role') == 'result':
                    self._cleanup_form(job)

    def close(self, timeout=3):
        self.cancel()
        deadline = time.monotonic() + max(0, min(timeout, 10))
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.get('worker') is threading.current_thread():
                continue
            job['done'].wait(max(0, deadline-time.monotonic()))
            if job['done'].is_set() and job.get('cleanup_pending') and time.monotonic() < deadline:
                self._cleanup_form(job)
        return all((job['done'].is_set() and not job.get('cleanup_pending'))
                   or job.get('worker') is threading.current_thread() for job in jobs)

    def _cleanup_form(self, job):
        from process_editor import ProcessEditors
        from learning_picker import _end_helper
        try:
            _end_helper(job.get('helper'))
        except Exception as exc:
            job['cleanup_pending'] = True
            if job.get('helper_role') == 'result':
                job['result_display_error'] = str(exc)
            else:
                self._failure(job, exc)
            return False
        job['helper'] = None
        job['cleanup_pending'] = False
        for path in job.get('exchange_paths', ()):
            try:
                ProcessEditors._check_path(path)
                path.unlink(missing_ok=True)
            except (OSError, OperationError):
                pass
        return True

    def _failure(self, job, exc):
        with self.lock:
            job['local_error'] = str(exc)
            job['result'] = {**job['result'],
                'status': 'cancelled' if getattr(exc, 'code', '') == 'local_input_cancelled' else 'blocked',
                'task_verified': False, 'diagnostic': {'code': getattr(exc, 'code', 'local_operation_failed')}}

    def _resume_work(self, job):
        try:
            self._execute(job)
        except Exception as exc:
            self._failure(job, exc)
        finally:
            self._finish(job)

    def _finish(self, job):
        # Spawn one result surface only after execution has returned. It is not
        # a checkpoint and never contributes an approval or another input.
        with self.lock:
            if not job['cancel'].is_set() and not job.get('cleanup_pending'):
                try:
                    self._show_result(job)
                except Exception as exc:
                    job['result_display_error'] = str(exc)
                    self._cleanup_form(job)
            job['done'].set()

    def _show_result(self, job):
        from process_editor import ProcessEditors, HELPER_NAME
        from menu_expansion import local_menu_summary
        from local_result_details import project_local_diagnostics
        if job.get('result_displayed') or job['result'].get('status') in {'awaiting_input', 'cancelled', 'editing', 'starting'}:
            return
        raw = job.get('local_result') or job['result']
        diagnostic = raw.get('diagnostic', {}) if isinstance(raw.get('diagnostic'), dict) else {}
        visible = {key: job['result'][key] for key in ('status', 'task_verified', 'completed_steps', 'total_steps') if key in job['result']}
        visible.update(local_menu_summary(raw))
        visible.update(project_local_diagnostics(raw))
        last = raw.get('last_result', {}) if isinstance(raw, dict) else {}
        if 'menu_expansion' in visible and not isinstance(raw.get('menu_expansion'), dict) and isinstance(last, dict) and isinstance(last.get('diagnostic'), dict):
            diagnostic = last['diagnostic']
        summary = raw.get('summary') or raw.get('message')
        if isinstance(summary, str):
            visible['summary'] = summary[:4000]
        message = job.get('local_error') or diagnostic.get('message')
        if diagnostic.get('code') or message:
            visible['diagnostic'] = {'code': str(diagnostic.get('code') or job['result'].get('diagnostic', {}).get('code', 'local_operation_failed'))[:120],
                                     'message': str(message or '')[:4000]}
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        folder = Path(self.manager.config['state_dir']) / 'local-results'
        request, response = [folder / (nonce[:16]+suffix) for suffix in ('.request.json', '.response.json')]
        ready = Path(str(response)+'.ready.json')
        ProcessEditors._check_path(request)
        folder.mkdir(parents=True, exist_ok=True)
        job['exchange_paths'] = (request, response, ready)
        job['helper_role'] = 'result'
        ProcessEditors._write(request, {'nonce': nonce, 'timeout_seconds': 600, 'result': visible})
        helper = Path(__file__).with_name(HELPER_NAME)
        job['helper'] = subprocess.Popen([str(helper), '--result', str(request), str(response)],
            cwd=str(helper.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job['result_displayed'] = True

    @staticmethod
    def _ready(info, child):
        if (info.get('status') != 'ready' or type(info.get('helper_pid')) is not int
                or info['helper_pid'] != child.pid or type(info.get('helper_window_id')) is not int
                or info['helper_window_id'] <= 0):
            raise OperationError('로컬 입력창의 실행 정보를 확인하지 못했습니다.', 'local_input_invalid')

    def _work(self, job):
        from process_editor import ProcessEditors, HELPER_NAME
        from learning_picker import _end_helper, _helper_visible
        from authoring_inputs import describe_inputs
        helper = Path(__file__).with_name(HELPER_NAME)
        nonce = uuid.uuid4().hex + uuid.uuid4().hex
        folder = Path(self.manager.config['state_dir']) / 'local-inputs'
        request, response = [folder / (nonce[:16]+suffix) for suffix in ('.request.json', '.response.json')]
        ready = Path(str(response)+'.ready.json')
        job['exchange_paths'] = (request, response, ready)
        job['helper_role'] = 'input'
        child = None
        try:
            if job['cancel'].is_set():
                raise OperationError('로컬 입력을 취소했습니다.', 'local_input_cancelled')
            ProcessEditors._check_path(request)
            folder.mkdir(parents=True, exist_ok=True)
            ProcessEditors._write(request, {'nonce': nonce, 'timeout_seconds': self.INPUT_TIMEOUT,
                'inputs': describe_inputs(job['task']['steps'], job['task'].get('variables', {})), **job.get('form_metadata', {})})
            child = subprocess.Popen([str(helper), '--inputs', str(request), str(response)],
                cwd=str(helper.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            job['helper'] = child
            start, visible = time.monotonic(), False
            while not response.exists():
                if job['cancel'].wait(.05): raise OperationError('로컬 입력을 취소했습니다.', 'local_input_cancelled')
                if ready.exists() and not visible:
                    info = ProcessEditors._read(ready, nonce)
                    self._ready(info, child)
                    if not _helper_visible(child.pid, info['helper_window_id']):
                        raise OperationError('로컬 입력창을 확인하지 못했습니다.')
                    visible = True
                if child.poll() is not None or time.monotonic()-start > self.INPUT_TIMEOUT+3:
                    raise OperationError('로컬 입력창이 종료됐습니다.', 'local_input_cancelled')
            value = ProcessEditors._read(response, nonce)
            if not visible and ready.exists():
                info = ProcessEditors._read(ready, nonce)
                self._ready(info, child)
                # The completed form may already have exited before the first
                # poll. Only our own normally exited child plus its nonce-bound
                # ready record can provide that fast-completion proof.
                visible = _helper_visible(child.pid, info['helper_window_id']) or child.poll() == 0
            if not visible or value.get('status') != 'confirmed' or job['cancel'].is_set():
                raise OperationError('입력 확인 없이 실행하지 않았습니다.', 'local_input_cancelled')
            job['inputs'] = copy.deepcopy(resolve_values(job['task'].get('variables', {}), value.get('inputs', {})))
            _end_helper(child); child = None; job['helper'] = None
            if job.get('kind') == 'editor':
                self._open_editor(job)
            else:
                self._execute(job)
        except Exception as exc:
            self._failure(job, exc)
        finally:
            self._cleanup_form(job)
            self._finish(job)

    def _execute(self, job):
        """Reuse public execution semantics with frozen private inputs."""
        args = {'task_id': job['task']['id'], 'revision': job['task'].get('revision', 1), 'inputs': copy.deepcopy(job['inputs'])}
        if job.get('run_id'):
            args['resume_run_id'] = job['run_id']
        seen = set()
        for _ in range(self.MAX_CONTINUATIONS):
            if job['cancel'].is_set(): raise OperationError('실행을 중지했습니다.', 'local_input_cancelled')
            local_results = getattr(self.manager, 'local_results', None)
            if local_results is not None:
                local_results.value = None
            response = self.manager.call('computer_run_task', copy.deepcopy(args), job['cancel'])
            # An acknowledgement belongs to exactly one checkpoint and one
            # continuation call. Never carry it into a later review or chunk.
            args.pop('acknowledge_checkpoint', None)
            value = response.get('structuredContent', {})
            if not isinstance(value, dict):
                raise OperationError('실행 응답을 확인하지 못했습니다.', 'local_operation_failed')
            raw = getattr(local_results, 'value', None)
            if isinstance(raw, dict):
                raw = raw.get('structuredContent', raw)
            job['local_result'] = copy.deepcopy(raw if isinstance(raw, dict) else value)
            job.pop('local_error', None)
            with self.lock: job['result'] = copy.deepcopy(value)
            # The public privacy boundary intentionally has no raw snapshots.
            # Continuation is retrieved locally from the runner, never rebuilt.
            run_id = value.get('run_id')
            if run_id:
                if job.get('run_id') and job['run_id'] != run_id:
                    raise OperationError('이어갈 실행의 식별자가 바뀌었습니다.', 'local_run_changed')
                job['run_id'] = run_id
            if value.get('status') == 'verified' or not run_id: return
            progress = self.manager.workflows.progress(run_id)
            if (progress.get('run_id') != run_id or progress.get('task_id') != job['task']['id']
                    or progress.get('revision') != job['task'].get('revision', 1)):
                raise OperationError('같은 작업 실행인지 확인하지 못했습니다.', 'local_run_changed')
            continuation = progress.get('continuation', {})
            if continuation.get('reason') in {'workflow_chunk_complete', 'session_budget_exhausted'}:
                boundary = (run_id, progress.get('completed_steps'), progress.get('pending_step'),
                            continuation.get('reason'), continuation.get('session_id'))
                if boundary in seen:
                    raise OperationError('같은 완료 지점에서 진행되지 않아 멈췄습니다. 입력은 반복하지 않았습니다.', 'local_run_no_progress')
                seen.add(boundary)
                args['resume_run_id'] = run_id
                continue
            # A human checkpoint may finish while the form stays closed. Only
            # its actual local receipt can authorize continuing this same run.
            checkpoint = progress.get('checkpoint', {})
            checkpoint_id = checkpoint.get('checkpoint_id') or checkpoint.get('id')
            if value.get('status') == 'needs_review' and checkpoint_id and checkpoint.get('review_mode', 'human') == 'human':
                deadline = time.monotonic()+self.HUMAN_TIMEOUT
                while time.monotonic() < deadline:
                    if job['cancel'].is_set():
                        raise OperationError('실행을 중지했습니다.', 'local_input_cancelled')
                    ack = self.manager.checkpoint_reviews.acknowledgement(self.manager.session, run_id, checkpoint_id)
                    if (ack.get('human_reviewed') is True and ack.get('status') == 'confirmed'
                            and ack.get('run_id') == run_id and ack.get('checkpoint_id') == checkpoint_id):
                        boundary = ('human', run_id, checkpoint_id)
                        if boundary in seen:
                            raise OperationError('같은 확인 지점에서 진행되지 않아 멈췄습니다.', 'local_run_no_progress')
                        seen.add(boundary)
                        args.update(resume_run_id=run_id, acknowledge_checkpoint=checkpoint_id)
                        break
                    if ack.get('status') not in {'pending', 'starting'}:
                        return
                    if job['cancel'].wait(min(self.POLL_SECONDS, max(0, deadline-time.monotonic()))):
                        raise OperationError('실행을 중지했습니다.', 'local_input_cancelled')
                else: return
                continue
            return
