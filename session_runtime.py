"""One approved desktop session. Does not start an LLM or edit client settings."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from settings import VERSION
from closing import ClosureManager
from program_launch import LaunchObservation, create_application
from vendor.guard import (Guard, GuardError, DriverTransport, COMMON_TOOLS, MODE_TOOLS,
                          OBSERVATIONS, check_app, utc_now, atomic_json, journal_route)


SAFE_TOOLS = COMMON_TOOLS | MODE_TOOLS["uia"] | MODE_TOOLS["visual"]
ACTION_LABELS = {"click": "선택한 버튼 또는 위치 누르기", "double_click": "두 번 누르기",
                 "right_click": "오른쪽 버튼 메뉴 열기", "type_text": "글자 입력", "set_value": "입력칸 값 변경",
                 "press_key": "키보드 키 누르기", "hotkey": "단축키 누르기", "scroll": "화면 스크롤",
                 "drag": "선택한 위치 끌기", "bring_to_front": "선택한 창 앞으로 가져오기", "invoke_menu": "프로그램 메뉴 실행"}


class SessionError(RuntimeError):
    pass


def manifest_for(paths, minutes):
    return {"version": 3, "expires_after": f"{minutes}m", "idle_timeout": f"{minutes}m",
            "allow": {"tools": sorted(SAFE_TOOLS)},
            "resources": {"apps": [{"executable": path, "launch": False, "windows": "all"} for path in paths],
                          "desktop": {"display": False}}}


class ConsentGuard(Guard):
    def __init__(self, policy, transport, session):
        self.session_runtime = session
        super().__init__(policy, transport=transport)

    def approve(self, name, args, call_id):
        self.session_runtime.check_active()
        if self.session_runtime.config["approval"] != "each":
            return
        label = ACTION_LABELS.get(name, name)
        target = self.process_resolver(args["pid"]) if "pid" in args else ""
        okay = self.session_runtime.confirm("action", "화면 조작 승인 — " + label,
            f"실행할 동작: {label}\n대상 실행파일: {target}\n"
            f"대상 창: PID {args.get('pid')} / 창 번호 {args.get('window_id')}\n\n"
            f"상세 요청 ({name}):\n" + json.dumps(args, ensure_ascii=False, indent=2))
        if not okay:
            raise GuardError("사용자가 조작을 거절했거나 승인이 취소되었습니다. 화면은 조작하지 않았습니다.")
        self.log("approval", request_id=call_id, tool=name, route=journal_route(name, args, self.policy["mode"]), success=True,
                 summary="The local user approved this action.")


class SessionRuntime:
    """Resources are published before any blocking approval/driver operation.

    stop() is callable from the input reader, deadline thread, or hotkey thread;
    it never waits for the task execution lock or approval worker.
    """
    def __init__(self, config, programs, mode, task, *, max_minutes=None, max_actions=None,
                 broker_factory=None, lease_factory=None, emergency_factory=None,
                 transport_factory=DriverTransport, guard_factory=ConsentGuard,
                 process_factory=subprocess.Popen, cancel_event=None,
                 registry_register=None, registry_unregister=None):
        if broker_factory is None or lease_factory is None or emergency_factory is None:
            from consent import ConsentBroker, DesktopLease, EmergencyStop
            broker_factory = broker_factory or ConsentBroker
            lease_factory = lease_factory or DesktopLease
            emergency_factory = emergency_factory or EmergencyStop
        if registry_register is None or registry_unregister is None:
            from consent import register_active_run, unregister_active_run
            registry_register = registry_register or register_active_run
            registry_unregister = registry_unregister or unregister_active_run
        self.config = copy.deepcopy(config)
        self.programs = copy.deepcopy(programs)
        self.mode = mode
        self.task = copy.deepcopy(task)
        self.minutes = max_minutes if max_minutes is not None else config["max_minutes"]
        self.max_actions = max_actions if max_actions is not None else config["max_actions"]
        self.id = uuid.uuid4().hex
        self.run_dir = Path(config["state_dir"]) / "runs" / self.id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.stop_event = cancel_event or threading.Event()
        self.stop_complete = threading.Event()
        self.stop_recorded = False
        self.resource_lock = threading.RLock()
        self.execution_lock = threading.RLock()
        self.closures = ClosureManager(self)
        self.lease = lease_factory()
        self.lease_acquired = False
        self.broker_factory = broker_factory
        self.emergency_factory = emergency_factory
        self.transport_factory = transport_factory
        self.guard_factory = guard_factory
        self.process_factory = process_factory
        self.registry_register = registry_register
        self.registry_unregister = registry_unregister
        self._registered = False
        self.broker = None
        self.emergency = None
        self.guard = None
        self.transport = None
        self.state = "awaiting_approval"
        self.reason = ""
        self.created_at = utc_now()
        self.deadline = time.monotonic() + self.minutes * 60
        self._deadline_thread = None

    def _check_not_stopped(self):
        if self.stop_event.is_set() or (self.run_dir / "stop.flag").exists():
            self.stop(self.reason or "중지됨")
            raise SessionError(self.reason or "세션이 중지되었습니다.")
        if time.monotonic() >= self.deadline:
            self.stop("설정한 실행 시간이 지났습니다.")
            raise SessionError(self.reason)

    def check_active(self):
        self._check_not_stopped()
        if self.state != "active":
            raise SessionError("먼저 computer_begin으로 화면 작업을 승인하고 시작하세요.")

    def confirm(self, kind, title, details):
        self._check_not_stopped()
        if self.config["approval"] == "client":
            return True
        broker = self.broker
        if broker is None:
            raise SessionError("로컬 승인 창을 시작하지 못했습니다.")
        answer = broker.confirm(kind, title, details, self.stop_event)
        self._check_not_stopped()
        return answer is True

    def start(self):
        try:
            paths = []
            for app in self.programs:
                for path in [app["exe"]] + list(app.get("control_exes", [])):
                    canonical = check_app(path)
                    if canonical not in paths:
                        paths.append(canonical)
            if not paths:
                raise SessionError("승인할 프로그램이 없습니다.")
            with self.resource_lock:
                self._check_not_stopped()
                if not self.lease.acquire():
                    raise SessionError("이 Windows 사용자 세션에서 다른 화면 작업이 진행 중입니다. 먼저 해당 작업을 종료하세요.")
                self.lease_acquired = True
                self.registry_register(self.run_dir)
                self._registered = True
                self.broker = self.broker_factory(self.config, self.run_dir)
                self.emergency = self.emergency_factory(self.run_dir, lambda: self.stop("사용자가 긴급 중지했습니다."))
                self.emergency.start()
            self._deadline_thread = threading.Thread(target=self._watch_deadline, daemon=True)
            self._deadline_thread.start()
            details = "\n".join(f"- {app['name']}: {app['exe']}" + "".join(
                f"\n  추가 조작 실행파일: {path}" for path in app.get("control_exes", [])) for app in self.programs)
            details += (f"\n\n방식: {self.mode}\n제한: {self.minutes}분 / {self.max_actions}회 조작"
                        f"\n요청한 작업:\n{self.task.get('instructions', '')}\n완료 기준:\n{self.task.get('expected', '')}"
                        "\n\n선택한 프로그램의 창 내용을 읽고 조작합니다. 이 서버는 AI 모델을 실행하지 않습니다."
                        " 화면 내용은 연결한 MCP 클라이언트와 그 모델로 전달됩니다. 앱 제한은 PC 전체 격리가 아닙니다.")
            if not self.confirm("session", "화면 작업 시작 승인", details):
                raise SessionError("사용자가 화면 작업 시작을 거절했습니다.")
            self._check_not_stopped()
            manifest_path = self.run_dir / "capabilities.json"
            atomic_json(manifest_path, manifest_for(paths, self.minutes))
            policy = {"driver": self.config["driver"], "allowed_apps": paths, "mode": self.mode,
                "approval_mode": "run", "run_dir": str(self.run_dir), "max_actions": self.max_actions,
                "log_detail": self.config.get("log_detail", "metadata"),
                "observation_timeout_seconds": self.config.get("observation_timeout_seconds", 20),
                "driver_env": {"CUA_DRIVER_PERMISSION_MODE": "bounded", "CUA_DRIVER_CAPABILITY_MANIFEST_FILE": str(manifest_path),
                               "CUA_DRIVER_CAPABILITY_MANIFEST_APPROVED": "1"}}
            atomic_json(self.run_dir / "policy.json", policy)
            with self.resource_lock:
                self._check_not_stopped()
                self.transport = self.transport_factory(policy)
                self.guard = self.guard_factory(policy, self.transport, self)
            self.guard.handle({"method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "company-computer-use", "version": VERSION}}})
            self.guard.handle({"method": "notifications/initialized"})
            with self.resource_lock:
                self._check_not_stopped()
                self.state = "active"
            self._record_state()
            return self.status()
        except Exception as exc:
            self.stop(str(exc))
            raise

    def _record_state(self):
        # Serialize snapshots with state transitions so a late active snapshot
        # cannot overwrite the final stopped record.
        with self.resource_lock:
            atomic_json(self.run_dir / "session.json", self.status())

    def wait_stopped(self, timeout=10):
        """For connection shutdown only; emergency stop requests never wait here."""
        return self.stop_complete.wait(timeout) and self.stop_recorded

    def _watch_deadline(self):
        while not self.stop_event.wait(0.05):
            if (self.run_dir / "stop.flag").exists():
                self.stop("로컬 중지 버튼으로 화면 작업을 중지했습니다.")
                return
            if time.monotonic() >= self.deadline:
                self.stop("설정한 실행 시간이 지났습니다.")
                return
            if self.state == "active" and self._driver_ended():
                self.stop("Cua Driver 연결이 종료되어 화면 작업을 중지했습니다. 적용된 결과를 확인한 뒤 computer_begin으로 다시 시작하세요.")
                return

    def _driver_ended(self):
        transport = self.transport
        if transport is None:
            return False
        closed = getattr(transport, "closed", None)
        if closed is not None and closed.is_set():
            return True
        child = getattr(transport, "child", None)
        return child is not None and child.poll() is not None

    def call(self, name, arguments):
        with self.execution_lock:
            self.check_active()
            if name not in SAFE_TOOLS:
                raise SessionError("허용되지 않은 화면 도구입니다.")
            answer = self.guard.call(name, arguments)
            if self._driver_ended():
                self.stop("Cua Driver 연결이 종료되어 화면 작업을 중지했습니다. 적용된 결과를 확인한 뒤 computer_begin으로 다시 시작하세요.")
                answer.setdefault("structuredContent", {})["session_recovery"] = {
                    "state": self.state, "automatic_retry": False,
                    "next_step": "이전 입력이 적용됐는지 확인하세요. computer_begin으로 새 승인을 받은 뒤 새 창 상태를 관찰해 계속할 수 있습니다."}
                answer.setdefault("content", []).append({"type": "text", "text": self.reason})
            return answer

    def launch(self, program_id):
        with self.execution_lock:
            self.check_active()
            app = next((p for p in self.programs if p["id"] == program_id), None)
            if app is None:
                raise SessionError("이번 세션에 승인한 프로그램만 실행할 수 있습니다.")
            executable = check_app(app["exe"])
            if executable not in self.guard.policy["allowed_apps"]:
                raise SessionError("승인 후 실행파일 경로가 변경되었습니다. 세션을 종료하고 다시 승인하세요.")
            if not Path(executable).is_file():
                raise SessionError("등록된 실행파일을 찾지 못했습니다.")
            if self.guard.action_count >= self.max_actions:
                raise SessionError("승인한 최대 조작 횟수에 도달했습니다.")
            if self.config["approval"] == "each" and not self.confirm("launch", "프로그램 실행 승인", f"{app['name']}\n{executable}"):
                raise SessionError("사용자가 프로그램 실행을 거절했습니다.")
            observation = LaunchObservation([executable] + list(app.get("control_exes", [])),
                lambda: self.guard.call("list_windows", {"on_screen_only": True}),
                check_active=self.check_active, wait=self.stop_event.wait)
            observation.capture()
            with self.resource_lock:
                self.check_active()
                proc = create_application(executable, self.process_factory)
                self.guard.action_count += 1
                self.guard.observed_targets.clear()
            result = observation.finish(proc)
            result.update(program_id=program_id, working_directory=str(Path(executable).parent),
                          runtime_environment_isolated=True)
            call_id = uuid.uuid4().hex
            self.guard.log("result", request_id=call_id, tool="computer_launch", route="management",
                           pid=proc.pid, exe=executable, success=result["launch_status"] != "startup_failed",
                           summary={"launch_status": result["launch_status"], "exit_code": result["exit_code"],
                                    "window_verified": result["window_verified"], "task_verified": False})
            return result

    def status(self):
        return {"session_id": self.id, "state": self.state, "reason": self.reason, "mode": self.mode,
                "program_ids": [p["id"] for p in self.programs], "approval": self.config["approval"],
                "approval_description": ("MCP 클라이언트의 승인에 의존합니다. 서버의 별도 승인 창은 생략합니다."
                                         if self.config["approval"] == "client" else
                                         "이번 작업 전체를 로컬에서 승인합니다." if self.config["approval"] == "session" else
                                         "로컬 시작 승인 후 각 조작을 다시 승인합니다."),
                "max_minutes": self.minutes, "max_actions": self.max_actions,
                "log_detail": self.config.get("log_detail", "metadata"),
                "actions_used": self.guard.action_count if self.guard is not None else 0,
                "created_at": self.created_at, "run_dir": str(self.run_dir),
                "emergency_hotkey_available": bool(getattr(self.emergency, "available", False))}

    def stop(self, reason="사용자가 화면 작업을 중지했습니다."):
        # Set cancellation first: approval and in-flight calls see it immediately.
        self.stop_event.set()
        try:
            (self.run_dir / "stop.flag").touch()
        except OSError:
            pass
        with self.resource_lock:
            if self.state in {"stopped", "stopping"}:
                return
            self.stop_complete.clear()
            self.stop_recorded = False
            self.state = "stopping"
            self.reason = reason
            transport, emergency, broker = self.transport, self.emergency, self.broker
            acquired = self.lease_acquired
        # No task/approval lock is held here. Retain the lease until the driver
        # and local UI/hotkey owners are confirmed closed, including kill failure.
        cleanup_ok = True
        if transport is not None:
            try:
                transport.close()
            except Exception:
                cleanup_ok = False
            child = getattr(transport, "child", None)
            if child is not None:
                try:
                    if child.poll() is None:
                        cleanup_ok = False
                except Exception:
                    cleanup_ok = False
        for resource in (self.closures, broker, emergency):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    cleanup_ok = False
        if cleanup_ok and acquired:
            try:
                self.lease.release()
            except Exception:
                cleanup_ok = False
        with self.resource_lock:
            if cleanup_ok:
                self.lease_acquired = False
                self.state = "stopped"
            else:
                self.state = "stop_failed"
                self.reason = reason + " 중지 완료를 확인하지 못해 작업 잠금을 유지합니다. Driver 종료 후 computer_stop을 다시 호출하세요."
        if cleanup_ok and self._registered:
            try:
                self.registry_unregister(self.run_dir)
                self._registered = False
            except OSError:
                # Desktop control has ended. The registry owns stale-entry
                # validation and can remove this stopped reference later.
                pass
        try:
            self._record_state()
            self.stop_recorded = True
        except OSError:
            pass
        finally:
            self.stop_complete.set()
