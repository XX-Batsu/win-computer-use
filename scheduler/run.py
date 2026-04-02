"""
run.py — Task execution loop for the Windows Computer Use scheduler.

Two usage modes:
  1. Imported and called by scheduler.py:  run_task(task_name)
  2. CLI:  python run.py --task <name> [--model <model>]

Environment variables (may be set in project-root .env):
  ENGINE_URL     — default http://127.0.0.1:8765
  ENGINE_SECRET  — bearer token for Engine HTTP API
  TASKS_FILE     — path to tasks YAML (default ../tasks/tasks.yaml rel to scheduler/)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCHEDULER_DIR = Path(__file__).parent
if str(_SCHEDULER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCHEDULER_DIR))

import argparse
import base64
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

import psutil
import requests
import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Bootstrap: load .env from project root, configure logging
# ---------------------------------------------------------------------------

SCHEDULER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCHEDULER_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("run")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

ENGINE_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:8765").rstrip("/")
ENGINE_SECRET = os.environ.get("ENGINE_SECRET", "")
TASKS_FILE_DEFAULT = PROJECT_ROOT / "tasks" / "tasks.yaml"

# ---------------------------------------------------------------------------
# Lazy import of protocol (avoids circular issues at top-level)
# ---------------------------------------------------------------------------

from protocol import (  # noqa: E402
    COMMAND_SCHEMA,
    ClaudeResponse,
    Command,
    TaskDefinition,
)


# ===========================================================================
# Task YAML loading
# ===========================================================================

def load_tasks(tasks_file: Path | None = None) -> dict[str, TaskDefinition]:
    """Load and validate tasks.yaml; return {name: TaskDefinition}."""
    if tasks_file is None:
        env_path = os.environ.get("TASKS_FILE")
        tasks_file = Path(env_path) if env_path else TASKS_FILE_DEFAULT

    if not tasks_file.exists():
        raise FileNotFoundError(f"Tasks file not found: {tasks_file}")

    with open(tasks_file, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, list):
        raise ValueError("tasks.yaml must be a YAML list of task definitions")

    tasks: dict[str, TaskDefinition] = {}
    for idx, item in enumerate(raw):
        try:
            td = TaskDefinition.model_validate(item)
        except ValidationError as exc:
            raise ValueError(
                f"Task at index {idx} failed validation:\n{exc}"
            ) from exc

        if td.name in tasks:
            logger.warning(
                "DUPLICATE TASK NAME IGNORED: %r at index %d — keeping first occurrence",
                td.name,
                idx,
            )
            continue
        tasks[td.name] = td

    return tasks


# ===========================================================================
# Filesystem helpers
# ===========================================================================

def sanitize_name(name: str) -> str:
    """Replace characters outside [a-zA-Z0-9_-] with underscores."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)


def ts_now() -> datetime:
    """Current local time with UTC offset."""
    return datetime.now(timezone.utc).astimezone()


def ts_dir(dt: datetime) -> str:
    """ISO-8601 timestamp safe for use in directory names (colons → hyphens)."""
    return dt.isoformat(timespec="seconds").replace(":", "-")


# ===========================================================================
# PID lockfile
# ===========================================================================

def _lockfile_path(sanitized_name: str) -> Path:
    return PROJECT_ROOT / "logs" / f"{sanitized_name}.lock"


def _acquire_lock(sanitized_name: str) -> Path:
    """
    Acquire a PID-based lockfile.  Returns the lockfile path on success.
    Raises RuntimeError if a live process already holds the lock.
    Uses atomic O_CREAT|O_EXCL to avoid TOCTOU race conditions.
    """
    lock_path = _lockfile_path(sanitized_name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    started_at = psutil.Process(pid).create_time()
    payload = {"pid": pid, "started_at": started_at}
    payload_bytes = json.dumps(payload).encode("utf-8")

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload_bytes)
        finally:
            os.close(fd)
        logger.debug("Acquired lock: %s", lock_path)
        return lock_path
    except FileExistsError:
        # Lockfile already exists — check if the holder is still alive
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            existing_pid = existing["pid"]
            existing_started = existing["started_at"]
        except (json.JSONDecodeError, KeyError, OSError):
            logger.warning("Malformed lockfile %s — treating as stale", lock_path)
        else:
            if psutil.pid_exists(existing_pid):
                try:
                    actual_ct = psutil.Process(existing_pid).create_time()
                except psutil.NoSuchProcess:
                    pass  # race: process died between pid_exists and create_time
                else:
                    if abs(actual_ct - existing_started) < 1.0:
                        raise RuntimeError(
                            f"Task {sanitized_name!r} already running as PID "
                            f"{existing_pid} (started {existing_started}). Skipping."
                        )
                    else:
                        logger.warning(
                            "Lockfile PID %d reused by different process — stale lock",
                            existing_pid,
                        )
            else:
                logger.info("Stale lockfile (PID %d not alive) — overwriting", existing_pid)

        # Stale lock — remove and re-acquire atomically
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass  # another process already removed it
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload_bytes)
            finally:
                os.close(fd)
        except FileExistsError:
            raise RuntimeError(
                f"Lost lock race for {sanitized_name!r} — another scheduler acquired it"
            )
        logger.debug("Acquired lock (replaced stale): %s", lock_path)
        return lock_path


def _release_lock(lock_path: Path) -> None:
    try:
        try:
            existing = json.loads(lock_path.read_text(encoding="utf-8"))
            lock_pid = existing.get("pid")
        except (json.JSONDecodeError, KeyError, OSError):
            lock_pid = None

        if lock_pid is not None and lock_pid != os.getpid():
            logger.warning(
                "Refusing to delete lockfile %s — PID mismatch (file=%d, ours=%d)",
                lock_path,
                lock_pid,
                os.getpid(),
            )
            return

        lock_path.unlink(missing_ok=True)
        logger.debug("Released lock: %s", lock_path)
    except OSError as exc:
        logger.warning("Failed to remove lockfile %s: %s", lock_path, exc)


# ===========================================================================
# Engine HTTP client
# ===========================================================================

def _engine_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ENGINE_SECRET}"}


def _engine_get(path: str, *, timeout: int = 10, **kwargs: Any) -> dict[str, Any]:
    url = f"{ENGINE_URL}{path}"
    resp = requests.get(url, headers=_engine_headers(), timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _engine_post(path: str, body: dict[str, Any] | None = None, *, timeout: int = 10) -> dict[str, Any]:
    url = f"{ENGINE_URL}{path}"
    resp = requests.post(
        url,
        headers=_engine_headers(),
        json=body or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _engine_delete(path: str, *, timeout: int = 10) -> dict[str, Any]:
    url = f"{ENGINE_URL}{path}"
    resp = requests.delete(url, headers=_engine_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def check_engine_health() -> bool:
    """Return True if Engine is reachable and healthy."""
    try:
        result = _engine_get("/health")
        return result.get("success", False)
    except Exception as exc:
        logger.error("Engine health check failed: %s", exc)
        return False


_image_scale: float = 1.0
"""Scale factor from the last screenshot (image pixels / logical pixels)."""

_crop_offset_x: int = 0
_crop_offset_y: int = 0
"""Logical-pixel origin of the last crop (0,0 for full-screen).
Mirrors the MCP layer's cropOffsetX/Y so headless crop screenshots work."""


def take_screenshot() -> bytes:
    """Capture full-screen screenshot; return raw PNG bytes.

    Also updates the module-level ``_image_scale`` so that
    ``dispatch_command`` can remap coordinates from image-pixel space
    back to logical-pixel space.
    """
    global _image_scale
    result = _engine_post("/screenshot", {}, timeout=60)
    if not result.get("success"):
        raise RuntimeError(f"Screenshot failed: {result.get('error')}")
    _image_scale = result["data"].get("image_scale", 1.0)
    image_b64: str = result["data"]["image"]
    return base64.b64decode(image_b64)


# ===========================================================================
# Command → Engine HTTP dispatch
# ===========================================================================

def _to_logical_pos(v: float, offset: int) -> int:
    """Map a position from image-pixel space to full-screen logical-pixel space."""
    if _image_scale <= 0.0:
        return int(v) + offset
    base = round(v / _image_scale) if _image_scale != 1.0 else int(v)
    return base + offset


def _to_logical_x(v: float) -> int:
    """Map an X position: scale + crop offset."""
    return _to_logical_pos(v, _crop_offset_x)


def _to_logical_y(v: float) -> int:
    """Map a Y position: scale + crop offset."""
    return _to_logical_pos(v, _crop_offset_y)


def _to_logical_dim(v: float) -> int:
    """Map a dimension (width/height): scale only, no offset."""
    if _image_scale <= 0.0:
        return int(v)
    return round(v / _image_scale) if _image_scale != 1.0 else int(v)


def dispatch_command(cmd: Command) -> dict[str, Any]:
    """
    Execute one validated Command against the Engine HTTP API.
    Returns the full Engine response dict {success, data, error, timed_out}.
    """
    tool = cmd.tool  # type: ignore[union-attr]
    args = cmd.args  # type: ignore[union-attr]

    # --- Screenshot ---
    if tool == "screenshot":
        global _image_scale, _crop_offset_x, _crop_offset_y
        a = args  # ScreenshotArgs
        is_crop = a.width is not None and a.height is not None
        body: dict[str, Any] = {}
        logical_left = 0
        logical_top = 0
        if a.left is not None:
            logical_left = _to_logical_x(a.left)
            body["left"] = logical_left
        if a.top is not None:
            logical_top = _to_logical_y(a.top)
            body["top"] = logical_top
        if a.width is not None:
            body["width"] = _to_logical_dim(a.width)
        if a.height is not None:
            body["height"] = _to_logical_dim(a.height)
        result = _engine_post("/screenshot", body)
        # Update crop state to mirror MCP behaviour
        if result.get("success"):
            _image_scale = result["data"].get("image_scale", 1.0)
            if is_crop:
                _crop_offset_x = logical_left
                _crop_offset_y = logical_top
            else:
                _crop_offset_x = 0
                _crop_offset_y = 0
        return result

    # --- Mouse (coordinates remapped from image-pixel to logical) ---
    if tool == "mouse_move":
        return _engine_post("/mouse", {"action": "move", "x": _to_logical_x(args.x), "y": _to_logical_y(args.y)})
    if tool == "mouse_click":
        return _engine_post(
            "/mouse", {"action": "click", "x": _to_logical_x(args.x), "y": _to_logical_y(args.y), "button": args.button}
        )
    if tool == "mouse_drag":
        drag_body: dict[str, Any] = {
            "action": "drag",
            "x": _to_logical_x(args.x1),
            "y": _to_logical_y(args.y1),
            "x2": _to_logical_x(args.x2),
            "y2": _to_logical_y(args.y2),
            "button": args.button,
            "duration": args.duration,
        }
        if hasattr(args, "hold_before") and args.hold_before is not None:
            drag_body["hold_before"] = args.hold_before
        if hasattr(args, "steps") and args.steps is not None:
            drag_body["steps"] = args.steps
        if hasattr(args, "waypoints") and args.waypoints:
            drag_body["waypoints"] = [
                {"x": _to_logical_x(wp.x), "y": _to_logical_y(wp.y)}
                for wp in args.waypoints
            ]
        return _engine_post("/mouse", drag_body)
    if tool == "mouse_scroll":
        return _engine_post(
            "/mouse", {"action": "scroll", "x": _to_logical_x(args.x), "y": _to_logical_y(args.y), "amount": args.amount}
        )
    if tool == "mouse_double_click":
        return _engine_post(
            "/mouse", {"action": "double_click", "x": _to_logical_x(args.x), "y": _to_logical_y(args.y), "button": args.button}
        )
    if tool == "mouse_down":
        return _engine_post(
            "/mouse", {"action": "mousedown", "x": _to_logical_x(args.x), "y": _to_logical_y(args.y), "button": args.button}
        )
    if tool == "mouse_up":
        body_up: dict[str, Any] = {"action": "mouseup", "button": args.button}
        if args.x is not None and args.y is not None:
            body_up["x"] = _to_logical_x(args.x)
            body_up["y"] = _to_logical_y(args.y)
        return _engine_post("/mouse", body_up)

    # --- Keyboard ---
    if tool == "keyboard_type":
        return _engine_post(
            "/keyboard",
            {"action": "type", "text": args.text, "interval": args.interval},
        )
    if tool == "keyboard_hotkey":
        return _engine_post("/keyboard", {"action": "hotkey", "keys": args.keys})
    if tool == "keydown":
        return _engine_post("/keyboard", {"action": "keydown", "key": args.key})
    if tool == "keyup":
        return _engine_post("/keyboard", {"action": "keyup", "key": args.key})

    # --- Windows ---
    if tool == "list_windows":
        return _engine_get("/windows")
    if tool == "focus_window":
        return _engine_post("/window/focus", {"hwnd": args.hwnd})
    if tool == "set_window_state":
        return _engine_post("/window/state", {"hwnd": args.hwnd, "state": args.state})

    # --- Clipboard ---
    if tool == "get_clipboard":
        return _engine_get("/clipboard")
    if tool == "set_clipboard":
        return _engine_post("/clipboard", {"text": args.text})

    # --- Shell ---
    if tool == "run_shell":
        body = {"command": args.command, "shell": args.shell, "timeout": args.timeout}
        if args.cwd is not None:
            body["cwd"] = args.cwd
        if args.env_extra is not None:
            body["env_extra"] = args.env_extra
        return _engine_post("/shell", body, timeout=60)

    # --- Virtual Desktop ---
    if tool == "list_desktops":
        return _engine_get("/desktops")
    if tool == "switch_desktop":
        return _engine_post("/desktop/switch", {"index": args.index})
    if tool == "create_desktop":
        return _engine_post("/desktop/create")
    if tool == "delete_desktop":
        return _engine_delete(f"/desktop/{args.index}")

    raise ValueError(f"Unknown tool: {tool!r}")


# ===========================================================================
# Prompt builder
# ===========================================================================

_SYSTEM_SECTION = """\
## System

You are a Windows desktop automation agent.
Respond ONLY with valid JSON matching the schema provided via --json-schema.
Do not include any prose, explanation, or markdown outside the JSON.

Available tools and their argument shapes:
- screenshot(top?, left?, width?, height?) — capture screen or region; pass coordinates from the current screenshot directly
- mouse_move(x, y)
- mouse_click(x, y, button?) — button: "left"|"right"|"middle", default "left"
- mouse_double_click(x, y, button?) — double-click at (x, y)
- mouse_drag(x1, y1, x2, y2, button?, duration?, hold_before?, steps?, waypoints?) — button: "left"|"right"|"middle"; duration: seconds, default 0.5; waypoints: [{x, y}, ...] for non-straight paths
- mouse_scroll(x, y, amount) — amount: wheel notches, positive=up, negative=down
- mouse_down(x, y, button?) — hold button at (x, y)
- mouse_up(button?, x?, y?) — release button, optionally move to (x, y) first; x and y must be provided together
- keyboard_type(text, interval?) — type literal characters; ASCII uses individual keystrokes, non-ASCII auto-pastes via clipboard. For special keys (Enter, Tab, Escape, arrows) or combos, use keyboard_hotkey instead
- keyboard_hotkey(keys: list) — e.g. {"keys": ["ctrl","c"]}; also for single special keys: ["enter"], ["tab"], etc.
- keydown(key) / keyup(key) — always pair keydown with keyup to avoid stuck keys
- list_windows() — returns [{hwnd, title, pid}]
- focus_window(hwnd)
- set_window_state(hwnd, state) — state: "maximize"|"minimize"|"restore"
- get_clipboard() / set_clipboard(text)
- run_shell(command, shell?, cwd?, timeout?, env_extra?) — shell: "cmd"|"powershell"; timeout: 1-300s, default 30s; env_extra: {"KEY": "value"}
- list_desktops() / switch_desktop(index) / create_desktop() / delete_desktop(index)

All coordinates are screenshot pixels — use coordinates from the most recent screenshot.
Coordinates from any screenshot (full-screen or crop) can be passed directly to other tools — the scheduler remaps them automatically.
Coordinates from older screenshots are invalid after any new screenshot is taken.
NOTE: screenshot_zoom, screenshot_annotate, find_element, and element_at are NOT available in scheduled mode.

## Response rules
- `reasoning` (required): briefly explain what you observe and why you chose these commands.
- `commands`: always include at least one command per step. Do not emit an empty commands array.
- `done`: set to true ONLY when the task is fully complete and you have verified the result. Set to false if more steps are needed.
- If a previous step failed (visible in History), adapt your approach rather than blindly retrying.\
"""


def _format_args(args_obj: Any) -> str:
    """Render command args as a compact string."""
    try:
        d = args_obj.model_dump(exclude_none=True)
    except AttributeError:
        d = {}
    if not d:
        return "()"
    parts = ", ".join(f"{k}={v!r}" for k, v in d.items())
    return f"({parts})"


def _truncate(s: str, max_len: int = 200) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def build_prompt(
    task: TaskDefinition,
    step_number: int,
    screenshot_path: Path,
    step_records: list[dict[str, Any]],
) -> str:
    """Build the full step_NN_prompt.txt content."""
    lines: list[str] = [_SYSTEM_SECTION, ""]

    # --- History ---
    lines.append("## History")
    lines.append("")
    recent = step_records[-10:]  # last 10 steps
    if not recent:
        lines.append("*(no previous steps)*")
        lines.append("")
    else:
        for record in recent:
            lines.append(f"### Step {record['step']}")
            lines.append("Commands:")
            for ce in record.get("commands_executed", []):
                tool_name = ce.get("tool", "?")
                result_raw = ce.get("result", {})
                result_str = json.dumps(result_raw)
                lines.append(f"- {tool_name} → {_truncate(result_str)}")
            if record.get("error"):
                lines.append(f"Error: {record['error']}")
            lines.append("")

    # --- Task ---
    lines.append("## Task")
    lines.append("")
    # Expand %USERPROFILE% and other env vars so Claude sees real paths
    lines.append(os.path.expandvars(task.prompt))
    lines.append("")

    # --- Current State ---
    lines.append("## Current State")
    lines.append("")
    lines.append("Current screenshot: see the attached image file.")
    lines.append("")

    return "\n".join(lines)


# ===========================================================================
# run.json helpers
# ===========================================================================

def _write_run_json(run_json_path: Path, payload: dict[str, Any]) -> None:
    with open(run_json_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)


def _sanitize_result_for_log(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """Replace base64 image data with a placeholder in log output."""
    if not isinstance(result.get("data"), (str, dict)):
        return result

    data = result["data"]
    # Screenshot command always has image data
    if tool == "screenshot":
        return {**result, "data": "<omitted — base64 image data excluded from log>"}
    # Any other command whose data dict contains an 'image' key
    if isinstance(data, dict) and "image" in data:
        sanitized_data = {**data, "image": "<omitted — base64 image data excluded from log>"}
        return {**result, "data": sanitized_data}
    return result


# ===========================================================================
# Core execution loop
# ===========================================================================

def run_task(task_name: str, model_override: str | None = None) -> None:
    """
    Run a single task end-to-end.  Safe to call from scheduler.py.
    Raises on fatal errors (task not found, validation failure, engine down).
    """
    if not ENGINE_SECRET:
        raise RuntimeError("ENGINE_SECRET is not set or is empty")
    # 1. Load tasks
    try:
        tasks = load_tasks()
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Failed to load tasks: %s", exc)
        raise

    if task_name not in tasks:
        raise KeyError(f"Task {task_name!r} not found in tasks file")

    task = tasks[task_name]
    if model_override:
        # Create a copy with the overridden model
        task = task.model_copy(update={"model": model_override})

    # 2. Sanitize name for filesystem
    safe_name = sanitize_name(task.name)

    # 3. Acquire PID lockfile
    lock_path = _lockfile_path(safe_name)
    try:
        _acquire_lock(safe_name)
    except RuntimeError as exc:
        logger.info("Skipping task %r: %s", task_name, exc)
        return

    try:
        _run_task_inner(task, safe_name)
    finally:
        _release_lock(lock_path)


def _run_task_inner(task: TaskDefinition, safe_name: str) -> None:
    """Inner execution — lock is already held."""
    global _image_scale, _crop_offset_x, _crop_offset_y
    _image_scale = 1.0
    _crop_offset_x = 0
    _crop_offset_y = 0

    # 4. Check Engine health
    if not check_engine_health():
        raise RuntimeError("Engine is not healthy; aborting task")

    # 4a. Release any held mouse buttons / keyboard keys from a previous run
    try:
        _engine_post("/release-held")
    except Exception as exc:
        logger.warning("Failed to release held state: %s", exc)

    # 5. Create log directory
    run_start = ts_now()
    log_dir = PROJECT_ROOT / "logs" / safe_name / ts_dir(run_start)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Log directory: %s", log_dir)

    # 6. Write initial run.json
    run_json_path = log_dir / "run.json"
    run_data: dict[str, Any] = {
        "task": task.name,
        "model": task.model,
        "started_at": run_start.isoformat(),
        "finished_at": None,
        "status": "in_progress",
        "error": None,
        "steps": [],
    }
    _write_run_json(run_json_path, run_data)

    final_status = "failed"
    final_error: str | None = None

    try:
        # 7. Execution loop
        for step_num in range(1, task.max_steps + 1):
            step_result = _execute_step(task, safe_name, step_num, log_dir, run_data)
            run_data["steps"].append(step_result)
            _write_run_json(run_json_path, run_data)

            if step_result.get("error"):
                final_error = step_result["error"]
                final_status = "failed"
                logger.error("Step %d failed: %s", step_num, final_error)
                break

            if step_result.get("done"):
                final_status = "completed"
                logger.info("Task %r completed at step %d", task.name, step_num)
                break
        else:
            # max_steps reached without done: true
            final_status = "timed_out"
            logger.warning(
                "Task %r reached max_steps (%d) without completing",
                task.name,
                task.max_steps,
            )

    except Exception as exc:
        final_error = str(exc)
        final_status = "failed"
        logger.exception("Unexpected error in task %r", task.name)

    # 9. Write final run.json
    run_data["finished_at"] = ts_now().isoformat()
    run_data["status"] = final_status
    run_data["error"] = final_error
    _write_run_json(run_json_path, run_data)
    logger.info("Task %r finished with status: %s", task.name, final_status)

    # 11. Optional toast notification
    if task.notify_on_finish:
        _send_toast(task.name, final_status)


def _execute_step(
    task: TaskDefinition,
    safe_name: str,
    step_num: int,
    log_dir: Path,
    run_data: dict[str, Any],
) -> dict[str, Any]:
    """
    Execute one step.  Returns a step record dict.
    'error' key is set on failure; 'done' key reflects Claude's done flag.
    """
    step_str = f"step_{step_num:02d}"
    logger.info("=== Step %d / %d ===", step_num, task.max_steps)

    # 7a. Take screenshot
    screenshot_path = log_dir / f"{step_str}_pre.png"
    try:
        png_bytes = take_screenshot()
        screenshot_path.write_bytes(png_bytes)
    except Exception as exc:
        return {
            "step": step_num,
            "screenshot": None,
            "prompt_file": None,
            "claude_response_file": None,
            "commands_executed": [],
            "error": f"Screenshot failed: {exc}",
            "done": False,
        }

    # 7b. Build and write prompt
    prompt_path = log_dir / f"{step_str}_prompt.txt"
    prompt_text = build_prompt(
        task,
        step_num,
        screenshot_path,
        run_data["steps"],
    )
    prompt_path.write_text(prompt_text, encoding="utf-8")

    # 7c. Invoke claude -p
    # The screenshot is passed as a positional file argument so that the
    # Claude CLI attaches it as an image.  The prompt text (fed via stdin)
    # references "the attached image file" instead of an @-path, because
    # @-path expansion only works when the prompt is a CLI argument, not
    # when it is piped through stdin.
    schema_json = json.dumps(COMMAND_SCHEMA)
    cmd = [
        "claude",
        "-p",
        "--model", task.model,
        "--json-schema", schema_json,
        "--output-format", "json",
        str(screenshot_path.resolve()),
    ]
    claude_result = _invoke_claude(cmd, prompt_path, task.step_timeout_seconds)
    if claude_result is None:
        return {
            "step": step_num,
            "screenshot": screenshot_path.name,
            "prompt_file": prompt_path.name,
            "claude_response_file": None,
            "commands_executed": [],
            "error": "claude invocation failed after retry",
            "done": False,
        }

    if claude_result == "timed_out":
        return {
            "step": step_num,
            "screenshot": screenshot_path.name,
            "prompt_file": prompt_path.name,
            "claude_response_file": None,
            "commands_executed": [],
            "error": "claude step timed out",
            "done": False,
        }

    # 7d. Parse and validate
    try:
        structured = _extract_structured_output(claude_result)
    except ValueError as exc:
        return {
            "step": step_num,
            "screenshot": screenshot_path.name,
            "prompt_file": prompt_path.name,
            "claude_response_file": None,
            "commands_executed": [],
            "error": f"structured_output missing: {exc}",
            "done": False,
        }

    try:
        claude_response = ClaudeResponse.model_validate(structured)
    except ValidationError as exc:
        logger.error("ClaudeResponse validation error:\n%s", exc)
        return {
            "step": step_num,
            "screenshot": screenshot_path.name,
            "prompt_file": prompt_path.name,
            "claude_response_file": None,
            "commands_executed": [],
            "error": f"ClaudeResponse validation failed: {exc}",
            "done": False,
        }

    # Save response JSON
    response_path = log_dir / f"{step_str}_response.json"
    response_path.write_text(
        json.dumps(structured, indent=2, default=str), encoding="utf-8"
    )

    # 7e. Execute commands
    commands_executed: list[dict[str, Any]] = []
    step_error: str | None = None

    for cmd_obj in claude_response.commands:
        tool_name = cmd_obj.tool  # type: ignore[union-attr]
        args_obj = cmd_obj.args  # type: ignore[union-attr]

        try:
            engine_result = dispatch_command(cmd_obj)
        except Exception as exc:
            engine_result = {
                "success": False,
                "data": None,
                "error": str(exc),
                "timed_out": False,
            }

        log_result = _sanitize_result_for_log(tool_name, engine_result)
        commands_executed.append(
            {
                "tool": tool_name,
                "args": args_obj.model_dump(exclude_none=True) if hasattr(args_obj, "model_dump") else {},
                "result": log_result,
            }
        )

        if not engine_result.get("success", False):
            step_error = engine_result.get("error") or "Engine returned success=false"
            logger.error("Command %r failed: %s", tool_name, step_error)
            break  # stop executing further commands

    return {
        "step": step_num,
        "screenshot": screenshot_path.name,
        "prompt_file": prompt_path.name,
        "claude_response_file": response_path.name,
        "commands_executed": commands_executed,
        "error": step_error,
        "done": claude_response.done if step_error is None else False,
    }


# ===========================================================================
# Claude invocation helpers
# ===========================================================================

def _invoke_claude(
    cmd: list[str],
    prompt_path: Path,
    timeout_seconds: int,
) -> str | None:
    """
    Run claude with stdin from prompt_path.

    Returns:
      - stdout string on success
      - None if both attempts failed (non-zero exit or empty stdout)
      - "timed_out" on TimeoutExpired
    """
    for attempt in range(1, 3):  # try twice
        logger.debug("claude invocation attempt %d", attempt)
        try:
            with open(prompt_path, "r", encoding="utf-8") as fh:
                proc = subprocess.run(
                    cmd,
                    stdin=fh,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
        except subprocess.TimeoutExpired as exc:
            logger.warning("claude timed out on attempt %d: %s", attempt, exc)
            if exc.process is not None:
                try:
                    exc.process.kill()
                    exc.process.communicate()  # flush stdout/stderr buffers
                except OSError:
                    pass
            return "timed_out"
        except FileNotFoundError:
            logger.error("'claude' binary not found in PATH")
            return None

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0 or not stdout:
            logger.warning(
                "claude attempt %d failed (rc=%d, stdout=%r, stderr=%r)",
                attempt,
                proc.returncode,
                stdout[:200] if stdout else "",
                stderr[:200] if stderr else "",
            )
            if attempt == 2:
                return None
            continue  # retry

        return stdout

    return None  # unreachable but satisfies type-checker


def _extract_structured_output(raw_stdout: str) -> dict[str, Any]:
    """
    claude --output-format json wraps the response in an envelope.
    The structured output from --json-schema is in the 'result' key
    (or 'structured_output' in some versions).  We parse and return it.
    """
    try:
        envelope = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"claude stdout is not valid JSON: {exc}") from exc

    # Support both 'result' (current claude CLI) and 'structured_output'
    for key in ("result", "structured_output"):
        if key in envelope and envelope[key] is not None:
            val = envelope[key]
            # If it's already a dict, return it
            if isinstance(val, dict):
                return val
            # If it's a JSON string, parse it
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"structured_output is a non-JSON string: {exc}"
                    ) from exc

    raise ValueError(
        f"No structured_output/result in claude response. Keys: {list(envelope.keys())}"
    )


# ===========================================================================
# Toast notification
# ===========================================================================

def _send_toast(task_name: str, status: str) -> None:
    try:
        from winotify import Notification  # type: ignore

        toast = Notification(
            app_id="Claude Scheduler",
            title=f"Task: {task_name}",
            msg=f"Finished with status: {status}",
        )
        toast.show()
    except Exception as exc:
        logger.warning("Toast notification failed: %s", exc)


# ===========================================================================
# CLI entry point
# ===========================================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a scheduled task defined in tasks.yaml"
    )
    parser.add_argument(
        "--task",
        required=True,
        help="Task name (must match a name in tasks.yaml)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the model specified in tasks.yaml",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    ns = _parse_args()
    try:
        run_task(ns.task, model_override=ns.model)
    except (KeyError, RuntimeError, FileNotFoundError, ValueError) as exc:
        logger.error("run_task failed: %s", exc)
        sys.exit(1)
