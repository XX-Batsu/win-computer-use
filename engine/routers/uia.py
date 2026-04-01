"""
routers/uia.py — POST /find_element, POST /element_at

UI Automation element discovery using pywinauto (UIA backend).
pywinauto is an optional dependency — if unavailable, endpoints return 503.
"""

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from engine import dpi_utils

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Optional dependency: pywinauto
# ---------------------------------------------------------------------------
_pywinauto_available = False
_pywinauto_error = ""

try:
    from pywinauto import Application
    from pywinauto.findwindows import ElementNotFoundError
    from pywinauto.uia_element_info import UIAElementInfo
    import pywinauto.findwindows as findwindows
    _pywinauto_available = True
except Exception as exc:  # noqa: BLE001
    _pywinauto_error = str(exc)


def _unavailable_response():
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "data": None,
            "error": f"pywinauto is not available: {_pywinauto_error}. "
                     "Install it with: pip install pywinauto",
            "timed_out": False,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rect_to_logical(rect, monitor_map: List[Dict]) -> Dict[str, Any]:
    """Convert a pywinauto RECT (physical pixels) to logical bounding_rect + center."""
    phys_left = rect.left
    phys_top = rect.top
    phys_right = rect.right
    phys_bottom = rect.bottom

    log_left, log_top, _ = dpi_utils.physical_to_logical(phys_left, phys_top, monitor_map)
    log_right, log_bottom, _ = dpi_utils.physical_to_logical(phys_right, phys_bottom, monitor_map)

    center_x = (log_left + log_right) // 2
    center_y = (log_top + log_bottom) // 2

    return {
        "bounding_rect": {
            "left": log_left,
            "top": log_top,
            "right": log_right,
            "bottom": log_bottom,
        },
        "center": {"x": center_x, "y": center_y},
    }


def _element_info(wrapper, monitor_map: List[Dict]) -> Dict[str, Any]:
    """Extract element info dict from a pywinauto wrapper or element_info."""
    try:
        info = wrapper.element_info
    except AttributeError:
        info = wrapper

    try:
        rect = info.rectangle
    except Exception:  # noqa: BLE001
        rect = None

    result = {
        "name": getattr(info, "name", "") or "",
        "control_type": getattr(info, "control_type", "") or "",
        "automation_id": getattr(info, "automation_id", "") or "",
        "class_name": getattr(info, "class_name", "") or "",
    }

    if rect is not None:
        result.update(_rect_to_logical(rect, monitor_map))
    else:
        result["bounding_rect"] = None
        result["center"] = None

    return result


def _matches_filter(info, name: Optional[str], control_type: Optional[str],
                    automation_id: Optional[str]) -> bool:
    """Check if an element_info matches the given filters."""
    if name is not None:
        elem_name = getattr(info, "name", "") or ""
        if name.lower() not in elem_name.lower():
            return False
    if control_type is not None:
        elem_ct = getattr(info, "control_type", "") or ""
        if elem_ct != control_type:
            return False
    if automation_id is not None:
        elem_aid = getattr(info, "automation_id", "") or ""
        if elem_aid != automation_id:
            return False
    return True


def _search_recursive(wrapper, name, control_type, automation_id,
                      max_depth, current_depth, results, max_results, deadline):
    """Depth-limited recursive search through UI tree."""
    if current_depth > max_depth:
        return
    if len(results) >= max_results:
        return
    if time.monotonic() > deadline:
        return

    try:
        children = wrapper.children()
    except Exception:  # noqa: BLE001
        return

    for child in children:
        if len(results) >= max_results or time.monotonic() > deadline:
            return

        try:
            info = child.element_info
        except Exception:  # noqa: BLE001
            continue

        if _matches_filter(info, name, control_type, automation_id):
            results.append(child)

        # Recurse into children
        _search_recursive(child, name, control_type, automation_id,
                          max_depth, current_depth + 1, results, max_results, deadline)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class FindElementRequest(BaseModel):
    hwnd: Optional[int] = None
    title: Optional[str] = None
    name: Optional[str] = None
    control_type: Optional[str] = None
    automation_id: Optional[str] = None
    max_depth: int = Field(5, ge=1, le=20)
    max_results: int = Field(20, ge=1, le=100)
    timeout: float = Field(5.0, ge=0.5, le=30.0)

    @model_validator(mode="after")
    def validate_request(self):
        if self.hwnd is None and self.title is None:
            raise ValueError("At least one of 'hwnd' or 'title' must be provided")
        if self.name is None and self.control_type is None and self.automation_id is None:
            raise ValueError("At least one search filter ('name', 'control_type', or 'automation_id') must be provided")
        return self


class ElementAtRequest(BaseModel):
    x: int
    y: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/find_element")
async def find_element(req: FindElementRequest):
    if not _pywinauto_available:
        return _unavailable_response()

    try:
        monitor_map = dpi_utils.get_monitor_map()

        def _do_search():
            # Resolve window handle
            hwnd = req.hwnd
            if hwnd is None:
                # Find by title substring
                try:
                    pattern = f".*{re.escape(req.title)}.*"
                    matches = findwindows.find_elements(title_re=pattern)
                except Exception as exc:  # noqa: BLE001
                    return {
                        "success": False,
                        "data": None,
                        "error": f"Error searching for window with title '{req.title}': {exc}",
                        "timed_out": False,
                    }

                if not matches:
                    return {
                        "success": False,
                        "data": None,
                        "error": f"No window found matching title '{req.title}'. "
                                 "Use list_windows to see available windows.",
                        "timed_out": False,
                    }
                hwnd = matches[0].handle

            # Connect to window
            try:
                app = Application(backend="uia").connect(handle=hwnd)
            except Exception as exc:  # noqa: BLE001 — covers ElementNotFoundError, ProcessNotFoundError, etc.
                return {
                    "success": False,
                    "data": None,
                    "error": f"Window not found (hwnd={hwnd}) — it may have been closed. "
                             f"Call list_windows to get current handles. Detail: {exc}",
                    "timed_out": False,
                }

            window = app.window(handle=hwnd)

            # Search with depth limit and timeout
            results = []
            deadline = time.monotonic() + req.timeout
            _search_recursive(
                window, req.name, req.control_type, req.automation_id,
                req.max_depth, 0, results, req.max_results, deadline,
            )

            timed_out = time.monotonic() > deadline

            # Count total matches (we stopped at max_results, so total_count
            # equals len(results) unless we hit max_results or timed out)
            total_count = len(results)
            truncated = total_count >= req.max_results or timed_out

            elements = [_element_info(r, monitor_map) for r in results[:req.max_results]]

            return {
                "success": True,
                "data": {
                    "elements": elements,
                    "total_count": total_count,
                    "truncated": truncated,
                },
                "error": None,
                "timed_out": timed_out,
            }

        # pywinauto is synchronous — run in thread to avoid blocking event loop
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_do_search),
                timeout=req.timeout + 2.0,  # outer timeout slightly longer than inner
            )
            return result
        except asyncio.TimeoutError:
            return {
                "success": False,
                "data": None,
                "error": "Search timed out",
                "timed_out": True,
            }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }


@router.post("/element_at")
async def element_at(req: ElementAtRequest):
    if not _pywinauto_available:
        return _unavailable_response()

    try:
        monitor_map = dpi_utils.get_monitor_map()
        phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)

        def _do_lookup():
            try:
                elem_info = UIAElementInfo.from_point(phys_x, phys_y)
            except Exception as exc:  # noqa: BLE001
                return {
                    "success": True,
                    "data": None,
                    "error": f"No element found at ({req.x}, {req.y}): {exc}",
                    "timed_out": False,
                }

            if elem_info is None:
                return {
                    "success": True,
                    "data": None,
                    "error": f"No meaningful element at ({req.x}, {req.y})",
                    "timed_out": False,
                }

            return {
                "success": True,
                "data": _element_info(elem_info, monitor_map),
                "error": None,
                "timed_out": False,
            }

        return await asyncio.to_thread(_do_lookup)

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
