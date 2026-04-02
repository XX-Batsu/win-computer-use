"""
routers/keyboard.py — POST /keyboard

Dispatches keyboard actions (type, hotkey, keydown, keyup) through a subprocess
worker to avoid blocking the async event loop and to safely isolate pyautogui's
low-level keyboard calls.

The keyboard_worker module must be a top-level importable module because
Windows multiprocessing uses 'spawn'.
"""

import asyncio
import multiprocessing
from queue import Empty
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


def get_held_keys() -> set[str]:
    """Return the set of currently held keys."""
    return set(_held_keys)


def release_all_held_keys() -> list[str]:
    """Release all held keys via subprocess workers. Returns keys released."""
    released = []
    for key in list(_held_keys):
        q: multiprocessing.Queue = multiprocessing.Queue()
        try:
            proc = multiprocessing.Process(
                target=kw_module.keyboard_worker,
                args=(q, "keyup", {"key": key}),
            )
            proc.start()
            proc.join(5)
            if proc.is_alive():
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                q.close()
                q.join_thread()
            except Exception:  # noqa: BLE001
                pass
        released.append(key)
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
    q: multiprocessing.Queue | None = None
    try:
        args = _build_args(req)

        q = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=kw_module.keyboard_worker,
            args=(q, req.action, args),
        )
        proc.start()

        loop = asyncio.get_running_loop()
        # run_in_executor lets the async loop stay responsive while we wait for
        # the subprocess to finish (proc.join blocks its OS thread, not the loop)
        await loop.run_in_executor(None, proc.join, KEYBOARD_TIMEOUT)

        if proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                try:
                    proc.kill()
                except OSError:
                    pass
            return {
                "success": False,
                "data": None,
                "error": "operation timed out",
                "timed_out": True,
            }

        import queue as _queue
        try:
            result = q.get_nowait()
        except _queue.Empty:
            result = {"success": False, "error": "worker exited without result", "timed_out": False}

        # Track held-key state on success
        if result.get("success"):
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
    finally:
        if q is not None:
            try:
                q.close()
                q.join_thread()
            except Exception:  # noqa: BLE001
                pass
