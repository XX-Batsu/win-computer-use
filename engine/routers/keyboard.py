"""
routers/keyboard.py — POST /keyboard

Dispatches keyboard actions (type, hotkey, keydown, keyup) through a worker
thread to avoid blocking the async event loop while keeping pyautogui's
low-level keyboard calls isolated from the request handler.

Threading notes:
- pyautogui uses some global state (PAUSE, failsafe). Concurrent requests run
  from separate threads sharing this state; this is safe for the default PAUSE=0
  and failsafe settings but callers should not mutate pyautogui globals at runtime.
- On timeout the worker thread continues until its pyautogui call returns; Python
  threads cannot be forcibly cancelled. Phantom keystrokes may appear in the
  foreground window after a timeout response has already been returned.
"""

import asyncio
import queue
import threading
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from .. import keyboard_worker as kw_module

router = APIRouter()

KEYBOARD_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Held-key tracking — detect orphaned keydown without matching keyup
# ---------------------------------------------------------------------------
_held_keys: set[str] = set()
_held_keys_lock = threading.Lock()


def get_held_keys() -> set[str]:
    """Return the set of currently held keys."""
    with _held_keys_lock:
        return set(_held_keys)


def release_all_held_keys() -> list[str]:
    """Release all held keys via worker threads. Returns keys released."""
    with _held_keys_lock:
        keys_to_release = list(_held_keys)

    released = []
    for key in keys_to_release:
        q: queue.Queue = queue.Queue()
        t = threading.Thread(
            target=kw_module.keyboard_worker,
            args=(q, "keyup", {"key": key}),
            daemon=True,
        )
        t.start()
        t.join(5)
        released.append(key)

    with _held_keys_lock:
        _held_keys.clear()
    return released


class KeyboardRequest(BaseModel):
    action: Literal["type", "hotkey", "keydown", "keyup"]
    # For "type"
    text: Optional[str] = None
    interval: Optional[float] = 0.0
    # For "hotkey"
    keys: Optional[List[str]] = None
    # For "keydown" / "keyup"
    key: Optional[str] = None


def _build_args(req: KeyboardRequest) -> Dict[str, Any]:
    """Translate request fields into the args dict expected by keyboard_worker."""
    if req.action == "type":
        return {"text": req.text or "", "interval": req.interval or 0.0}
    elif req.action == "hotkey":
        return {"keys": req.keys or []}
    elif req.action in ("keydown", "keyup"):
        return {"key": req.key or ""}
    return {}


@router.post("/keyboard")
async def keyboard_action(req: KeyboardRequest):
    try:
        if req.action == "type" and not req.text:
            return {"success": False, "data": None, "error": "text is required for action 'type'", "timed_out": False}
        if req.action == "hotkey" and not req.keys:
            return {"success": False, "data": None, "error": "keys must be a non-empty list for action 'hotkey'", "timed_out": False}
        if req.action in ("keydown", "keyup") and not req.key:
            return {"success": False, "data": None, "error": f"key is required for action '{req.action}'", "timed_out": False}

        args = _build_args(req)

        q: queue.Queue = queue.Queue()
        t = threading.Thread(
            target=kw_module.keyboard_worker,
            args=(q, req.action, args),
            daemon=True,
        )
        t.start()

        loop = asyncio.get_running_loop()
        # run_in_executor keeps the async loop responsive while the thread runs
        await loop.run_in_executor(None, t.join, KEYBOARD_TIMEOUT)

        if t.is_alive():
            return {
                "success": False,
                "data": None,
                "error": "operation timed out",
                "timed_out": True,
            }

        try:
            result = q.get_nowait()
        except queue.Empty:
            result = {"success": False, "error": "worker exited without result", "timed_out": False}

        # Track held-key state on success
        if result.get("success"):
            with _held_keys_lock:
                if req.action == "keydown" and req.key:
                    _held_keys.add(req.key)
                elif req.action == "keyup" and req.key:
                    _held_keys.discard(req.key)

        return result

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
