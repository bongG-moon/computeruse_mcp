"""Private, persistent public aliases for legacy user-named task identifiers.

The alias registry contains business identifiers and must stay in local state.
It never rewrites recipes or revisions. Callers must not hold TaskStore's lock
when invoking alias()/resolve(); both use that same cross-process lock.
alias_locked() is solely for existing local store transactions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid


_NATIVE = re.compile(r"[a-f0-9]{32}\Z")
_ALIAS = re.compile(r"task-[a-f0-9]{32}\Z")
_TASK = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class LocalTaskIdError(ValueError):
    def __init__(self, message, code="local_task_identity_invalid"):
        super().__init__(message)
        self.code = code


class LocalTaskIds:
    MAX_ALIASES = 1000
    MAX_BYTES = 512 * 1024

    def __init__(self, store, state_dir):
        self.store = store
        self.root = Path(os.path.abspath(state_dir))
        if self.root != Path(os.path.abspath(store.path.parent)):
            raise LocalTaskIdError("작업 저장 위치와 별칭 저장 위치가 일치하지 않습니다.")
        self.path = self.root / "local-task-ids.json"

    def _check_paths(self):
        # Reject directory junctions as well as file links before taking the
        # existing task-store lock, whose lock file must use this same folder.
        for path in (*reversed(self.root.parents), self.root, self.path):
            self.store._reject_link(path)

    @staticmethod
    def _pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def _read(self):
        self._check_paths()
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(self.MAX_BYTES + 1)
        except FileNotFoundError:
            return {}
        if len(raw) > self.MAX_BYTES:
            raise LocalTaskIdError("로컬 작업 별칭 기록의 크기 제한을 넘었습니다.", "local_task_identity_corrupt")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=self._pairs)
            if (not isinstance(data, dict) or set(data) != {"version", "map"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["map"], dict) or len(data["map"]) > self.MAX_ALIASES):
                raise ValueError("invalid registry")
            mapping = data["map"]
            if any(not _ALIAS.fullmatch(key) or not isinstance(value, str)
                   or not _TASK.fullmatch(value) or _NATIVE.fullmatch(value) or key == value
                   for key, value in mapping.items()):
                raise ValueError("invalid alias")
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("duplicate original")
            return mapping
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise LocalTaskIdError("로컬 작업 별칭 기록이 손상되었습니다. 기존 기록은 보존했습니다.",
                                   "local_task_identity_corrupt") from None

    def _write(self, mapping):
        self._check_paths()
        body = json.dumps({"version": 1, "map": mapping}, ensure_ascii=True, sort_keys=True).encode("utf-8")
        if len(body) > self.MAX_BYTES:
            raise LocalTaskIdError("로컬 작업 별칭 기록의 크기 제한을 넘었습니다.", "local_task_identity_corrupt")
        temporary = self.root / (".local-task-ids-" + uuid.uuid4().hex + ".tmp")
        created = False
        try:
            with temporary.open("xb") as stream:
                created = True
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_paths()
            self.store._reject_link(temporary)
            os.replace(temporary, self.path)
        finally:
            if created and temporary.exists():
                temporary.unlink()

    def alias(self, task_id):
        """Return an opaque ID; already-public values are idempotent."""
        if not isinstance(task_id, str) or not _TASK.fullmatch(task_id):
            raise LocalTaskIdError("저장된 작업 식별자가 올바르지 않습니다.")
        # Generated IDs already contain no business text. This pure fast path
        # keeps repeated privacy filtering out of task-store IO on replay.
        # resolve() still checks existence before any task is used.
        if _NATIVE.fullmatch(task_id):
            return task_id
        self._check_paths()
        with self.store._locked():
            return self.alias_locked(task_id)

    def alias_locked(self, task_id):
        """Alias while the caller already holds this store's _locked() scope."""
        if not isinstance(task_id, str) or not _TASK.fullmatch(task_id):
            raise LocalTaskIdError("저장된 작업 식별자가 올바르지 않습니다.")
        if _NATIVE.fullmatch(task_id):
            return task_id
        self._check_paths()
        existing = {task["id"] for task in self.store._read()}
        mapping = self._read()
        if task_id in mapping:
            # Double filtering must preserve the same public alias. A user
            # naming another recipe after that alias is an ambiguity, not a
            # reason to reinterpret the public ID as the new recipe.
            if task_id in existing:
                raise LocalTaskIdError("로컬 작업 식별자가 충돌합니다.", "local_task_identity_corrupt")
            if mapping[task_id] not in existing:
                raise LocalTaskIdError("저장된 작업을 찾지 못했습니다.", "local_task_identity_unknown")
            return task_id
        if task_id not in existing:
            raise LocalTaskIdError("저장된 작업을 찾지 못했습니다.", "local_task_identity_unknown")
        known = next((key for key, value in mapping.items() if value == task_id), None)
        if known:
            if known in existing:
                raise LocalTaskIdError("로컬 작업 식별자가 충돌합니다.", "local_task_identity_corrupt")
            return known
        if len(mapping) >= self.MAX_ALIASES:
            raise LocalTaskIdError("로컬 작업 별칭 한도에 도달했습니다.", "local_task_identity_limit")
        while True:
            public = "task-" + uuid.uuid4().hex
            if public not in mapping and public not in existing:
                break
        mapping[public] = task_id
        self._write(mapping)
        return public

    def resolve(self, public_id):
        """Resolve existing opaque identifiers; never fall back to legacy text."""
        if not isinstance(public_id, str) or not (_NATIVE.fullmatch(public_id) or _ALIAS.fullmatch(public_id)):
            raise LocalTaskIdError("목록에서 받은 로컬 작업 식별자를 사용하세요.")
        self._check_paths()
        with self.store._locked():
            existing = {task["id"] for task in self.store._read()}
            if _NATIVE.fullmatch(public_id):
                real = public_id
            else:
                real = self._read().get(public_id)
                if public_id in existing:
                    raise LocalTaskIdError("로컬 작업 식별자가 충돌합니다.", "local_task_identity_corrupt")
            if real is None or real not in existing:
                raise LocalTaskIdError("저장된 작업을 찾지 못했습니다.", "local_task_identity_unknown")
            return real
