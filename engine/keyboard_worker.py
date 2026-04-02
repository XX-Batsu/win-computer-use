"""
keyboard_worker.py — Standalone top-level module for subprocess keyboard input.

Must remain importable by a fresh Python process because Windows multiprocessing
uses 'spawn'. No closures, no __main__ guard around the worker function.
"""

import sys
from pathlib import Path
# Ensure engine/ parent is on sys.path for spawn-mode subprocess imports
_worker_dir = str(Path(__file__).parent)
if _worker_dir not in sys.path:
    sys.path.insert(0, _worker_dir)

import multiprocessing
import pyautogui


def keyboard_worker(queue: multiprocessing.Queue, action: str, args: dict) -> None:
    """
    Always puts exactly one item into queue: success result or error sentinel.
    actions: "type", "hotkey", "keydown", "keyup"
    """
    try:
        result = _run_pyautogui(action, args)
        queue.put({"success": True, "data": result, "error": None, "timed_out": False})
    except Exception as e:
        queue.put({"success": False, "data": None, "error": str(e), "timed_out": False})


def _run_pyautogui(action: str, args: dict):
    if action == "type":
        text = args["text"]
        interval = args.get("interval", 0.0)
        # pyautogui.typewrite() only supports ASCII printable characters.
        # For text containing non-ASCII, use clipboard-paste fallback.
        has_non_ascii = any(ord(c) > 126 or (ord(c) < 32 and c not in ('\t', '\n', '\r')) for c in text)
        if has_non_ascii:
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.typewrite(text, interval=interval)
        return None
    elif action == "hotkey":
        pyautogui.hotkey(*args["keys"])
        return None
    elif action == "keydown":
        pyautogui.keyDown(args["key"])
        return None
    elif action == "keyup":
        pyautogui.keyUp(args["key"])
        return None
    else:
        raise ValueError(f"Unknown keyboard action: {action}")
