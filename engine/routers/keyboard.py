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
        args = _build_args(req)

        q: multiprocessing.Queue = multiprocessing.Queue()
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
        return result

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
