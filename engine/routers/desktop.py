"""
routers/desktop.py — GET /desktops, POST /desktop/switch, POST /desktop/create,
                     DELETE /desktop/{index}

Virtual desktop management via pyvda (0.3.x API).

If pyvda fails to initialise at import time (e.g. because the Windows Virtual
Desktop API is unavailable) every endpoint returns HTTP 503 so the rest of the
engine continues to function normally.

pyvda 0.3.x API used:
    pyvda.GetCurrentDesktopNumber()  → int (0-based)
    pyvda.GetDesktopCount()          → int
    pyvda.MoveToDesktopNumber(n)     → None
    pyvda.CreateDesktop()            → None (or new desktop object)
    pyvda.RemoveDesktopByNumber(n)   → None
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

# Attempt to import pyvda once at module load time.
try:
    import pyvda  # noqa: F401
    _PYVDA_AVAILABLE = True
    _PYVDA_ERROR: str = ""
except Exception as _exc:  # noqa: BLE001
    _PYVDA_AVAILABLE = False
    _PYVDA_ERROR = str(_exc)


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "data": None,
            "error": f"pyvda unavailable: {_PYVDA_ERROR}",
            "timed_out": False,
        },
    )


# ---------------------------------------------------------------------------
# GET /desktops
# ---------------------------------------------------------------------------

@router.get("/desktops")
async def list_desktops():
    if not _PYVDA_AVAILABLE:
        return _unavailable()
    try:
        count = pyvda.GetDesktopCount()
        current = pyvda.GetCurrentDesktopNumber()
        result = [
            {"index": i, "name": f"Desktop {i + 1}", "current": (i == current)}
            for i in range(count)
        ]
        return {
            "success": True,
            "data": result,
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
# POST /desktop/switch
# ---------------------------------------------------------------------------

class DesktopSwitchRequest(BaseModel):
    index: int


@router.post("/desktop/switch")
async def switch_desktop(req: DesktopSwitchRequest):
    if not _PYVDA_AVAILABLE:
        return _unavailable()
    try:
        count = pyvda.GetDesktopCount()
        if req.index < 0 or req.index >= count:
            return {
                "success": False,
                "data": None,
                "error": f"Desktop index {req.index} out of range (0–{count - 1})",
                "timed_out": False,
            }
        pyvda.MoveToDesktopNumber(req.index)
        return {
            "success": True,
            "data": {"switched_to": req.index},
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
# POST /desktop/create
# ---------------------------------------------------------------------------

@router.post("/desktop/create")
async def create_desktop():
    """Create a new virtual desktop and return its index.

    Note (BUG-018): ``new_index`` is calculated by calling
    ``GetDesktopCount()`` immediately after ``CreateDesktop()``.  If pyvda
    completes the creation asynchronously the count may not yet have
    incremented, in which case ``new_index`` is returned as ``null`` rather
    than an incorrect value.
    """
    if not _PYVDA_AVAILABLE:
        return _unavailable()
    try:
        count_before = pyvda.GetDesktopCount()
        pyvda.CreateDesktop()
        count_after = pyvda.GetDesktopCount()
        # Guard against async delay: only trust the index if the count grew by 1.
        if count_after == count_before + 1:
            new_index = count_after - 1
        else:
            new_index = None
        return {
            "success": True,
            "data": {"new_index": new_index},
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
# DELETE /desktop/{index}
# ---------------------------------------------------------------------------

@router.delete("/desktop/{index}")
async def delete_desktop(index: int):
    if not _PYVDA_AVAILABLE:
        return _unavailable()
    try:
        count = pyvda.GetDesktopCount()
        if index < 0 or index >= count:
            return {
                "success": False,
                "data": None,
                "error": f"Desktop index {index} out of range (0–{count - 1})",
                "timed_out": False,
            }
        pyvda.RemoveDesktopByNumber(index)
        return {
            "success": True,
            "data": {"deleted_index": index},
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
