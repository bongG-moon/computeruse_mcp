"""Local performance hints from verified runs; never permission or live UI state."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
from settings import VERSION
from vendor.guard import atomic_json, utc_now


class RepeatProfiles:
    def __init__(self, state_dir):
        self.root = Path(state_dir) / "repeat-profiles"

    def signature(self, task, programs, delivery):
        def identity(path):
            try:
                stat = Path(path).stat()
                return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            except (OSError, ValueError, TypeError):
                return None
        data = {"version": VERSION, "task_id": task["id"], "revision": task.get("revision", 1),
                "steps": task.get("steps", []), "variables": task.get("variables", {}), "delivery": delivery,
                "programs": sorted([{**{k: p.get(k) for k in ("id", "exe", "control_exes", "launch")},
                                     "file_identities": [identity(path) for path in [p.get("exe"), *p.get("control_exes", [])]]} for p in programs
                                    if p["id"] in task["program_ids"]], key=lambda p: p["id"])}
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _path(self, signature):
        if not isinstance(signature, str) or not re.fullmatch(r"[a-f0-9]{64}", signature):
            raise ValueError("invalid_repeat_signature")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (signature + ".json")
        if any(p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
               for p in (self.root, path)):
            raise ValueError("linked_repeat_profile")
        return path

    def read(self, signature):
        try:
            path = self._path(signature)
            if not path.exists(): return None
            if path.stat().st_size > 16384: return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("format") != "computer-repeat/v1"
                    or data.get("signature") != signature or data.get("version") != VERSION
                    or data.get("eligible") is not True or type(data.get("successes")) is not int
                    or data["successes"] < 1): return None
            return data
        except (OSError, ValueError):
            return None

    def record(self, signature, result):
        path = self._path(signature)
        previous = self.read(signature) or {}
        verified = result.get("task_verified") is True and result.get("status") == "verified"
        atomic_json(path, {"format": "computer-repeat/v1", "signature": signature, "version": VERSION,
            "eligible": verified, "successes": previous.get("successes", 0) + int(verified),
            "last_run_id": result.get("run_id"), "last_duration_ms": result.get("duration_ms"),
            "updated_at": utc_now(), "last_status": result.get("status", "unknown")})
