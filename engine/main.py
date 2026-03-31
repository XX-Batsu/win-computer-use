"""
main.py — FastAPI engine entry point for win-computer-use.

Startup sequence
----------------
1. multiprocessing.freeze_support()     — must be first for frozen-exe / spawn
2. Load .env via python-dotenv
3. Validate ENGINE_SECRET               — refuse to start if not set
4. Validate required dependencies       — log pass/fail per library
5. Build per-monitor DPI map
6. Set pyautogui.FAILSAFE = False
7. Mount routers, attach auth middleware
8. Expose /health and /reload-dpi directly on app

Binding: 127.0.0.1:8765 (loopback only)
"""

# ---------------------------------------------------------------------------
# Step 1: freeze_support MUST be called before any other code so that Windows
# multiprocessing (spawn mode) works correctly in both frozen executables and
# normal interpreter invocations.
# ---------------------------------------------------------------------------
import multiprocessing
multiprocessing.freeze_support()

# ---------------------------------------------------------------------------
# Standard-library imports (safe to import before dotenv)
# ---------------------------------------------------------------------------
import ctypes
import ctypes.wintypes
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Ensure the engine/ directory is on sys.path so keyboard_worker is importable
# as a bare top-level module name in spawned worker processes on Windows.
# ---------------------------------------------------------------------------
_engine_dir = str(Path(__file__).parent)
if _engine_dir not in sys.path:
    sys.path.insert(0, _engine_dir)

# ---------------------------------------------------------------------------
# Step 2: Load .env before anything reads os.environ
# ---------------------------------------------------------------------------
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

# ---------------------------------------------------------------------------
# Step 3: Refuse to start without ENGINE_SECRET
# ---------------------------------------------------------------------------
if not os.environ.get("ENGINE_SECRET"):
    print(
        "FATAL: ENGINE_SECRET environment variable is not set. "
        "Set it in your environment or .env file before starting the engine.",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging setup (after dotenv so LOG_LEVEL can come from .env)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step 4: Validate required dependencies — log pass/fail per library
# ---------------------------------------------------------------------------
REQUIRED_LIBS = ["mss", "pyautogui", "PIL", "win32api", "pyperclip"]

_dep_status: Dict[str, Any] = {}


def _check_dep(display_name: str, import_path: str) -> bool:
    try:
        __import__(import_path)
        _dep_status[display_name] = True
        logger.info("dependency %-20s  OK", display_name)
        return True
    except Exception as exc:  # noqa: BLE001
        _dep_status[display_name] = False
        logger.warning("dependency %-20s  FAILED: %s", display_name, exc)
        return False


for _lib in REQUIRED_LIBS:
    _check_dep(_lib, _lib)

# pyvda is optional — log availability separately
try:
    import pyvda  # noqa: F401
    _pyvda_available = True
    logger.info("pyvda available")
except Exception as _pyvda_exc:  # noqa: BLE001
    _pyvda_available = False
    logging.warning(f"pyvda unavailable: {_pyvda_exc} — /desktop/* endpoints will return 503")

# ---------------------------------------------------------------------------
# Step 5: Per-monitor DPI map — single source of truth is dpi_utils
# ---------------------------------------------------------------------------
from engine import dpi_utils  # noqa: E402

# Populate dpi_utils._monitor_map at startup so all routers share one map.
dpi_utils.reload_monitor_map()


def get_dpi_map() -> List[Dict]:
    """Return the cached monitor map (delegates to dpi_utils)."""
    return dpi_utils.get_monitor_map()


def logical_to_physical(logical_x: int, logical_y: int) -> Tuple[int, int]:
    """Convert logical pixel coords to physical (delegates to dpi_utils)."""
    px, py, _ = dpi_utils.logical_to_physical(logical_x, logical_y, dpi_utils.get_monitor_map())
    return px, py

# ---------------------------------------------------------------------------
# Step 6: Configure pyautogui
# ---------------------------------------------------------------------------
try:
    import pyautogui
    pyautogui.FAILSAFE = False
    logger.info("pyautogui.FAILSAFE set to False")
except Exception as exc:  # noqa: BLE001
    logger.warning("Could not configure pyautogui: %s", exc)

# ---------------------------------------------------------------------------
# FastAPI app (defined after all startup validation)
# ---------------------------------------------------------------------------
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from engine.middleware.auth import BearerAuthMiddleware  # noqa: E402
from engine.routers import (  # noqa: E402
    screenshot,
    mouse,
    keyboard,
    window,
    clipboard,
    shell,
    desktop,
)

ENGINE_VERSION = "0.1.0"

app = FastAPI(
    title="Claude Win PC-Use Engine",
    version=ENGINE_VERSION,
    docs_url=None,
    redoc_url=None,
)

# Step 7a: Authentication middleware — applied to ALL routes
app.add_middleware(BearerAuthMiddleware)

# Step 7b: Mount routers
app.include_router(screenshot.router)
app.include_router(mouse.router)
app.include_router(keyboard.router)
app.include_router(window.router)
app.include_router(clipboard.router)
app.include_router(shell.router)
app.include_router(desktop.router)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health(request: Request):
    return {
        "success": True,
        "data": {
            "version": ENGINE_VERSION,
            "dpi_map": get_dpi_map(),
            "pyvda_available": _pyvda_available,
            "dependencies": _dep_status,
        },
        "error": None,
        "timed_out": False,
    }


# ---------------------------------------------------------------------------
# /reload-dpi
# ---------------------------------------------------------------------------
@app.post("/reload-dpi")
async def reload_dpi():
    new_map = dpi_utils.reload_monitor_map()
    return {"success": True, "data": {"dpi_map": new_map}, "error": None, "timed_out": False}


# ---------------------------------------------------------------------------
# Step 8: Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
