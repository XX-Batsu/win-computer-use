"""
routers/desktop.py — GET /desktops, POST /desktop/switch, POST /desktop/create,
                     DELETE /desktop/{index}

Virtual desktop management via pyvda (0.5.x API).

If pyvda fails to initialise at import time (e.g. because the Windows Virtual
Desktop API is unavailable) every endpoint returns HTTP 503 so the rest of the
engine continues to function normally.

pyvda 0.5.x API used:
    get_virtual_desktops()        → list[VirtualDesktop] (1-based .number)
    VirtualDesktop.current()      → VirtualDesktop (1-based)
    VirtualDesktop(n).go()        → switch to desktop n (1-based)
    VirtualDesktop.create()       → classmethod, creates new desktop
    VirtualDesktop(n).remove()    → remove desktop n (1-based)

All external indexes are 0-based; we add 1 when calling pyvda.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()

# Attempt to import pyvda once at module load time.
try:
    from pyvda import VirtualDesktop, get_virtual_desktops
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
        desktops = get_virtual_desktops()
        current_number = VirtualDesktop.current().number
        result = [
            {
                "index": vd.number - 1,
                "name": vd.name or f"Desktop {vd.number}",
                "current": (vd.number == current_number),
            }
            for vd in desktops
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
        count = len(get_virtual_desktops())
        if req.index < 0 or req.index >= count:
            return {
                "success": False,
                "data": None,
                "error": f"Desktop index {req.index} out of range (0–{count - 1})",
                "timed_out": False,
            }
        VirtualDesktop(req.index + 1).go()
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
        count_before = len(get_virtual_desktops())
        VirtualDesktop.create()
        count_after = len(get_virtual_desktops())
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
        count = len(get_virtual_desktops())
        if index < 0 or index >= count:
            return {
                "success": False,
                "data": None,
                "error": f"Desktop index {index} out of range (0–{count - 1})",
                "timed_out": False,
            }
        VirtualDesktop(index + 1).remove()
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
