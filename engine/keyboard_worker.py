"""
keyboard_worker.py — Worker function for threaded keyboard input.
"""

import queue

import pyautogui


def keyboard_worker(q: queue.Queue, action: str, args: dict) -> None:
    """
    Always puts exactly one item into q: success result or error sentinel.
    actions: "type", "hotkey", "keydown", "keyup"
    """
    try:
        result = _run_pyautogui(action, args)
        q.put({"success": True, "data": result, "error": None, "timed_out": False})
    except Exception as e:
        q.put({"success": False, "data": None, "error": str(e), "timed_out": False})


def _run_pyautogui(action: str, args: dict):
    if action == "type":
        text = args["text"]
        interval = args.get("interval", 0.0)
        # pyautogui.typewrite() only supports ASCII printable characters.
        # For text containing non-ASCII, use clipboard-paste fallback.
        has_non_ascii = any(ord(c) > 126 or (ord(c) < 32 and c not in ('\t', '\n', '\r')) for c in text)
        if has_non_ascii:
            import win32clipboard
            # Atomically set text on the clipboard (lock held only during set),
            # then paste via Ctrl+V. Note: a brief race window exists between
            # CloseClipboard() and the Ctrl+V keypress; eliminating it would
            # require bypassing the clipboard via SendInput Unicode injection.
            win32clipboard.OpenClipboard()
            try:
                try:
                    old = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                except Exception:
                    # Clipboard was empty or held a non-text format (image, file
                    # list, etc.). We cannot save/restore those formats, so we
                    # leave the clipboard with our text after the paste.
                    old = None
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            pyautogui.hotkey("ctrl", "v")
            # Restore original text only if we saved it; if the clipboard held
            # a non-text format we cannot restore it, so we leave it as-is.
            if old is not None:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, old)
                finally:
                    win32clipboard.CloseClipboard()
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
