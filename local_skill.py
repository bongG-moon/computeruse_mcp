"""Inert skill previews for the local editor, never a public MCP response.

Previewing and exporting perform no desktop work, networking, or installation.
The editor must keep these results on its private local channel. Public tools
may return a content-free receipt, but must not relay the body or local path.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from task_revision import TaskRevisions
from workflows import digest, require_runnable
from local_task_ids import LocalTaskIds


class LocalSkillError(ValueError):
    def __init__(self, message, code="local_skill_invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Preview:
    task_id: str
    public_task_id: str
    revision: int
    recipe_hash: str
    schema_hash: str
    current_revision: int
    current_hash: str
    body: bytes
    sha256: str
    expires: float


def _render(task_id, revision):
    """Only opaque public identifiers enter; never legacy names or task data."""
    slug = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:20]
    request = json.dumps({"action": "run_local", "task_id": task_id, "revision": revision},
                         ensure_ascii=True, indent=2)
    return (
        "---\nname: local-computer-task-" + slug + "\n"
        'description: "Run this saved desktop workflow through its local input form when the user requests it."\n'
        "---\n\n"
        "Use the registered Computer Use MCP. This file is an inert adapter for an existing local task; "
        "the same task revision, assets and program registrations must remain on this computer. "
        "It does not install anything or include the task's contents.\n\n"
        "When the user requests this workflow, call `computer_tasks` once with the exact arguments below. "
        "The MCP opens a local input form and keeps task contents and entered values on this computer. "
        "Do not request the full task, ask for business input values in chat, copy defaults into a tool call, "
        "or send screenshots, local files or skill contents to another service.\n\n"
        "```json\n" + request + "\n```\n\n"
        "Follow only the returned content-free status or continuation request for that same local run. "
        "Keep its run identity and frozen revision/inputs on continuation; do not start another run to retry a pause. "
        "If local execution or verification is unavailable, report the status and let the user inspect the local window. "
        "Do not switch to an external image/model review or retrieve detailed task data as a fallback.\n\n"
        "A required human confirmation must be performed by the user in the local window. "
        "Never fabricate confirmation records, skip checks, change permissions, regenerate the workflow, "
        "or repeat input whose delivery is uncertain. Completion requires the MCP's `task_verified: true`; "
        "opening a form, queued input, a pending review or an exported file is not workflow success.\n"
    ).encode("utf-8")


class LocalSkillPreviews:
    MAX_PREVIEWS = 16
    TTL_SECONDS = 30 * 60

    def __init__(self, task_store, state_dir):
        self.store = task_store
        self.revisions = TaskRevisions(task_store)
        self.state_dir = Path(state_dir).absolute()
        self.task_ids = LocalTaskIds(task_store, self.state_dir)
        self._previews = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self):
        now = time.monotonic()
        for key, item in list(self._previews.items()):
            if item.expires <= now:
                self._previews.pop(key)

    def _snapshot(self, task_id, revision):
        # Caller holds TaskStore's lock, including across the final export.
        current = self.revisions._current(self.store._read(), task_id)
        latest = self.revisions._get_revision(current, None)
        task = self.revisions._get_revision(current, revision)
        require_runnable(task)
        if not task.get("steps"):
            raise LocalSkillError("실행 단계가 저장된 작업만 스킬로 내보낼 수 있습니다.", "local_skill_empty")
        return task, latest

    def preview(self, task_id, revision=None):
        """Return the full local preview. Do not send this result over MCP."""
        with self._lock, self.store._locked():
            task, latest = self._snapshot(task_id, revision)
            pinned = task["revision"]
            public_task_id = self.task_ids.alias_locked(task["id"])
            body = _render(public_task_id, pinned)
            item = _Preview(task["id"], public_task_id, pinned, digest(task), digest(task.get("variables", {})),
                            latest["revision"], digest(latest), body, hashlib.sha256(body).hexdigest(),
                            time.monotonic() + self.TTL_SECONDS)
            self._expire()
            while len(self._previews) >= self.MAX_PREVIEWS:
                self._previews.popitem(last=False)
            ident = "skill-preview-" + uuid.uuid4().hex
            self._previews[ident] = item
            return {"preview_id": ident, "task_id": item.task_id, "revision": item.revision,
                    "recipe_hash": item.recipe_hash, "schema_hash": item.schema_hash,
                    "sha256": item.sha256, "skill_markdown": item.body.decode("utf-8"),
                    "filename": "SKILL.md", "installed": False, "requires_saved_task": True}

    def cancel(self, preview_id):
        """Invalidate an editor preview so late export commands cannot save it."""
        with self._lock:
            self._expire()
            return {"cancelled": self._previews.pop(preview_id, None) is not None}

    def _destination(self, item):
        # A hash keeps both legacy user-defined task IDs and path syntax out of
        # file names. Local and older chat exports cannot overwrite each other.
        parts = ("exports", "local", hashlib.sha256(item.task_id.encode("utf-8")).hexdigest(), str(item.revision))
        directory = self.state_dir
        self.store._reject_link(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for part in parts:
            directory = directory / part
            self.store._reject_link(directory)
            directory.mkdir(exist_ok=True)
        destination = directory / "SKILL.md"
        self.store._reject_link(destination)
        return destination

    def export(self, preview_id, task_id, revision):
        """Write exactly the bytes shown, under the task-store freshness lock."""
        with self._lock:
            self._expire()
            item = self._previews.get(preview_id)
            if item is None:
                raise LocalSkillError("미리보기가 만료되었거나 취소되었습니다. 다시 미리보세요.", "local_skill_preview_missing")
            if task_id != item.task_id or type(revision) is not int or revision != item.revision:
                raise LocalSkillError("미리본 작업과 버전이 일치하지 않습니다.", "local_skill_identity_mismatch")
            if hashlib.sha256(item.body).hexdigest() != item.sha256:
                self._previews.pop(preview_id)
                raise LocalSkillError("미리보기 내용의 무결성을 확인하지 못했습니다.", "local_skill_preview_changed")
            with self.store._locked():
                task, latest = self._snapshot(task_id, revision)
                if (latest["revision"] != item.current_revision or digest(latest) != item.current_hash
                        or digest(task) != item.recipe_hash or digest(task.get("variables", {})) != item.schema_hash):
                    self._previews.pop(preview_id)
                    raise LocalSkillError("작업이나 입력 정의가 바뀌었습니다. 저장된 버전을 다시 미리보세요.", "local_skill_preview_stale")
                if self.task_ids.alias_locked(task_id) != item.public_task_id:
                    self._previews.pop(preview_id)
                    raise LocalSkillError("로컬 작업 연결이 바뀌었습니다. 저장된 버전을 다시 미리보세요.", "local_skill_preview_stale")
                destination = self._destination(item)
                temporary = destination.with_name(".SKILL-" + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(item.body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Recheck every managed directory immediately before the
                    # atomic replacement; no linked file is ever written to.
                    if self._destination(item) != destination:
                        raise LocalSkillError("내보내기 위치가 바뀌었습니다.", "local_skill_path_changed")
                    os.replace(temporary, destination)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            self._previews.pop(preview_id)
            return {"status": "exported", "task_id": item.task_id, "revision": item.revision,
                    "sha256": item.sha256, "bytes": len(item.body), "path": str(destination),
                    "installed": False, "requires_saved_task": True}
