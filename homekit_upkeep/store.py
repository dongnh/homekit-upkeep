"""Persistent per-task completion timestamps shared between the HAP loop and
the HTTP API.

State is a small JSON file `{"last_done": {"<task id>": "<ISO local time>"}}`
written atomically (tempfile + rename) so concurrent readers never see a torn
document — the same mechanism as light-programmer's mode_state.
"""
import json
import os
import tempfile
import threading

_lock = threading.Lock()


def load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_done": {}}
    last = data.get("last_done")
    return {"last_done": dict(last) if isinstance(last, dict) else {}}


def _save_locked(path: str, state: dict) -> dict:
    """Atomic write WITHOUT acquiring _lock. Caller MUST already hold _lock."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upkeep_state.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return state


def mark_done(path: str, task_id: str, when_iso: str) -> dict:
    """Set `task_id`'s last-done timestamp (ISO string, naive local time)."""
    with _lock:
        state = load(path)
        state["last_done"][task_id] = when_iso
        return _save_locked(path, state)


def seed_missing(path: str, defaults: dict) -> dict:
    """First-run initialisation: give every task WITHOUT a stored timestamp
    the provided one (normally "now"), so a fresh install doesn't open every
    sensor at once. Existing timestamps are never touched."""
    with _lock:
        state = load(path)
        changed = False
        for task_id, when_iso in defaults.items():
            if task_id not in state["last_done"]:
                state["last_done"][task_id] = when_iso
                changed = True
        return _save_locked(path, state) if changed else state
