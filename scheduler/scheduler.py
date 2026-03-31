"""
scheduler.py — Cron-based scheduler for Windows Computer Use tasks.

Loads tasks from tasks.yaml, logs next run times on startup, and dispatches
tasks in subprocesses when their cron expression fires.

Usage:
  python scheduler.py [--tasks <path/to/tasks.yaml>]

Environment variables (may be set in project-root .env):
  TASKS_FILE     — override default tasks file location
  ENGINE_URL     — Engine HTTP API base URL
  ENGINE_SECRET  — bearer token
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

SCHEDULER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCHEDULER_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("scheduler")

# ---------------------------------------------------------------------------
# Lazy import of run helpers
# ---------------------------------------------------------------------------

# Add scheduler dir to path so we can import protocol + run
if str(SCHEDULER_DIR) not in sys.path:
    sys.path.insert(0, str(SCHEDULER_DIR))

from run import load_tasks, sanitize_name  # noqa: E402
from protocol import TaskDefinition  # noqa: E402


# ===========================================================================
# Scheduling helpers
# ===========================================================================

def _next_run(cron_expr: str, after: datetime) -> datetime:
    """Return the next datetime at which cron_expr fires after `after`."""
    itr = croniter(cron_expr, after)
    return itr.get_next(datetime)


def log_startup_schedule(tasks: dict[str, TaskDefinition]) -> None:
    """Log each task and its next scheduled run time."""
    now = datetime.now(timezone.utc).astimezone()
    logger.info("Scheduler starting — %d task(s) loaded:", len(tasks))
    for name, task in tasks.items():
        nxt = _next_run(task.cron, now)
        logger.info(
            "  %-40s cron=%-20s next=%s",
            f"{name!r}",
            task.cron,
            nxt.isoformat(timespec="seconds"),
        )


# ===========================================================================
# Dispatch: spawn run.py in a subprocess
# ===========================================================================

def _spawn_task(task: TaskDefinition) -> subprocess.Popen:  # type: ignore[type-arg]
    """
    Launch run.py for `task` in a new subprocess.
    Returns the Popen handle (scheduler does not wait — fire and forget).
    """
    run_script = SCHEDULER_DIR / "run.py"
    cmd = [sys.executable, str(run_script), "--task", task.name]
    logger.info("Spawning task %r: %s", task.name, " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )
    return proc


# ===========================================================================
# Main scheduling loop
# ===========================================================================

def run_scheduler(tasks_file: Path | None = None) -> None:
    """
    Load tasks and run the scheduling loop indefinitely.
    Raises on unrecoverable errors (tasks file missing, validation failure).
    """
    tasks = load_tasks(tasks_file)
    log_startup_schedule(tasks)

    # Track active child processes so we can log completions
    active: dict[str, subprocess.Popen] = {}  # type: ignore[type-arg]

    # Seed last-checked time to now so we don't fire tasks immediately on start
    now = datetime.now(timezone.utc).astimezone()
    last_check: dict[str, datetime] = {name: now for name in tasks}

    logger.info("Entering scheduling loop (Ctrl-C to stop)")
    try:
        while True:
            now = datetime.now(timezone.utc).astimezone()

            # Reap finished child processes
            for name in list(active.keys()):
                proc = active[name]
                rc = proc.poll()
                if rc is not None:
                    output = ""
                    if proc.stdout:
                        try:
                            output = proc.stdout.read().decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    logger.info(
                        "Task %r subprocess finished (rc=%d)%s",
                        name,
                        rc,
                        f": {output[:200]}" if output.strip() else "",
                    )
                    del active[name]

            # Check each task
            for name, task in tasks.items():
                # Is the task due since last check?
                prev = last_check[name]
                itr = croniter(task.cron, prev)
                next_fire = itr.get_next(datetime)

                if next_fire <= now:
                    last_check[name] = now  # update before spawning

                    if name in active:
                        logger.warning(
                            "Task %r is still running from previous invocation — skipping",
                            name,
                        )
                        continue

                    try:
                        proc = _spawn_task(task)
                        active[name] = proc
                    except Exception as exc:
                        logger.error("Failed to spawn task %r: %s", name, exc)

            # Sleep until the next earliest scheduled fire
            sleep_seconds = _seconds_until_next_fire(tasks, now)
            # Cap at 60s so we reap children promptly and react to clock skew
            sleep_seconds = min(sleep_seconds, 60)
            logger.debug("Sleeping %.1f seconds until next check", sleep_seconds)
            time.sleep(max(1.0, sleep_seconds))

    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user (KeyboardInterrupt)")
        # Wait for running children to finish
        for name, proc in active.items():
            logger.info("Waiting for task %r (pid=%d) to finish...", name, proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # 已盡力，繼續退出


def _seconds_until_next_fire(
    tasks: dict[str, TaskDefinition],
    after: datetime,
) -> float:
    """Return seconds until the soonest next cron fire across all tasks."""
    if not tasks:
        return 60.0
    earliest = min(
        (_next_run(t.cron, after) for t in tasks.values()),
    )
    delta = (earliest - after).total_seconds()
    return max(1.0, delta)


# ===========================================================================
# CLI
# ===========================================================================

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cron scheduler for Windows Computer Use tasks"
    )
    parser.add_argument(
        "--tasks",
        default=None,
        metavar="PATH",
        help="Path to tasks YAML file (default: ../tasks/tasks.yaml)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    ns = _parse_args()
    tasks_path = Path(ns.tasks) if ns.tasks else None
    try:
        run_scheduler(tasks_path)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("Scheduler failed to start: %s", exc)
        sys.exit(1)
