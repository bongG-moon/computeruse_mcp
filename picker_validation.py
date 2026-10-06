"""Developer-only acceptance of the complete native teaching MCP workflow.

Default invocation prepares isolated synthetic WinForms fixtures only. --run
opens owned test windows, uses synthetic F8 over an owned fixture control,
confirms the visible native review, and verifies persistence/reuse via MCP.
Never run concurrently with another foreground GUI validation. This is not
human, enterprise-program, LLM, elevated-GUI, or production acceptance evidence.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid
import zlib

from generic_validation import expect, prepare
from live_validation import Client, DRIVER, decoded, safe
from native_fixture import DATA


HERE = Path(__file__).resolve().parent
HELPER = "Computer Use MCP 요소 선택.exe"


class Native:
    """Harness-only Win32 operations, restricted to recorded owned processes."""
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Interactive Windows is required; no GUI was opened")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        for name, args, result in (
            ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ("GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            ("EnumWindows", [self.callback, wintypes.LPARAM], wintypes.BOOL),
            ("EnumChildWindows", [wintypes.HWND, self.callback, wintypes.LPARAM], wintypes.BOOL),
            ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            ("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
            ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            ("GetParent", [wintypes.HWND], wintypes.HWND),
            ("GetDlgCtrlID", [wintypes.HWND], ctypes.c_int),
            ("GetForegroundWindow", [], wintypes.HWND),
            ("SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
            ("SetCursorPos", [ctypes.c_int, ctypes.c_int], wintypes.BOOL),
            ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            ("SendMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            ("keybd_event", [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t], None),
        ):
            method = getattr(self.user, name)
            method.argtypes, method.restype = args, result
        self.user.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def pid(self, hwnd):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def windows(self, pid, parent=None):
        rows = []
        @self.callback
        def visit(hwnd, unused):
            if self.pid(hwnd) != pid:
                return True
            text, classname, rect = ctypes.create_unicode_buffer(4096), ctypes.create_unicode_buffer(256), wintypes.RECT()
            self.user.GetWindowTextW(hwnd, text, len(text))
            self.user.GetClassNameW(hwnd, classname, len(classname))
            self.user.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append({"hwnd": int(hwnd), "text": text.value, "class": classname.value,
                         "visible": bool(self.user.IsWindowVisible(hwnd)), "enabled": bool(self.user.IsWindowEnabled(hwnd)),
                         "bounds": [rect.left, rect.top, rect.right, rect.bottom]})
            return True
        if parent is not None:
            if self.pid(parent) != pid:
                raise AssertionError("Window is not owned by the expected process")
            self.user.EnumChildWindows(parent, visit, 0)
        else:
            self.user.EnumWindows(visit, 0)
        return rows

    def ready_helper(self, answer, executable):
        from vendor.guard import normalize_exe, windows_process_exe
        pid, hwnd = answer.get("helper_pid"), answer.get("helper_window_id")
        assert type(pid) is int and pid > 0 and type(hwnd) is int and hwnd > 0, answer
        assert normalize_exe(windows_process_exe(pid)) == normalize_exe(str(executable)), answer
        assert self.pid(hwnd) == pid and self.user.IsWindowVisible(hwnd), answer
        handle = self.kernel.OpenProcess(0x101001, False, pid)  # exact helper: synchronize/query/cleanup only
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot retain the exact owned helper process handle")
        if self.kernel.WaitForSingleObject(handle, 0) != 258:
            self.kernel.CloseHandle(handle)
            raise AssertionError("Helper exited before its visible-ready response")
        return {"pid": pid, "hwnd": hwnd, "handle": handle, "closed": False}

    def wait_exited(self, helper, timeout=10):
        ended = self.kernel.WaitForSingleObject(helper["handle"], int(timeout * 1000)) == 0
        if ended:
            helper["closed"] = True
        return ended

    def click_helper(self, helper, label, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.wait_exited(helper, 0):
                raise AssertionError("Helper exited before visible confirmation")
            matches = [row for row in self.windows(helper["pid"], helper["hwnd"])
                       if row["text"] == label and "BUTTON" in row["class"].upper() and row["visible"] and row["enabled"]]
            if len(matches) == 1:
                button = matches[0]["hwnd"]
                assert int(self.user.GetAncestor(button, 2)) == helper["hwnd"]
                if not self.user.PostMessageW(button, 0x00F5, 0, 0):  # BM_CLICK, owned helper only
                    raise OSError(ctypes.get_last_error(), "Owned helper button could not be clicked")
                return
            time.sleep(.05)
        raise AssertionError("Enabled native review button missing: " + label)

    def point_at(self, target, helper, caption, role="Button"):
        assert self.pid(target["window_id"]) == target["pid"]
        assert self.pid(helper["hwnd"]) == helper["pid"] and not self.wait_exited(helper, 0)
        candidates = [row for row in self.windows(target["pid"], target["window_id"])
                      if row["visible"] and ((role == "Button" and row["text"] == caption and "BUTTON" in row["class"].upper())
                                             or (role == "ComboBox" and "COMBOBOX" in row["class"].upper()))]
        assert len(candidates) == 1, candidates
        self.user.SetForegroundWindow(target["window_id"])
        deadline = time.monotonic()+2
        while self.user.GetForegroundWindow() != target["window_id"] and time.monotonic() < deadline:
            time.sleep(.03)
        assert self.user.GetForegroundWindow() == target["window_id"], "Fixture focus not confirmed; no hotkey was sent"
        x1, y1, x2, y2 = candidates[0]["bounds"]
        # On a combo arrow UIA commonly identifies a child button; explicitly
        # selecting its ComboBox ancestor exercises the candidate review list.
        assert self.user.SetCursorPos(x2-8 if role == "ComboBox" else (x1+x2)//2, (y1+y2)//2)

    def key(self, target, helper, vk):
        assert self.pid(target["window_id"]) == target["pid"] and not self.wait_exited(helper, 0)
        assert self.user.GetForegroundWindow() == target["window_id"], "Fixture focus not confirmed"
        self.user.keybd_event(vk, 0, 0, 0)  # Consumed by the visible teaching helper.
        time.sleep(.06)
        self.user.keybd_event(vk, 0, 2, 0)

    def choose_by_f8(self, target, helper, caption):
        self.point_at(target, helper, caption)
        self.key(target, helper, 0x77)

    def wait_review(self, helper):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            rows = self.windows(helper["pid"], helper["hwnd"])
            buttons = [row for row in rows if row["text"] == "이 요소로 선택" and row["visible"] and row["enabled"]]
            lists = [row for row in rows if "LISTBOX" in row["class"].upper() and row["visible"]]
            ready_text = any(row["visible"] and "요소 확인 후" in row["text"] for row in rows)
            if ready_text and len(buttons) == 1 and lists and any(self.user.SendMessageW(row["hwnd"], 0x018B, 0, 0) > 0 for row in lists):
                return rows
            assert not self.wait_exited(helper, 0), "Helper exited before candidate review"
            time.sleep(.05)
        raise AssertionError("Candidate review did not expose a selectable element")

    def choose_review_role(self, helper, role):
        rows = self.wait_review(helper)
        lists = [row for row in rows if "LISTBOX" in row["class"].upper() and row["visible"]]
        assert len(lists) == 1, lists
        hwnd = lists[0]["hwnd"]
        count, labels = self.user.SendMessageW(hwnd, 0x018B, 0, 0), []
        assert 1 <= count <= 32
        for index in range(count):
            length = self.user.SendMessageW(hwnd, 0x018A, index, 0)
            assert 0 <= length <= 2048
            text = ctypes.create_unicode_buffer(length+1)
            assert self.user.SendMessageW(hwnd, 0x0189, index, ctypes.addressof(text)) >= 0
            labels.append(text.value)
        matches = [index for index, label in enumerate(labels) if role in label]
        assert len(matches) == 1, {"role": role, "candidates": labels}
        chosen = matches[0]
        assert self.user.SendMessageW(hwnd, 0x0186, chosen, 0) == chosen  # LB_SETCURSEL
        parent = self.user.GetParent(hwnd)
        assert self.pid(parent) == helper["pid"]
        control_id = self.user.GetDlgCtrlID(hwnd)
        self.user.SendMessageW(parent, 0x0111, (1 << 16) | (control_id & 0xffff), hwnd)  # WM_COMMAND/LBN_SELCHANGE
        assert self.user.SendMessageW(hwnd, 0x0188, 0, 0) == chosen
        return {"candidate_labels": labels, "selected_index": chosen, "selected_role": role,
                "parent_selection_exercised": chosen > 0}

    def capture_helper(self, helper, output):
        """Capture only the owned native helper, including when another app is behind it."""
        hwnd = helper["hwnd"]
        assert self.pid(hwnd) == helper["pid"]
        rect = wintypes.RECT()
        self.user.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right-rect.left, rect.bottom-rect.top
        assert 100 <= width <= 4000 and 100 <= height <= 3000
        for library, name, args, result in (
            (self.user, "GetWindowDC", [wintypes.HWND], wintypes.HDC),
            (self.user, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (self.user, "PrintWindow", [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
            (self.gdi, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC),
            (self.gdi, "CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
            (self.gdi, "SelectObject", [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
            (self.gdi, "GetBitmapBits", [wintypes.HBITMAP, wintypes.LONG, ctypes.c_void_p], wintypes.LONG),
            (self.gdi, "DeleteObject", [wintypes.HGDIOBJ], wintypes.BOOL),
            (self.gdi, "DeleteDC", [wintypes.HDC], wintypes.BOOL),
        ):
            method = getattr(library, name)
            method.argtypes, method.restype = args, result
        dc = self.user.GetWindowDC(hwnd)
        memory = self.gdi.CreateCompatibleDC(dc)
        bitmap = self.gdi.CreateCompatibleBitmap(dc, width, height)
        previous = self.gdi.SelectObject(memory, bitmap)
        try:
            assert self.user.PrintWindow(hwnd, memory, 2), "Owned helper capture failed"
            data = ctypes.create_string_buffer(width*height*4)
            assert self.gdi.GetBitmapBits(bitmap, len(data), data) == len(data)
            pixels = bytearray(data.raw)
            pixels[0::4], pixels[2::4], pixels[3::4] = pixels[2::4], pixels[0::4], bytes([255])*(width*height)
            rows = b"".join(b"\0" + pixels[y*width*4:(y+1)*width*4] for y in range(height))
            def chunk(kind, value):
                return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind+value) & 0xffffffff)
            output.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                               + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
        finally:
            self.gdi.SelectObject(memory, previous)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(memory)
            self.user.ReleaseDC(hwnd, dc)


def run_picker(manifest, bundle=None):
    folder = Path(manifest["folder"])
    app, native = manifest["apps"][0], Native()
    helper_executable = (bundle or HERE) / HELPER
    if not helper_executable.is_file():
        raise FileNotFoundError("Build the native helper before acceptance: " + str(helper_executable))
    records, processes, helpers = [], [], []
    client = None
    current_run = None
    report = {"passed": False, "developer_only": True, "synthetic_fixture": True,
              "human_training_tested": False, "automated_f8": True, "external_llm_used": False,
              "private_application_used": False, "administrator_gui_tested": False,
              "packaged": bundle is not None, "scenarios": {}}
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    from build_portable import source_files
    tested_root = bundle or HERE
    tested_paths = [tested_root / name for name in source_files() if name.endswith(".py")]
    tested_paths.append(helper_executable)
    if (tested_root / "ElementPicker.cs").is_file():
        tested_paths.append(tested_root / "ElementPicker.cs")
    if (tested_root / "BUILD-MANIFEST.json").is_file():
        tested_paths.append(tested_root / "BUILD-MANIFEST.json")
    tested_hashes = {path.relative_to(tested_root).as_posix(): digest(path) for path in tested_paths}
    report["tested_build"] = {"root": str(tested_root), "sha256": tested_hashes,
        "harness_sha256": digest(Path(__file__)), "fixture_sha256": digest(Path(app["exe"])),
        "driver_sha256": digest(Path(manifest["driver"]))}

    def save():
        (folder / "picker-steps.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        (folder / "picker-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def call(name, args=None, *, allow_error=False):
        started = time.monotonic()
        answer = client.request("tools/call", {"name": name, "arguments": args or {}}, timeout=100)
        records.append({"tool": name, "seconds": round(time.monotonic()-started, 3), "result": safe(answer)})
        save()
        if answer.get("isError") and not allow_error:
            raise AssertionError(name + " failed: " + json.dumps(safe(decoded(answer)), ensure_ascii=False)[:3000])
        return decoded(answer)

    def connect():
        if bundle:
            from build_portable import runtime_environment
            return Client(folder / "config.json", bundle / "server.py", bundle / "runtime/python.exe",
                          runtime_environment(bundle / "runtime"))
        return Client(folder / "config.json")

    def spawn(suffix):
        title = app["title"] + " " + suffix
        process = subprocess.Popen([app["exe"], title, app["receipt"]], cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        return process, title

    def begin():
        nonlocal current_run
        status = call("computer_begin", {"program_ids": [app["id"]], "task_description":
             "새로 연 합성 시험 앱에서 직접 가리키는 요소 학습, 확인 창, 취소, 재사용과 종료를 검증합니다."})
        current_run = Path(status["run_dir"]).resolve()
        current_run.relative_to((folder / "state/runs").resolve())

    def tree_read_count():
        journal = current_run / "actions.jsonl"
        if not journal.exists():
            return 0
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
        return sum(row.get("event") == "request" and row.get("tool") == "get_window_state" for row in rows)

    def target_for(process, title):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            rows = call("list_windows", {"pid": process.pid, "on_screen_only": True}).get("windows", [])
            matched = [row for row in rows if row.get("title") == title]
            if len(matched) == 1:
                return {key: matched[0][key] for key in ("pid", "window_id")}
            time.sleep(.1)
        raise AssertionError("Owned synthetic fixture window was not observed")

    def receipt():
        deadline = time.monotonic()+2
        while True:
            try:
                value = json.loads(Path(app["receipt"]).read_text(encoding="utf-8"))
                assert value["synthetic_fixture"] is True
                return value
            except (FileNotFoundError, json.JSONDecodeError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.03)

    def teach_args(target, label):
        return {**target, "program_id": app["id"], "label": label, "screen": "직접 선택 합성 검증", "timeout_seconds": 90}

    def start_teaching(args):
        previous_reads = tree_read_count()
        started = time.monotonic()
        answer = call("computer_teach_element", args)
        assert answer["status"] == "awaiting_selection" and answer["picker_visible"] is True, answer
        helper = native.ready_helper(answer, helper_executable)
        helpers.append(helper)
        helper["teaching_id"] = answer["teaching_id"]
        helper["ready_response_seconds"] = round(time.monotonic()-started, 3)
        helper["driver_tree_reads_before_visible_ready"] = tree_read_count()-previous_reads
        assert helper["driver_tree_reads_before_visible_ready"] == 0, "UIA tree was read before the helper appeared"
        return answer, helper

    def terminal(teaching_id, allowed):
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            answer = call("computer_teach_status", {"teaching_id": teaching_id, "wait_ms": 5000}, allow_error=True)
            if answer.get("status") not in {"starting", "awaiting_selection", "verifying_selection", "cancelling"}:
                assert answer.get("status") in allowed, answer
                return answer
        raise AssertionError("Teaching request did not reach a terminal status")

    def use_button(target, element_id):
        value = call("computer_use_element", {**target, "id": element_id, "delivery_mode": "foreground", "step": {
            "operation": "click", "expect": [expect("적용 횟수", "1")]}})
        assert value["task_verified"] is True and value["input_dispatched"] is True, value
        assert receipt()["apply_count"] == 1
        return value

    def close(target, process):
        value = call("computer_close", {**target, "scope": "process", "delivery_mode": "foreground",
            "close_action": {"operation": "hotkey", "keys": ["alt", "f4"]}, "timeout_ms": 5000})
        assert value["task_verified"] is True and process.wait(timeout=5) == 0, value
        return value

    try:
        first, title = spawn("first")
        client = connect()
        report["version"] = call("computer_status")["version"]
        available = {item["name"] for item in client.request("tools/list")["tools"]}
        assert {"computer_teach_element", "computer_teach_status", "computer_elements", "computer_use_element"} <= available
        begin()
        target = target_for(first, title)
        before = receipt()
        args = teach_args(target, "적용 버튼")
        waiting, helper = start_teaching(args)
        native.capture_helper(helper, folder / "picker-awaiting.png")
        duplicate = call("computer_teach_element", args)
        assert duplicate["teaching_id"] == waiting["teaching_id"] and duplicate.get("reused") is True, duplicate
        assert duplicate["helper_pid"] == helper["pid"] and duplicate["helper_window_id"] == helper["hwnd"]
        busy = call("computer_teach_element", {**args, "label": "중복 열기 방지"}, allow_error=True)
        assert busy["teaching_id"] == waiting["teaching_id"] and busy.get("busy") is True, busy
        report["scenarios"]["visible_ready_and_duplicate_request"] = {"passed": True, "teaching_id": waiting["teaching_id"],
            "helper_pid": helper["pid"], "helper_window_id": helper["hwnd"], "reused_same_helper": True,
            "visible_ready_response_seconds": helper["ready_response_seconds"], "performance_guarantee": False,
            "driver_tree_reads_before_visible_ready": helper["driver_tree_reads_before_visible_ready"]}
        native.choose_by_f8(target, helper, "적용")
        native.wait_review(helper)
        native.capture_helper(helper, folder / "picker-review.png")
        assert call("computer_elements", {"program_id": app["id"]})["total"] == 0, "Selection must require review confirmation"
        assert receipt() == before, "F8 or selection changed the synthetic business app"
        native.click_helper(helper, "이 요소로 선택")
        learned = terminal(waiting["teaching_id"], {"learned"})
        assert learned["input_dispatched"] is False and learned["model_trained"] is False, learned
        assert native.wait_exited(helper), "Confirmed helper did not exit"
        assert receipt() == before, "Native confirmation leaked app input"
        listed = call("computer_elements", {"program_id": app["id"], "query": "적용 버튼"})
        assert listed["total"] == 1 and listed["elements"][0]["id"] == learned["id"], listed
        stored_before = (folder / "state/elements.json").read_bytes()
        report["scenarios"]["f8_review_confirm_and_automatic_save"] = {"passed": True, "element_id": learned["id"],
            "selector": learned["selector"], "fixture_unchanged": True, "key_count": before["key_count"],
            "confirmed_helper_exited": True, "human_input_used": False}
        first_use = use_button(target, learned["id"])
        report["scenarios"]["learned_button_verified_use"] = {"passed": True, "result": first_use, "receipt": receipt()}

        combo_before = receipt()
        combo_waiting, combo_helper = start_teaching(teach_args(target, "처리 상태 선택"))
        native.click_helper(combo_helper, "3초 후 위치 선택")
        native.point_at(target, combo_helper, "처리 상태", "ComboBox")
        parent_choice = native.choose_review_role(combo_helper, "ComboBox")
        native.capture_helper(combo_helper, folder / "picker-combobox-review.png")
        assert receipt() == combo_before, "Countdown/parent selection leaked fixture input"
        native.click_helper(combo_helper, "이 요소로 선택")
        combo = terminal(combo_waiting["teaching_id"], {"learned"})
        assert combo["selector"]["role"] == "ComboBox" and native.wait_exited(combo_helper), combo
        assert receipt() == combo_before
        stored_before = (folder / "state/elements.json").read_bytes()
        report["scenarios"]["countdown_and_combobox_parent_review"] = {"passed": True, **parent_choice,
            "element_id": combo["id"], "selector": combo["selector"], "fixture_unchanged": True,
            "keyboard_selection_used": False, "confirmed_helper_exited": True}

        esc_before = receipt()
        esc_waiting, esc_helper = start_teaching(teach_args(target, "Esc로 취소할 요소"))
        native.point_at(target, esc_helper, "적용")
        native.key(target, esc_helper, 0x1B)
        esc_answer = terminal(esc_waiting["teaching_id"], {"cancelled"})
        assert native.wait_exited(esc_helper) and receipt() == esc_before
        assert (folder / "state/elements.json").read_bytes() == stored_before
        report["scenarios"]["escape_cancellation"] = {"passed": True, "status": esc_answer["status"],
            "fixture_unchanged": True, "stored_elements_unchanged": True, "cancelled_helper_exited": True}

        cancel_before = receipt()
        cancel_waiting, cancel_helper = start_teaching(teach_args(target, "취소될 요소"))
        cancelled = call("computer_teach_status", {"teaching_id": cancel_waiting["teaching_id"], "cancel": True, "wait_ms": 5000}, allow_error=True)
        if cancelled.get("status") in {"starting", "awaiting_selection", "verifying_selection", "cancelling"}:
            cancelled = terminal(cancel_waiting["teaching_id"], {"cancelled"})
        assert cancelled["status"] == "cancelled", cancelled
        assert native.wait_exited(cancel_helper), "Cancelled helper did not exit"
        assert receipt() == cancel_before
        assert (folder / "state/elements.json").read_bytes() == stored_before
        report["scenarios"]["cancellation_and_owned_helper_cleanup"] = {"passed": True, "status": cancelled["status"],
            "fixture_unchanged": True, "stored_elements_unchanged": True, "cancelled_helper_exited": True}
        report["scenarios"]["first_closure"] = {"passed": True, "result": close(target, first)}
        call("computer_end")
        client.close()
        client = None

        second, second_title = spawn("reopened")
        assert first.pid != second.pid
        client = connect()
        persisted = call("computer_elements", {"program_id": app["id"]})
        assert persisted["total"] == 2 and {row["id"] for row in persisted["elements"]} == {learned["id"], combo["id"]}
        begin()
        new_target = target_for(second, second_title)
        reused = use_button(new_target, learned["id"])
        combo_reused = call("computer_use_element", {**new_target, "id": combo["id"], "delivery_mode": "foreground", "step": {
            "operation": "select_option", "value": "진행", "option_order": ["대기", "진행", "완료"],
            "expect": [expect("선택 결과", "진행")]}})
        assert combo_reused["task_verified"] is True and receipt()["status"] == "진행"
        assert (folder / "state/elements.json").read_bytes() == stored_before
        report["scenarios"]["reopen_reconnect_and_reuse"] = {"passed": True, "first_target": target, "new_target": new_target,
            "element_ids": [learned["id"], combo["id"]], "relearned": False, "button_result": reused,
            "combo_result": combo_reused, "receipt": receipt(), "option_order_source": "Exact synthetic fixture source"}
        report["scenarios"]["second_closure"] = {"passed": True, "result": close(new_target, second)}
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)[:5000]
    finally:
        if client is not None:
            try:
                call("computer_end", allow_error=True)
                client.close()
            except Exception as error:
                report["cleanup_error"] = str(error)[:2000]
                report["passed"] = False
                if client.child.poll() is None:
                    client.child.kill()  # Exact owned test MCP process only.
                    client.child.wait(timeout=5)
        for helper in helpers:
            if not native.wait_exited(helper, 5):
                native.kernel.TerminateProcess(helper["handle"], 1)  # Exact retained helper handle, never PID/name lookup.
                native.wait_exited(helper, 5)
                report["forced_helper_failure_cleanup_used"] = True
                report["passed"] = False
            native.kernel.CloseHandle(helper["handle"])
        for process in processes:
            if process.poll() is None:
                process.terminate()  # Exact synthetic fixture only; this is not closure success evidence.
                process.wait(timeout=5)
                report["fixture_failure_cleanup_used"] = True
                report["passed"] = False
        report["owned_helpers_exited"] = bool(helpers) and all(helper["closed"] for helper in helpers)
        report["owned_fixtures_exited"] = bool(processes) and all(process.poll() is not None for process in processes)
        report["tested_build"]["unchanged_through_validation"] = all(digest(tested_root / name) == value for name, value in tested_hashes.items())
        if not report["tested_build"]["unchanged_through_validation"]:
            report["passed"] = False
        save()
    return report


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=DATA / "picker-validation" / uuid.uuid4().hex[:10])
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    if (options.folder / "state/elements.json").exists() or (options.folder / "picker-result.json").exists():
        raise ValueError("Use a fresh isolated acceptance folder; existing evidence and learned elements are retained")
    manifest = prepare(options.folder, options.driver)
    report = run_picker(manifest, options.bundle.resolve() if options.bundle else None) if options.run else manifest
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not options.run or report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
