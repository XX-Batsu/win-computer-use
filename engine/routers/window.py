"""
routers/window.py — GET /windows, POST /window/focus, POST /window/state

Window management via pywin32.
/window/focus uses the full AttachThreadInput sequence to reliably foreground
a window even when the shell is running in a non-interactive session.
"""

from typing import Literal

import win32api
import win32con
import win32gui
import win32process
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /windows
# ---------------------------------------------------------------------------

def _enum_windows_callback(hwnd, result_list):
    if not win32gui.IsWindowVisible(hwnd):
        return True
    title = win32gui.GetWindowText(hwnd)
    if not title:
        return True
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:  # noqa: BLE001
        pid = None
    result_list.append({"hwnd": hwnd, "title": title, "pid": pid})
    return True


@router.get("/windows")
async def list_windows():
    try:
        windows = []
        win32gui.EnumWindows(_enum_windows_callback, windows)
        return {
            "success": True,
            "data": windows,
            "error": None,
            "timed_out": False,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }


# ---------------------------------------------------------------------------
# POST /window/focus
# ---------------------------------------------------------------------------

class WindowFocusRequest(BaseModel):
    hwnd: int


@router.post("/window/focus")
async def focus_window(req: WindowFocusRequest):
    hwnd = req.hwnd
    caller_tid = None
    target_tid = None
    attached = False

    try:
        if not win32gui.IsWindow(hwnd):
            return {
                "success": False,
                "data": None,
                "error": f"hwnd {hwnd} is not a valid window",
                "timed_out": False,
            }

        # Step 1 & 2: get thread IDs
        target_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
        caller_tid = win32api.GetCurrentThreadId()

        # Step 3: attach input queues
        if caller_tid != target_tid:
            win32process.AttachThreadInput(caller_tid, target_tid, True)
            attached = True

        try:
            # Step 4: restore if minimised
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            # Step 5–7: bring to front
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetFocus(hwnd)
        finally:
            # Step 8: always detach
            if attached and caller_tid != target_tid:
                win32process.AttachThreadInput(caller_tid, target_tid, False)

        # Step 9: verify
        fg = win32gui.GetForegroundWindow()
        if fg != hwnd:
            return {
                "success": False,
                "data": {"hwnd": hwnd, "foreground": fg},
                "error": "Window focused but GetForegroundWindow did not confirm",
                "timed_out": False,
            }

        return {
            "success": True,
            "data": {"hwnd": hwnd, "focused": True},
            "error": None,
            "timed_out": False,
        }

    except Exception as e:  # noqa: BLE001
        # Ensure detach even on unexpected exception
        try:
            if attached and caller_tid is not None and target_tid is not None:
                win32process.AttachThreadInput(caller_tid, target_tid, False)
        except Exception:  # noqa: BLE001
            pass
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }


# ---------------------------------------------------------------------------
# POST /window/state
# ---------------------------------------------------------------------------

class WindowStateRequest(BaseModel):
    hwnd: int
    state: Literal["maximize", "minimize", "restore"]


_STATE_MAP = {
    "maximize": win32con.SW_MAXIMIZE,
    "minimize": win32con.SW_MINIMIZE,
    "restore": win32con.SW_RESTORE,
}


@router.post("/window/state")
async def set_window_state(req: WindowStateRequest):
    try:
        if not win32gui.IsWindow(req.hwnd):
            return {
                "success": False,
                "data": None,
                "error": f"hwnd {req.hwnd} is not a valid window",
                "timed_out": False,
            }
        cmd = _STATE_MAP[req.state]
        win32gui.ShowWindow(req.hwnd, cmd)
        return {
            "success": True,
            "data": {"hwnd": req.hwnd, "state": req.state},
            "error": None,
            "timed_out": False,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
